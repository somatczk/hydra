"""The 21 research + strategy MCP tools, called through the mounted transport.

Every test drives the real JSON-RPC surface — initialize, notifications/initialized,
tools/list, tools/call — over the real mount (token gate, SDK streamable-HTTP app)
the way the engine assembles it, and the tool bodies resolve the engine app's state
(``db_pool``, ``session_manager``) exactly as they do in the engine process. Error
results must carry the full service error envelope verbatim, never a sanitized
internal error.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from structlog.testing import capture_logs

from hydra.core.config import HydraConfig
from hydra.core.logging import get_logger
from hydra.dashboard import security
from hydra.dashboard.api import app as engine_app
from hydra.dashboard.mcp import mount_mcp, server
from hydra.dashboard.security import install_security
from hydra.execution.errors import ConfigReloadRefusedError
from hydra.services import strategies as strategy_svc
from tests.unit.test_execution.session_harness import Harness, write_strategy

from .conftest import TEST_API_TOKEN

TEST_MCP_TOKEN = "m" + secrets.token_urlsafe(48)
ACCEPT_BOTH = "application/json, text/event-stream"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "hydra-tests", "version": "0.0.0"},
    },
}

B1_TOOLS = frozenset(
    {
        "backtest_run",
        "backtest_status",
        "backtest_cancel",
        "backtest_results",
        "backtest_result",
        "hyperopt_run",
        "hyperopt_progress",
        "hyperopt_results",
        "walkforward_run",
        "walkforward_results",
        "strategy_params",
        "data_coverage",
        "strategy_list",
        "strategy_get",
        "strategy_save",
        "strategy_update",
        "strategy_delete",
        "strategy_toggle",
        "strategy_reload",
        "strategy_import",
        "strategy_export",
    }
)

_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": "sma:period=5",
            }
        ],
    },
    "exit_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_below",
                "value": "sma:period=5",
            }
        ],
    },
}


def _write_strategy(directory: Any, strategy_id: str) -> None:
    data = {
        "id": strategy_id,
        "name": f"Strategy {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _RULES, "required_history": 10},
    }
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


def _sse_json(response: Response) -> Any:
    """The first JSON-RPC message framed in an SSE body."""
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"no data frame in SSE body: {response.text!r}")


class _McpClient:
    """One initialized MCP session over the mounted transport."""

    def __init__(self, client: TestClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._id = 0

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = {
            "Authorization": f"Bearer {TEST_MCP_TOKEN}",
            "Accept": ACCEPT_BOTH,
            "Mcp-Session-Id": self._session_id,
        }
        return _sse_json(self._client.post("/mcp/", json=payload, headers=headers))

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload)["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        return self.request("tools/list")["tools"]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[bool, str]:
        """One tools/call: ``(is_error, text)`` — the text carries the result or envelope."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        return bool(result.get("isError")), content[0]["text"]


Bind = Callable[..., HydraConfig]


@pytest.fixture()
def mcp(bind_api_config: Bind) -> Iterator[_McpClient]:
    """The mounted MCP app over a TestClient, with the engine state the tools resolve."""
    cfg = bind_api_config(
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        mount = mount_mcp(app, cfg)
        async with mount.session_manager.run():
            yield

    mounted = FastAPI(lifespan=lifespan)
    install_security(mounted)
    mounted.state.config = cfg
    with TestClient(mounted) as client:
        initialize = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "hydra-tests", "version": "0.0.0"},
                },
            },
            headers={
                "Authorization": f"Bearer {TEST_MCP_TOKEN}",
                "Accept": ACCEPT_BOTH,
            },
        )
        session_id = initialize.headers["mcp-session-id"]
        client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Authorization": f"Bearer {TEST_MCP_TOKEN}",
                "Accept": ACCEPT_BOTH,
                "Mcp-Session-Id": session_id,
            },
        )
        yield _McpClient(client, session_id)


class _AsyncMcpClient:
    """One initialized MCP session driven over ASGI on the caller's own event loop."""

    def __init__(self, client: httpx.AsyncClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._id = 0

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {
            "Authorization": f"Bearer {TEST_MCP_TOKEN}",
            "Accept": ACCEPT_BOTH,
            "Mcp-Session-Id": self._session_id,
        }
        return _sse_json(await self._client.post("/mcp/", json=payload, headers=headers))["result"]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[bool, str]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        return bool(result.get("isError")), content[0]["text"]


@asynccontextmanager
async def _async_mcp(cfg: HydraConfig) -> AsyncIterator[_AsyncMcpClient]:
    """The mounted MCP app driven over ASGI, entered inside the test's own task.

    Tools that await objects living on the pytest loop (a real SessionManager) need the
    transport on that loop — the TestClient would run tool bodies on its portal loop.
    Must be entered in the test body, not a fixture: the session manager's anyio cancel
    scope cannot span pytest-asyncio's fixture/test task boundary.
    """
    mounted = FastAPI()
    install_security(mounted)
    mounted.state.config = cfg
    mount = mount_mcp(mounted, cfg)
    async with (
        mount.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mounted), base_url="http://testserver"
        ) as client,
    ):
        initialize = await client.post(
            "/mcp/",
            json=INITIALIZE,
            headers={"Authorization": f"Bearer {TEST_MCP_TOKEN}", "Accept": ACCEPT_BOTH},
        )
        session_id = initialize.headers["mcp-session-id"]
        await client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Authorization": f"Bearer {TEST_MCP_TOKEN}",
                "Accept": ACCEPT_BOTH,
                "Mcp-Session-Id": session_id,
            },
        )
        yield _AsyncMcpClient(client, session_id)


class _RefusingManager:
    """A session manager whose reload refuses everything but a parameters-only change."""

    def __init__(self, changed_fields: list[str]) -> None:
        self._changed_fields = changed_fields

    async def reload_config(self, strategy_id: str) -> Any:
        raise ConfigReloadRefusedError(self._changed_fields)


class _CoverageConn:
    """Serves one MIN/MAX/COUNT row for the data_coverage read."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "FROM ts.ohlcv_1m" in query
        return self.row


class _FakePool:
    def __init__(self, conn: _CoverageConn) -> None:
        self.conn = conn

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> _CoverageConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_tools_list_advertises_the_21_research_and_strategy_tools(mcp: _McpClient) -> None:
    names = {tool["name"] for tool in mcp.list_tools()}
    assert names >= B1_TOOLS
    assert len(names) == 21  # B2 adds 10 (31 total)
    assert not [name for name in names if name.startswith("live_")]


async def _risk_live_status() -> str:
    """Stands in for B2's pinned read-only live-status tool; registered only in one test."""
    return "{}"


def test_the_live_pin_forbids_only_live_prefixed_tools_not_b2s_risk_live_status(
    mcp: _McpClient,
) -> None:
    """B2 pins ``risk_live_status`` (a read); the pin must hold for it, not break W3."""
    server.add_tool(_risk_live_status, name="risk_live_status", structured_output=False)
    try:
        names = {tool["name"] for tool in mcp.list_tools()}
    finally:
        server.remove_tool("risk_live_status")
    assert "risk_live_status" in names
    assert not [name for name in names if name.startswith("live_")]


def test_backtest_run_on_a_missing_strategy_is_the_404_envelope(mcp: _McpClient) -> None:
    is_error, text = mcp.call(
        "backtest_run",
        {"strategy_id": "ghost", "start_date": "2026-01-01", "end_date": "2026-02-01"},
    )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool backtest_run: "))
    assert envelope == {
        "status": 404,
        "detail": {
            "code": "strategy_not_found",
            "message": "Strategy ghost not found",
            "state": "not_found",
            "strategy_id": "ghost",
        },
    }


def test_strategy_reload_409_keeps_restart_required_and_changed_fields(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "session_manager", _RefusingManager(["risk", "symbols"]))
    is_error, text = mcp.call("strategy_reload", {"strategy_id": "saw"})
    assert is_error
    assert "restart_required" in text
    assert '"changed_fields": ["risk", "symbols"]' in text
    envelope = json.loads(text.removeprefix("Error executing tool strategy_reload: "))
    assert envelope["status"] == 409
    assert envelope["detail"]["changed_fields"] == ["risk", "symbols"]


def test_strategy_list_round_trip(
    mcp: _McpClient, strategies_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    _write_strategy(strategies_dir, "saw")
    is_error, text = mcp.call("strategy_list")
    assert not is_error
    listed = json.loads(text)
    assert [entry["id"] for entry in listed] == ["saw"]
    assert listed[0]["name"] == "Strategy saw"
    assert listed[0]["enabled"] is False


def test_data_coverage_round_trips_the_stored_range(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = {
        "first_ts": datetime(2026, 1, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 2, 23, tzinfo=UTC),
        "n": 48,
    }
    monkeypatch.setattr(engine_app.state, "db_pool", _FakePool(_CoverageConn(row)))
    is_error, text = mcp.call(
        "data_coverage", {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h"}
    )
    assert not is_error
    assert json.loads(text) == {
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-02T23:00:00+00:00",
        "bar_count": 48,
    }


def test_strategy_export_returns_the_yaml_text(mcp: _McpClient, strategies_dir: Any) -> None:
    _write_strategy(strategies_dir, "saw")
    is_error, text = mcp.call("strategy_export", {"strategy_id": "saw"})
    assert not is_error
    exported = yaml.safe_load(text)
    assert exported["id"] == "saw"
    assert exported["strategy_class"] == "hydra.strategy.builtin.rule_based.RuleBasedStrategy"


def test_strategy_export_on_a_missing_strategy_is_the_404_envelope(mcp: _McpClient) -> None:
    is_error, text = mcp.call("strategy_export", {"strategy_id": "ghost"})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool strategy_export: "))
    assert envelope["status"] == 404
    assert envelope["detail"]["code"] == "strategy_not_found"


def test_strategy_toggle_round_trips_and_audits_as_the_mcp_principal(
    mcp: _McpClient,
    strategies_dir: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_strategy(strategies_dir, "saw")
    with capture_logs() as logs:
        # a logger cached by an earlier audit() call would bypass capture_logs
        monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
        is_error, text = mcp.call("strategy_toggle", {"strategy_id": "saw"})
    assert not is_error
    assert json.loads(text) == {"id": "saw", "enabled": True}
    assert logs == [
        {
            "event": "audit",
            "log_level": "info",
            "action": "strategy_toggle",
            "principal": "mcp",
            "principal_kind": "service",
            "strategy_id": "saw",
            "enabled": True,
        }
    ]


def test_hyperopt_run_rejects_an_unknown_param_name_as_the_422_envelope(
    mcp: _McpClient, strategies_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service models revalidate inside the tool: schema drift cannot smuggle past."""
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    _write_strategy(strategies_dir, "saw")
    is_error, text = mcp.call(
        "hyperopt_run",
        {
            "strategy_id": "saw",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "param_space": [{"name": "no_such_param", "type": "int", "low": 3, "high": 8}],
        },
    )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool hyperopt_run: "))
    assert envelope["status"] == 422
    assert envelope["detail"]["code"] == "invalid_param_space"
    assert "no_such_param" in envelope["detail"]["message"]


def test_backtest_run_with_a_non_iso_date_names_the_offending_field(mcp: _McpClient) -> None:
    """A pydantic failure inside the tool body names the field and reason, like a REST 422."""
    is_error, text = mcp.call(
        "backtest_run",
        {"strategy_id": "ghost", "start_date": "2026/01/01", "end_date": "2026-02-01"},
    )
    assert is_error
    assert "start_date" in text
    envelope = json.loads(text.removeprefix("Error executing tool backtest_run: "))
    assert envelope["status"] == 422
    assert envelope["detail"]["code"] == "validation_error"
    assert envelope["detail"]["state"] == "invalid"
    assert envelope["detail"]["message"].startswith("start_date:")
    assert "Input should be a valid datetime or date" in envelope["detail"]["message"]


def test_strategy_save_with_an_empty_name_names_the_offending_field(mcp: _McpClient) -> None:
    is_error, text = mcp.call("strategy_save", {"name": "", "rules": _RULES})
    assert is_error
    assert "name" in text
    envelope = json.loads(text.removeprefix("Error executing tool strategy_save: "))
    assert envelope["status"] == 422
    assert envelope["detail"]["code"] == "validation_error"
    assert envelope["detail"]["message"].startswith("name:")


def test_strategy_export_on_a_file_deleted_after_the_scan_is_the_404_envelope(
    mcp: _McpClient, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file vanished between the finder's scan and the read: an envelope, not a crash."""
    _write_strategy(strategies_dir, "saw")
    gone = strategies_dir / "saw.yaml"
    gone.unlink()
    monkeypatch.setattr(strategy_svc, "find_strategy_file", lambda _sid: gone)
    is_error, text = mcp.call("strategy_export", {"strategy_id": "saw"})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool strategy_export: "))
    assert envelope["status"] == 404
    assert envelope["detail"]["code"] == "strategy_not_found"


def test_strategy_export_on_an_unreadable_file_is_the_503_envelope(
    mcp: _McpClient, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that fails on a permission error is a 503 envelope, not a crash."""
    _write_strategy(strategies_dir, "locked")
    locked = strategies_dir / "locked.yaml"
    locked.chmod(0o000)
    try:
        monkeypatch.setattr(strategy_svc, "find_strategy_file", lambda _sid: locked)
        is_error, text = mcp.call("strategy_export", {"strategy_id": "locked"})
    finally:
        locked.chmod(0o644)
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool strategy_export: "))
    assert envelope["status"] == 503
    assert envelope["detail"]["code"] == "strategy_file_unreadable"
    assert envelope["detail"]["state"] == "unavailable"


class _JobRowConn:
    """Serves one backtest_jobs row for the cancel read."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "FROM backtest_jobs" in query
        return self.row


def test_backtest_cancel_on_a_terminal_task_surfaces_the_plain_string_detail(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ServiceError whose detail is a plain string (the cancel 409) is valid envelope JSON."""
    monkeypatch.setattr(
        engine_app.state, "db_pool", _FakePool(_JobRowConn({"status": "completed"}))
    )
    is_error, text = mcp.call("backtest_cancel", {"job_id": "task-abc"})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool backtest_cancel: "))
    assert envelope == {"status": 409, "detail": "Task task-abc is already completed"}


def test_data_coverage_without_a_pool_is_the_503_envelope(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    is_error, text = mcp.call(
        "data_coverage", {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h"}
    )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool data_coverage: "))
    assert envelope == {
        "status": 503,
        "detail": {
            "code": "database_unavailable",
            "message": "Database is not available",
            "state": "unavailable",
        },
    }


async def test_strategy_reload_round_trip_applies_a_params_only_change(
    bind_api_config: Bind,
    strategies_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running session on a snapshot plus a params-only file change: the reload succeeds
    with from_hash/to_hash and audits as the MCP principal (the S4 acceptance round trip)."""
    cfg = bind_api_config(
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )
    h = Harness(strategies_dir)
    await h.start()
    try:
        sid = write_strategy(strategies_dir, "saw")
        session = await h.session(sid)
        monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
        data = yaml.safe_load((strategies_dir / f"{sid}.yaml").read_text())
        data["parameters"]["required_history"] = 5
        (strategies_dir / f"{sid}.yaml").write_text(yaml.safe_dump(data))
        async with _async_mcp(cfg) as mcp:
            with capture_logs() as logs:
                # a logger cached by an earlier audit() call would bypass capture_logs
                monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
                is_error, text = await mcp.call("strategy_reload", {"strategy_id": sid})
        assert not is_error
        result = json.loads(text)
        assert result["session_id"] == session.session_id
        assert result["from_hash"] != result["to_hash"]
        assert result["applies_at"] == "next_closed_bar"
        audit_lines = [event for event in logs if event.get("event") == "audit"]
        assert [
            (line["action"], line["principal"], line["principal_kind"]) for line in audit_lines
        ] == [("strategy_reload", "mcp", "service")]
        assert audit_lines[0]["strategy_id"] == sid
        assert audit_lines[0]["session_id"] == session.session_id
    finally:
        await h.close()
