"""Condition schema for the rule-based strategy no-code builder.

Defines the structure of condition trees that can be serialized to/from
YAML configuration files.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Comparator(StrEnum):
    """Supported comparison operators for rule conditions."""

    LESS_THAN = "less_than"
    GREATER_THAN = "greater_than"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    EQUALS = "equals"


class LogicOperator(StrEnum):
    """Logical operators for combining conditions."""

    AND = "AND"
    OR = "OR"


class Condition(BaseModel):
    """A single rule condition.

    Evaluates ``indicator(params)[current] <comparator> value``.
    For ``crosses_above`` / ``crosses_below`` comparators, the previous
    bar value is also considered.

    Top-level strategy ``parameters`` rewrite a condition only through a binding
    (``hydra.strategy.builtin.rule_based.param_bindings`` is the one resolver that
    the strategy, ``strategy_params`` and the hyperopt name check all read):

    - ``param_key``: the top-level name sets the FIRST key of ``params`` (legacy).
    - ``param_overrides``: ``{top_level_name: param_name}`` sets any parameter the
      indicator takes (in ``params`` or its signature); an unknown name is a ValueError.
    - ``value_param_key``: the top-level name sets a numeric ``value``.
    - ``value_ref_overrides``: ``{top_level_name: param_name}`` sets a parameter of the
      ``value`` indicator reference (``"sma:period=50"``); a name the referenced
      indicator does not take binds nothing.
    - Implicit name-match: a condition with neither ``param_key`` nor
      ``param_overrides`` binds ``{indicator}_{param}`` for each of its ``params``, and
      a reference without ``value_ref_overrides`` binds ``{ref_indicator}_{param}``.
      ``{}`` opts a condition out. An implicit name whose sites hold different values
      (two SMAs of different periods) is ambiguous: it is never listed or optimisable,
      and setting it is a ValueError — bind the intended site explicitly.
    """

    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    comparator: Comparator
    value: float | str  # float literal or another indicator reference
    param_key: str | None = None
    param_overrides: dict[str, str] | None = None
    value_param_key: str | None = None
    value_ref_overrides: dict[str, str] | None = None


class ConditionGroup(BaseModel):
    """A group of conditions combined by a logical operator."""

    operator: LogicOperator = LogicOperator.AND
    conditions: list[Condition] = Field(default_factory=list)


class RuleSet(BaseModel):
    """Full rule set for entry/exit in both directions."""

    entry_long: ConditionGroup | None = None
    exit_long: ConditionGroup | None = None
    entry_short: ConditionGroup | None = None
    exit_short: ConditionGroup | None = None
