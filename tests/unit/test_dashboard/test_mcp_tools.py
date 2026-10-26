"""The 31 MCP tools (21 research + strategy, 10 session + risk), called through the
mounted transport.

Every test drives the real JSON-RPC surface — initialize, notifications/initialized,
tools/list, tools/call — over the real mount (token gate, SDK streamable-HTTP app)
the way the engine assembles it, and the tool bodies resolve the engine app's state
(``db_pool``, ``session_manager``) exactly as they do in the engine process. Error
results must carry the full service error envelope verbatim, never a sanitized
internal error.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
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
from hydra.dashboard.mcp import mount_mcp
from hydra.dashboard.security import install_security
from hydra.execution.errors import ConfigReloadRefusedError
from hydra.services import strategies as strategy_svc
from tests.unit.test_execution.session_harness import Harness, held, write_strategy

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

B2_TOOLS = frozenset(
    {
        "session_list",
        "session_detail",
        "session_start",
        "session_stop",
        "quick_trade",
        "risk_live_status",
        "risk_status",
        "positions",
        "kill_switch_on",
        "kill_switch_release",
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


class _DownPool:
    """A pool whose acquire fails: the session's terminal row cannot be persisted."""

    def acquire(self) -> Any:
        raise ConnectionError("database down")


def test_tools_list_advertises_the_31_tools(mcp: _McpClient) -> None:
    names = {tool["name"] for tool in mcp.list_tools()}
    assert names == B1_TOOLS | B2_TOOLS
    assert len(names) == 31
    # the live pin: no live-prefixed tool, and B2's read-only risk_live_status is allowed
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


# ---------------------------------------------------------------------------
# B2: session + risk tools (real SessionManager on the pytest loop)
# ---------------------------------------------------------------------------

D = Decimal


@pytest.fixture(autouse=True)
def _fresh_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never leak an approval store (or its executor) wired to a closed harness."""
    monkeypatch.setattr(engine_app.state, "approval_store", None, raising=False)


@pytest.fixture()
def mcp_cfg(bind_api_config: Bind) -> HydraConfig:
    """Dev auth on the engine app (the activity approve route needs a human principal)."""
    return bind_api_config(
        HYDRA_API__AUTH_MODE="dev",
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
    )


class TestSessionTools:
    async def test_round_trip_start_list_detail_quick_trade_and_stop(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Paper round trip through the tools, attributed service:mcp (created_by + audit)."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            sid = write_strategy(strategies_dir, "saw")
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            monkeypatch.setattr(engine_app.state, "db_pool", None, raising=False)
            assert h.mgr is not None
            async with _async_mcp(mcp_cfg) as mcp:
                with capture_logs() as logs:
                    # a logger cached by an earlier audit() call would bypass capture_logs
                    monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
                    is_error, text = await mcp.call(
                        "session_start", {"strategy_id": sid, "paper_capital": "250"}
                    )
                    assert not is_error, text
                    started = json.loads(text)
                    assert (started["status"], started["created_by"]) == (
                        "running",
                        "service:mcp",
                    )

                    is_error, text = await mcp.call("session_list")
                    assert not is_error
                    rows = json.loads(text)
                    assert [r["created_by"] for r in rows] == ["service:mcp"]
                    assert rows[0]["strategy_name"] == "saw"

                    session = h.mgr.get_session(started["session_id"])
                    assert session is not None
                    await h.bar(session, "100")

                    is_error, text = await mcp.call(
                        "session_detail", {"session_id": session.session_id}
                    )
                    assert not is_error
                    assert json.loads(text)["metrics"]["balance"]["USDT"] == 250.0

                    is_error, text = await mcp.call(
                        "quick_trade",
                        {"session_id": session.session_id, "side": "buy", "quantity": "0.2"},
                    )
                    assert not is_error, text
                    body = json.loads(text)
                    assert (body["status"], body["protection"], body["fill_price"]) == (
                        "FILLED",
                        "armed",
                        100.1,
                    )
                    assert await held(session) == D("0.2")

                    is_error, text = await mcp.call("positions")
                    assert not is_error
                    held_rows = json.loads(text)
                    assert [row["session_id"] for row in held_rows] == [session.session_id]
                    assert held_rows[0]["positions"][0]["symbol"] == "BTCUSDT"

                    is_error, text = await mcp.call(
                        "session_stop", {"session_id": session.session_id}
                    )
                    assert not is_error
                    assert json.loads(text)["status"] == "stopped"
            audit_lines = [event for event in logs if event.get("event") == "audit"]
            assert [
                (line["action"], line["principal"], line["principal_kind"]) for line in audit_lines
            ] == [
                ("session_start", "mcp", "service"),
                ("manual_order", "mcp", "service"),
                ("session_stop", "mcp", "service"),
            ]
            assert audit_lines[0]["session_id"] == started["session_id"]
            assert audit_lines[1]["order_id"] == body["order_id"]
        finally:
            await h.close()

    async def test_quick_trade_validation_names_the_offending_field(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In-body pydantic revalidation: a LIMIT without a price is the 422 envelope."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            write_strategy(strategies_dir, "saw")
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            assert h.mgr is not None
            async with _async_mcp(mcp_cfg) as mcp:
                is_error, text = await mcp.call(
                    "quick_trade",
                    {"session_id": "s1", "side": "buy", "quantity": "0.1", "order_type": "LIMIT"},
                )
            assert is_error
            envelope = json.loads(text.removeprefix("Error executing tool quick_trade: "))
            assert envelope["status"] == 422
            assert envelope["detail"]["code"] == "validation_error"
            assert "price" in envelope["detail"]["message"]
        finally:
            await h.close()

    async def test_stop_that_cannot_be_persisted_is_503_and_audits(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An MCP-initiated stop that cannot persist still writes the persisted=False
        audit line — the same line the REST route writes before answering its 503."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            session = await h.session(write_strategy(strategies_dir, "unsaved"))
            await h.bar(session, "100")
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            assert h.mgr is not None
            h.mgr._db_pool = _DownPool()
            async with _async_mcp(mcp_cfg) as mcp:
                with capture_logs() as logs:
                    # a logger cached by an earlier audit() call would bypass capture_logs
                    monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
                    is_error, text = await mcp.call(
                        "session_stop", {"session_id": session.session_id}
                    )
            assert is_error
            envelope = json.loads(text.removeprefix("Error executing tool session_stop: "))
            assert (envelope["status"], envelope["detail"]["code"]) == (
                503,
                "session_stop_not_persisted",
            )
            assert session.status == "stopped" and not session.task_alive
            audit_lines = [event for event in logs if event.get("event") == "audit"]
            assert [
                (line["action"], line["principal"], line["principal_kind"], line["persisted"])
                for line in audit_lines
            ] == [("session_stop", "mcp", "service", False)]
            assert audit_lines[0]["session_id"] == session.session_id
        finally:
            h.mgr._db_pool = None
            await h.close()

    async def test_risk_reads_mirror_the_rest_shapes(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        h = Harness(strategies_dir)
        await h.start()
        try:
            sid = write_strategy(strategies_dir, "saw")
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            monkeypatch.setattr(engine_app.state, "db_pool", None, raising=False)
            assert h.mgr is not None
            await h.session(sid)
            async with _async_mcp(mcp_cfg) as mcp:
                is_error, text = await mcp.call("risk_live_status")
                assert not is_error
                live = json.loads(text)
                assert (live["running_sessions"], live["kill_switch_state"]) == (1, "unknown")
                assert live["circuit_breakers_state"] == "live"

                is_error, text = await mcp.call("risk_status")
                assert not is_error
                status = json.loads(text)
                assert status["circuit_breakers_state"] == "live"
                assert len(status["circuit_breakers"]) == 4
                assert status["restrictions"]["active_tier"] == 0
                assert status["current_drawdown"] == 0.0
        finally:
            await h.close()


class TestKillSwitchTools:
    async def test_on_reason_is_bounded_like_the_release_reason(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 501-char latch reason is the 422 envelope naming the field and nothing
        latches; the default reason still latches."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            assert h.mgr is not None and not h.mgr.kill_switch_active()
            async with _async_mcp(mcp_cfg) as mcp:
                is_error, text = await mcp.call("kill_switch_on", {"reason": "x" * 501})
                assert is_error
                envelope = json.loads(text.removeprefix("Error executing tool kill_switch_on: "))
                assert (envelope["status"], envelope["detail"]["code"]) == (
                    422,
                    "validation_error",
                )
                assert envelope["detail"]["message"].startswith("reason:")
                assert not h.mgr.kill_switch_active()

                is_error, text = await mcp.call("kill_switch_on")
                assert not is_error, text
                assert json.loads(text)["active"] is True
                assert h.mgr.kill_switch_active()
        finally:
            await h.close()

    async def test_on_is_immediate_latches_and_is_attributed_then_release_is_gated(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S9 + seam 5: kill_switch_on runs now and latches (start refused, attributed
        risk_events row); kill_switch_release releases nothing until a human approves."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            sid = write_strategy(strategies_dir, "ks")
            other = write_strategy(strategies_dir, "other")
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            # the release executor persists through whatever pool the engine state carries
            monkeypatch.setattr(engine_app.state, "db_pool", object(), raising=False)
            assert h.mgr is not None
            session = await h.session(sid)
            await h.bar(session, "100")
            async with _async_mcp(mcp_cfg) as mcp:
                is_error, text = await mcp.call("kill_switch_on", {"reason": "claude drill"})
                assert not is_error, text
                body = json.loads(text)
                assert (body["active"], body["sessions_stopped"], body["persisted"]) == (
                    True,
                    1,
                    True,
                )
                assert h.mgr.kill_switch_active()
                assert session.status == "stopped"

                # latched: a new session is refused (the belt-and-braces S9 test)
                is_error, text = await mcp.call("session_start", {"strategy_id": other})
                assert is_error
                envelope = json.loads(text.removeprefix("Error executing tool session_start: "))
                assert (envelope["status"], envelope["detail"]["code"]) == (
                    409,
                    "kill_switch_active",
                )

                # the latch is attributed to the service in risk_events
                assert h.store is not None
                on_rows = [e for e in h.store.events if e.kind == "kill_switch_on"]
                assert on_rows and on_rows[-1].actor == "service:mcp"

                # the gated release: an approval is required, nothing is released
                is_error, text = await mcp.call("kill_switch_release", {"reason": "drill over"})
                assert not is_error, text
                result = json.loads(text)
                assert result["status"] == "approval_required"
                assert re.fullmatch(r"[0-9]{6}", result["code"])
                assert result["expires_at"]
                assert "Hydra UI Activity page" in result["note"]
                assert h.mgr.kill_switch_active()

                # a second request dedupes into the same pending approval (A2)
                again = json.loads(
                    (await mcp.call("kill_switch_release", {"reason": "drill over"}))[1]
                )
                assert again["approval_id"] == result["approval_id"]

                # a short reason is refused before anything is created
                is_error, text = await mcp.call("kill_switch_release", {"reason": "no"})
                assert is_error
                envelope = json.loads(
                    text.removeprefix("Error executing tool kill_switch_release: ")
                )
                assert (envelope["status"], envelope["detail"]["code"]) == (
                    422,
                    "validation_error",
                )

                # the human types the code in the UI: the release runs as service:mcp
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=engine_app),
                    base_url="http://engine",
                    headers={"X-Hydra-Request": "1"},
                ) as ui:
                    resp = await ui.post(
                        f"/api/activity/approvals/{result['approval_id']}/approve",
                        json={"code": result["code"]},
                    )
                assert resp.status_code == 200, resp.text
                approved = resp.json()
                assert approved["executed"] is True
                assert approved == {
                    "executed": True,
                    "result": {
                        "active": False,
                        "message": "Kill switch released - trading can resume",
                        "sessions_stopped": 0,
                    },
                    "actor": "service:mcp",
                    "approved_by": "dev:dev",
                }
                assert not h.mgr.kill_switch_active()
                off_rows = [e for e in h.store.events if e.kind == "kill_switch_off"]
                assert off_rows and off_rows[-1].actor == "service:mcp"
        finally:
            await h.close()

    async def test_release_of_an_already_released_switch_is_a_clean_no_op(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The executor re-validates at execution time: nothing latched, no exception."""
        h = Harness(strategies_dir)
        await h.start()
        try:
            monkeypatch.setattr(engine_app.state, "session_manager", h.mgr)
            monkeypatch.setattr(engine_app.state, "db_pool", object(), raising=False)
            assert h.mgr is not None and not h.mgr.kill_switch_active()
            async with _async_mcp(mcp_cfg) as mcp:
                is_error, text = await mcp.call("kill_switch_release", {"reason": "not latched"})
                assert not is_error, text
                result = json.loads(text)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=engine_app),
                    base_url="http://engine",
                    headers={"X-Hydra-Request": "1"},
                ) as ui:
                    resp = await ui.post(
                        f"/api/activity/approvals/{result['approval_id']}/approve",
                        json={"code": result["code"]},
                    )
                assert resp.status_code == 200, resp.text
                approved = resp.json()
                assert approved["executed"] is True
                assert approved["result"] == {
                    "status": "already_released",
                    "active": False,
                    "message": "The kill switch was already released; nothing changed",
                }
        finally:
            await h.close()
