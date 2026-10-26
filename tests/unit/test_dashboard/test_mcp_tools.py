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
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

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

from .conftest import TEST_API_TOKEN

TEST_MCP_TOKEN = "m" + secrets.token_urlsafe(48)
ACCEPT_BOTH = "application/json, text/event-stream"

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
    assert not [name for name in names if "live" in name]


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
