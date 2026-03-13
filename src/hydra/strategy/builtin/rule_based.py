"""Rule-based strategy (no-code builder output).

Evaluates condition trees loaded from YAML configuration.  Each rule set
defines entry_long, exit_long, entry_short, exit_short sections with
AND/OR operators and indicator conditions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.strategy.base import BaseStrategy
from hydra.strategy.condition_schema import ConditionGroup, RuleSet
from hydra.strategy.rule_engine import evaluate_condition_group


class RuleBasedStrategy(BaseStrategy):
    """Strategy driven by declarative condition trees from YAML config."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rules = self._parse_rules()

    def _parse_rules(self) -> RuleSet:
        """Parse the ``rules`` dict from strategy parameters into a RuleSet."""
        rules_dict = self._config.parameters.get("rules", {})
        if not rules_dict:
            return RuleSet()
        return RuleSet(
            entry_long=self._parse_group(rules_dict.get("entry_long")),
            exit_long=self._parse_group(rules_dict.get("exit_long")),
            entry_short=self._parse_group(rules_dict.get("entry_short")),
            exit_short=self._parse_group(rules_dict.get("exit_short")),
        )

    @staticmethod
    def _parse_group(data: dict[str, Any] | None) -> ConditionGroup | None:
        if data is None:
            return None
        return ConditionGroup(**data)

    @property
    def required_history(self) -> int:
        return self._config.parameters.get("required_history", 50)

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        signals: list[EntrySignal | ExitSignal] = []
        symbol = str(bar.symbol)
        tf = bar.timeframe

        bars = self._context.bars(symbol, tf, self.required_history)
        if len(bars) < self.required_history:
            return signals

        # Evaluate entry_long
        if evaluate_condition_group(self._rules.entry_long, self._context, symbol, tf):
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal("0.5"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        # Evaluate entry_short
        if evaluate_condition_group(self._rules.entry_short, self._context, symbol, tf):
            signals.append(
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.SHORT,
                    strength=Decimal("0.5"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            )

        # Evaluate exit_long
        position = self._context.position(symbol)
        if (
            position is not None
            and position.direction == Direction.LONG
            and evaluate_condition_group(self._rules.exit_long, self._context, symbol, tf)
        ):
            signals.append(
                ExitSignal(
                    symbol=Symbol(symbol),
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    reason="Rule-based exit (long)",
                )
            )

        # Evaluate exit_short
        if (
            position is not None
            and position.direction == Direction.SHORT
            and evaluate_condition_group(self._rules.exit_short, self._context, symbol, tf)
        ):
            signals.append(
                ExitSignal(
                    symbol=Symbol(symbol),
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    reason="Rule-based exit (short)",
                )
            )

        return signals
