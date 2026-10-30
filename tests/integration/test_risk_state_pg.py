"""RiskService state on real TimescaleDB: idempotent revision, restart survival, audit trail."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from hydra.core.types import Side
from hydra.dashboard.routes.risk import router
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.circuit_breakers import CircuitBreakerState
from hydra.risk.pretrade import OPEN
from hydra.risk.service import RiskService
from hydra.risk.state import PgRiskStateStore, RiskState
from hydra.services.trader import LocalTrader
from tests.unit.test_risk.harness import Clock, order, rules_cache

REVISION = (
    Path(__file__).resolve().parents[2] / "src/hydra/data/migrations/versions/0003_risk_state.py"
)
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _revision_statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("risk_state_revision", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module._STATEMENTS)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


async def _service(pool: asyncpg.Pool, clock: Clock) -> RiskService:
    svc = RiskService(PgRiskStateStore(pool), clock=clock, market_rules=rules_cache(clock))
    await svc.load()
    await svc.recovery_complete()
    return svc


async def test_revision_is_idempotent_and_seeds_paper_scope(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for statement in _revision_statements():
            await conn.execute(statement)
        scopes = [r["scope"] for r in await conn.fetch("SELECT scope FROM risk_state ORDER BY 1")]
        heat = await conn.fetchval(
            "SELECT max_portfolio_heat FROM risk_config WHERE scope = 'global'"
        )
        events = await conn.fetchval("SELECT count(*) FROM risk_events")
    assert scopes == ["global", "live", "paper"]  # 0009 seeds the live scope
    assert heat == Decimal("0.3000")
    assert events == 0


async def test_state_survives_restart_without_resetting_peak(pool: asyncpg.Pool) -> None:
    clock = Clock(T0)
    halt = T0 + timedelta(hours=15)
    store = PgRiskStateStore(pool)
    await store.save_state(
        RiskState(
            equity=Decimal("8900"),
            equity_at=T0,
            peak_equity=Decimal("10000"),
            current_drawdown=Decimal("0.11"),
            day=T0.date(),
            day_start_equity=Decimal("9100"),
            consecutive_losses=3,
            last_loss_at=T0,
            carried_pnl=Decimal("-1100"),
            cb=CircuitBreakerState(
                tier=3, tier_since=T0, requires_manual_review=True, daily_halt_until=halt
            ),
        )
    )

    svc = await _service(pool, clock)
    view = svc.account()
    assert (view.equity, view.peak_equity, view.day_start_equity) == (
        Decimal("8900"),
        Decimal("10000"),
        Decimal("9100"),
    )
    assert (view.consecutive_losses, view.carried_pnl, view.tier) == (3, Decimal("-1100"), 3)
    assert view.restrictions.daily_halt_until == halt

    ex = PaperTradingExecutor(initial_balances={"USDT": Decimal("5000")})
    await svc.register_session(
        "s1", ex, allocated=Decimal("5000"), symbols=["BTCUSDT"], exchange_id="binance"
    )
    await svc.on_mark("binance", "BTCUSDT", Decimal("60000"), T0 + timedelta(hours=1))

    restarted = await _service(pool, clock)
    after = restarted.account()
    assert after.peak_equity == Decimal("10000")
    assert after.equity == Decimal("8900")
    assert after.tier == 3
    assert after.consecutive_losses == 3
    async with pool.acquire() as conn:
        legacy = await conn.fetchrow(
            "SELECT peak_portfolio_value, consecutive_losses FROM risk_state WHERE scope = 'global'"
        )
        basis = await conn.fetchval("SELECT paper_capital FROM risk_state WHERE scope = 'paper'")
    assert tuple(legacy) == (Decimal("0"), 0)
    assert basis is not None
    assert basis == restarted.paper_capital
    assert restarted.pending_paper_capital is None


async def test_kill_switch_and_audit_trail_persist(pool: asyncpg.Pool) -> None:
    clock = Clock(T0)
    svc = await _service(pool, clock)
    result = await svc.activate_kill_switch("user:alice", "drill")
    assert result.persisted is True

    restarted = await _service(pool, clock)
    assert restarted.kill_switch_active() is True

    app = FastAPI()
    app.state.db_pool = pool
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/api/risk/events?limit=5")
    assert resp.status_code == 200
    rows = resp.json()
    assert [(r["kind"], r["actor"], r["reason"]) for r in rows] == [
        ("kill_switch_on", "user:alice", "drill")
    ]
    assert rows[0]["details"] == {"persisted": True}

    await restarted.release_kill_switch("user:alice", "drill over")
    assert (await _service(pool, clock)).kill_switch_active() is False


class _Mgr:
    """The LocalTrader's session-manager shim: only the risk service is needed here."""

    def __init__(self, svc: RiskService) -> None:
        self.risk = svc

    def list_sessions(self) -> list[Any]:
        return []


async def _client(pool: asyncpg.Pool, svc: RiskService) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.db_pool = pool
    app.state.trader = LocalTrader(pool, _Mgr(svc))
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _registered(svc: RiskService) -> PaperTradingExecutor:
    ex = PaperTradingExecutor(initial_balances={"USDT": Decimal("10000")})
    await svc.register_session(
        "s1",
        ex,
        allocated=Decimal("10000"),
        symbols=["BTCUSDT"],
        exchange_id="binance",
        strategy_id="saw",
    )
    await svc.on_mark("binance", "BTCUSDT", Decimal("60000"), T0 + timedelta(hours=1))
    return ex


async def test_strategy_override_round_trip_and_mid_session_tightening(
    pool: asyncpg.Pool,
) -> None:
    """The full flow on real PG: PUT reloads into the running service (no restart), a
    restart picks the override up, and DELETE clears it."""
    clock = Clock(T0)
    svc = await _service(pool, clock)
    await _registered(svc)
    probe = order(Side.BUY, "0.01")  # notional 600 against the global cap 0.10 x 10000
    assert (await svc.check(probe, "s1", OPEN)).approved is True

    client = await _client(pool, svc)
    try:
        resp = await client.get("/api/risk/config/saw")
        assert resp.status_code == 200
        assert resp.json()["overrides"]["max_position_pct"] is None
        assert resp.json()["effective"]["max_position_pct"] == 0.10

        resp = await client.put(
            "/api/risk/config/saw",
            json={"max_position_pct": 0.05, "max_concurrent_positions": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["overrides"] == {
            "strategy_id": "saw",
            "max_position_pct": 0.05,
            "max_risk_per_trade": None,
            "max_concurrent_positions": 2,
        }
        assert resp.json()["effective"]["max_position_pct"] == 0.05

        # mid-session tightening: the same probe that passed is now refused, no restart
        decision = await svc.check(probe, "s1", OPEN)
        assert (decision.approved, decision.check) == (False, "position_size")

        events = (await client.get("/api/risk/events?limit=5")).json()
        assert events[0]["kind"] == "strategy_override_set"
        assert events[0]["actor"] == "anonymous"
        assert events[0]["details"] == {
            "max_position_pct": 0.05,
            "max_risk_per_trade": None,
            "max_concurrent_positions": 2,
        }

        resp = await client.request(
            "DELETE", "/api/risk/config/saw", json={"reason": "back to the global cap"}
        )
        assert resp.status_code == 200
        assert resp.json()["overrides"]["max_position_pct"] is None
        assert resp.json()["effective"]["max_position_pct"] == 0.10

        cleared = (await client.get("/api/risk/events?limit=5")).json()
        assert cleared[0]["kind"] == "strategy_override_cleared"
        assert cleared[0]["reason"] == "back to the global cap"
        assert cleared[0]["details"] == {"strategy_id": "saw"}
    finally:
        await client.aclose()
    assert (await svc.check(probe, "s1", OPEN)).approved is True

    # PUT again, then prove a restart enforces the stored override from the first load
    client = await _client(pool, svc)
    try:
        resp = await client.put("/api/risk/config/saw", json={"max_position_pct": 0.05})
        assert resp.status_code == 200
    finally:
        await client.aclose()
    restarted = await _service(pool, clock)
    await _registered(restarted)
    decision = await restarted.check(probe, "s1", OPEN)
    assert (decision.approved, decision.check) == (False, "position_size")

    # a looser stored value never loosens past the global
    client = await _client(pool, restarted)
    try:
        resp = await client.put("/api/risk/config/saw", json={"max_position_pct": 0.50})
        assert resp.json()["effective"]["max_position_pct"] == 0.10
        resp = await client.delete("/api/risk/config/saw")  # clean up for other readers
        assert resp.status_code == 200
    finally:
        await client.aclose()


async def test_a_strategy_rows_kill_switch_is_never_read(pool: asyncpg.Pool) -> None:
    """A poisoned strategy row with kill_switch_active = TRUE never latches or blocks."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO risk_config (scope, kill_switch_active) "
            "VALUES ('strategy:saw', TRUE) ON CONFLICT (scope) DO NOTHING"
        )
    clock = Clock(T0)
    svc = await _service(pool, clock)
    assert svc.kill_switch_active() is False
    await _registered(svc)
    probe = order(Side.BUY, "0.001")
    assert (await svc.check(probe, "s1", OPEN)).approved is True

    restarted = await _service(pool, clock)
    assert restarted.kill_switch_active() is False
