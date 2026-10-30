"""The volume model: one fill core, both executors (the parity discipline).

Every order is capped to ``max_participation`` (10 % by default) of the volume of the bar
it fills on. An entry market order fills the fillable slice and cancels the remainder
(reason ``participation_cap``, nothing traded); a resting order slices per bar and the
remainder keeps working under the same order id, its lock shrinking with each slice. A
market order that would open exposure on a stale mark is refused; reduce-only exits keep
filling at the stale price, labelled. Numbers are exact: fee 0.1 %, slippage 0.1 %.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.core.types import OHLCV
from hydra.execution.paper_trading import OrderRejectedError, PaperTradingExecutor
from hydra.execution.perp_trading import PerpPaperTradingExecutor

D = Decimal
BTC = "BTCUSDT"
T0 = datetime(2024, 6, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str, volume: str = "1000") -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(volume),
        timestamp=T0 + timedelta(hours=i),
    )


def flat(i: int, price: str, volume: str = "1000") -> OHLCV:
    return bar(i, price, price, price, price, volume)


def falling(i: int, o: str, volume: str = "20") -> OHLCV:
    """A thin bar gapping under the 95 stop, like the ones a sliced exit fights through."""
    return bar(i, o, o, D(o) - D("1"), D(o) - D("0.5"), volume)


def spot(capital: str = "100000", max_participation: str = "0.1") -> PaperTradingExecutor:
    return PaperTradingExecutor(
        initial_balances={"USDT": D(capital)},
        fee_pct=D("0.001"),
        slippage_pct=D("0.001"),
        max_participation=D(max_participation),
    )


def perp(capital: str = "100000", max_participation: str = "0.1") -> PerpPaperTradingExecutor:
    return PerpPaperTradingExecutor(
        initial_balances={"USDT": D(capital)},
        fee_pct=D("0.001"),
        slippage_pct=D("0.001"),
        max_participation=D(max_participation),
    )


async def spot_long(qty: str, entry_volume: str = "1000") -> PaperTradingExecutor:
    ex = spot()
    await ex.on_closed_bar(BTC, flat(0, "100", entry_volume))
    await ex.create_order(BTC, "BUY", "MARKET", D(qty))
    return ex


async def perp_long(qty: str, entry_volume: str = "1000") -> PerpPaperTradingExecutor:
    ex = perp()
    await ex.on_closed_bar(BTC, flat(0, "100", entry_volume))
    await ex.create_order(BTC, "BUY", "MARKET", D(qty))
    return ex


class TestSpotMarketCap:
    async def test_a_market_buy_fills_the_slice_and_cancels_the_remainder(self) -> None:
        ex = spot()
        await ex.on_closed_bar(BTC, flat(0, "100", "100"))  # cap: 10 of the 100 volume

        resp = await ex.create_order(BTC, "BUY", "MARKET", D("15"))

        fill, cancel = ex.get_fills()
        # 10 at 100 x 1.001, taker fee 0.1 %: notional 1001, fee 1.001
        assert (fill.side, fill.quantity, fill.price, fill.fee) == (
            "BUY",
            D("10"),
            D("100.100"),
            D("1.001000"),
        )
        assert (cancel.status, cancel.reason, cancel.quantity, cancel.fee, cancel.fill_seq) == (
            "CANCELLED",
            "participation_cap",
            D("5"),
            D("0"),
            0,
        )
        assert cancel.price == fill.price
        assert (resp["quantity"], resp["requested_quantity"], resp["participation_capped"]) == (
            10.0,
            15.0,
            True,
        )
        assert resp["fee"]["cost"] == 1.001
        assert await ex.fetch_balance() == {"USDT": D("98997.999"), "BTC": D("10")}

    async def test_an_uncapped_market_order_carries_no_cap_fields(self) -> None:
        ex = spot()
        await ex.on_closed_bar(BTC, flat(0, "100", "1000"))

        resp = await ex.create_order(BTC, "BUY", "MARKET", D("15"))

        assert resp["quantity"] == 15.0
        assert "requested_quantity" not in resp
        assert "participation_capped" not in resp

    async def test_before_any_closed_bar_the_volume_is_unknown_and_fills_uncapped(self) -> None:
        ex = spot()
        ex.set_mark(BTC, D("100"))

        resp = await ex.create_order(BTC, "BUY", "MARKET", D("15"))

        assert resp["quantity"] == 15.0
        assert ex.get_fills()[0].quantity == D("15")

    async def test_a_reduce_only_market_sell_is_not_capped(self) -> None:
        ex = await spot_long("10")
        await ex.on_closed_bar(BTC, flat(1, "100", "1"))

        resp = await ex.create_order(BTC, "SELL", "MARKET", D("10"), params={"reduce_only": True})

        # an exit must never be unfillable: 10 on volume 1 still fills whole
        assert resp["quantity"] == 10.0
        assert "participation_capped" not in resp


class TestSpotStaleMark:
    async def test_an_opening_market_buy_on_a_stale_mark_is_refused(self) -> None:
        ex = spot()
        await ex.on_closed_bar(BTC, flat(0, "100"))
        ex.set_mark(BTC, D("103"))
        ex.mark_feed_stale()

        with pytest.raises(OrderRejectedError, match="the mark 103 is stale"):
            await ex.create_order(BTC, "BUY", "MARKET", D("1"))

        assert ex.get_fills() == []
        assert await ex.fetch_balance() == {"USDT": D("100000")}

    async def test_a_reduce_only_exit_still_fills_at_the_stale_mark(self) -> None:
        ex = await spot_long("1")
        ex.set_mark(BTC, D("97"))
        ex.mark_feed_stale()

        resp = await ex.create_order(BTC, "SELL", "MARKET", D("1"), params={"reduce_only": True})

        fill = ex.get_fills()[-1]
        assert (fill.price, fill.mark_source, resp["mark_source"]) == (
            D("96.903"),
            "stale_live",
            "stale_live",
        )


class TestSpotRestingSlices:
    async def test_a_sell_stop_slices_five_times_two_and_the_remainder_keeps_working(self):
        ex = await spot_long("10")
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("10"), stop_price=D("95"))

        fills: list = []
        for i, op in enumerate(("94", "93", "92", "91", "90"), start=1):
            bar_fills = await ex.on_closed_bar(BTC, falling(i, op, "20"))
            assert len(bar_fills) == 1
            fills.append(bar_fills[0])
            open_orders = await ex.fetch_open_orders(BTC)
            if i < 5:
                # the remainder keeps working under the same id, its lock shrinking
                assert [(o["id"], o["amount"]) for o in open_orders] == [
                    (fills[0].order_id, 10.0 - 2 * i)
                ]
                assert ex.get_account_snapshot().quote_locked == D("0")
                assert ex._ledger.get_locked("BTC") == D(10 - 2 * i)
            else:
                assert open_orders == []
                assert ex._ledger.get_locked("BTC") == D("0")

        # five FILLED of exactly 2 each, at each bar's open slipped 0.1 %
        prices = [D("93.906"), D("92.907"), D("91.908"), D("90.909"), D("89.910")]
        entry_fee_share = D("1.001") * D("2") / D("10")  # the pro-rata entry fee per slice
        for fill, price in zip(fills, prices, strict=True):
            gross = D("2") * (price - D("100.100"))
            fee = D("2") * price * D("0.001")
            assert (fill.quantity, fill.price, fill.reason, fill.liquidity) == (
                D("2"),
                price,
                "stop_loss",
                "taker",
            )
            assert fill.fee == fee
            assert fill.net_pnl == gross - fee - entry_fee_share
        # 100000 - the 1001 entry notional and 1.001 fee, plus five slices'
        # 2 x (93.906+92.907+91.908+90.909+89.910) less their 0.1 % fees
        assert await ex.fetch_balance() == {"USDT": D("99916.1599200")}

    async def test_a_buy_limit_slices_and_consumes_its_lock(self) -> None:
        ex = spot("1002")
        await ex.on_closed_bar(BTC, flat(0, "110", "1000"))
        await ex.create_order(BTC, "BUY", "LIMIT", D("10"), price=D("100"))
        assert ex.get_account_snapshot().quote_locked == D("1001.000")

        # touches the limit without gapping through it: fills at 100, slice 2 of 10
        (fill,) = await ex.on_closed_bar(BTC, bar(1, "100.5", "101", "99.5", "100.2", "20"))

        # the lock shrank by exactly the slice's cost at the limit: 2 x 100 x 1.001
        snap = ex.get_account_snapshot()
        assert snap.quote_locked == D("800.800")
        assert (fill.quantity, fill.price, fill.liquidity) == (D("2"), D("100"), "maker")
        [(order)] = await ex.fetch_open_orders(BTC)
        assert order["amount"] == 8.0

        (fill,) = await ex.on_closed_bar(BTC, bar(2, "100.5", "101", "99.5", "100.2", "100"))
        assert (fill.quantity, fill.price) == (D("8"), D("100"))
        assert ex.get_account_snapshot().quote_locked == D("0")
        assert await ex.fetch_open_orders(BTC) == []

    async def test_a_partial_oco_leg_fill_drops_its_sibling_and_continues_standalone(self):
        ex = await spot_long("10")
        oco = await ex.create_order(BTC, "SELL", "OCO", D("10"), price=D("110"), stop_price=D("90"))

        (fill,) = await ex.on_closed_bar(BTC, bar(1, "105", "111", "104", "110", "20"))

        # the take-profit leg sliced 2 of 10; the stop sibling is gone, the TP remainder
        # continues standalone under the same leg id
        assert (fill.quantity, fill.price, fill.reason, fill.liquidity) == (
            D("2"),
            D("110"),
            "take_profit",
            "maker",
        )
        assert fill.parent_order_id == oco["id"]
        [(order)] = await ex.fetch_open_orders(BTC)
        assert (order["id"], order["type"], order["amount"], order["oco_id"]) == (
            oco["take_profit_order_id"],
            "LIMIT",
            8.0,
            None,
        )

        (fill,) = await ex.on_closed_bar(BTC, bar(2, "109", "112", "108", "111", "1000"))
        assert (fill.quantity, fill.parent_order_id) == (D("8"), None)
        assert await ex.fetch_open_orders(BTC) == []
        assert ex.get_account_snapshot().quote_locked == D("0")


class TestPerpMarketCap:
    async def test_a_perp_market_buy_fills_the_slice_and_cancels_the_remainder(self) -> None:
        ex = perp()
        await ex.on_closed_bar(BTC, flat(0, "100", "100"))

        resp = await ex.create_order(BTC, "BUY", "MARKET", D("15"))

        fill, cancel = ex.get_fills()
        assert (fill.side, fill.quantity, fill.price, fill.fee) == (
            "BUY",
            D("10"),
            D("100.100"),
            D("1.001000"),
        )
        assert (cancel.status, cancel.reason, cancel.quantity) == (
            "CANCELLED",
            "participation_cap",
            D("5"),
        )
        assert (resp["quantity"], resp["requested_quantity"], resp["participation_capped"]) == (
            10.0,
            15.0,
            True,
        )
        snap = ex.get_account_snapshot()
        assert (snap.wallet_available, snap.margin_used, snap.base_totals["BTC"]) == (
            D("98997.999"),
            D("1001.000"),
            D("10"),
        )

    async def test_a_perp_opening_sell_on_a_stale_mark_is_refused(self) -> None:
        ex = perp()
        await ex.on_closed_bar(BTC, flat(0, "100"))
        ex.set_mark(BTC, D("97"))
        ex.mark_feed_stale()

        with pytest.raises(OrderRejectedError, match="the mark 97 is stale"):
            await ex.create_order(BTC, "SELL", "MARKET", D("1"))  # a short entry

        assert ex.get_fills() == []

    async def test_a_perp_reduce_only_exit_fills_at_the_stale_mark(self) -> None:
        ex = await perp_long("1")
        ex.set_mark(BTC, D("97"))
        ex.mark_feed_stale()

        resp = await ex.create_order(BTC, "SELL", "MARKET", D("1"), params={"reduce_only": True})

        assert (resp["price"], resp["mark_source"]) == (96.903, "stale_live")


class TestPerpRestingSlices:
    async def test_a_perp_stop_slices_reduce_only_aware(self) -> None:
        ex = await perp_long("10")
        await ex.create_order(
            BTC,
            "SELL",
            "STOP_MARKET",
            D("10"),
            stop_price=D("95"),
            params={"reduce_only": True},
        )

        fills: list = []
        for i, op in enumerate(("94", "93", "92", "91", "90"), start=1):
            bar_fills = await ex.on_closed_bar(BTC, falling(i, op, "20"))
            assert len(bar_fills) == 1
            fills.append(bar_fills[0])

        prices = [D("93.906"), D("92.907"), D("91.908"), D("90.909"), D("89.910")]
        for fill, price in zip(fills, prices, strict=True):
            assert (fill.quantity, fill.price) == (D("2"), price)
        assert await ex.fetch_open_orders(BTC) == []
        assert ex.get_account_snapshot().base_totals["BTC"] == 0

    async def test_a_reduce_only_slice_never_exceeds_the_position_left(self) -> None:
        ex = await perp_long("10")
        await ex.create_order(
            BTC,
            "SELL",
            "STOP_MARKET",
            D("10"),
            stop_price=D("95"),
            params={"reduce_only": True},
        )
        # close 6 at market first (an exit, uncapped), leaving 4 for the stop to slice
        await ex.create_order(BTC, "SELL", "MARKET", D("6"), params={"reduce_only": True})

        (fill,) = await ex.on_closed_bar(BTC, falling(1, "94", "100"))

        # the slice clamps to the 4 left, never flipping into a short
        assert (fill.quantity, fill.side) == (D("4"), "SELL")
        assert await ex.fetch_open_orders(BTC) == []
        assert ex.get_account_snapshot().base_totals["BTC"] == 0
