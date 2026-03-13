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
    """

    indicator: str
    params: dict[str, Any] = Field(default_factory=dict)
    comparator: Comparator
    value: float | str  # float literal or another indicator reference


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
