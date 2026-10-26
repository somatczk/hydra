"""The research MCP tools against a real TimescaleDB, through the mounted transport.

The tools resolve the engine app's state exactly as in the engine process, so these
tests bind a real pool (a fresh migrated database) and drive JSON-RPC tools/call over
the real mount: data_coverage reads back exactly what was stored, a backtest on an
empty range is the honest 422 no-market-data envelope, a backtest on stored bars
queues a durable job whose task_id is readable via backtest_status, and a seeded
completed result reads back in full via backtest_result.

The mounted app is driven with httpx's ASGI transport on the pytest event loop — the
same loop the asyncpg pool lives on. A starlette TestClient would run the tool
coroutines on its own portal loop and every pool operation would fail with asyncpg's
``got Future attached to a different loop``. The session manager is entered inside
each test (not a fixture) because its anyio cancel scope cannot span pytest-asyncio's
fixture/test task boundary.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI
from httpx import Response
from structlog.testing import capture_logs

from hydra.backtest import jobs
from hydra.backtest.data import DataProvenance
from hydra.core.config import HydraConfig, load_config
from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard import security
from hydra.dashboard.api import app as engine_app
from hydra.dashboard.mcp import mount_mcp
from hydra.dashboard.security import install_security
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.execution.client_factory import ClientFactory, bars_repository
from hydra.execution.session_manager import SessionManager
from hydra.risk.state import fetch_risk_events
from tests.unit.test_execution.session_harness import Harness, write_strategy

pytestmark = pytest.mark.integration

TEST_API_TOKEN = secrets.token_urlsafe(32)
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

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)

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


def _write_strategy(directory: Path, strategy_id: str) -> None:
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


def _bar(i: int) -> OHLCV:
    close = Decimal(100 + i)
    return OHLCV(
        open=Decimal(100),
        high=Decimal(str(100 + i + 1)),
        low=Decimal("99"),
        close=close,
        volume=Decimal("1"),
        timestamp=T0 + i * H,
    )


def _sse_json(response: Response) -> Any:
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"no data frame in SSE body: {response.text!r}")


class _McpClient:
    """One initialized MCP session over the mounted transport, on the pytest loop."""

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


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


def _bind_engine_state(
    pool: Any, monkeypatch: pytest.MonkeyPatch, strategies_dir: Path
) -> HydraConfig:
    """Write the strategy, bind the pool on the engine state, and load the test config."""
    _write_strategy(strategies_dir, "saw")
    monkeypatch.setattr(engine_app.state, "db_pool", pool)
    monkeypatch.setattr(engine_app.state, "session_manager", None)
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR", "DATABASE_URL", "REDIS_URL", "HYDRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HYDRA_API_TOKEN", TEST_API_TOKEN)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", TEST_MCP_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    return load_config()


@asynccontextmanager
async def _mcp_session(cfg: HydraConfig) -> AsyncIterator[_McpClient]:
    """The mounted MCP app driven over ASGI, entered inside the test's own task.

    httpx's ASGI transport runs the app (and so the tool coroutines) on the caller's
    event loop — the same loop the asyncpg pool fixture built its pool on.
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
        yield _McpClient(client, session_id)


async def test_data_coverage_returns_the_exact_stored_range(
    pool: Any, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _bind_engine_state(pool, monkeypatch, strategies_dir)
    async with _mcp_session(cfg) as mcp:
        repo = MarketDataRepository.from_pool(pool)
        stored = await repo.store_bars(
            [("binance", "BTCUSDT", Timeframe.H1, _bar(i)) for i in range(5)]
        )
        assert stored == 5
        is_error, text = await mcp.call(
            "data_coverage", {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h"}
        )
    assert not is_error
    assert json.loads(text) == {
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-01T04:00:00+00:00",
        "bar_count": 5,
    }


async def test_backtest_run_on_an_empty_range_is_the_honest_no_data_envelope(
    pool: Any, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _bind_engine_state(pool, monkeypatch, strategies_dir)
    async with _mcp_session(cfg) as mcp:
        is_error, text = await mcp.call(
            "backtest_run",
            {"strategy_id": "saw", "start_date": "2026-03-01", "end_date": "2026-03-02"},
        )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool backtest_run: "))
    assert envelope["status"] == 422
    assert envelope["detail"]["code"] == "no_market_data"
    assert envelope["detail"]["symbol"] == "BTCUSDT"
    assert envelope["detail"]["timeframe"] == "1h"
    assert "No market data for binance BTCUSDT 1h" in envelope["detail"]["message"]


async def test_backtest_run_on_stored_bars_queues_a_durable_job(
    pool: Any, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _bind_engine_state(pool, monkeypatch, strategies_dir)
    async with _mcp_session(cfg) as mcp:
        repo = MarketDataRepository.from_pool(pool)
        stored = await repo.store_bars(
            [("binance", "BTCUSDT", Timeframe.H1, _bar(i)) for i in range(30)]
        )
        assert stored == 30
        with capture_logs() as logs:
            # a logger cached by an earlier audit() call would bypass capture_logs
            monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
            is_error, text = await mcp.call(
                "backtest_run",
                {"strategy_id": "saw", "start_date": "2026-01-01", "end_date": "2026-01-02"},
            )
        assert not is_error
        queued = json.loads(text)
        assert queued["status"] == "queued"
        task_id = queued["task_id"]
        assert task_id.startswith("task-")

        audit_lines = [event for event in logs if event.get("event") == "audit"]
        assert [
            (line["action"], line["principal"], line["principal_kind"]) for line in audit_lines
        ] == [("backtest_run", "mcp", "service")]
        assert audit_lines[0]["strategy_id"] == "saw"
        assert audit_lines[0]["task_id"] == task_id

        is_error, text = await mcp.call("backtest_status", {"task_id": task_id})
        assert not is_error
        status = json.loads(text)
        assert status["task_id"] == task_id
        assert status["status"] in ("queued", "running")  # no worker runs in this test
        assert status["result_id"] is None


async def test_backtest_result_reads_back_a_seeded_completed_result(
    pool: Any, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'read the result' leg of the acceptance scenario: a completed result seeded
    through the repository seam (jobs.persist_result) surfaces in full through the tool."""
    cfg = _bind_engine_state(pool, monkeypatch, strategies_dir)
    async with _mcp_session(cfg) as mcp:
        await jobs.persist_result(
            pool,
            {
                "id": "bt-seeded",
                "strategy": "Strategy saw",
                "strategy_id": "saw",
                "initial_capital": 10000.0,
                "period": f"{T0.isoformat()} - {(T0 + 29 * H).isoformat()}",
                "status": "completed",
                "name": "seeded run",
                "stopped_reason": None,
                "metrics": {
                    "total_trades": 3,
                    "win_rate": 66.67,
                    "total_pnl": 125.5,
                    "max_drawdown": 2.1,
                    "sharpe_ratio": 1.42,
                    "benchmark_return": 8.4,
                    "alpha": 0.0123,
                    "beta": 0.87,
                    "max_consecutive_wins": 2,
                    "max_consecutive_losses": 1,
                    "expectancy": 41.83,
                    "total_return": 1.26,
                    "annualized_return": 15.1,
                    "sortino_ratio": 1.9,
                    "calmar_ratio": 3.2,
                    "profit_factor": 2.5,
                    "max_drawdown_duration_s": 3600,
                },
                "equity_curve": [{"ts": T0.isoformat(), "equity": 10000.0}],
                "benchmark_equity": [{"ts": T0.isoformat(), "equity": 10000.0}],
                "transactions": [],
            },
            [],
            DataProvenance(
                exchange="binance",
                symbol="BTCUSDT",
                timeframe=Timeframe.H1,
                bar_count=30,
                first_ts=T0,
                last_ts=T0 + 29 * H,
                gap_count=0,
                requested_start=T0,
                requested_end=T0 + 29 * H,
            ),
        )
        is_error, text = await mcp.call("backtest_result", {"result_id": "bt-seeded"})
        assert not is_error
        detail = json.loads(text)
        assert detail["id"] == "bt-seeded"
        assert detail["status"] == "completed"
        assert detail["strategy_id"] == "saw"
        assert detail["metrics"]["total_trades"] == 3
        assert detail["metrics"]["sharpe_ratio"] == 1.42
        assert detail["provenance"]["bar_count"] == 30
        assert detail["trades"] == []

        is_error, text = await mcp.call("backtest_results")
        assert not is_error
        assert [summary["id"] for summary in json.loads(text)] == ["bt-seeded"]


def _real_clients(h: Harness) -> ClientFactory:
    """The harness's fakes behind the real bars repository (the recovery-test wiring)."""
    fake = h.clients()
    return ClientFactory(
        ws_factory=fake.ws_factory,
        rest_factory=fake.rest_factory,
        markets_loader=fake.markets_loader,
        repository_factory=bars_repository,
        history_window=fake.history_window,
    )


async def test_gated_kill_switch_release_writes_the_risk_event_rows(
    pool: Any, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gated flow end to end on a real database: a session started through the
    service persists ``created_by``; ``kill_switch_on`` latches (and persists); the
    gated ``kill_switch_release`` creates an approval without releasing; approving it
    through the activity route runs the release, and ``approval_requested`` then
    ``kill_switch_off`` land in ``risk_events`` attributed ``service:mcp``."""
    monkeypatch.setenv("HYDRA_API__AUTH_MODE", "dev")  # survives _bind_engine_state
    cfg = _bind_engine_state(pool, monkeypatch, strategies_dir)
    monkeypatch.setattr(engine_app.state, "approval_store", None, raising=False)
    h = Harness(strategies_dir)
    mgr = SessionManager(db_pool=pool, config=HydraConfig(), clients=_real_clients(h))
    await mgr.startup()
    monkeypatch.setattr(engine_app.state, "session_manager", mgr)
    try:
        sid = write_strategy(strategies_dir, "pg-b2")
        session_id = await mgr.start_session(sid, "paper", Decimal("500"), actor="service:mcp")
        async with pool.acquire() as conn:
            stored_by = await conn.fetchval(
                "SELECT created_by FROM trading_sessions WHERE id = $1", session_id
            )
        assert stored_by == "service:mcp"

        async with _mcp_session(cfg) as mcp:
            is_error, text = await mcp.call("kill_switch_on", {"reason": "integration drill"})
            assert not is_error, text
            assert json.loads(text)["persisted"] is True

            is_error, text = await mcp.call("kill_switch_release", {"reason": "drill over"})
            assert not is_error, text
            result = json.loads(text)
            assert result["status"] == "approval_required"
            assert re.fullmatch(r"[0-9]{6}", result["code"])
            assert result["expires_at"]

            # gated: the request released nothing — the DB row stays latched
            async with pool.acquire() as conn:
                assert await conn.fetchval(
                    "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
                )

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=engine_app),
                base_url="http://t",
                headers={"X-Hydra-Request": "1"},
            ) as ui:
                resp = await ui.post(
                    f"/api/activity/approvals/{result['approval_id']}/approve",
                    json={"code": result["code"]},
                )
            assert resp.status_code == 200, resp.text
            assert resp.json()["executed"] is True
            assert resp.json()["actor"] == "service:mcp"
        assert not mgr.kill_switch_active()

        rows = await fetch_risk_events(pool, 20)  # newest first
        assert [r.kind for r in rows[:4]] == [
            "kill_switch_off",
            "approval_approved",
            "approval_requested",
            "kill_switch_on",
        ]
        assert [r.actor for r in rows[:4]] == [
            "service:mcp",
            "dev:dev",
            "service:mcp",
            "service:mcp",
        ]
    finally:
        await mgr.graceful_shutdown()
