"""The reconcile loop against real TimescaleDB: the Pg store, the persisted sleeve, the
manager's holder and the health fields round-trip, and the audited mismatch transition.

Only the ccxt boundary is fake; the live_orders/live_fills tables, the paper_accounts
persistence, the risk_events audit row and the health snapshot are the real schema."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from prometheus_client import REGISTRY

from hydra.core.config import HydraConfig
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution.live_executor import LiveTradingExecutor
from hydra.execution.reconcile import ReconcileStatus
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.risk.market_rules import MarketRules
from tests.fixtures.binance_trading import FakeBinanceTradingClient

pytestmark = pytest.mark.integration

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("5"),
    price_tick=Decimal("0.01"),
    trailing_delta_bips=(Decimal("10"), Decimal("2000")),
)


@pytest.fixture()
async def pool(fresh_database: str):
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield created
    finally:
        await created.close()


def _executor(fake: FakeBinanceTradingClient, pool: Any, session: str) -> LiveTradingExecutor:
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": Decimal("10000")},
        db_pool=pool,
        strategy_id="strat",
        session_id=session,
        market_rules={"BTCUSDT": RULES},
        poll_interval=5.0,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", Decimal("100000"))
    return ex


async def _reconciled_pass(mgr: SessionManager, executor: LiveTradingExecutor) -> None:
    """One loop tick with the cadence gate reset, as the per-minute loop would run it."""
    mgr._reconcile_last = None
    await mgr._reconcile_live_sessions()


async def test_the_ok_pass_and_the_replay_lock_on_real_pg(pool: Any) -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, pool, "sess-pg-1")
    await ex.checkpoint()
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.01"), params={"tag": "entry"})

    first = await reconcile(ex, fake)
    assert first.state == "ok", first.detail
    async with pool.acquire() as conn:
        fills = await conn.fetchval(
            "SELECT COUNT(*) FROM live_fills WHERE session_id = $1", "sess-pg-1"
        )
        orders = await conn.fetchval(
            "SELECT COUNT(*) FROM live_orders WHERE session_id = $1", "sess-pg-1"
        )
    assert (fills, orders) == (1, 1)
    balance_after_first = await ex.fetch_balance()

    second = await reconcile(ex, fake)
    assert second.state == "ok"
    assert await ex.fetch_balance() == balance_after_first  # the unique constraint guards
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM live_fills WHERE session_id = $1", "sess-pg-1"
            )
            == 1
        )


async def test_untracked_orders_are_reported_never_adopted_on_real_pg(pool: Any) -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, pool, "sess-pg-2")
    await ex.checkpoint()
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.01"), params={"tag": "entry"})
    status = await reconcile(ex, fake)  # the session trades BTCUSDT
    assert status.state == "ok", status.detail
    await fake.create_order("BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("90000"), None, {})

    status = await reconcile(ex, fake)

    assert status.state == "mismatch"
    assert "untracked order 101 on BTCUSDT (not managed)" in (status.detail or "")
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM live_orders WHERE session_id = $1", "sess-pg-2"
            )
            == 1
        )  # nothing was adopted into the outbox: the session's own row only


async def test_the_loop_holds_the_status_and_the_health_fields_round_trip(pool: Any) -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, pool, "sess-pg-3")
    await ex.checkpoint()
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.01"), params={"tag": "entry"})
    mgr = SessionManager(db_pool=pool, config=HydraConfig())
    session = TradingSession(
        session_id="sess-pg-3",
        strategy_id="strat",
        trading_mode="live",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=Decimal("10000"),
    )
    session._executor = ex
    mgr._sessions["sess-pg-3"] = session

    await _reconciled_pass(mgr, ex)

    status = mgr.reconcile_status("sess-pg-3")
    assert status is not None and status.state == "ok", status
    health = await mgr.health_snapshot()
    (live,) = [h for h in health.sessions if h.session_id == "sess-pg-3"]
    assert live.reconcile_state == "ok"
    assert live.last_reconciled_at == status.last_reconciled_at
    assert (
        REGISTRY.get_sample_value("hydra_live_reconcile_state", {"session_id": "sess-pg-3"}) == 0.0
    )


async def test_the_mismatch_transition_is_audited_once_per_entry(pool: Any) -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, pool, "sess-pg-4")
    await ex.checkpoint()
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.01"), params={"tag": "entry"})
    mgr = SessionManager(db_pool=pool, config=HydraConfig())
    session = TradingSession(
        session_id="sess-pg-4",
        strategy_id="strat",
        trading_mode="live",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=Decimal("10000"),
    )
    session._executor = ex
    mgr._sessions["sess-pg-4"] = session
    await fake.create_order("BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("90000"), None, {})

    await _reconciled_pass(mgr, ex)  # enters mismatch: one audit row
    await _reconciled_pass(mgr, ex)  # stays mismatch: no second row
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT reason, details FROM risk_events"
            " WHERE scope = 'live' AND kind = 'live_reconcile_mismatch' ORDER BY id"
        )
    assert len(rows) == 1
    assert "untracked order" in rows[0]["reason"]
    assert json.loads(rows[0]["details"])["session_id"] == "sess-pg-4"

    (user_order,) = fake.open_orders_raw()
    await fake.cancel_order(str(user_order["orderId"]), "BTCUSDT")
    await _reconciled_pass(mgr, ex)  # ok again
    await fake.create_order("BTCUSDT", "BUY", "LIMIT", Decimal("0.01"), Decimal("90000"), None, {})
    await _reconciled_pass(mgr, ex)  # a fresh mismatch is a fresh event

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM risk_events"
            " WHERE scope = 'live' AND kind = 'live_reconcile_mismatch'"
        )
    assert count == 2
    assert mgr.reconcile_status("sess-pg-4") is not None


async def reconcile(ex: LiveTradingExecutor, fake: FakeBinanceTradingClient) -> ReconcileStatus:
    from hydra.execution.reconcile import reconcile_live_session

    return await reconcile_live_session(ex.live_orders, fake, ex)
