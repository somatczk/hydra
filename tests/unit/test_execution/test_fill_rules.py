"""Intrabar fill rules, including parity with the backtester's OCO leg choice."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.backtest.fills import CommissionConfig, FillSimulator, SlippageModel
from hydra.core.types import OHLCV, MarketType, OrderRequest, OrderType, Side, Symbol
from hydra.execution import fill_rules
from hydra.execution.fill_rules import FillPrice

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def bar(o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(open=D(o), high=D(h), low=D(low), close=D(c), volume=D("10"), timestamp=T0)


class TestPath:
    def test_bullish_bar_goes_low_before_high(self) -> None:
        assert fill_rules.intrabar_path(bar("100", "110", "90", "105")) == (
            D("100"),
            D("90"),
            D("110"),
            D("105"),
        )

    @pytest.mark.parametrize("close", ["95", "100"])
    def test_bearish_or_flat_bar_goes_high_before_low(self, close: str) -> None:
        assert fill_rules.intrabar_path(bar("100", "110", "90", close))[1:3] == (D("110"), D("90"))


class TestGapAndLimitRules:
    def test_gap_through_sell_stop_fills_at_open(self) -> None:
        fp = fill_rules.stop_market_fill("SELL", D("90"), bar("70", "72", "65", "70"), D("0"))
        assert fp == FillPrice(D("70"), "taker")

    def test_gap_up_buy_stop_fills_at_open(self) -> None:
        fp = fill_rules.stop_market_fill("BUY", D("110"), bar("120", "125", "118", "121"), D("0"))
        assert fp == FillPrice(D("120"), "taker")

    def test_sell_stop_inside_bar_fills_at_stop_minus_slippage(self) -> None:
        fp = fill_rules.stop_market_fill(
            "SELL", D("57000"), bar("58000", "58500", "56000", "56500"), D("0.001")
        )
        assert fp == FillPrice(D("56943.000"), "taker")

    def test_untouched_stop_does_not_fill(self) -> None:
        assert (
            fill_rules.stop_market_fill("SELL", D("90"), bar("100", "105", "91", "95"), D("0"))
            is None
        )

    def test_limit_gap_improves_to_open(self) -> None:
        fp = fill_rules.limit_fill("BUY", D("100"), bar("95", "99", "94", "96"))
        assert fp == FillPrice(D("95"), "maker")

    def test_limit_inside_bar_fills_at_limit(self) -> None:
        assert fill_rules.limit_fill("SELL", D("110"), bar("100", "112", "99", "105")) == FillPrice(
            D("110"), "maker"
        )

    def test_market_fill_slips_against_trader(self) -> None:
        assert fill_rules.market_fill("BUY", D("100"), D("0.01")).price == D("101.00")
        assert fill_rules.market_fill("SELL", D("100"), D("0.01")).price == D("99.00")


class TestStopLimit:
    def test_triggered_and_marketable_fills_at_limit(self) -> None:
        out = fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("100", "101", "93", "96"))
        assert out is not None
        assert out.fill == FillPrice(D("94"), "taker")

    def test_stop_limit_not_reached_converts_to_resting_limit(self) -> None:
        out = fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("90", "92", "88", "91"))
        assert out is not None
        assert out.fill is None
        assert out.converted_to_limit is True

    def test_gap_then_recovery_to_limit_fills_as_maker(self) -> None:
        out = fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("90", "96", "89", "91"))
        assert out is not None
        assert out.fill == FillPrice(D("94"), "maker")

    def test_untriggered_returns_none(self) -> None:
        assert (
            fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("100", "101", "96", "97"))
            is None
        )


class TestTrailing:
    def test_trailing_stop_no_lookahead_bullish_bar_triggers_on_old_peak(self) -> None:
        out = fill_rules.trailing_fill(
            "SELL", D("100"), D("0.05"), bar("100", "110", "94", "108"), D("0")
        )
        assert out.fill == FillPrice(D("95.00"), "taker")
        assert out.extreme == D("100")

    def test_trailing_stop_bearish_bar_raises_peak_then_triggers(self) -> None:
        out = fill_rules.trailing_fill(
            "SELL", D("100"), D("0.05"), bar("100", "110", "103", "104"), D("0")
        )
        assert out.fill == FillPrice(D("104.50"), "taker")
        assert out.extreme == D("110")

    def test_trailing_stop_only_moves_peak_when_not_hit(self) -> None:
        out = fill_rules.trailing_fill(
            "SELL", D("100"), D("0.05"), bar("100", "120", "99", "118"), D("0")
        )
        assert out.fill is None
        assert out.extreme == D("120")

    def test_trailing_gap_fills_at_open(self) -> None:
        out = fill_rules.trailing_fill(
            "SELL", D("100"), D("0.05"), bar("90", "91", "85", "88"), D("0")
        )
        assert out.fill == FillPrice(D("90"), "taker")

    def test_trailing_buy_tracks_trough(self) -> None:
        out = fill_rules.trailing_fill(
            "BUY", D("100"), D("0.05"), bar("100", "101", "80", "83"), D("0")
        )
        assert out.fill is None
        assert out.extreme == D("80")
        hit = fill_rules.trailing_fill(
            "BUY", D("80"), D("0.05"), bar("82", "90", "81", "89"), D("0")
        )
        assert hit.fill == FillPrice(D("84.00"), "taker")


def _oco_request(side: Side, tp: str, sl: str) -> OrderRequest:
    return OrderRequest(
        symbol=Symbol("BTCUSDT"),
        side=side,
        order_type=OrderType.OCO,
        quantity=D("1"),
        strategy_id="parity",
        exchange_id="binance",
        market_type=MarketType.SPOT,
        price=D(tp),
        stop_price=D(sl),
    )


_PARITY_BARS = [
    ("bullish_both", bar("100", "120", "80", "110"), Side.SELL, "110", "90"),
    ("bearish_both", bar("100", "120", "80", "95"), Side.SELL, "110", "90"),
    ("flat_both", bar("100", "120", "80", "100"), Side.SELL, "110", "90"),
    ("only_tp", bar("100", "115", "95", "112"), Side.SELL, "110", "90"),
    ("only_stop", bar("100", "105", "85", "88"), Side.SELL, "110", "90"),
    ("buy_bullish_both", bar("100", "120", "80", "110"), Side.BUY, "90", "110"),
    ("buy_bearish_both", bar("100", "120", "80", "95"), Side.BUY, "90", "110"),
]


class TestOcoParityWithBacktest:
    @pytest.mark.parametrize(("name", "b", "side", "tp", "sl"), _PARITY_BARS)
    def test_same_leg_as_fill_simulator(
        self, name: str, b: OHLCV, side: Side, tp: str, sl: str
    ) -> None:
        sim = FillSimulator(SlippageModel(spread_factor=D("0"), volume_impact_factor=D("0")))
        bt = sim.simulate_fill(_oco_request(side, tp, sl), b, None, CommissionConfig())
        assert bt is not None
        bt_stop_won = bt.price != D(tp)

        stop_first = fill_rules.oco_stop_first(side.value, D(sl), D(tp), b)
        assert stop_first is bt_stop_won, name
        outcome = fill_rules.oco_fill(side.value, D(sl), D(tp), b, D("0"))
        assert outcome is not None
        assert outcome[0] is bt_stop_won
        assert outcome[1].price == bt.price

    def test_oco_both_legs_hit_bullish_bar_stop_wins(self) -> None:
        assert fill_rules.oco_stop_first("SELL", D("90"), D("110"), bar("100", "120", "80", "110"))

    def test_oco_both_legs_hit_bearish_bar_tp_wins(self) -> None:
        assert not fill_rules.oco_stop_first(
            "SELL", D("90"), D("110"), bar("100", "120", "80", "95")
        )

    def test_open_below_stop_is_a_gap_and_stop_wins_even_on_bearish_bar(self) -> None:
        b = bar("85", "120", "80", "84")
        assert fill_rules.oco_stop_first("SELL", D("90"), D("110"), b) is True
        outcome = fill_rules.oco_fill("SELL", D("90"), D("110"), b, D("0"))
        assert outcome == (True, FillPrice(D("85"), "taker"))

    def test_neither_leg_touched(self) -> None:
        assert (
            fill_rules.oco_fill("SELL", D("90"), D("110"), bar("100", "105", "95", "101"), D("0"))
            is None
        )
