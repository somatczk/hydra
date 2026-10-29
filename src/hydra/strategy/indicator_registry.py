"""Indicator registry: introspects hydra.indicators.library to build metadata.

Auto-discovers all indicator functions from the library module and extracts
parameter schemas via inspect, providing structured IndicatorInfo objects
for the no-code strategy builder.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from hydra.indicators import library as ind_lib
from hydra.strategy.context import DATA_PARAMS, MULTI_OUTPUT, PARAM_LIMITS

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Mapping of indicator names to categories
_CATEGORY_MAP: dict[str, str] = {
    "sma": "trend",
    "ema": "trend",
    "macd": "trend",
    "macd_signal": "trend",
    "macd_hist": "trend",
    "supertrend": "trend",
    "supertrend_direction": "trend",
    "adx": "trend",
    "plus_di": "trend",
    "minus_di": "trend",
    "lag": "trend",
    "rsi": "momentum",
    "stochastic": "momentum",
    "stochastic_d": "momentum",
    "cci": "momentum",
    "williams_r": "momentum",
    "roc": "momentum",
    "atr": "volatility",
    "atr_pct": "volatility",
    "bb_width": "volatility",
    "bollinger_upper": "volatility",
    "bollinger_middle": "volatility",
    "bollinger_lower": "volatility",
    "rolling_max": "volatility",
    "rolling_min": "volatility",
    "rolling_mid": "volatility",
    "rolling_high": "volatility",
    "rolling_low": "volatility",
    "keltner_upper": "volatility",
    "keltner_lower": "volatility",
    "obv": "volume",
    "vwap": "volume",
    "mfi": "volume",
}

# Human-readable descriptions for each indicator
_DESCRIPTION_MAP: dict[str, str] = {
    "sma": "Simple Moving Average",
    "ema": "Exponential Moving Average",
    "macd": "MACD line (fast EMA - slow EMA)",
    "macd_signal": "MACD signal line (EMA of the MACD line)",
    "macd_hist": "MACD histogram (MACD line - signal line)",
    "supertrend": "Supertrend line (ATR-based trailing band)",
    "supertrend_direction": "Supertrend direction (+1 bullish, -1 bearish)",
    "adx": "Average Directional Index (0-100 trend strength, Wilder)",
    "plus_di": "+DI, upward directional indicator (0-100, Wilder)",
    "minus_di": "-DI, downward directional indicator (0-100, Wilder)",
    "lag": "Close N bars ago (time-series momentum: close > lag)",
    "rsi": "Relative Strength Index (0-100 oscillator)",
    "stochastic": "Fast stochastic %K (0-100)",
    "stochastic_d": "Fast stochastic %D (SMA of %K)",
    "cci": "Commodity Channel Index",
    "williams_r": "Williams %R (-100 to 0 oscillator)",
    "roc": "Rate of change: % change of the close over N bars",
    "atr": "Average True Range (Wilder)",
    "atr_pct": "ATR as a % of the close",
    "bb_width": "Bollinger bandwidth: (upper - lower) / middle, in %",
    "bollinger_upper": "Upper Bollinger Band",
    "bollinger_middle": "Middle Bollinger Band (SMA)",
    "bollinger_lower": "Lower Bollinger Band",
    "rolling_max": "Highest close of the previous N bars (Donchian upper)",
    "rolling_min": "Lowest close of the previous N bars (Donchian lower)",
    "rolling_mid": "Midpoint of the previous N closes (Donchian midline)",
    "rolling_high": "Highest high of the previous N bars (Donchian upper on highs)",
    "rolling_low": "Lowest low of the previous N bars (Donchian lower on lows)",
    "keltner_upper": "Upper Keltner Channel (EMA + ATR multiple)",
    "keltner_lower": "Lower Keltner Channel (EMA - ATR multiple)",
    "obv": "On Balance Volume",
    "vwap": "Volume Weighted Average Price",
    "mfi": "Money Flow Index (0-100 oscillator)",
}


@dataclass(frozen=True, slots=True)
class ParamInfo:
    """Schema for a single indicator parameter."""

    name: str
    type: str
    default: Any = None
    min: int | float | None = None
    max: int | float | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class IndicatorInfo:
    """Schema for a single indicator function."""

    name: str
    category: str
    description: str
    params: list[ParamInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry logic
# ---------------------------------------------------------------------------


def _python_type_to_str(annotation: Any) -> str:
    """Convert a Python type annotation to a simple string label."""
    if annotation is inspect.Parameter.empty:
        return "float"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    # Handle string annotations
    type_str = str(annotation)
    if "int" in type_str:
        return "int"
    return "float"


def _extract_params(func: Any) -> list[ParamInfo]:
    """Extract user-configurable parameters from an indicator function."""
    sig = inspect.signature(func)
    params: list[ParamInfo] = []

    for name, param in sig.parameters.items():
        # Skip data array parameters
        if name in DATA_PARAMS:
            continue

        default = param.default if param.default is not inspect.Parameter.empty else None
        param_type = _python_type_to_str(param.annotation)

        min_val, max_val = PARAM_LIMITS.get(name, (None, None))

        params.append(
            ParamInfo(
                name=name,
                type=param_type,
                default=default,
                min=min_val,
                max=max_val,
                description=f"Parameter: {name}",
            )
        )

    return params


def get_all_indicators() -> list[IndicatorInfo]:
    """Introspect hydra.indicators.library to discover all indicator functions.

    Returns a list of IndicatorInfo objects with parameter schemas,
    categories, and descriptions.  Only public functions (no leading
    underscore) that are defined directly in the library module are included.
    """
    indicators: list[IndicatorInfo] = []

    for name in sorted(dir(ind_lib)):
        # Skip private/internal names, non-functions and multi-output functions
        if name.startswith("_") or name in MULTI_OUTPUT:
            continue

        obj = getattr(ind_lib, name)
        if not callable(obj):
            continue

        # Only include functions defined in the library module
        if getattr(obj, "__module__", None) != ind_lib.__name__:
            continue

        category = _CATEGORY_MAP.get(name, "other")
        description = _DESCRIPTION_MAP.get(name, f"{name} indicator")
        params = _extract_params(obj)

        indicators.append(
            IndicatorInfo(
                name=name,
                category=category,
                description=description,
                params=params,
            )
        )

    return indicators
