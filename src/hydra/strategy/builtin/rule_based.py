"""Rule-based strategy (no-code builder output).

Evaluates condition trees loaded from YAML configuration.  Each rule set
defines entry_long, exit_long, entry_short, exit_short sections with
AND/OR operators and indicator conditions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import Direction, Symbol
from hydra.strategy.base import BaseStrategy
from hydra.strategy.condition_schema import ConditionGroup, RuleSet
from hydra.strategy.context import indicator_params, resolve_indicator
from hydra.strategy.rule_engine import (
    evaluate_condition_group,
    parse_indicator_ref,
    rules_required_history,
)

logger = logging.getLogger(__name__)

_GROUPS = ("entry_long", "exit_long", "entry_short", "exit_short")


@dataclass(frozen=True, slots=True)
class ParamBinding:
    """One condition site a top-level parameter sets: its ``params``, ``value`` or ``ref`` key."""

    group: str
    index: int
    target: Literal["params", "value", "ref"]
    key: str
    default: Any
    implicit: bool = False

    def __str__(self) -> str:
        return f"{self.group}[{self.index}].{self.target}.{self.key}={self.default}"


def param_bindings(rules: RuleSet) -> dict[str, list[ParamBinding]]:
    """Every top-level name the rules bind, with its sites in rule order (see ``Condition``).

    Raises ValueError for a ``param_overrides`` entry naming a parameter its indicator lacks.
    """
    table: dict[str, list[ParamBinding]] = {}

    def bind(name: str, site: ParamBinding) -> None:
        table.setdefault(name, []).append(site)

    for group in _GROUPS:
        rule_group: ConditionGroup | None = getattr(rules, group)
        for i, cond in enumerate(rule_group.conditions if rule_group else []):
            if cond.param_key and cond.params:
                key = next(iter(cond.params))
                bind(cond.param_key, ParamBinding(group, i, "params", key, cond.params[key]))
            if cond.param_overrides:
                known = indicator_params(cond.indicator, cond.params)
                for name, key in cond.param_overrides.items():
                    if key not in known:
                        msg = (
                            f"{group}[{i}] param_overrides {name!r}: {cond.indicator} has no "
                            f"parameter {key!r} (it takes {', '.join(known) or 'none'})"
                        )
                        raise ValueError(msg)
                    bind(name, ParamBinding(group, i, "params", key, known[key]))
            if cond.value_param_key and not isinstance(cond.value, str):
                bind(cond.value_param_key, ParamBinding(group, i, "value", "value", cond.value))
            ref, embedded = (
                parse_indicator_ref(cond.value) if isinstance(cond.value, str) else ("", {})
            )
            if cond.value_ref_overrides and ref:
                known = indicator_params(ref, embedded)
                for name, key in cond.value_ref_overrides.items():
                    if key in known:
                        bind(name, ParamBinding(group, i, "ref", key, known[key]))
            if not cond.param_key and cond.param_overrides is None:
                for key, default in cond.params.items():
                    bind(
                        f"{cond.indicator}_{key}",
                        ParamBinding(group, i, "params", key, default, True),
                    )
            if cond.value_ref_overrides is None:
                for key, default in embedded.items():
                    bind(f"{ref}_{key}", ParamBinding(group, i, "ref", key, default, True))
    return table


def ambiguous(sites: list[ParamBinding]) -> bool:
    """An implicit name-match whose sites hold different values: it never binds."""
    return any(s.implicit for s in sites) and len({s.default for s in sites}) > 1


def bindable_params(rules: RuleSet) -> dict[str, list[ParamBinding]]:
    """The names a top-level parameter can set: ``param_bindings`` without the ambiguous ones."""
    return {name: sites for name, sites in param_bindings(rules).items() if not ambiguous(sites)}


def _set_ref_param(ref: str, key: str, raw: Any) -> str:
    """Set *key* in an indicator reference (``"sma:period=50"``); an integral float stays an int."""
    name, _, rest = ref.partition(":")
    embedded = {
        k.strip(): v.strip() for k, _, v in (kv.partition("=") for kv in rest.split(",") if kv)
    }
    embedded[key] = str(int(raw)) if isinstance(raw, float) and raw == int(raw) else str(raw)
    return f"{name}:{','.join(f'{k}={v}' for k, v in embedded.items())}"


class RuleBasedStrategy(BaseStrategy):
    """Strategy driven by declarative condition trees from YAML config."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rules = self._parse_rules()
        self._apply_param_overrides()
        self._validate_indicators()
        self._validate_required_history()
        # a parameter that lengthens a declared window (a hyperopt trial) warms up by as much
        self._override_headroom = max(
            0, rules_required_history(self._rules) - rules_required_history(self._parse_rules())
        )

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
        """Set every bound condition site from the top-level ``parameters`` (see ``Condition``).

        An ambiguous implicit name that is set is a ValueError; an implicit one that applies
        is logged, so a name-match never rewires conditions silently.
        """
        top = self._config.parameters
        implicit: list[str] = []
        for name, sites in param_bindings(self._rules).items():
            if name not in top:
                continue
            if ambiguous(sites):
                msg = (
                    f"parameter {name!r} matches conditions with different values by name "
                    f"({', '.join(map(str, sites))}); bind the one it is meant for with "
                    "param_overrides or value_ref_overrides"
                )
                raise ValueError(msg)
            if any(site.implicit for site in sites):
                implicit.append(name)
            for site in sites:
                cond = getattr(self._rules, site.group).conditions[site.index]
                if site.target == "params":
                    cond.params[site.key] = top[name]
                elif site.target == "value":
                    cond.value = float(top[name])
                else:
                    cond.value = _set_ref_param(cond.value, site.key, top[name])
        if implicit:
            logger.warning(
                "strategy %s binds %s to its rules by name ({indicator}_{param}); declare "
                "param_overrides or value_ref_overrides to bind them explicitly",
                self.strategy_id,
                ", ".join(implicit),
            )

    def _validate_indicators(self) -> None:
        """Raise ValueError now for a rule that would raise on every bar (unknown or
        multi-output indicator, bad parameters, unparsable value reference)."""
        for group in (
            self._rules.entry_long,
            self._rules.exit_long,
            self._rules.entry_short,
            self._rules.exit_short,
        ):
            for cond in group.conditions if group else []:
                resolve_indicator(cond.indicator, cond.params)
                if isinstance(cond.value, str):
                    resolve_indicator(*parse_indicator_ref(cond.value))

    def _validate_required_history(self) -> None:
        """Raise now for a required_history that is not a positive int.

        It slices the context bars on every ``on_bar``; a string or float would fail on every
        bar instead, while ``probe_strategy``'s ``int()`` cast hides it at session start.
        """
        value = self._config.parameters.get("required_history", 50)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"parameters.required_history must be an integer >= 1, got {value!r}")

    @property
    def required_history(self) -> int:
        """The declared ``parameters.required_history`` (any value: the explicit override),
        plus the lookback that parameter overrides add beyond the declared rules."""
        declared: int = self._config.parameters.get("required_history", 50)
        return declared + self._override_headroom

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
