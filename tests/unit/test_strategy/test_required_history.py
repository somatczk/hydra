"""The bar history a rule set needs: ``rules_required_history`` and its strategy seam.

A rule strategy is gated on ``required_history`` bars and every walk-forward / hyperopt
out-of-sample window is warmed with exactly that many, so a short value trades its first
bars on NaN (or a still-seeded) indicator. These tests pin the derivation on real rules
and prove, on real indicator code, that the derived count is enough.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.indicators import library as ind_lib
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.condition_schema import RuleSet
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import PARAM_LIMITS, StrategyContext
from hydra.strategy.indicator_registry import get_all_indicators
from hydra.strategy.rule_engine import indicator_lookback, rules_required_history


def _cond(indicator: str, params: dict[str, Any], comparator: str, value: Any) -> dict[str, Any]:
    return {"indicator": indicator, "params": params, "comparator": comparator, "value": value}


def _rules(*entry: dict[str, Any], exit_short: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    rules: dict[str, Any] = {"entry_long": {"operator": "AND", "conditions": list(entry)}}
    if exit_short:
        rules["exit_short"] = {"operator": "OR", "conditions": list(exit_short)}
    return rules


EMA_TREND = _rules(_cond("ema", {"period": 50}, "crosses_above", "ema:period=200"))
DONCHIAN_150 = _rules(_cond("close", {}, "crosses_above", "rolling_high:period=150"))
RSI_DIP = _rules(_cond("rsi", {"period": 14}, "less_than", 30))


class TestRulesRequiredHistory:
    def test_an_ema_200_trend_needs_three_slow_windows(self) -> None:
        assert rules_required_history(RuleSet(**EMA_TREND)) == 3 * 200 + 2

    def test_a_donchian_150_breakout_needs_its_window_and_the_previous_bar(self) -> None:
        assert rules_required_history(RuleSet(**DONCHIAN_150)) == 150 + 2

    def test_a_wilder_rsi_needs_six_windows(self) -> None:
        assert rules_required_history(RuleSet(**RSI_DIP)) == 6 * 14 + 2

    def test_short_rules_and_no_rules_keep_the_floor_of_50(self) -> None:
        bollinger = _rules(_cond("close", {}, "less_than", "bollinger_lower:period=20,std_dev=2"))
        assert rules_required_history(RuleSet(**bollinger)) == 50
        assert rules_required_history(RuleSet()) == 50

    def test_declared_defaults_and_every_group_count(self) -> None:
        macd = _rules(_cond("macd", {}, "crosses_above", "macd_signal"))
        assert rules_required_history(RuleSet(**macd)) == (12 + 26 + 9) * 3 + 2
        adx = _rules(_cond("adx", {}, "greater_than", 25))
        assert rules_required_history(RuleSet(**adx)) == 14 * 8 + 2
        short_exit = _rules(
            *RSI_DIP["entry_long"]["conditions"],
            exit_short=(_cond("close", {}, "crosses_above", "sma:period=200"),),
        )
        assert rules_required_history(RuleSet(**short_exit)) == 202

    def test_an_unparsable_value_reference_raises(self) -> None:
        bad = _rules(_cond("close", {}, "greater_than", "sma:period=abc"))
        with pytest.raises(ValueError, match="abc"):
            rules_required_history(RuleSet(**bad))


def _walk(count: int, seed: int = 3) -> list[OHLCV]:
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, count)))
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        OHLCV(
            open=Decimal(str(round(c, 4))),
            high=Decimal(str(round(c * 1.006, 4))),
            low=Decimal(str(round(c * 0.994, 4))),
            close=Decimal(str(round(c, 4))),
            volume=Decimal(str(round(1000 + 500 * abs(rng.normal()), 2))),
            timestamp=t0 + timedelta(hours=i),
        )
        for i, c in enumerate(closes)
    ]


def _windows(name: str, *, widest: bool) -> dict[str, int]:
    """The indicator's integer windows: its defaults, or each at its rule maximum."""
    params: dict[str, int] = {}
    for key, param in inspect.signature(getattr(ind_lib, name)).parameters.items():
        if param.annotation not in (int, "int"):
            continue
        if widest:
            params[key] = int(PARAM_LIMITS[key][1])
        elif param.default is inspect.Parameter.empty:
            params[key] = 20
    return params


@pytest.mark.parametrize("widest", [False, True], ids=["defaults", "widest"])
@pytest.mark.parametrize("name", sorted(i.name for i in get_all_indicators()))
def test_the_lookback_gives_every_rule_indicator_a_current_and_previous_value(
    name: str, widest: bool
) -> None:
    params = _windows(name, widest=widest)
    bars = _walk(indicator_lookback(name, params))
    ctx = StrategyContext()
    for bar in bars:
        ctx.add_bar("BTCUSDT", Timeframe.H1, bar)
    values = ctx.indicator(name, "BTCUSDT", Timeframe.H1, **params)
    assert len(values) == len(bars)
    assert not np.isnan(values[-1])
    assert not np.isnan(values[-2])


@pytest.mark.parametrize(
    ("name", "period"),
    [
        ("ema", 200),
        ("rsi", 14),
        ("rsi", 100),
        ("atr", 14),
        ("plus_di", 14),
        ("adx", 14),
        ("adx", 100),
    ],
)
def test_a_smoother_warmed_on_its_lookback_matches_unlimited_history(
    name: str, period: int
) -> None:
    """The multiple is about convergence, not just a first value: within 2 % of the value
    the indicator reaches on 8000 bars (a Wilder RSI on 3 windows is up to 7 % off, ADX
    on 3 windows up to 50 %)."""
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 8000)))
    cols = {
        "data": close,
        "close": close,
        "high": close * (1 + np.abs(rng.normal(0, 0.005, 8000))),
        "low": close * (1 - np.abs(rng.normal(0, 0.005, 8000))),
    }
    func = getattr(ind_lib, name)
    wanted = [p for p in inspect.signature(func).parameters if p in cols]
    lookback = indicator_lookback(name, {"period": period})

    full = func(*(cols[p] for p in wanted), period)[-1]
    warmed = func(*(cols[p][-lookback:] for p in wanted), period)[-1]
    assert warmed == pytest.approx(full, rel=0.02)


def _strategy(rules: dict[str, Any], **parameters: Any) -> RuleBasedStrategy:
    config = StrategyConfig(
        id="rh",
        name="RH",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=["BTCUSDT"],
        parameters={"rules": rules, **parameters},
    )
    return RuleBasedStrategy(config=config, context=StrategyContext())


SMA_CROSS = _rules(
    {
        **_cond("close", {}, "crosses_above", "sma:period=50"),
        "value_ref_overrides": {"sma_period": "period"},
    }
)


class TestRuleBasedRequiredHistory:
    def test_the_declared_value_is_an_explicit_override_either_way(self) -> None:
        assert _strategy(EMA_TREND, required_history=10).required_history == 10
        assert _strategy(RSI_DIP, required_history=900).required_history == 900

    def test_a_parameter_that_lengthens_a_window_adds_exactly_its_extra_bars(self) -> None:
        """What a hyperopt trial or walk-forward fold's best params do to the warm-up."""
        assert _strategy(SMA_CROSS, required_history=60).required_history == 60
        longer = _strategy(SMA_CROSS, required_history=60, sma_period=150)
        assert longer.required_history == 60 + (152 - 52)
        assert _strategy(SMA_CROSS, required_history=60, sma_period=20).required_history == 60

    def test_an_auto_matched_parameter_counts_too(self) -> None:
        ema = _rules(_cond("ema", {"period": 20}, "crosses_above", "close"))
        assert _strategy(ema, required_history=62).required_history == 62
        assert _strategy(ema, required_history=62, ema_period=200).required_history == 62 + (
            602 - 62
        )
