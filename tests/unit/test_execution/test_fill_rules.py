"""Intrabar fill rules: the path heuristic, gaps, limits, stops and the OCO leg choice."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.types import OHLCV
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


class TestParticipationCap:
    def test_the_slice_is_the_capped_share_of_the_bar(self) -> None:
        assert fill_rules.participation_fillable(D("15"), D("100"), D("0.1")) == D("10")
        assert fill_rules.participation_fillable(D("5"), D("100"), D("0.1")) == D("5")
        assert fill_rules.participation_fillable(D("15"), D("0"), D("0.1")) == D("0")

    def test_a_full_cap_and_a_zero_volume_bar(self) -> None:
        assert fill_rules.participation_fillable(D("15"), D("10"), D("1")) == D("10")
        assert fill_rules.participation_fillable(D("15"), D("150"), D("0.1")) == D("15")

    def test_the_cap_bounds_are_validated(self) -> None:
        with pytest.raises(ValueError, match="max_participation"):
            fill_rules.participation_fillable(D("15"), D("100"), D("1.5"))
        with pytest.raises(ValueError, match="max_participation"):
            fill_rules.participation_fillable(D("15"), D("100"), D("-0.1"))


class TestStopLimit:
    def test_triggered_and_marketable_fills_at_trigger_not_limit(self) -> None:
        out = fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("100", "101", "93", "96"))
        assert out is not None
        assert out.fill == FillPrice(D("95"), "taker")

    def test_marketable_stop_limit_never_fills_outside_the_bar(self) -> None:
        sell = fill_rules.stop_limit_fill(
            "SELL", D("95"), D("94"), bar("100", "100.5", "94.8", "96")
        )
        assert sell is not None
        assert sell.fill == FillPrice(D("95"), "taker")
        buy = fill_rules.stop_limit_fill(
            "BUY", D("105"), D("106"), bar("100", "105.5", "99", "105")
        )
        assert buy is not None
        assert buy.fill == FillPrice(D("105"), "taker")

    def test_gap_inside_the_limit_fills_at_the_open(self) -> None:
        out = fill_rules.stop_limit_fill("SELL", D("95"), D("94"), bar("94.5", "95", "93", "94"))
        assert out is not None
        assert out.fill == FillPrice(D("94.5"), "taker")

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


_OCO_BARS = [
    ("gap_below_stop_bearish", bar("85", "112", "80", "84"), "SELL", "110", "90", True),
    ("gap_above_tp_bullish", bar("120", "130", "80", "125"), "SELL", "110", "90", False),
    ("buy_gap_above_stop_bullish", bar("115", "120", "80", "118"), "BUY", "90", "110", True),
    ("buy_gap_below_tp_bearish", bar("85", "120", "80", "84"), "BUY", "90", "110", False),
    ("bullish_both", bar("100", "120", "80", "110"), "SELL", "110", "90", True),
    ("bearish_both", bar("100", "120", "80", "95"), "SELL", "110", "90", False),
    ("flat_both", bar("100", "120", "80", "100"), "SELL", "110", "90", False),
    ("only_tp", bar("100", "115", "95", "112"), "SELL", "110", "90", False),
    ("only_stop", bar("100", "105", "85", "88"), "SELL", "110", "90", True),
    ("buy_bullish_both", bar("100", "120", "80", "110"), "BUY", "90", "110", False),
    ("buy_bearish_both", bar("100", "120", "80", "95"), "BUY", "90", "110", True),
]


class TestOcoLegChoice:
    @pytest.mark.parametrize(("name", "b", "side", "tp", "sl", "stop_wins"), _OCO_BARS)
    def test_leg_follows_the_intrabar_path(
        self, name: str, b: OHLCV, side: str, tp: str, sl: str, stop_wins: bool
    ) -> None:
        assert fill_rules.oco_stop_first(side, D(sl), D(tp), b) is stop_wins, name
        outcome = fill_rules.oco_fill(side, D(sl), D(tp), b, D("0"))
        assert outcome is not None
        assert outcome[0] is stop_wins

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


def test_every_fill_rule_fills_inside_the_bar() -> None:
    gen = np.random.default_rng(7)

    def num(lo: int, hi: int) -> Decimal:
        return D(int(gen.integers(lo, hi + 1)))

    zero = D("0")
    checked = 0
    for _ in range(3000):
        o, c = num(80, 120), num(80, 120)
        b = bar(str(o), str(max(o, c) + num(0, 10)), str(min(o, c) - num(0, 10)), str(c))
        side = "BUY" if num(0, 1) else "SELL"
        level, other = num(70, 130), num(70, 130)
        prices: list[Decimal] = []
        if (lf := fill_rules.limit_fill(side, level, b)) is not None:
            prices.append(lf.price)
        if (sm := fill_rules.stop_market_fill(side, level, b, zero)) is not None:
            prices.append(sm.price)
        sl = fill_rules.stop_limit_fill(side, level, other, b)
        if sl is not None and sl.fill is not None:
            prices.append(sl.fill.price)
        stop, tp = (min(level, other), max(level, other))
        if side == "BUY":
            stop, tp = tp, stop
        if stop != tp and (oco := fill_rules.oco_fill(side, stop, tp, b, zero)) is not None:
            prices.append(oco[1].price)
        trail = fill_rules.trailing_fill(side, num(80, 120), D("0.05"), b, zero)
        if trail.fill is not None:
            prices.append(trail.fill.price)
        for price in prices:
            assert b.low <= price <= b.high, (side, level, other, b)
        checked += len(prices)
    assert checked > 3000
