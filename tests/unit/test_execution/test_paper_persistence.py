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
    PaperSessionHaltedError,
    PaperStateCorruptError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
)

D = Decimal
BTC = "BTCUSDT"
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str, volume: str = "1000") -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(volume),
        timestamp=T0 + timedelta(hours=i),
    )


class RecordingConn:
    """An asyncpg-shaped connection over an in-memory table set (the DB boundary only).

    It honours the ``state_version`` guard and ``ON CONFLICT (session_id, fill_seq)``.
    """

    def __init__(self, store: RecordingPool) -> None:
        self._store = store

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        staged: list[tuple[str, tuple[Any, ...]]] = []
        self._store.staged = staged
        yield
        store = self._store
        for sql, args in staged:
            if sql.startswith("INSERT INTO paper_accounts"):
                store.accounts[args[0]] = args
            if sql.startswith("INSERT INTO trades") and (args[10], args[12]) in store.trade_keys:
                continue
            if sql.startswith("INSERT INTO trades"):
                store.trade_keys.add((args[10], args[12]))
            store.committed.append((sql, args))
        if store.ack_lost:
            store.ack_lost -= 1
            raise OSError("connection lost after COMMIT")

    async def execute(self, sql: str, *args: Any) -> str:
        if self._store.fail:
            raise OSError("connection refused")
        sql = " ".join(sql.split())
        if sql.startswith("INSERT INTO paper_accounts"):
            held = self._store.accounts.get(args[0])
            if held is not None and held[5] >= args[5]:
                return "INSERT 0 0"
        self._store.staged.append((sql, args))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, session_id: str) -> dict[str, Any] | None:
        held = self._store.accounts.get(session_id)
        if held is None:
            return None
        keys = ("session_id", "strategy_id", "exchange_id", "quote_asset", "state")
        return {**dict(zip(keys, held[:5], strict=True)), "state_version": held[5]}

    async def fetchval(self, sql: str, session_id: str) -> bool:
        return False

    async def fetch(self, sql: str, session_id: str) -> list[dict[str, Any]]:
        names = [
            "symbol",
            "side",
            "price",
            "quantity",
            "fee",
            "pnl",
            "net_pnl",
            "strategy_id",
            "exchange_id",
            "timestamp",
            "session_id",
            "order_id",
            "fill_seq",
            "liquidity",
            "reason",
            "order_type",
            "source",
        ]
        rows = [
            dict(zip(names, args, strict=True))
            for sql, args in self._store.committed
            if sql.startswith("INSERT INTO trades") and args[10] == session_id
        ]
        return sorted(rows, key=lambda r: r["fill_seq"])


class RecordingPool:
    def __init__(self) -> None:
        self.fail = False
        self.ack_lost = 0
        self.committed: list[tuple[str, tuple[Any, ...]]] = []
        self.staged: list[tuple[str, tuple[Any, ...]]] = []
        self.accounts: dict[str, tuple[Any, ...]] = {}
        self.trade_keys: set[tuple[str, int]] = set()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[RecordingConn]:
        yield RecordingConn(self)

    def version(self, session_id: str) -> int:
        return int(self.accounts[session_id][5])

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


async def test_restore_keeps_the_live_mark_an_order_was_placed_at() -> None:
    a = PaperTradingExecutor(initial_balances={"USDT": D("1000")})
    await a.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await a.create_order(BTC, "BUY", "MARKET", D("1"))
    a.set_mark(BTC, D("106"))
    await a.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("101"))
    state = paper_store.loads_state(paper_store.dumps_state(a.to_state()))
    legacy = {
        **state,
        "pending": [{k: v for k, v in o.items() if k != "placed_at"} for o in state["pending"]],
    }

    restored = PaperTradingExecutor.from_state(state)
    old = PaperTradingExecutor.from_state(legacy)

    assert [o["placed_at"] for o in restored.to_state()["pending"]] == ["106"]
    entry_bar = bar(1, "100", "106", "99", "104")
    assert await restored.on_closed_bar(BTC, entry_bar) == []
    assert [f.reason for f in await old.on_closed_bar(BTC, entry_bar)] == ["stop_loss"]


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


async def test_a_partially_filled_stop_round_trips_and_keeps_slicing() -> None:
    """The volume model's state: a sliced stop's quantity and lock persist, and the
    restored remainder keeps filling under the same order id."""
    a = PaperTradingExecutor(initial_balances={"USDT": D("1000")})
    await a.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await a.create_order(BTC, "BUY", "MARKET", D("1"))
    await a.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("95"))
    # a thin bar slices 0.2 of the 1-unit stop; 0.8 keeps resting, locked
    (slice_fill,) = await a.on_closed_bar(BTC, bar(1, "94", "94", "93", "94", "2"))
    assert slice_fill.quantity == D("0.2")

    state = paper_store.loads_state(paper_store.dumps_state(a.to_state()))
    b = PaperTradingExecutor.from_state(state)

    assert b.to_state() == a.to_state()
    assert b.get_account_snapshot() == a.get_account_snapshot()
    [resting] = await b.fetch_open_orders(BTC)
    assert resting["id"] == slice_fill.order_id  # the same id, no format bump
    assert resting["amount"] == 0.8

    # the restored remainder keeps slicing identically on the next thin bar
    nxt = bar(2, "92", "92", "91", "92", "2")
    fa = await a.on_closed_bar(BTC, nxt)
    fb = await b.on_closed_bar(BTC, nxt)
    assert [(f.order_id, f.quantity, f.price) for f in fa] == [
        (f.order_id, f.quantity, f.price) for f in fb
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


async def test_lost_commit_ack_is_adopted_and_persistence_resumes() -> None:
    pool = RecordingPool()
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-ack"
    )
    await ex.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    pool.ack_lost = 1
    await ex.create_order(BTC, "BUY", "MARKET", D("1"))
    assert ex.get_account_snapshot().persist_dirty is True
    assert (pool.version("s-ack"), ex._state_version) == (2, 1)

    await ex.on_closed_bar(BTC, bar(1, "100", "101", "99", "100"))
    await ex.create_order(BTC, "SELL", "MARKET", D("1"))

    snap = ex.get_account_snapshot()
    assert (snap.persist_dirty, snap.halted) == (False, None)
    assert pool.version("s-ack") == ex._state_version == 4
    assert pool.trade_seqs() == [1, 2]
    assert ex._outbox == []
    await ex.create_order(BTC, "BUY", "MARKET", D("1"))
    assert pool.trade_seqs() == [1, 2, 3]


async def test_second_writer_halts_the_executor() -> None:
    pool = RecordingPool()
    first = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-two"
    )
    await first.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await first.create_order(BTC, "BUY", "MARKET", D("1"))
    stored = pool.accounts["s-two"]

    fresh = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-two"
    )
    await fresh.on_closed_bar(BTC, bar(1, "100", "100", "100", "100"))

    halted = fresh.get_account_snapshot().halted
    assert halted is not None and "another executor" in halted
    with pytest.raises(PaperSessionHaltedError):
        await fresh.create_order(BTC, "BUY", "MARKET", D("1"))
    with pytest.raises(PaperSessionHaltedError):
        await fresh.create_order(BTC, "SELL", "MARKET", D("1"))
    await fresh.on_closed_bar(BTC, bar(2, "100", "100", "100", "100"))
    assert pool.accounts["s-two"] == stored
    assert pool.trade_seqs() == [1]


async def test_restore_rebuilds_the_fill_history() -> None:
    pool = RecordingPool()
    a = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")}, db_pool=pool, session_id="s-hist"
    )
    await a.on_closed_bar(BTC, bar(0, "100", "100", "100", "100"))
    await a.create_order(BTC, "BUY", "MARKET", D("1"))
    await a.create_order(BTC, "SELL", "LIMIT", D("1"), price=D("110"))
    await a.on_closed_bar(BTC, bar(1, "100", "112", "99", "111"))

    b = await PaperTradingExecutor.restore(pool, "s-hist")

    assert b is not None
    assert await b.get_filled_orders() == await a.get_filled_orders()
    assert [(f.fill_seq, f.order_type, f.gross_pnl, f.net_pnl) for f in b.get_fills()] == [
        (1, "MARKET", None, None),
        (2, "LIMIT", D("9.900"), D("9.6899")),
    ]


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
