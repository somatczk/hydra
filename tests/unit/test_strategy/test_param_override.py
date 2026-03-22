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

    def test_no_param_key_unchanged(self) -> None:
        """Conditions without param_key should not be affected by top-level params."""
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
