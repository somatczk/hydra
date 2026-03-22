"""Tests for RuleBasedStrategy param_key / value_param_key overrides."""

from __future__ import annotations

from unittest.mock import MagicMock

from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.condition_schema import Comparator, Condition, ConditionGroup
from hydra.strategy.config import StrategyConfig


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
