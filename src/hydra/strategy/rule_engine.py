"""Rule evaluation engine for the RuleBasedStrategy.

Evaluates condition trees against indicator values from the strategy context, and
derives the bar history a rule set needs before its indicators are valid.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping

import numpy as np
from numpy import ndarray

from hydra.core.types import Timeframe
from hydra.indicators import library as ind_lib
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
    RuleSet,
)
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)

MIN_REQUIRED_HISTORY = 50
"""The floor of a derived ``required_history`` (the builder's constant before it derived one)."""

_EMA = ("ema", "macd", "macd_signal", "macd_hist")
_WILDER = (
    "rsi",
    "atr",
    "atr_pct",
    "plus_di",
    "minus_di",
    "supertrend",
    "supertrend_direction",
    "keltner_upper",
    "keltner_lower",
)
WINDOW_MULTIPLE: dict[str, int] = {**dict.fromkeys(_EMA, 3), **dict.fromkeys(_WILDER, 6), "adx": 8}
"""Windows a recursive smoother needs before its seed has faded (windowed indicators: 1).

A smoother is seeded from its first window and carries that seed afterwards. Three spans
bring an EMA within 0.1 % of its value on unlimited history; a Wilder average (alpha 1/N,
the span of a 2N-1 EMA) needs six windows for about 1 %, and ADX, a Wilder average of DX
whose first value comes two windows in, needs eight."""


def _get_indicator_values(
    ctx: StrategyContext,
    symbol: str,
    timeframe: Timeframe,
    indicator_name: str,
    params: Mapping[str, object],
) -> ndarray:
    """Fetch indicator values from the context or compute directly."""
    return ctx.indicator(indicator_name, symbol, timeframe, **params)


def parse_indicator_ref(value: str) -> tuple[str, dict[str, int | float]]:
    """Parse an indicator reference ``"name:param1=val1,param2=val2"``."""
    parts = value.split(":")
    params: dict[str, int | float] = {}
    if len(parts) > 1:
        for kv in parts[1].split(","):
            k, _, v = kv.partition("=")
            try:
                params[k.strip()] = int(v.strip())
            except ValueError:
                params[k.strip()] = float(v.strip())
    return parts[0], params


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
    ind_name, params = parse_indicator_ref(str(value))
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


def indicator_lookback(name: str, params: Mapping[str, object]) -> int:
    """Bars before ``name(**params)`` has a trustworthy current and previous value.

    The sum of the indicator's integer windows (declared defaults included; that sum is
    exact for chained windows like MACD's slow + signal and an over-estimate otherwise),
    times its ``WINDOW_MULTIPLE``, plus the first valid bar and the previous bar a cross
    compares. An unknown name is 0: validation reports it, not this.
    """
    func = getattr(ind_lib, name, None)
    if not inspect.isfunction(func):
        return 0
    window = 0
    for key, param in inspect.signature(func).parameters.items():
        value = params.get(key, param.default)
        if param.annotation in (int, "int") and type(value) is int:
            window += value
    return window * WINDOW_MULTIPLE.get(name, 1) + 2


def rules_required_history(rules: RuleSet) -> int:
    """The ``required_history`` a rule set needs: its longest indicator lookback, at least
    ``MIN_REQUIRED_HISTORY``.

    Every condition's indicator and every indicator-reference value counts. Anything else
    a strategy reads (an ``ml_overlay``'s feature window) is the caller's to ``max`` in.
    Raises ``ValueError`` for an unparsable value reference.
    """
    lookbacks = [MIN_REQUIRED_HISTORY]
    for group in (rules.entry_long, rules.exit_long, rules.entry_short, rules.exit_short):
        for cond in group.conditions if group else []:
            lookbacks.append(indicator_lookback(cond.indicator, cond.params))
            if isinstance(cond.value, str):
                lookbacks.append(indicator_lookback(*parse_indicator_ref(cond.value)))
    return max(lookbacks)
