"""RiskService state on real TimescaleDB: idempotent revision, restart survival, audit trail."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from hydra.dashboard.routes.risk import router
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.circuit_breakers import CircuitBreakerState
from hydra.risk.service import RiskService
from hydra.risk.state import PgRiskStateStore, RiskState
from tests.unit.test_risk.harness import Clock, rules_cache

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
    assert scopes == ["global", "paper"]
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
