"""PaperTradingExecutor: market fills, resting orders on closed bars, OCO, flatten, shims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.core.types import OHLCV
from hydra.execution.paper_trading import (
    InsufficientBalanceError,
    OrderRejectedError,
    PaperFill,
    PaperTradingExecutor,
)

D = Decimal
BTC = "BTCUSDT"
ETH = "ETHUSDT"
T0 = datetime(2024, 6, 1, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D("100"),
        timestamp=T0 + timedelta(hours=i),
    )


def flat_bar(i: int, price: str) -> OHLCV:
    return bar(i, price, price, price, price)


def executor(
    capital: str = "100000", fee: str = "0.001", slippage: str = "0.001"
) -> PaperTradingExecutor:
    return PaperTradingExecutor(
        initial_balances={"USDT": D(capital)}, fee_pct=D(fee), slippage_pct=D(slippage)
    )


async def holding(qty: str, price: str, **kwargs: str) -> PaperTradingExecutor:
    ex = executor(**kwargs)
    await ex.on_closed_bar(BTC, flat_bar(0, price))
    await ex.create_order(BTC, "BUY", "MARKET", D(qty))
    return ex


class TestMarketOrders:
    async def test_market_buy_fills_at_last_close_plus_slippage(self) -> None:
        ex = executor(slippage="0.01", fee="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "10000"))

        result = await ex.create_order(BTC, "BUY", "MARKET", D("1"))

        assert result["status"] == "FILLED"
        assert result["filled"] is True
        assert result["price"] == 10100.0
        assert result["liquidity"] == "taker"
        assert result["fill_seq"] == 1
        assert result["timestamp"] == T0.isoformat()

    async def test_market_sell_after_buy(self) -> None:
        ex = await holding("0.1", "42000")
        result = await ex.create_order(BTC, "SELL", "MARKET", D("0.1"))
        assert result["status"] == "FILLED"
        assert await ex.fetch_positions() == []

    async def test_market_order_no_price_raises(self) -> None:
        with pytest.raises(ValueError, match="No market price"):
            await executor().create_order("UNKNOWNUSDT", "BUY", "MARKET", D("1"))

    async def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(OrderRejectedError, match="not supported"):
            await executor().create_order(
                BTC, "SELL", "TAKE_PROFIT_MARKET", D("1"), stop_price=D("1")
            )


class TestRestingOrders:
    async def test_limit_buy_fills_when_low_reaches_price(self) -> None:
        ex = executor()
        await ex.on_closed_bar(BTC, flat_bar(0, "42000"))
        await ex.create_order(BTC, "BUY", "LIMIT", D("0.1"), price=D("41000"))

        (fill,) = await ex.on_closed_bar(BTC, bar(1, "42000", "42100", "40900", "41500"))

        assert fill.price == D("41000")
        assert fill.liquidity == "maker"
        assert fill.reason == "limit"
        assert fill.fee == D("4.1000")
        assert ex.get_account_snapshot().quote_locked == D("0")

    async def test_limit_buy_not_filled_when_low_above_price(self) -> None:
        ex = executor()
        await ex.on_closed_bar(BTC, flat_bar(0, "42000"))
        await ex.create_order(BTC, "BUY", "LIMIT", D("0.1"), price=D("40000"))
        assert await ex.on_closed_bar(BTC, bar(1, "42000", "42500", "41000", "42100")) == []
        assert len(await ex.fetch_open_orders()) == 1

    async def test_stop_limit_not_reached_rests_as_limit_then_fills_as_maker(self) -> None:
        ex = await holding("1", "100", fee="0.001", slippage="0")
        await ex.create_order(BTC, "SELL", "STOP_LIMIT", D("1"), price=D("94"), stop_price=D("95"))

        assert await ex.on_closed_bar(BTC, bar(1, "90", "92", "88", "91")) == []
        (order,) = await ex.fetch_open_orders(BTC)
        assert order["type"] == "LIMIT"
        assert order["price"] == 94.0

        (fill,) = await ex.on_closed_bar(BTC, bar(2, "91", "96", "90", "95"))
        assert fill.price == D("94")
        assert fill.liquidity == "maker"

    async def test_cancel_pending_order_releases_lock(self) -> None:
        ex = executor()
        result = await ex.create_order(BTC, "BUY", "LIMIT", D("0.1"), price=D("40000"))
        assert (await ex.cancel_order(result["id"], BTC))["status"] == "CANCELLED"
        assert await ex.fetch_open_orders() == []
        assert await ex.fetch_balance() == {"USDT": D("100000.0000")}

    async def test_cancel_unknown_order_raises(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            await executor().cancel_order("nope", BTC)

    async def test_open_orders_show_stop_trigger_and_reason(self) -> None:
        ex = await holding("1", "100")
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("90"))
        (order,) = await ex.fetch_open_orders(BTC)
        assert order["type"] == "STOP_MARKET"
        assert order["stop_price"] == 90.0
        assert order["reason"] == "stop_loss"


class TestClosedBarSemantics:
    async def test_stop_loss_closes_position_on_bar(self) -> None:
        ex = await holding("0.1", "60000", fee="0.001", slippage="0.001")
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("0.1"), stop_price=D("57000"))

        (fill,) = await ex.on_closed_bar(BTC, bar(1, "58000", "58500", "56000", "56500"))

        assert fill.reason == "stop_loss"
        assert fill.price == D("56943.000")
        assert fill.fee == D("5.6943000000")
        # entry 0.1 @ 60060 (fee 6.006), exit 0.1 @ 56943 (fee 5.6943)
        assert fill.gross_pnl == D("-311.7000")
        assert fill.net_pnl == D("-323.4003000000")
        assert await ex.get_positions() == []
        assert ex.get_account_snapshot().open_orders == 0

    async def test_oco_wide_bar_exactly_one_leg_fills(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))

        fills = await ex.on_closed_bar(BTC, bar(1, "100", "120", "80", "100"))

        assert len(fills) == 1
        assert fills[0].reason == "take_profit"
        assert fills[0].price == D("110")
        snap = ex.get_account_snapshot()
        assert snap.base_totals == {"BTC": D("0")}
        assert snap.open_orders == 0
        assert snap.quote_locked == D("0")
        assert await ex.get_positions() == []

    async def test_oco_bullish_wide_bar_stop_wins(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        oco = await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))
        (fill,) = await ex.on_closed_bar(BTC, bar(1, "100", "120", "80", "110"))
        assert fill.reason == "stop_loss"
        assert fill.price == D("90")
        assert fill.order_id == oco["stop_loss_order_id"]
        assert fill.parent_order_id == oco["id"]

    async def test_cancel_oco_by_parent_or_leg_cancels_both(self) -> None:
        ex = await holding("1", "100")
        oco = await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))
        await ex.cancel_order(oco["take_profit_order_id"], BTC)
        assert await ex.fetch_open_orders() == []
        assert (await ex.fetch_balance())["BTC"] == D("1")

        oco = await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))
        await ex.cancel_order(oco["id"], BTC)
        assert await ex.fetch_open_orders() == []

    async def test_on_closed_bar_is_symbol_scoped(self) -> None:
        ex = executor()
        await ex.on_closed_bar(ETH, flat_bar(0, "3000"))
        await ex.on_closed_bar(BTC, flat_bar(0, "60000"))
        await ex.create_order(ETH, "BUY", "LIMIT", D("1"), price=D("2000"))

        assert await ex.on_closed_bar(BTC, bar(1, "60000", "60100", "59900", "60050")) == []

        (order,) = await ex.fetch_open_orders()
        assert order["symbol"] == ETH
        assert await ex.get_last_price(ETH) == D("3000")
        assert await ex.get_last_price(BTC) == D("60050")

    async def test_order_not_evaluated_on_its_creation_bar(self) -> None:
        ex = await holding("1", "100")
        t1 = bar(1, "100", "101", "80", "95")
        await ex.on_closed_bar(BTC, t1)
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("90"))

        assert await ex.on_closed_bar(BTC, t1) == []
        assert len(await ex.fetch_open_orders()) == 1

        fills = await ex.on_closed_bar(BTC, bar(2, "95", "96", "89", "92"))
        assert [f.reason for f in fills] == ["stop_loss"]

    async def test_duplicate_bar_timestamp_is_noop(self) -> None:
        ex = await holding("1", "100")
        await ex.create_order(BTC, "SELL", "LIMIT", D("1"), price=D("110"))
        b = bar(1, "100", "115", "99", "112")

        first = await ex.on_closed_bar(BTC, b)
        again = await ex.on_closed_bar(BTC, b)
        older = await ex.on_closed_bar(BTC, flat_bar(0, "500"))

        assert len(first) == 1
        assert again == [] and older == []
        assert [f.fill_seq for f in ex.get_fills()] == [1, 2]
        assert await ex.get_last_price(BTC) == D("112")

    async def test_trailing_stop_sell_follows_peak_then_triggers(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        await ex.create_order(BTC, "SELL", "TRAILING_STOP", D("1"), params={"trail_pct": "0.05"})

        assert await ex.on_closed_bar(BTC, bar(1, "100", "120", "99", "118")) == []
        (order,) = await ex.fetch_open_orders()
        assert order["trail_pct"] == 0.05

        (fill,) = await ex.on_closed_bar(BTC, bar(2, "118", "119", "110", "111"))
        assert fill.reason == "trailing_stop"
        assert fill.price == D("114.00")

    async def test_trailing_stop_requires_trail_pct(self) -> None:
        ex = await holding("1", "100")
        with pytest.raises(ValueError, match="trail_pct"):
            await ex.create_order(BTC, "SELL", "TRAILING_STOP", D("1"))

    async def test_trailing_buy_entry_locks_quote_and_fills_on_reversal(self) -> None:
        ex = executor(capital="1000", fee="0", slippage="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        await ex.create_order(BTC, "BUY", "TRAILING_STOP", D("1"), params={"trail_pct": "0.05"})
        assert ex.get_account_snapshot().quote_locked == D("105.00")

        assert await ex.on_closed_bar(BTC, bar(1, "100", "101", "80", "83")) == []
        (fill,) = await ex.on_closed_bar(BTC, bar(2, "82", "90", "81", "89"))
        assert fill.price == D("84.00")
        assert await ex.fetch_balance() == {"USDT": D("916.00"), "BTC": D("1")}


class TestFlatten:
    async def test_flatten_cancels_orders_and_sells_all(self) -> None:
        ex = await holding("1", "100", fee="0.001", slippage="0.001")
        await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("110"), stop_price=D("90"))

        fills = await ex.flatten()

        assert len(fills) == 1
        (fill,) = fills
        assert isinstance(fill, PaperFill)
        assert fill.reason == "flatten"
        assert fill.price == D("99.900")
        assert fill.fee == D("0.0999000")
        assert fill.liquidity == "taker"
        snap = ex.get_account_snapshot()
        assert snap.base_totals == {"BTC": D("0")}
        assert snap.open_orders == 0
        assert snap.quote_locked == D("0")

    async def test_flatten_one_symbol_leaves_others(self) -> None:
        ex = executor()
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        await ex.on_closed_bar(ETH, flat_bar(0, "10"))
        await ex.create_order(BTC, "BUY", "MARKET", D("1"))
        await ex.create_order(ETH, "BUY", "MARKET", D("1"))

        await ex.flatten(BTC)

        assert [p.symbol for p in await ex.get_positions()] == [ETH]

    async def test_flatten_without_mark_raises(self) -> None:
        ex = await holding("1", "100")
        ex._marks.clear()
        with pytest.raises(ValueError, match="no market price"):
            await ex.flatten()

    async def test_flatten_when_flat_is_a_noop(self) -> None:
        assert await executor().flatten() == []


class TestQueries:
    async def test_position_filter_by_symbol(self) -> None:
        ex = executor(fee="0", slippage="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "42000"))
        await ex.on_closed_bar(ETH, flat_bar(0, "3000"))
        await ex.create_order(BTC, "BUY", "MARKET", D("0.1"))
        await ex.create_order(ETH, "BUY", "MARKET", D("1"))

        (btc,) = await ex.fetch_positions(symbol=BTC)
        assert btc == {
            "symbol": BTC,
            "side": "long",
            "contracts": 0.1,
            "entryPrice": 42000.0,
            "unrealizedPnl": 0.0,
            "realizedPnl": 0.0,
        }
        assert await ex.fetch_balance() == {"USDT": D("92800.0"), "BTC": D("0.1"), "ETH": D("1")}

    async def test_filled_orders_history_reports_quantity_and_reason(self) -> None:
        ex = await holding("0.3", "100")
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("0.3"), stop_price=D("90"))
        await ex.on_closed_bar(BTC, bar(1, "95", "96", "85", "88"))
        history = await ex.get_filled_orders()
        assert [(h["side"], h["quantity"], h["amount"], h["reason"]) for h in history] == [
            ("BUY", 0.3, 0.3, "market"),
            ("SELL", 0.3, 0.3, "stop_loss"),
        ]

    async def test_snapshot_tracks_peak_and_drawdown(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0", capital="1000")
        await ex.on_closed_bar(BTC, flat_bar(1, "200"))
        await ex.on_closed_bar(BTC, flat_bar(2, "150"))
        snap = ex.get_account_snapshot()
        assert snap.peak_equity == D("1100")
        assert snap.equity == D("1050")
        assert snap.drawdown == D("50") / D("1100")

    async def test_insufficient_balance_is_a_value_error(self) -> None:
        ex = executor(capital="100")
        await ex.on_closed_bar(BTC, flat_bar(0, "50000"))
        with pytest.raises(ValueError, match="Insufficient"):
            await ex.create_order(BTC, "BUY", "MARKET", D("1"))
        with pytest.raises(InsufficientBalanceError):
            await ex.create_order(BTC, "BUY", "MARKET", D("1"))


class TestLiveMark:
    async def test_market_buy_fills_at_the_live_mark(self) -> None:
        ex = executor(capital="1000")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        ex.set_mark(BTC, D("103"))

        result = await ex.create_order(BTC, "BUY", "MARKET", D("0.5"))

        assert (result["price"], result["mark_source"]) == (103.103, "live")
        (fill,) = ex.get_fills()
        assert (fill.price, fill.fee, fill.mark_source) == (D("103.103"), D("0.0515515"), "live")
        assert await ex.get_balance() == {"USDT": D("948.3969485")}

    async def test_without_a_live_mark_market_fills_at_last_close_and_is_flagged(self) -> None:
        ex = executor(capital="1000")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))

        result = await ex.create_order(BTC, "BUY", "MARKET", D("0.5"))

        assert (result["price"], result["mark_source"]) == (100.1, "last_close")
        assert ex.get_fills()[0].mark_source == "last_close"

    async def test_next_closed_bar_replaces_the_live_mark(self) -> None:
        ex = executor(capital="1000", fee="0", slippage="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        ex.set_mark(BTC, D("103"))
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        assert (await ex.create_order(BTC, "BUY", "MARKET", D("1")))["price"] == 103.0

        await ex.on_closed_bar(BTC, flat_bar(1, "101"))
        result = await ex.create_order(BTC, "BUY", "MARKET", D("1"))
        assert (result["price"], result["mark_source"]) == (101.0, "last_close")

    async def test_live_mark_moves_neither_equity_nor_resting_orders(self) -> None:
        ex = executor(capital="1000", fee="0", slippage="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        await ex.create_order(BTC, "BUY", "MARKET", D("2"))
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("95"))
        await ex.create_order(BTC, "BUY", "LIMIT", D("1"), price=D("90"))

        ex.set_mark(BTC, D("85"))

        assert await ex.get_equity() == D("1000")
        assert len(await ex.fetch_open_orders()) == 2
        assert [f.side for f in ex.get_fills()] == ["BUY"]
        fills = await ex.on_closed_bar(BTC, bar(1, "88", "88", "85", "86"))
        assert sorted((f.side, f.price) for f in fills) == [("BUY", D("88")), ("SELL", D("88"))]

    async def test_trailing_stop_starts_from_the_live_mark(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        ex.set_mark(BTC, D("110"))
        await ex.create_order(BTC, "SELL", "TRAILING_STOP", D("1"), params={"trail_pct": "0.02"})

        assert await ex.on_closed_bar(BTC, bar(1, "109", "109", "107.5", "108")) == []
        (fill,) = await ex.on_closed_bar(BTC, bar(2, "108", "108", "107", "107.5"))
        assert (fill.reason, fill.price) == ("trailing_stop", D("107.80"))

    async def test_order_placed_at_a_live_mark_sees_only_mark_to_close_on_its_bar(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        ex.set_mark(BTC, D("106"))
        await ex.create_order(BTC, "SELL", "STOP_MARKET", D("1"), stop_price=D("101"))

        assert await ex.on_closed_bar(BTC, bar(1, "100", "106", "99", "104")) == []
        (fill,) = await ex.on_closed_bar(BTC, bar(2, "104", "104", "100", "100"))
        assert (fill.reason, fill.price) == ("stop_loss", D("101"))

    async def test_oco_placed_at_a_live_mark_skips_the_pre_placement_range(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        ex.set_mark(BTC, D("106"))
        await ex.create_order(BTC, "SELL", "OCO", D("1"), price=D("112"), stop_price=D("101"))

        (fill,) = await ex.on_closed_bar(BTC, bar(1, "100", "113", "99", "112.5"))
        assert (fill.reason, fill.price) == ("take_profit", D("112"))

    async def test_feed_stale_labels_the_held_mark_until_a_fresh_one(self) -> None:
        ex = await holding("2", "100", fee="0", slippage="0")
        ex.set_mark(BTC, D("97"))
        ex.mark_feed_stale()

        stale = await ex.create_order(BTC, "SELL", "MARKET", D("1"))
        ex.set_mark(BTC, D("98"))
        fresh = await ex.create_order(BTC, "SELL", "MARKET", D("1"))

        assert (stale["price"], stale["mark_source"]) == (97.0, "stale_live")
        assert (fresh["price"], fresh["mark_source"]) == (98.0, "live")

    async def test_flatten_sells_at_the_live_mark(self) -> None:
        ex = await holding("1", "100", fee="0", slippage="0")
        ex.set_mark(BTC, D("97"))
        (fill,) = await ex.flatten(BTC)
        assert (fill.price, fill.mark_source, fill.net_pnl) == (D("97"), "live", D("-3"))

    async def test_non_positive_mark_is_ignored_and_foreign_quote_rejected(self) -> None:
        ex = executor(capital="1000", fee="0", slippage="0")
        await ex.on_closed_bar(BTC, flat_bar(0, "100"))
        ex.set_mark(BTC, D("0"))
        assert (await ex.create_order(BTC, "BUY", "MARKET", D("1")))["price"] == 100.0
        with pytest.raises(OrderRejectedError, match="USDT-quoted"):
            ex.set_mark("BTCEUR", D("90"))
