"""Spot ledger rules of the paper executor: no naked shorts, fees in quote, exact equity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.types import OHLCV, Direction, Position
from hydra.execution.paper_trading import (
    InsufficientBalanceError,
    OrderRejectedError,
    PaperTradingExecutor,
)

D = Decimal
SYM = "BTCUSDT"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D("1"),
        timestamp=T0 + timedelta(hours=i),
    )


def flat_bar(i: int, price: str) -> OHLCV:
    return bar(i, price, price, price, price)


def executor(capital: str, fee: str = "0.001", slippage: str = "0") -> PaperTradingExecutor:
    return PaperTradingExecutor(
        initial_balances={"USDT": D(capital)}, fee_pct=D(fee), slippage_pct=D(slippage)
    )


async def test_sell_without_base_is_rejected_and_state_unchanged() -> None:
    ex = executor("100")
    await ex.on_closed_bar(SYM, flat_bar(0, "60000"))

    with pytest.raises(InsufficientBalanceError, match="cannot sell short"):
        await ex.create_order(SYM, "SELL", "MARKET", D("1"))

    assert await ex.get_balance() == {"USDT": D("100")}
    assert await ex.get_positions() == []
    assert await ex.get_equity() == D("100")


async def test_buy_more_than_quote_rejected() -> None:
    ex = executor("1000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))

    with pytest.raises(InsufficientBalanceError):
        await ex.create_order(SYM, "BUY", "MARKET", D("10"))
    assert await ex.get_balance() == {"USDT": D("1000")}

    await ex.create_order(SYM, "BUY", "MARKET", D("9.98"))
    assert await ex.get_balance() == {"USDT": D("1.002"), "BTC": D("9.98")}


async def test_equity_is_cash_plus_marked_base_no_double_count() -> None:
    ex = executor("10000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "MARKET", D("1"))
    assert (await ex.get_balance())["USDT"] == D("9899.9")

    await ex.on_closed_bar(SYM, flat_bar(1, "150"))

    snap = ex.get_account_snapshot()
    assert snap.equity == D("10049.9")
    assert await ex.get_equity() == D("10049.9")
    assert snap.unrealized == D("50")
    assert (
        snap.equity == snap.initial_equity + snap.realized_gross - snap.fees_paid + snap.unrealized
    )
    (pos,) = await ex.get_positions(SYM)
    assert pos.unrealized_pnl == D("50")
    assert pos.direction == Direction.LONG


async def test_round_trip_fees_in_net_pnl() -> None:
    ex = executor("1000", fee="0.01")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    buy = await ex.create_order(SYM, "BUY", "MARKET", D("1"))
    sell = await ex.create_order(SYM, "SELL", "MARKET", D("1"))

    assert buy["pnl"] == 0.0
    assert buy["net_pnl"] is None
    assert sell["pnl"] == 0.0
    assert buy["fee"] == {"cost": 1.0, "currency": "USDT"}
    assert sell["fee"] == {"cost": 1.0, "currency": "USDT"}
    fills = ex.get_fills()
    assert fills[1].gross_pnl == D("0")
    assert fills[1].net_pnl == D("-2.00")
    assert await ex.get_balance() == {"USDT": D("998.00")}
    wallet = D("1000")
    sum_pnl = sum((f.gross_pnl or D(0) for f in fills), D(0))
    sum_fee = sum((f.fee for f in fills), D(0))
    snap = ex.get_account_snapshot()
    assert wallet + sum_pnl + snap.unrealized - sum_fee == await ex.get_equity() == D("998")


async def test_partial_close_allocates_entry_fee_pro_rata() -> None:
    ex = executor("1000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "MARKET", D("2"))
    await ex.on_closed_bar(SYM, flat_bar(1, "110"))

    await ex.create_order(SYM, "SELL", "MARKET", D("1"))

    fill = ex.get_fills()[-1]
    assert fill.fee == D("0.110")
    assert fill.gross_pnl == D("10")
    assert fill.net_pnl == D("9.790")
    lot = ex.to_state()["lots"][SYM]
    assert D(lot["price_basis"]) + D(lot["entry_fees_open"]) == D("100.1")
    (pos,) = await ex.get_positions(SYM)
    assert pos.realized_pnl == D("9.790")


async def test_second_independent_sell_rejected_by_lock() -> None:
    ex = executor("1000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "MARKET", D("1"))

    tp = await ex.create_order(SYM, "SELL", "LIMIT", D("1"), price=D("110"))
    with pytest.raises(InsufficientBalanceError):
        await ex.create_order(SYM, "SELL", "STOP_MARKET", D("1"), stop_price=D("90"))

    await ex.cancel_order(tp["id"], SYM)
    oco = await ex.create_order(SYM, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))
    assert oco["status"] == "PENDING"
    assert ex.get_account_snapshot().open_orders == 2
    assert await ex.get_balance() == {"USDT": D("899.900")}


async def test_stop_locked_market_sell_needs_disarm_first() -> None:
    ex = executor("1000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "MARKET", D("1"))
    stop = await ex.create_order(SYM, "SELL", "STOP_MARKET", D("1"), stop_price=D("90"))

    with pytest.raises(InsufficientBalanceError):
        await ex.create_order(SYM, "SELL", "MARKET", D("1"))

    await ex.cancel_order(stop["id"], SYM)
    sold = await ex.create_order(SYM, "SELL", "MARKET", D("1"))
    assert sold["status"] == "FILLED"
    assert await ex.get_balance() == {"USDT": D("999.800")}


async def test_buy_limit_locks_quote_until_cancelled() -> None:
    ex = executor("150")
    first = await ex.create_order(SYM, "BUY", "LIMIT", D("1"), price=D("100"))
    snap = ex.get_account_snapshot()
    assert snap.quote_free == D("49.900")
    assert snap.quote_locked == D("100.100")
    assert await ex.get_equity() == D("150")
    with pytest.raises(InsufficientBalanceError):
        await ex.create_order(SYM, "BUY", "LIMIT", D("1"), price=D("100"))

    await ex.cancel_order(first["id"], SYM)
    assert ex.get_account_snapshot().quote_free == D("150.000")
    assert ex.get_account_snapshot().quote_locked == D("0")


async def test_gap_up_buy_stop_draws_shortfall_from_free_quote() -> None:
    ex = executor("1000")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "STOP_MARKET", D("1"), stop_price=D("110"))

    (fill,) = await ex.on_closed_bar(SYM, bar(1, "130", "131", "129", "130"))

    assert fill.price == D("130")
    assert await ex.get_balance() == {"USDT": D("869.870"), "BTC": D("1")}


async def test_gap_up_buy_stop_without_enough_quote_is_cancelled() -> None:
    ex = executor("115")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    await ex.create_order(SYM, "BUY", "STOP_MARKET", D("1"), stop_price=D("110"))

    assert await ex.on_closed_bar(SYM, bar(1, "130", "131", "129", "130")) == []

    assert ex.get_account_snapshot().open_orders == 0
    assert await ex.get_balance() == {"USDT": D("115")}


async def test_market_rules_reject_step_and_min_notional() -> None:
    ex = PaperTradingExecutor(
        initial_balances={"USDT": D("1000")},
        step_size=D("0.001"),
        min_notional=D("5"),
    )
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    with pytest.raises(OrderRejectedError, match="step"):
        await ex.create_order(SYM, "BUY", "MARKET", D("0.0015"))
    with pytest.raises(OrderRejectedError, match="notional"):
        await ex.create_order(SYM, "BUY", "MARKET", D("0.001"))
    assert (await ex.create_order(SYM, "BUY", "MARKET", D("0.06")))["status"] == "FILLED"


async def test_market_rules_mapping_overrides_per_symbol() -> None:
    class Rules:
        amount_step = D("0.1")
        min_qty = D("0.2")
        min_notional = None

    ex = PaperTradingExecutor(initial_balances={"USDT": D("1000")}, market_rules={SYM: Rules()})
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    with pytest.raises(OrderRejectedError, match="minimum"):
        await ex.create_order(SYM, "BUY", "MARKET", D("0.1"))


def test_non_quote_symbols_and_balances_are_rejected() -> None:
    with pytest.raises(ValueError, match="start with USDT"):
        PaperTradingExecutor(initial_balances={"USDT": D("1"), "BTC": D("1")})


async def test_symbol_not_quoted_in_usdt_is_rejected() -> None:
    ex = executor("100")
    with pytest.raises(OrderRejectedError, match="USDT-quoted"):
        await ex.create_order("ETHBTC", "BUY", "LIMIT", D("1"), price=D("0.05"))


def _assert_ledger_sound(ex: PaperTradingExecutor, positions: list[Position]) -> None:
    ex._check_invariants()
    state = ex.to_state()
    for amount in (*state["free"].values(), *state["locked"].values()):
        assert D(amount) >= 0
    for lock in state["locks"].values():
        assert D(lock["amount"]) >= 0
    for pos in positions:
        assert pos.direction == Direction.LONG
        assert pos.quantity > 0
    snap = ex.get_account_snapshot()
    identity = snap.initial_equity + snap.realized_gross - snap.fees_paid + snap.unrealized
    assert abs(snap.equity - identity) <= D("1e-12")
    assert snap.quote_free >= 0
    assert snap.quote_locked >= 0


async def test_no_negative_balance_invariant_property() -> None:
    gen = np.random.default_rng(20261020)

    def randint(lo: int, hi: int) -> int:
        return int(gen.integers(lo, hi + 1))

    def pick(options: list[str]) -> str:
        return options[randint(0, len(options) - 1)]

    ex = executor("10000", fee="0.001", slippage="0.0005")
    price = D("100")
    await ex.on_closed_bar(SYM, flat_bar(0, "100"))
    i = 1
    rejected = 0
    filled = 0
    for _ in range(500):
        action = pick(["buy", "sell", "limit", "stop", "oco", "trail", "cancel", "bar", "bar"])
        qty = D(randint(1, 400)) / D(100)
        try:
            if action == "buy":
                await ex.create_order(SYM, "BUY", "MARKET", qty)
            elif action == "sell":
                await ex.create_order(SYM, "SELL", "MARKET", qty)
            elif action == "limit":
                side = pick(["BUY", "SELL"])
                await ex.create_order(SYM, side, "LIMIT", qty, price=price + D(randint(-8, 8)))
            elif action == "stop":
                side = pick(["BUY", "SELL"])
                offset = D(randint(1, 8))
                trigger = price + offset if side == "BUY" else price - offset
                await ex.create_order(SYM, side, "STOP_MARKET", qty, stop_price=trigger)
            elif action == "oco":
                await ex.create_order(
                    SYM, "SELL", "OCO", qty, price=price + D(5), stop_price=price - D(5)
                )
            elif action == "trail":
                await ex.create_order(
                    SYM,
                    pick(["BUY", "SELL"]),
                    "TRAILING_STOP",
                    qty,
                    params={"trail_pct": "0.03"},
                )
            elif action == "cancel":
                orders = await ex.fetch_open_orders(SYM)
                if orders:
                    await ex.cancel_order(orders[randint(0, len(orders) - 1)]["id"], SYM)
            else:
                o = price
                c = max(D(50), o + D(randint(-10, 10)))
                h = max(o, c) + D(randint(0, 6))
                low = max(D(1), min(o, c) - D(randint(0, 6)))
                filled += len(await ex.on_closed_bar(SYM, bar(i, str(o), str(h), str(low), str(c))))
                price = c
                i += 1
        except (InsufficientBalanceError, OrderRejectedError):
            rejected += 1
        _assert_ledger_sound(ex, list(await ex.get_positions()))
    await ex.flatten()
    _assert_ledger_sound(ex, list(await ex.get_positions()))
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0")}
    assert rejected > 0
    assert filled > 0
