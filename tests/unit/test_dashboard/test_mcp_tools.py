"""The 42 MCP tools (23 research + strategy, 10 session + risk, 2 code-gated live,
7 model registry), called through the mounted transport. The model tools' behaviour
lives in ``test_mcp_model_tools.py``.

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
import sys
import types
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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

from hydra.backtest.jobs import MAX_RESULT_SUMMARIES
from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.logging import get_logger
from hydra.dashboard import security
from hydra.dashboard.api import app as engine_app
from hydra.dashboard.mcp import mount_mcp
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import install_security
from hydra.execution.errors import ConfigReloadRefusedError
from hydra.execution.session_manager import TradingSession
from hydra.services import strategies as strategy_svc
from hydra.services.errors import ServiceError, error_detail
from hydra.services.trader import LocalTrader
from tests.fixtures.universe import OFFLINE_RANKED_AT, seeded_universe_pool
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
        "data_inventory",
        "universe_list",
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

LIVE_TOOLS = frozenset({"live_session_start", "live_order"})

MODEL_TOOLS = frozenset(
    {
        "model_list",
        "model_get",
        "model_retrain",
        "model_retrain_status",
        "model_retrain_cancel",
        "model_promote",
        "model_rollback",
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
    """Serves the three data_coverage reads: the range row, the source split, the ledger row."""

    def __init__(
        self,
        row: dict[str, Any],
        sources: list[dict[str, Any]] | None = None,
        entry: dict[str, Any] | None = None,
    ) -> None:
        self.row = row
        self.sources = sources or []
        self.entry = entry

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM data_cache_entries" in query:
            return self.entry
        assert "FROM ts.ohlcv_1m" in query
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM ts.ohlcv_1m" in query and "GROUP BY source" in query
        return self.sources


class _InventoryConn:
    """Serves the two data_inventory reads: the LRU-ordered ledger rows and the totals."""

    def __init__(self, entries: list[dict[str, Any]], totals: dict[str, Any]) -> None:
        self.entries = entries
        self.totals = totals

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM data_cache_entries" in query
        if "ORDER BY" in query:
            return self.entries
        return [self.totals]


class _UniverseConn:
    """Serves the system_config universe row for the universe_list read."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM system_config" in query
        return self.row


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _DownPool:
    """A pool whose acquire fails: the session's terminal row cannot be persisted."""

    def acquire(self) -> Any:
        raise ConnectionError("database down")


# ---------------------------------------------------------------------------
# Engine-state resolution: the engine serves `python -m hydra.dashboard.api`
# ---------------------------------------------------------------------------


def test_engine_state_prefers_the_served_python_m_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine container serves ``python -m hydra.dashboard.api``, which runs
    ``api.py`` as ``__main__``: the lifespan-bound app is ``__main__.app`` and the
    module import builds a stateless second instance — the tools must resolve the
    served app, not the import."""
    served = FastAPI()
    sentinel = object()
    served.state.db_pool = sentinel
    # the stateless second instance the import builds in the served process
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    main = types.ModuleType("__main__")
    main.__file__ = sys.modules["hydra.dashboard.api"].__file__
    main.app = served
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert _deps.engine_state() is served.state
    assert _deps.db_pool() is sentinel
    assert engine_app.state is not served.state
    assert engine_app.state.db_pool is None


def test_engine_state_falls_back_when_the_python_m_app_is_stateless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``__main__`` whose ``__file__`` is the served ``api.py`` but whose app has
    no lifespan binding (neither pool nor session manager) is not the served engine:
    the imported module's state — the unit-test seam — wins, not the stateless
    ``__main__`` app."""
    stateless = FastAPI()
    stateless.state.db_pool = None
    stateless.state.session_manager = None
    sentinel = object()
    monkeypatch.setattr(engine_app.state, "db_pool", sentinel)
    main = types.ModuleType("__main__")
    main.__file__ = sys.modules["hydra.dashboard.api"].__file__
    main.app = stateless
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert _deps.engine_state() is engine_app.state
    assert _deps.db_pool() is sentinel


def test_engine_state_falls_back_to_the_imported_module_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test layout (no served ``__main__``): the imported module's app state is
    the engine state — the monkeypatch seam every other tool test patches."""
    sentinel = object()
    monkeypatch.setattr(engine_app.state, "db_pool", sentinel)
    assert _deps.engine_state() is engine_app.state
    assert _deps.db_pool() is sentinel


def test_tools_list_advertises_the_42_tools(mcp: _McpClient) -> None:
    names = {tool["name"] for tool in mcp.list_tools()}
    assert names == B1_TOOLS | B2_TOOLS | LIVE_TOOLS | MODEL_TOOLS
    assert len(names) == 42
    # the live pin: the two gated live tools; no live_arm/live_disarm (the operator's ceremony)
    assert "risk_live_status" in names
    assert {name for name in names if name.startswith("live_")} == {
        "live_session_start",
        "live_order",
    }


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


def test_backtest_run_description_pins_the_auto_fetch_copy(mcp: _McpClient) -> None:
    """The copy: cold ranges are auto-fetched before the run; a genuinely-unavailable
    range fails the job (no new tool — the pin stays at 34)."""
    [tool] = [t for t in mcp.list_tools() if t["name"] == "backtest_run"]
    description = " ".join(tool["description"].split())
    assert "a range with no data is auto-fetched before the run" in description
    assert "a genuinely-unavailable range fails the job with" in description
    assert "no_market_data" in description


def test_strategy_reload_409_keeps_restart_required_and_changed_fields(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusing = _RefusingManager(["risk", "symbols"])
    monkeypatch.setattr(engine_app.state, "session_manager", refusing)
    monkeypatch.setattr(engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, refusing))
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
    monkeypatch.setattr(
        engine_app.state,
        "db_pool",
        _FakePool(_CoverageConn(row, sources=[{"source": "feed", "n": 48}])),
    )
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
        "sources": {"feed": 48, "auto": 0},
        "estimated_bytes": 48 * 205,
        "cache": None,
    }


def test_data_coverage_reads_the_estimate_from_the_engine_config(
    mcp: _McpClient, bind_api_config: Bind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``estimated_bytes`` follows the engine's ``data.row_bytes_estimate``, never a
    baked-in constant."""
    cfg = bind_api_config(
        HYDRA_API_TOKEN=TEST_API_TOKEN,
        HYDRA_MCP_TOKEN=TEST_MCP_TOKEN,
        HYDRA_MCP__ALLOWED_HOSTS="testserver",
        HYDRA_DATA__ROW_BYTES_ESTIMATE="250",
    )
    assert cfg.data.row_bytes_estimate == 250
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
    assert json.loads(text)["estimated_bytes"] == 48 * 250


def _inventory_entry(
    symbol: str, timeframe: str, rows: int, status: str, last_used: datetime
) -> dict[str, Any]:
    return {
        "exchange": "binance",
        "symbol": symbol,
        "timeframe": timeframe,
        "first_ts": datetime(2025, 12, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 31, 23, tzinfo=UTC),
        "rows": rows,
        "status": status,
        "created_at": datetime(2025, 12, 1, tzinfo=UTC),
        "last_fetched_at": datetime(2026, 10, 27, 9, tzinfo=UTC),
        "last_used_at": last_used,
    }


def test_data_inventory_lists_the_cached_datasets_lru_first_with_the_totals(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics.mark_data_sweep(at=1_761_628_200.0)
    entries = [
        _inventory_entry("ETHUSDT", "1h", 0, "ready", datetime(2026, 10, 1, tzinfo=UTC)),
        _inventory_entry("BTCUSDT", "1m", 500, "fetching", datetime(2026, 10, 2, tzinfo=UTC)),
    ]
    monkeypatch.setattr(
        engine_app.state,
        "db_pool",
        _FakePool(_InventoryConn(entries, {"rows": 500, "datasets": 2})),
    )
    is_error, text = mcp.call("data_inventory", {})
    assert not is_error
    assert json.loads(text) == {
        "datasets": [
            {
                "exchange": "binance",
                "symbol": "ETHUSDT",
                "timeframe": "1h",
                "coverage": {
                    "first_ts": "2025-12-01T00:00:00+00:00",
                    "last_ts": "2026-01-31T23:00:00+00:00",
                },
                "rows": 0,
                "estimated_bytes": 0,
                "status": "ready",
                "last_used_at": "2026-10-01T00:00:00+00:00",
            },
            {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "coverage": {
                    "first_ts": "2025-12-01T00:00:00+00:00",
                    "last_ts": "2026-01-31T23:00:00+00:00",
                },
                "rows": 500,
                "estimated_bytes": 500 * 205,
                "status": "fetching",
                "last_used_at": "2026-10-02T00:00:00+00:00",
            },
        ],
        "totals": {
            "bytes": 500 * 205,
            "rows": 500,
            "pairs": 2,
            "budget_bytes": 10 * 2**30,
            "last_sweep": "2025-10-28T05:10:00+00:00",
        },
    }


def test_data_inventory_description_pins_the_coverage_copy(mcp: _McpClient) -> None:
    """The copy: the LRU order, the requested-coverage range (never "auto-cached
    since"), the rows=0 honesty and the budget/totals."""
    [tool] = [t for t in mcp.list_tools() if t["name"] == "data_inventory"]
    description = " ".join(tool["description"].split())
    assert "least-recently-used first" in description
    assert "eviction candidates lead the list" in description
    assert "never an" in description
    assert "auto-cached since" in description
    assert 'an honest "no bars"' in description
    assert "budget" in description


def test_universe_list_returns_the_seeded_ranking(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranked_at = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    row = {
        "value": json.dumps(
            {
                "exchange_id": "binance",
                "quote_asset": "USDT",
                "source": "binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)",
                "ranked_at": ranked_at.isoformat(),
                "count": 2,
                "symbols": [
                    {"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1000.0},
                    {"rank": 2, "symbol": "ETHUSDT", "quote_volume": 500.0},
                ],
            }
        )
    }
    monkeypatch.setattr(engine_app.state, "db_pool", _FakePool(_UniverseConn(row)))
    is_error, text = mcp.call("universe_list", {})
    assert not is_error
    assert json.loads(text) == {
        "exchange_id": "binance",
        "quote_asset": "USDT",
        "source": "binance spot rolling-24h quote volume (GET /api/v3/ticker/24hr)",
        "ranked_at": ranked_at.isoformat(),
        "age_hours": pytest.approx(
            (datetime.now(UTC) - ranked_at).total_seconds() / 3600, abs=0.01
        ),
        "count": 2,
        "symbols": [
            {"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1000.0},
            {"rank": 2, "symbol": "ETHUSDT", "quote_volume": 500.0},
        ],
    }


def test_universe_list_before_the_first_refresh_is_the_503_envelope(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", _FakePool(_UniverseConn(None)))
    is_error, text = mcp.call("universe_list", {})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool universe_list: "))
    assert envelope == {
        "status": 503,
        "detail": {
            "code": "universe_unavailable",
            "message": "The top-100 universe has not been computed yet",
            "state": "unavailable",
        },
    }


def test_universe_list_without_a_pool_is_the_503_database_envelope(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    is_error, text = mcp.call("universe_list", {})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool universe_list: "))
    assert envelope == {
        "status": 503,
        "detail": {
            "code": "database_unavailable",
            "message": "Database is not available",
            "state": "unavailable",
        },
    }


class _SummariesConn:
    """Serves the backtest_results list read: newest first, honouring the SQL LIMIT."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM backtest_results" in query
        assert "LIMIT" in query
        newest_first = sorted(self.rows, key=lambda row: row["created_at"], reverse=True)
        return newest_first[: args[0]]


def _summary_row(i: int) -> dict[str, Any]:
    """One result-summary row in the LIST_SUMMARIES_SQL shape, oldest first."""
    stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)
    row: dict[str, Any] = {
        "id": f"bt-{i}",
        "strategy": "Saw",
        "period": "p",
        "status": "completed",
        "name": f"run {i}",
        "strategy_id": "saw",
        "initial_capital": 1000.0,
        "total_trades": 3,
        "win_rate": 33.33,
        "total_pnl": -950.0,
        "max_drawdown": 95.0,
        "sharpe_ratio": -2.1,
        "data_source": "db",
        "data_exchange": "binance",
        "data_symbol": "BTCUSDT",
        "data_timeframe": "1h",
        "bar_count": 25,
        "first_ts": stamp,
        "last_ts": stamp,
        "gap_count": 0,
        "requested_start": stamp,
        "requested_end": stamp,
        "created_at": stamp,
    }
    for metric in (
        "benchmark_return",
        "alpha",
        "beta",
        "max_consecutive_wins",
        "max_consecutive_losses",
        "expectancy",
        "total_return",
        "annualized_return",
        "sortino_ratio",
        "calmar_ratio",
        "profit_factor",
        "max_drawdown_duration_s",
    ):
        row[metric] = None
    return row


def test_backtest_results_listing_is_bounded_identically_on_both_surfaces(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 (audit #174): the route and the tool ride one shared, bounded results read."""
    total = MAX_RESULT_SUMMARIES + 30
    pool = _FakePool(_SummariesConn([_summary_row(i) for i in range(total)]))
    monkeypatch.setattr(engine_app.state, "db_pool", pool)
    route = TestClient(engine_app).get("/api/backtest/results")
    assert route.status_code == 200
    is_error, text = mcp.call("backtest_results")
    assert not is_error
    summaries = json.loads(text)
    assert len(summaries) == MAX_RESULT_SUMMARIES == len(route.json())
    assert [s["id"] for s in summaries] == [s["id"] for s in route.json()]
    assert summaries[0]["id"] == f"bt-{total - 1}"


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


def test_strategy_save_with_a_non_member_symbol_is_the_422_envelope(
    mcp: _McpClient, strategies_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", seeded_universe_pool())
    is_error, text = mcp.call(
        "strategy_save", {"name": "Ghost Pair", "symbol": "FOOBAR", "rules": _RULES}
    )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool strategy_save: "))
    assert envelope == {
        "status": 422,
        "detail": {
            "code": "symbol_not_in_universe",
            "message": (
                "FOOBAR is not in the current top-100 universe "
                f"(ranked at {OFFLINE_RANKED_AT.isoformat()}); running sessions and stored "
                "strategies are unaffected"
            ),
            "state": "invalid",
            "symbol": "FOOBAR",
            "universe_ranked_at": OFFLINE_RANKED_AT.isoformat(),
            "universe_count": 3,
        },
    }
    assert list(strategies_dir.glob("*.yaml")) == []


def test_strategy_save_with_a_member_symbol_saves(
    mcp: _McpClient, strategies_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", seeded_universe_pool())
    is_error, text = mcp.call(
        "strategy_save", {"name": "Member Pair", "symbol": "BTCUSDT", "rules": _RULES}
    )
    assert not is_error
    result = json.loads(text)
    assert result["name"] == "Member Pair"
    written = strategies_dir / f"{result['id']}.yaml"
    assert yaml.safe_load(written.read_text())["symbols"] == ["BTCUSDT"]


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


def test_data_inventory_without_a_pool_is_the_503_envelope(
    mcp: _McpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_app.state, "db_pool", None)
    is_error, text = mcp.call("data_inventory", {})
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool data_inventory: "))
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
        monkeypatch.setattr(
            engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
        )
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


_LIVE_LEGS: list[dict[str, Any]] = [
    {
        "info": {
            "symbol": "BTCUSDT",
            "orderId": 11,
            "orderListId": "5",
            "clientOrderId": "a",
            "price": "103.00000000",
            "origQty": "0.01000000",
            "status": "NEW",
            "type": "LIMIT_MAKER",
            "side": "SELL",
            "stopPrice": "0.00000000",
            "trailingDelta": "0",
        }
    },
    {
        "info": {
            "symbol": "BTCUSDT",
            "orderId": 12,
            "orderListId": "5",
            "clientOrderId": "b",
            "price": "0.00000000",
            "origQty": "0.01000000",
            "status": "NEW",
            "type": "STOP_LOSS",
            "side": "SELL",
            "stopPrice": "98.00000000",
            "trailingDelta": "0",
        }
    },
]


class _KeyedClient:
    """The live executor's keyed exchange client (the A8 fetch surface)."""

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return _LIVE_LEGS

    async def fetch_open_order_lists(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "BTCUSDT",
                "orderListId": 5,
                "contingencyType": "OCO",
                "listStatusType": "EXEC_STARTED",
            }
        ]


class _LiveDetailManager:
    """Serves one live session's detail through the service's A8-live branch."""

    def __init__(
        self, *, status: Any = None, store: Any = None, trades: list[dict[str, Any]] | None = None
    ) -> None:
        self.session = TradingSession(
            session_id="s1",
            strategy_id="saw",
            trading_mode="live",
            status="running",
            exchange_id="binance",
            symbols=["BTCUSDT"],
            timeframe="1h",
        )
        self.session._executor = types.SimpleNamespace(
            exchange_client=_KeyedClient(), live_orders=store
        )
        self.session._runtime = types.SimpleNamespace(
            protect=types.SimpleNamespace(
                armed=lambda symbol: types.SimpleNamespace(order_id="5", leg_ids=("11", "12")),
                is_protective=lambda order_id: order_id in ("5", "11", "12"),
            )
        )
        self.status = status
        self.trades = trades or []

    def reconcile_status(self, session_id: str) -> Any:
        return self.status

    def get_session(self, session_id: str) -> Any:
        return self.session if session_id == "s1" else None

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "strategy_id": self.session.strategy_id,
            "trading_mode": "live",
            "status": "running",
            "exchange_id": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "direction": "long",
                    "quantity": 0.01,
                    "avg_entry_price": 100.0,
                    "unrealized_pnl": 0.0,
                }
            ],
            "trades": list(self.trades),
        }


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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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

                    is_error, text = await mcp.call(
                        "session_detail", {"session_id": session.session_id}
                    )
                    assert not is_error
                    detail = json.loads(text)
                    assert [p["symbol"] for p in detail["positions"]] == ["BTCUSDT"]
                    # S1: paper positions never carry a live exchange stop
                    assert all(p.get("exchange_stop") is None for p in detail["positions"])

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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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

    async def test_session_detail_carries_the_live_exchange_stop(
        self,
        mcp_cfg: HydraConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S1: the A8-live exchange_stop read (the pinned seam #3 names) flows through
        the MCP session_detail tool, not only through the REST route's response model."""
        mgr = _LiveDetailManager()
        monkeypatch.setattr(engine_app.state, "trader", LocalTrader(None, mgr))
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call("session_detail", {"session_id": "s1"})

        assert not is_error, text
        (position,) = json.loads(text)["positions"]
        stop = position["exchange_stop"]
        assert stop["state"] == "confirmed"
        assert stop["kind"] == "oco"
        assert stop["stop_price"] == 98.0
        assert stop["take_profit"] == 103.0
        assert stop["order_list_id"] == "5"
        assert datetime.fromisoformat(stop["checked_at"]).tzinfo is not None

    async def test_session_detail_flows_the_a7_reconcile_reads(
        self,
        mcp_cfg: HydraConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The seam delegation: the tracked orders, the reconcile state and R20's
        reconciled flag ride session_detail through the backend, not a new tool."""
        from datetime import timedelta

        from hydra.execution.live_orders import (
            InMemoryLiveOrderStore,
            LiveOrderRecord,
            LiveOrderStatus,
        )
        from hydra.execution.reconcile import ReconcileStatus

        store = InMemoryLiveOrderStore()
        await store.create(
            LiveOrderRecord(
                client_order_id="hyd-1",
                session_id="s1",
                strategy_id="saw",
                exchange_id="binance",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("0.01"),
                price=D("90000"),
                status=LiveOrderStatus.NEW,
                tag="entry",
                actor="engine",
                exchange_order_id="7",
            )
        )
        covered = datetime.now(UTC) - timedelta(minutes=5)
        mgr = _LiveDetailManager(
            status=ReconcileStatus(state="ok", last_reconciled_at=covered, covered_at=covered),
            store=store,
            trades=[
                {"id": "t1", "timestamp": (covered - timedelta(minutes=30)).isoformat()},
                {"id": "t2", "timestamp": datetime.now(UTC).isoformat()},
            ],
        )
        monkeypatch.setattr(engine_app.state, "trader", LocalTrader(None, mgr))
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call("session_detail", {"session_id": "s1"})

        assert not is_error, text
        detail = json.loads(text)
        (order,) = detail["orders"]
        assert (order["client_order_id"], order["exchange_order_id"], order["status"]) == (
            "hyd-1",
            "7",
            "new",
        )
        assert detail["reconcile"]["state"] == "ok"
        assert detail["reconcile"]["last_reconciled_at"] is not None
        assert [t["reconciled"] for t in detail["trades"]] == [True, False]

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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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

    async def test_on_through_an_unreachable_trader_latches_the_db_and_answers_503(
        self,
        mcp_cfg: HydraConfig,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R7 through the MCP tool too: engine_unreachable latches the DB row, the tool
        error carries the honest 503 envelope (latched + the apply window)."""

        class _LatchPool:
            def __init__(self) -> None:
                self.statements: list[str] = []

            class _Conn:
                def __init__(self, pool: _LatchPool) -> None:
                    self._pool = pool

                async def execute(self, sql: str, *args: object) -> str:
                    self._pool.statements.append(sql)
                    return "UPDATE 1" if sql.lstrip().startswith("UPDATE") else "INSERT 0 1"

            class _Ctx:
                def __init__(self, pool: _LatchPool) -> None:
                    self._pool = pool

                async def __aenter__(self) -> _LatchPool._Conn:
                    return _LatchPool._Conn(self._pool)

                async def __aexit__(self, *exc: object) -> None:
                    return None

            def acquire(self) -> _LatchPool._Ctx:
                return _LatchPool._Ctx(self)

        class _Unreachable:
            async def activate_kill_switch(self, actor: str, reason: str) -> dict[str, Any]:
                raise ServiceError(
                    503,
                    error_detail(
                        "engine_unreachable",
                        "The trading engine is unreachable (HYDRA_TRADER__URL)",
                        "unavailable",
                    ),
                )

        pool = _LatchPool()
        monkeypatch.setattr(engine_app.state, "db_pool", pool)
        monkeypatch.setattr(engine_app.state, "trader", _Unreachable())
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call("kill_switch_on", {"reason": "claude drill"})
            assert is_error, text
            envelope = json.loads(text.removeprefix("Error executing tool kill_switch_on: "))
            assert envelope["status"] == 503
            detail = envelope["detail"]
            assert (detail["code"], detail["latched"], detail["apply_window_s"]) == (
                "engine_unreachable",
                True,
                60,
            )
        assert any("kill_switch_active = TRUE" in s for s in pool.statements)
        assert any("INSERT INTO risk_events" in s for s in pool.statements)

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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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
            monkeypatch.setattr(
                engine_app.state, "trader", LocalTrader(engine_app.state.db_pool, h.mgr)
            )
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


# ---------------------------------------------------------------------------
# P3-C: the two gated live tools
# ---------------------------------------------------------------------------

_ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-28T00:00:00+00:00"})


class _GateConn:
    """Serves the armed system_config row the live tools' fail-fast read resolves."""

    def __init__(self, armed: bool) -> None:
        self.row = {"value": _ARMED_ROW} if armed else None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        assert sql == "SELECT value FROM system_config WHERE key = $1", sql
        return self.row

    async def execute(self, sql: str, *args: Any) -> str:
        return "INSERT 0 1"  # the store's best-effort event rows


class _GatePool:
    def __init__(self, armed: bool) -> None:
        self.conn = _GateConn(armed)

    def acquire(self) -> _GatePool:
        return self

    async def __aenter__(self) -> _GateConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RecordingBackend:
    """Records the live executors' calls through the seam."""

    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or []
        self.start_calls: list[tuple[str, Any]] = []
        self.order_calls: list[Any] = []

    async def kill_switch_active(self) -> bool:
        return False

    async def live_status(self, source: str = "paper") -> dict[str, Any]:
        return {
            "kill_switch_active": False,
            "kill_switch_state": "inactive",
            "running_sessions": len(self.sessions),
            "circuit_breaker_restrictions": None,
            "circuit_breakers_state": "unavailable",
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]

    async def start_session(self, actor: str, body: Any) -> dict[str, Any]:
        self.start_calls.append((actor, body))
        return {
            "session_id": "s-live",
            "trading_mode": body.trading_mode,
            "status": "running",
            "created_by": actor,
        }

    async def quick_trade(self, order: Any) -> dict[str, Any]:
        self.order_calls.append(order)
        return {"order_id": "o-1", "session_id": order.session_id, "status": "SUBMITTED"}


_LIVE_SESSION = {"session_id": "s-live", "trading_mode": "live", "status": "running"}


class TestLiveTools:
    async def _approve(self, mcp_cfg: HydraConfig, approval_id: str, code: str) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=engine_app),
            base_url="http://engine",
            headers={"X-Hydra-Request": "1"},
        ) as ui:
            return await ui.post(
                f"/api/activity/approvals/{approval_id}/approve", json={"code": code}
            )

    async def test_live_session_start_is_gated_then_executes_on_approval(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _RecordingBackend()
        monkeypatch.setattr(engine_app.state, "db_pool", _GatePool(armed=True))
        monkeypatch.setattr(engine_app.state, "trader", backend)
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call(
                "live_session_start", {"strategy_id": "momentum", "reason": "claude goes live"}
            )
            assert not is_error, text
            result = json.loads(text)
            assert result["status"] == "approval_required"
            assert re.fullmatch(r"[0-9]{6}", result["code"])
            assert "Hydra UI Activity page" in result["note"]
            assert backend.start_calls == []  # gated: nothing started by the tool

            resp = await self._approve(mcp_cfg, result["approval_id"], result["code"])
            assert resp.status_code == 200, resp.text
            approved = resp.json()
            assert approved["executed"] is True
            assert (approved["actor"], approved["approved_by"]) == ("service:mcp", "dev:dev")
            assert approved["result"]["trading_mode"] == "live"
        [(actor, body)] = backend.start_calls
        assert (actor, body.trading_mode, body.strategy_id) == (
            "service:mcp",
            "live",
            "momentum",
        )

    async def test_live_session_start_refuses_when_disarmed(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_app.state, "db_pool", _GatePool(armed=False))
        monkeypatch.setattr(engine_app.state, "trader", _RecordingBackend())
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call(
                "live_session_start", {"strategy_id": "momentum", "reason": "claude goes live"}
            )
            assert is_error, text
            envelope = json.loads(text.removeprefix("Error executing tool live_session_start: "))
            assert (envelope["status"], envelope["detail"]["code"]) == (409, "live_not_armed")

    async def test_live_order_is_gated_then_places_on_approval(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _RecordingBackend(sessions=[_LIVE_SESSION])
        monkeypatch.setattr(engine_app.state, "db_pool", _GatePool(armed=True))
        monkeypatch.setattr(engine_app.state, "trader", backend)
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call(
                "live_order",
                {
                    "session_id": "s-live",
                    "side": "buy",
                    "quantity": "0.01",
                    "reason": "claude buys one lot",
                },
            )
            assert not is_error, text
            result = json.loads(text)
            assert result["status"] == "approval_required"
            assert backend.order_calls == []  # gated: nothing placed by the tool

            resp = await self._approve(mcp_cfg, result["approval_id"], result["code"])
            assert resp.status_code == 200, resp.text
            assert resp.json()["executed"] is True
            assert resp.json()["result"]["order_id"] == "o-1"
        [order] = backend.order_calls
        assert (order.session_id, order.side, order.quantity) == ("s-live", "buy", D("0.01"))

    async def test_live_order_on_a_paper_session_is_refused(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_app.state, "db_pool", _GatePool(armed=True))
        monkeypatch.setattr(
            engine_app.state,
            "trader",
            _RecordingBackend(
                sessions=[{"session_id": "s-paper", "trading_mode": "paper", "status": "running"}]
            ),
        )
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call(
                "live_order",
                {"session_id": "s-paper", "side": "buy", "quantity": "0.01", "reason": "one buy"},
            )
            assert is_error, text
            envelope = json.loads(text.removeprefix("Error executing tool live_order: "))
            assert (envelope["status"], envelope["detail"]["code"]) == (409, "not_a_live_session")

    async def test_quick_trade_on_a_live_session_is_the_409_envelope(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_app.state, "trader", _RecordingBackend(sessions=[_LIVE_SESSION]))
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call(
                "quick_trade", {"session_id": "s-live", "side": "buy", "quantity": "0.01"}
            )
            assert is_error, text
            envelope = json.loads(text.removeprefix("Error executing tool quick_trade: "))
            assert (envelope["status"], envelope["detail"]["code"]) == (409, "live_gate_required")
            assert "live_order" in envelope["detail"]["message"]

    async def test_risk_live_status_reports_the_armed_state(
        self, mcp_cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_app.state, "db_pool", _GatePool(armed=True))
        monkeypatch.setattr(engine_app.state, "trader", _RecordingBackend())
        async with _async_mcp(mcp_cfg) as mcp:
            is_error, text = await mcp.call("risk_live_status")
            assert not is_error, text
            assert json.loads(text)["live_armed"] is True
