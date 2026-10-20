"""Paper state serialisation round trip, the persist outbox and the dirty-entry guard."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.core.types import OHLCV
from hydra.execution import paper_store
from hydra.execution.paper_trading import (
    PaperStateCorruptError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
)

D = Decimal
BTC = "BTCUSDT"
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D("1"),
        timestamp=T0 + timedelta(hours=i),
    )


class RecordingConn:
    """An asyncpg-shaped connection that records statements (the DB boundary only)."""

    def __init__(self, store: RecordingPool) -> None:
        self._store = store

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        staged: list[tuple[str, tuple[Any, ...]]] = []
        self._store.staged = staged
        yield
        self._store.committed.extend(staged)

    async def execute(self, sql: str, *args: Any) -> str:
        if self._store.fail:
            raise OSError("connection refused")
        self._store.staged.append((" ".join(sql.split()), args))
        return "INSERT 0 1"


class RecordingPool:
    def __init__(self) -> None:
        self.fail = False
        self.committed: list[tuple[str, tuple[Any, ...]]] = []
        self.staged: list[tuple[str, tuple[Any, ...]]] = []

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[RecordingConn]:
        yield RecordingConn(self)

    def trade_seqs(self) -> list[int]:
        return [args[12] for sql, args in self.committed if sql.startswith("INSERT INTO trades")]

    def snapshots(self) -> list[tuple[Any, ...]]:
        return [args for sql, args in self.committed if "INTO balance_snapshots" in sql]


async def test_state_round_trip_is_exact() -> None:
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")},
        fee_pct=D("0.00075"),
        slippage_pct=D("0.0003"),
        step_size=D("0.00000001"),
    )
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100.12345678"))
    await ex.create_order(BTC, "BUY", "MARKET", D("0.00000001"))
    await ex.create_order(BTC, "BUY", "MARKET", D("1.23456789"))
    await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("120"), stop_price=D("90"))
    await ex.create_order(BTC, "SELL", "TRAILING_STOP", D("0.2"), params={"trail_pct": "0.07"})
    await ex.create_order(BTC, "BUY", "LIMIT", D("0.5"), price=D("95.5"))

    text = paper_store.dumps_state(ex.to_state())
    restored = PaperTradingExecutor.from_state(paper_store.loads_state(text))

    assert restored.to_state() == ex.to_state()
    assert json.loads(text)["free"]["BTC"] == "0.03456790"
    assert restored.get_account_snapshot() == ex.get_account_snapshot()
    assert await restored.get_equity() == await ex.get_equity()
    assert await restored.fetch_open_orders() == await ex.fetch_open_orders()


async def test_restored_executor_fills_like_the_original() -> None:
    a = PaperTradingExecutor(initial_balances={"USDT": D("1000")})
    await a.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await a.create_order(BTC, "BUY", "MARKET", D("1"))
    await a.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))
    b = PaperTradingExecutor.from_state(
        paper_store.loads_state(paper_store.dumps_state(a.to_state()))
    )

    nxt = bar(1, "95", "96", "85", "88")
    fa = await a.on_closed_bar(BTC, nxt)
    fb = await b.on_closed_bar(BTC, nxt)

    assert [(f.order_id, f.price, f.net_pnl, f.fill_seq) for f in fa] == [
        (f.order_id, f.price, f.net_pnl, f.fill_seq) for f in fb
    ]
    assert b.to_state() == a.to_state()


def test_corrupt_state_is_rejected() -> None:
    ex = PaperTradingExecutor(initial_balances={"USDT": D("10")})
    state = ex.to_state()
    state["free"]["USDT"] = "-1"
    with pytest.raises(PaperStateCorruptError, match="negative"):
        PaperTradingExecutor.from_state(state)
    state = ex.to_state()
    state["free"]["BTC"] = "1"
    with pytest.raises(PaperStateCorruptError, match="without a lot"):
        PaperTradingExecutor.from_state(state)
    with pytest.raises(PaperStateCorruptError, match="format"):
        PaperTradingExecutor.from_state({**ex.to_state(), "format": 99})


def test_db_pool_requires_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        PaperTradingExecutor(db_pool=RecordingPool())


async def test_each_fill_and_bar_is_persisted_in_one_transaction() -> None:
    pool = RecordingPool()
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-1", strategy_id="rsi"
    )
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await ex.create_order(BTC, "BUY", "MARKET", D("1"))
    await ex.on_closed_bar(BTC, bar(1, "100", "120", "100", "120"))

    statements = [sql.split(" (")[0] for sql, _ in pool.committed]
    assert statements.count("INSERT INTO paper_accounts") == 3
    assert pool.trade_seqs() == [1]
    last_snapshot = pool.snapshots()[-1]
    assert last_snapshot[0] == T0 + timedelta(hours=1)
    assert last_snapshot[1] == D("1019.7999")
    assert last_snapshot[2] == D("19.900")
    assert last_snapshot[6] == "s-1"
    positions = [args for sql, args in pool.committed if sql.startswith("INSERT INTO positions")]
    assert positions[-1][3:8] == (BTC, D("1"), D("100.100"), D("19.900"), D("0"))


async def test_exit_succeeds_while_persistence_dirty() -> None:
    pool = RecordingPool()
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-dirty"
    )
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await ex.create_order(BTC, "BUY", "MARKET", D("2"))
    await ex.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("90"))
    pool.fail = True

    (stop_fill,) = await ex.on_closed_bar(BTC, bar(1, "95", "96", "85", "88"))
    assert stop_fill.reason == "stop_loss"
    assert ex.get_account_snapshot().persist_dirty is True
    with pytest.raises(PersistenceUnavailableError):
        await ex.create_order(BTC, "BUY", "MARKET", D("0.1"))
    flat = await ex.flatten()
    assert len(flat) == 1
    assert [f.fill_seq for f in ex._outbox] == [2, 3]
    assert pool.trade_seqs() == [1]

    pool.fail = False
    await ex.create_order(BTC, "BUY", "MARKET", D("0.1"))

    assert ex.get_account_snapshot().persist_dirty is False
    assert pool.trade_seqs() == [1, 2, 3, 4]
    assert ex._outbox == []


async def test_stale_state_version_marks_dirty() -> None:
    class StaleConn(RecordingConn):
        async def execute(self, sql: str, *args: Any) -> str:
            return "INSERT 0 0"

    class StalePool(RecordingPool):
        @asynccontextmanager
        async def acquire(self) -> AsyncIterator[RecordingConn]:
            yield StaleConn(self)

    ex = PaperTradingExecutor(db_pool=StalePool(), session_id="s-stale")
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    assert ex.get_account_snapshot().persist_dirty is True


async def test_concurrent_persists_never_drop_a_fill() -> None:
    import asyncio

    class SlowPool(RecordingPool):
        @asynccontextmanager
        async def acquire(self) -> AsyncIterator[RecordingConn]:
            await asyncio.sleep(0.01)
            yield RecordingConn(self)

    pool = SlowPool()
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-race"
    )
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await asyncio.gather(
        ex.create_order(BTC, "BUY", "MARKET", D("1")),
        ex.create_order(BTC, "BUY", "MARKET", D("2")),
        ex.create_order(BTC, "BUY", "MARKET", D("3")),
    )
    assert sorted(pool.trade_seqs()) == [1, 2, 3]
    assert ex._outbox == []
    assert ex.get_account_snapshot().persist_dirty is False
