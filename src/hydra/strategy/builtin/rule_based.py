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


def _apply_value_ref_overrides(
    value_str: str,
    overrides: dict[str, str],
    top_params: dict[str, Any],
) -> str:
    """Replace embedded params in a value string reference.

    Given ``"sma:period=50"`` and overrides ``{"sma_period": "period"}``,
    if ``top_params["sma_period"]`` is set, replaces ``period=50`` with the
    new value.
    """
    parts = value_str.split(":", maxsplit=1)
    if len(parts) < 2:
        return value_str

    ind_name = parts[0]
    embedded: dict[str, str] = {}
    for kv in parts[1].split(","):
        k, _, v = kv.partition("=")
        embedded[k.strip()] = v.strip()

    for top_key, embedded_key in overrides.items():
        if top_key in top_params and embedded_key in embedded:
            raw = top_params[top_key]
            if isinstance(raw, float) and raw == int(raw):
                embedded[embedded_key] = str(int(raw))
            else:
                embedded[embedded_key] = str(raw)

    param_str = ",".join(f"{k}={v}" for k, v in embedded.items())
    return f"{ind_name}:{param_str}"


class RuleBasedStrategy(BaseStrategy):
    """Strategy driven by declarative condition trees from YAML config."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rules = self._parse_rules()
        self._apply_param_overrides()

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

    def _apply_param_overrides(self) -> None:
        """Override condition params from top-level strategy parameters.

        When a condition has ``param_key`` set, the first key in its ``params``
        dict is overridden with the matching value from top-level parameters.
        Similarly ``value_param_key`` overrides the condition's ``value``.
        """
        top_params = self._config.parameters
        for group in [
            self._rules.entry_long,
            self._rules.exit_long,
            self._rules.entry_short,
            self._rules.exit_short,
        ]:
            if group is None:
                continue
            for cond in group.conditions:
                # Explicit param_key → override cond.params[first_key]
                if cond.param_key and cond.param_key in top_params and cond.params:
                    primary = next(iter(cond.params))
                    cond.params[primary] = top_params[cond.param_key]
                # Explicit value_param_key → override cond.value as float
                if cond.value_param_key and cond.value_param_key in top_params:
                    cond.value = float(top_params[cond.value_param_key])
                # Explicit value_ref_overrides → modify embedded params in value string
                if cond.value_ref_overrides and isinstance(cond.value, str):
                    cond.value = _apply_value_ref_overrides(
                        cond.value, cond.value_ref_overrides, top_params
                    )
                # Auto-match: {indicator}_{param_name} → override cond.params
                if not cond.param_key:
                    for pname in list(cond.params):
                        auto_key = f"{cond.indicator}_{pname}"
                        if auto_key in top_params:
                            cond.params[pname] = top_params[auto_key]
                # Auto-match: {ref_indicator}_{param_name} → override value string
                if (
                    not cond.value_ref_overrides
                    and isinstance(cond.value, str)
                    and ":" in cond.value
                ):
                    value_str: str = cond.value
                    ref_parts = value_str.split(":", maxsplit=1)
                    ref_ind = ref_parts[0]
                    auto_overrides: dict[str, str] = {}
                    for kv in ref_parts[1].split(","):
                        k = kv.partition("=")[0].strip()
                        auto_key = f"{ref_ind}_{k}"
                        if auto_key in top_params:
                            auto_overrides[auto_key] = k
                    if auto_overrides:
                        cond.value = _apply_value_ref_overrides(
                            value_str, auto_overrides, top_params
                        )

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
