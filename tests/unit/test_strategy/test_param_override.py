"""RuleBasedStrategy parameter bindings: explicit keys and overrides, implicit name-match."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml

from hydra.core.events import BarEvent, EntrySignal
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.services.research import _extract_optimizable_params
from hydra.strategy.builtin.rule_based import RuleBasedStrategy, bindable_params
from hydra.strategy.condition_schema import RuleSet
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext


def _make_strategy(parameters: dict) -> RuleBasedStrategy:
    """Create a RuleBasedStrategy with the given parameters dict."""
    config = StrategyConfig(
        id="test_override",
        name="Override Test",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        parameters=parameters,
    )
    context = MagicMock()
    return RuleBasedStrategy(config=config, context=context)


class TestParamKeyOverride:
    def test_param_key_overrides_indicator_param(self) -> None:
        """Condition with param_key should have its primary param overridden."""
        params = {
            "rsi_period": 22,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                            "param_key": "rsi_period",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)

        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 22

    def test_value_param_key_overrides_threshold(self) -> None:
        """Condition with value_param_key should have its value overridden."""
        params = {
            "rsi_threshold": 40,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                            "value_param_key": "rsi_threshold",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)

        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == 40.0

    def test_no_param_key_no_matching_top_param(self) -> None:
        """Conditions without param_key and no matching top-level param are unchanged."""
        params = {
            "unrelated_param": 22,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)

        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 14
        assert cond.value == 30.0

    def test_both_param_key_and_value_param_key(self) -> None:
        """Both param_key and value_param_key can be used on the same condition."""
        params = {
            "sma_fast": 48,
            "sma_threshold": 0.02,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "sma",
                            "params": {"period": 20},
                            "comparator": "greater_than",
                            "value": 0.01,
                            "param_key": "sma_fast",
                            "value_param_key": "sma_threshold",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)

        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 48
        assert cond.value == 0.02

    def test_missing_top_level_param_ignored(self) -> None:
        """If param_key references a non-existent top-level param, condition is unchanged."""
        params = {
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                            "param_key": "nonexistent_param",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)

        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 14


class TestValueRefOverrides:
    def test_single_param_override(self) -> None:
        """Override period in value string reference."""
        params = {
            "sma_period": 100,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "close",
                            "params": {},
                            "comparator": "greater_than",
                            "value": "sma:period=50",
                            "value_ref_overrides": {"sma_period": "period"},
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == "sma:period=100"

    def test_multi_param_override(self) -> None:
        """Override both period and std_dev in Bollinger value ref."""
        params = {
            "bb_period": 45,
            "bb_std_dev": 1.5,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "close",
                            "params": {},
                            "comparator": "crosses_below",
                            "value": "bollinger_lower:period=60,std_dev=2.0",
                            "value_ref_overrides": {
                                "bb_period": "period",
                                "bb_std_dev": "std_dev",
                            },
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == "bollinger_lower:period=45,std_dev=1.5"

    def test_missing_top_param_unchanged(self) -> None:
        """Override key not in top_params → value string unchanged."""
        params = {
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "close",
                            "params": {},
                            "comparator": "greater_than",
                            "value": "sma:period=50",
                            "value_ref_overrides": {"sma_period": "period"},
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == "sma:period=50"

    def test_integer_formatting(self) -> None:
        """Int values should format as ints, not floats."""
        params = {
            "sma_period": 100,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "close",
                            "params": {},
                            "comparator": "greater_than",
                            "value": "sma:period=50",
                            "value_ref_overrides": {"sma_period": "period"},
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert "period=100" in cond.value
        assert "100.0" not in cond.value

    def test_combined_with_param_key(self) -> None:
        """param_key + value_ref_overrides on the same condition."""
        params = {
            "sma_fast": 30,
            "sma_slow": 100,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "sma",
                            "params": {"period": 20},
                            "comparator": "crosses_above",
                            "value": "sma:period=50",
                            "param_key": "sma_fast",
                            "value_ref_overrides": {"sma_slow": "period"},
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 30
        assert cond.value == "sma:period=100"

    def test_non_string_value_ignored(self) -> None:
        """value_ref_overrides set but value is a float → no crash."""
        params = {
            "sma_period": 100,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                            "value_ref_overrides": {"sma_period": "period"},
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == 30.0


class TestAutoMatching:
    def test_auto_match_indicator_param(self) -> None:
        """No param_key but top_params has rsi_period → auto-override."""
        params = {
            "rsi_period": 22,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.params["period"] == 22

    def test_auto_match_value_ref(self) -> None:
        """No value_ref_overrides but top_params has sma_period → auto-override."""
        params = {
            "sma_period": 30,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "close",
                            "params": {},
                            "comparator": "greater_than",
                            "value": "sma:period=50",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        assert cond.value == "sma:period=30"

    def test_explicit_annotation_takes_precedence(self) -> None:
        """param_key overrides auto-matching convention."""
        params = {
            "rsi_period": 99,
            "custom_rsi": 22,
            "rules": {
                "entry_long": {
                    "operator": "AND",
                    "conditions": [
                        {
                            "indicator": "rsi",
                            "params": {"period": 14},
                            "comparator": "less_than",
                            "value": 30.0,
                            "param_key": "custom_rsi",
                        }
                    ],
                },
            },
        }
        strategy = _make_strategy(params)
        cond = strategy._rules.entry_long.conditions[0]
        # Should use param_key (custom_rsi=22), not auto-match (rsi_period=99)
        assert cond.params["period"] == 22


def _cond(indicator: str, params: dict[str, Any], value: float | str, **binding: Any) -> dict:
    return {
        "indicator": indicator,
        "params": params,
        "comparator": "greater_than",
        "value": value,
        **binding,
    }


def _rules(entry: list[dict], exit_: list[dict] | None = None) -> dict[str, Any]:
    return {
        "entry_long": {"operator": "AND", "conditions": entry},
        "exit_long": {"operator": "AND", "conditions": exit_} if exit_ else None,
    }


_SUPERTREND = _cond(
    "supertrend_direction",
    {"period": 10},
    0,
    param_key="st_period",
    param_overrides={"st_mult": "multiplier"},
)


async def _supertrend_entries(parameters: dict[str, Any]) -> list[int]:
    """Bars on which a supertrend_direction > 0 rule emits a long entry, on a real context."""
    config = StrategyConfig(
        id="st",
        name="Supertrend",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        parameters={"required_history": 20, "rules": _rules([_SUPERTREND]), **parameters},
    )
    ctx = StrategyContext()
    strategy = RuleBasedStrategy(config=config, context=ctx)
    closes = 30000 * np.exp(np.cumsum(np.random.default_rng(7).normal(0, 0.01, 300)))
    entries: list[int] = []
    for t, c in enumerate(closes):
        bar = OHLCV(
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=Decimal(1),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=t),
        )
        ctx.add_bar("BTCUSDT", Timeframe.H1, bar)
        signals = await strategy.on_bar(
            BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=bar)
        )
        entries.extend(t for s in signals if isinstance(s, EntrySignal))
    return entries


class TestParamOverrides:
    """Bug 1: ``param_key`` reaches only the first param; ``param_overrides`` reaches any."""

    def test_binds_a_param_param_key_cannot_reach(self) -> None:
        strategy = _make_strategy({"st_period": 7, "st_mult": 1.5, "rules": _rules([_SUPERTREND])})
        assert strategy._rules.entry_long.conditions[0].params == {"period": 7, "multiplier": 1.5}

    async def test_a_tuned_multiplier_changes_the_signal_path(self) -> None:
        default = await _supertrend_entries({})
        assert len(default) == 19
        assert await _supertrend_entries({"st_mult": 3.0}) == default  # the signature default
        assert len(await _supertrend_entries({"st_mult": 0.5})) == 113
        assert await _supertrend_entries({"st_mult": 8.0}) == []
        # a declared top-level name nothing binds is a no-op (hyperopt refuses it by name)
        assert await _supertrend_entries({"multiplier": 0.5}) == default

    def test_an_unknown_param_name_is_refused_at_construction(self) -> None:
        bad = _cond("supertrend_direction", {"period": 10}, 0, param_overrides={"x": "mult"})
        with pytest.raises(ValueError, match="supertrend_direction has no parameter 'mult'"):
            _make_strategy({"rules": _rules([bad])})

    def test_strategy_params_lists_each_bound_param_with_its_range(self) -> None:
        params = {p["name"]: p for p in _extract_optimizable_params(_rules([_SUPERTREND]))}
        assert list(params) == ["st_period", "st_mult"]
        assert params["st_mult"] == {
            "name": "st_mult",
            "label": "ST Mult",
            "type": "float",
            "default": 3.0,
            "min": 0.1,
            "max": 10.0,
            "step": 0.49,
        }
        assert params["st_period"]["type"] == "int"
        assert params["st_period"]["default"] == 10.0


class TestImplicitBinding:
    """Bug 2: a ``{indicator}_{param}`` name-match never rewires conditions silently."""

    _TWO_SMAS = _rules([_cond("sma", {"period": 20}, 0)], [_cond("sma", {"period": 50}, 0)])

    def test_an_ambiguous_name_is_refused_not_applied_to_both(self) -> None:
        with pytest.raises(ValueError, match=r"'sma_period' matches conditions with different"):
            _make_strategy({"sma_period": 30, "rules": self._TWO_SMAS})

    def test_an_ambiguous_name_is_neither_listed_nor_optimisable(self) -> None:
        assert "sma_period" not in bindable_params(RuleSet.model_validate(self._TWO_SMAS))
        assert [p["name"] for p in _extract_optimizable_params(self._TWO_SMAS)] == []

    def test_a_reference_and_an_indicator_of_different_periods_are_ambiguous(self) -> None:
        crossover = _rules([_cond("sma", {"period": 20}, "sma:period=50")])
        with pytest.raises(
            ValueError, match=r"entry_long\[0\]\.params\.period=20, entry_long\[0\]\.ref\.period=50"
        ):
            _make_strategy({"sma_period": 30, "rules": crossover})

    def test_an_explicit_binding_disambiguates(self) -> None:
        rules = _rules(
            [_cond("sma", {"period": 20}, 0, param_overrides={"fast": "period"})],
            [_cond("sma", {"period": 50}, 0)],
        )
        strategy = _make_strategy({"fast": 10, "sma_period": 60, "rules": rules})
        assert strategy._rules.entry_long.conditions[0].params == {"period": 10}
        assert strategy._rules.exit_long.conditions[0].params == {"period": 60}

    def test_an_unambiguous_implicit_name_binds_every_site_and_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rules = _rules([_cond("rsi", {"period": 14}, 30)], [_cond("rsi", {"period": 14}, 70)])
        with caplog.at_level(logging.WARNING, logger="hydra.strategy.builtin.rule_based"):
            strategy = _make_strategy({"rsi_period": 21, "rules": rules})
        assert strategy._rules.entry_long.conditions[0].params == {"period": 21}
        assert strategy._rules.exit_long.conditions[0].params == {"period": 21}
        assert "binds rsi_period to its rules by name" in caplog.text

    def test_empty_overrides_opt_a_condition_out(self) -> None:
        rules = _rules(
            [_cond("sma", {"period": 20}, "sma:period=50", param_overrides={})],
            [_cond("close", {}, "sma:period=50", value_ref_overrides={})],
        )
        strategy = _make_strategy({"sma_period": 30, "rules": rules})
        assert strategy._rules.entry_long.conditions[0].params == {"period": 20}
        assert strategy._rules.entry_long.conditions[0].value == "sma:period=30"
        assert strategy._rules.exit_long.conditions[0].value == "sma:period=50"


_REPO = Path(__file__).resolve().parents[3]
# strategy_params of every shipped file, captured before the binding rework
_SHIPPED_PARAMS = {
    "strategies/breakout.yaml": ["bb_period", "bb_std_dev", "atr_period"],
    "strategies/dual_sma_trend_follower.yaml": ["sma_fast", "sma_slow"],
    "strategies/mean_reversion_bb.yaml": [
        "bb_period",
        "bb_std_dev",
        "rsi_period",
        "rsi_oversold",
        "rsi_overbought",
    ],
    "strategies/momentum_rsi_macd.yaml": [
        "rsi_period",
        "rsi_oversold",
        "macd_fast",
        "rsi_overbought",
    ],
    "strategies/research_bb_mean_reversion.yaml": ["bb_period", "bb_std_dev", "trend_sma"],
    "strategies/research_donchian_breakout.yaml": ["donchian_period"],
    "strategies/research_momentum_extremes.yaml": ["lookback"],
    "strategies/research_sma_trend.yaml": ["sma_period"],
    "strategies/rsi_macd_multi_confirmation.yaml": ["rsi_period", "macd_fast"],
    "strategies/rsi_momentum_crossover.yaml": ["rsi_period", "rsi_oversold", "rsi_overbought"],
    "strategies/trend_supertrend.yaml": ["ema_fast", "ema_slow", "rsi_period"],
    "templates/breakout_volume.yaml": [
        "bollinger_upper_period",
        "bollinger_upper_std_dev",
        "bollinger_middle_period",
        "bollinger_middle_std_dev",
    ],
    "templates/mean_reversion_bb.yaml": [
        "bollinger_lower_period",
        "bollinger_lower_std_dev",
        "rsi_period",
        "bollinger_middle_period",
        "bollinger_middle_std_dev",
    ],
    "templates/momentum_rsi.yaml": ["rsi_period"],
    "templates/multi_indicator.yaml": [
        "rsi_period",
        "macd_fast",
        "macd_slow",
        "macd_signal",
        "ema_period",
    ],
    "templates/trend_sma_crossover.yaml": ["slow_sma_period", "sma_period"],
}


@pytest.mark.parametrize("name", sorted(_SHIPPED_PARAMS))
def test_shipped_rule_trees_and_params_are_unchanged(name: str) -> None:
    """No shipped file sets a top-level knob, so its tree is exactly the YAML's rules."""
    data = yaml.safe_load((_REPO / "config" / name).read_text())
    rules = data["parameters"]["rules"]
    strategy = _make_strategy(data["parameters"])
    assert strategy._rules == RuleSet.model_validate(rules)
    assert [p["name"] for p in _extract_optimizable_params(rules)] == _SHIPPED_PARAMS[name]
