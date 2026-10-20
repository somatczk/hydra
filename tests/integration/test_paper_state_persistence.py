"""Paper account persistence on real TimescaleDB: save, restore, idempotent trades, snapshots."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest

from hydra.core.types import OHLCV
from hydra.data.migrations.runner import current_revision, head_revision, upgrade_to_head
from hydra.execution import paper_store
from hydra.execution.paper_trading import (
    PaperStateCorruptError,
    PaperStateUnavailableError,
    PaperTradingExecutor,
)

D = Decimal
BTC = "BTCUSDT"
SID = "paper-it-1"
T0 = datetime(2026, 5, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D("1"),
        timestamp=T0 + timedelta(hours=i),
    )


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    p = await asyncpg.create_pool(fresh_database, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _trading_executor(pool: asyncpg.Pool) -> PaperTradingExecutor:
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("10000")},
        db_pool=pool,
        session_id=SID,
        strategy_id="rsi",
        fee_pct=D("0.001"),
        slippage_pct=D("0.001"),
    )
    await ex.checkpoint()
    await ex.on_closed_bar(BTC, bar(0, "100", "101", "99", "100"))
    await ex.create_order(BTC, "BUY", "MARKET", D("2"))
    await ex.create_order(BTC, "SELL", "OCO", D("1.5"), price=D("130"), stop_price=D("90"))
    await ex.create_order(BTC, "SELL", "TRAILING_STOP", D("0.5"), params={"trail_pct": "0.1"})
    await ex.on_closed_bar(BTC, bar(1, "100", "112", "98", "110"))
    await ex.on_closed_bar(BTC, bar(2, "110", "125", "108", "120"))
    await ex.on_closed_bar(BTC, bar(3, "120", "121", "100", "104"))
    return ex


async def test_schema_has_paper_objects_and_upgrade_is_idempotent(
    pool: asyncpg.Pool, fresh_database: str
) -> None:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    assert await current_revision(fresh_database) == head_revision()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('public.paper_accounts') IS NOT NULL")
        assert await conn.fetchval(
            "SELECT to_regclass('public.ux_trades_session_fill_seq') IS NOT NULL"
        )
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('trades', 'balance_snapshots')"
            )
        }
    assert {"order_id", "order_type", "fill_seq", "net_pnl", "liquidity", "reason"} <= cols
    assert "session_id" in cols


async def test_restore_reproduces_state_and_the_next_bar(pool: asyncpg.Pool) -> None:
    a = await _trading_executor(pool)

    b = await PaperTradingExecutor.restore(pool, SID)

    assert b is not None
    assert b.to_state() == a.to_state()
    assert b.get_account_snapshot() == a.get_account_snapshot()
    assert await b.fetch_open_orders() == await a.fetch_open_orders()
    assert b._state_version == a._state_version
    assert await b.get_filled_orders() == await a.get_filled_orders()
    assert [f.fill_seq for f in b.get_fills()] == [1, 2]

    shadow = PaperTradingExecutor.from_state(a.to_state())
    nxt = bar(4, "104", "105", "85", "88")
    restored_fills = await b.on_closed_bar(BTC, nxt)
    shadow_fills = await shadow.on_closed_bar(BTC, nxt)
    assert [(f.order_id, f.price, f.net_pnl, f.fill_seq) for f in restored_fills] == [
        (f.order_id, f.price, f.net_pnl, f.fill_seq) for f in shadow_fills
    ]
    assert [(f.reason, f.price, f.net_pnl, f.fill_seq) for f in restored_fills] == [
        ("stop_loss", D("89.910"), D("-15.5700150"), 3)
    ]
    assert b.get_account_snapshot().persist_dirty is False

    async with pool.acquire() as conn:
        version = await conn.fetchval(
            "SELECT state_version FROM paper_accounts WHERE session_id = $1", SID
        )
        trades = await conn.fetch(
            "SELECT fill_seq, side, reason, pnl, net_pnl FROM trades "
            "WHERE session_id = $1 ORDER BY fill_seq",
            SID,
        )
        positions = await conn.fetch("SELECT quantity FROM positions WHERE session_id = $1", SID)
    assert version == b._state_version
    assert [(r["fill_seq"], r["side"], r["reason"]) for r in trades] == [
        (1, "BUY", "market"),
        (2, "SELL", "trailing_stop"),
        (3, "SELL", "stop_loss"),
    ]
    assert trades[0]["net_pnl"] is None
    assert trades[1]["net_pnl"] == D("6.03750625")
    assert trades[2]["net_pnl"] == D("-15.57001500")
    assert positions == []


async def test_trade_rows_are_unique_per_fill_seq(pool: asyncpg.Pool) -> None:
    a = await _trading_executor(pool)
    first = a.get_fills()[0]
    row = paper_store.TradeRow(
        symbol=first.symbol,
        side=first.side,
        price=first.price,
        quantity=first.quantity,
        fee=first.fee,
        gross_pnl=D("0"),
        net_pnl=None,
        order_id=first.order_id,
        order_type=first.order_type,
        fill_seq=first.fill_seq,
        liquidity=first.liquidity,
        reason=first.reason,
        timestamp=first.timestamp,
    )
    account = paper_store.AccountRow(
        session_id=SID,
        strategy_id="rsi",
        exchange_id="binance",
        quote_asset="USDT",
        state=a.to_state(),
        state_version=a._state_version + 1,
    )
    async with pool.acquire() as conn:
        await paper_store.save(conn, account, [row, row])
        count = await conn.fetchval(
            "SELECT count(*) FROM trades WHERE session_id = $1 AND fill_seq = $2",
            SID,
            first.fill_seq,
        )
        with pytest.raises(paper_store.StaleStateError):
            await paper_store.save(conn, account)
    assert count == 1


async def test_balance_snapshots_carry_session_and_real_drawdown(pool: asyncpg.Pool) -> None:
    a = await _trading_executor(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, total_value, drawdown_pct, peak_value, session_id "
            "FROM balance_snapshots ORDER BY id"
        )
    assert rows
    assert {r["session_id"] for r in rows} == {SID}
    last = rows[-1]
    snap = a.get_account_snapshot()
    assert last["timestamp"] == T0 + timedelta(hours=3)
    assert last["peak_value"] == snap.peak_equity.quantize(D("0.00000001"))
    assert last["total_value"] == snap.equity.quantize(D("0.00000001"))
    expected = ((snap.peak_equity - snap.equity) / snap.peak_equity).quantize(D("0.0001"))
    assert last["drawdown_pct"] == expected
    assert expected > 0


async def test_restore_without_state(pool: asyncpg.Pool) -> None:
    assert await PaperTradingExecutor.restore(pool, "never-ran") is None

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO positions (strategy_id, session_id, symbol, direction, quantity, source) "
            "VALUES ('rsi', 'pre-p1', 'BTCUSDT', 'SHORT', 1, 'paper')"
        )
    with pytest.raises(PaperStateUnavailableError, match="pre-P1"):
        await PaperTradingExecutor.restore(pool, "pre-p1")


async def test_restore_rejects_corrupt_state(pool: asyncpg.Pool) -> None:
    a = await _trading_executor(pool)
    state = a.to_state()
    state["free"]["USDT"] = "-5"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE paper_accounts SET state = $1::jsonb WHERE session_id = $2",
            json.dumps(state),
            SID,
        )
    with pytest.raises(PaperStateCorruptError):
        await PaperTradingExecutor.restore(pool, SID)


class _AckLostPool:
    """The real pool, except that one COMMIT's acknowledgement is lost after it lands."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.lose_next = False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as conn:
            yield conn
        if self.lose_next:
            self.lose_next = False
            raise ConnectionResetError("connection lost after COMMIT")


async def test_lost_commit_ack_is_adopted_on_the_real_jsonb_row(pool: asyncpg.Pool) -> None:
    flaky = _AckLostPool(pool)
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("10000")}, db_pool=flaky, session_id=SID, strategy_id="rsi"
    )
    await ex.checkpoint()
    await ex.on_closed_bar(BTC, bar(0, "100", "101", "99", "100"))
    flaky.lose_next = True
    await ex.create_order(BTC, "BUY", "MARKET", D("2"))
    assert ex.get_account_snapshot().persist_dirty is True

    await ex.on_closed_bar(BTC, bar(1, "100", "102", "99", "101"))
    await ex.create_order(BTC, "SELL", "MARKET", D("1"))

    snap = ex.get_account_snapshot()
    assert (snap.persist_dirty, snap.halted) == (False, None)
    async with pool.acquire() as conn:
        version = await conn.fetchval(
            "SELECT state_version FROM paper_accounts WHERE session_id = $1", SID
        )
        seqs = await conn.fetch(
            "SELECT fill_seq FROM trades WHERE session_id = $1 ORDER BY fill_seq", SID
        )
    assert version == ex._state_version == 5
    assert [r["fill_seq"] for r in seqs] == [1, 2]
