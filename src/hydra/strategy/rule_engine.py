"""Rule evaluation engine for the RuleBasedStrategy.

Evaluates condition trees against indicator values from the strategy context.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import numpy as np
from numpy import ndarray

from hydra.core.types import Timeframe
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
)
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


def _get_indicator_values(
    ctx: StrategyContext,
    symbol: str,
    timeframe: Timeframe,
    indicator_name: str,
    params: Mapping[str, object],
) -> ndarray:
    """Fetch indicator values from the context or compute directly."""
    return ctx.indicator(indicator_name, symbol, timeframe, **params)


def _resolve_value(
    value: float | str,
    ctx: StrategyContext,
    symbol: str,
    timeframe: Timeframe,
    index: int,
) -> float:
    """Resolve a condition value.

    If *value* is a float, return it directly. If it is a string, treat it
    as an indicator reference (e.g. ``"sma:period=20"``) and return the
    value at *index*.
    """
    if isinstance(value, (int, float)):
        return float(value)
    # Parse indicator reference: "indicator_name:param1=val1,param2=val2"
    parts = str(value).split(":")
    ind_name = parts[0]
    params: dict[str, int | float] = {}
    if len(parts) > 1:
        for kv in parts[1].split(","):
            k, _, v = kv.partition("=")
            try:
                params[k.strip()] = int(v.strip())
            except ValueError:
                params[k.strip()] = float(v.strip())
    vals = _get_indicator_values(ctx, symbol, timeframe, ind_name, params)
    if index < len(vals):
        return float(vals[index])
    return float("nan")


def evaluate_condition(
    cond: Condition,
    ctx: StrategyContext,
    symbol: str,
    timeframe: Timeframe,
) -> bool:
    """Evaluate a single condition against the latest indicator values."""
    vals = _get_indicator_values(ctx, symbol, timeframe, cond.indicator, cond.params)
    if len(vals) < 2:
        return False

    current_idx = len(vals) - 1
    prev_idx = len(vals) - 2
    current = float(vals[current_idx])
    prev = float(vals[prev_idx])

    if np.isnan(current) or np.isnan(prev):
        return False

    target = _resolve_value(cond.value, ctx, symbol, timeframe, current_idx)
    target_prev = _resolve_value(cond.value, ctx, symbol, timeframe, prev_idx)

    if np.isnan(target):
        return False

    if cond.comparator == Comparator.LESS_THAN:
        return current < target
    if cond.comparator == Comparator.GREATER_THAN:
        return current > target
    if cond.comparator == Comparator.EQUALS:
        return current == target
    if cond.comparator == Comparator.CROSSES_ABOVE:
        if np.isnan(target_prev):
            return False
        return prev <= target_prev and current > target
    if cond.comparator == Comparator.CROSSES_BELOW:
        if np.isnan(target_prev):
            return False
        return prev >= target_prev and current < target
    return False


def evaluate_condition_group(
    group: ConditionGroup | None,
    ctx: StrategyContext,
    symbol: str,
    timeframe: Timeframe,
) -> bool:
    """Evaluate a condition group (AND/OR logic)."""
    if group is None or not group.conditions:
        return False

    results = [evaluate_condition(c, ctx, symbol, timeframe) for c in group.conditions]

    if group.operator == LogicOperator.AND:
        return all(results)
    return any(results)
