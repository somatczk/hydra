"""Read-only context provided to strategies at runtime.

The context wraps data providers, portfolio tracker, and caches indicator
computations so that multiple strategies sharing the same symbol/timeframe
do not redundantly recompute indicators.
"""

from __future__ import annotations

import inspect
import logging
from bisect import bisect_left
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.types import OHLCV, OrderRequest, Position, Timeframe
from hydra.indicators import library as ind_lib

logger = logging.getLogger(__name__)

# Rules read one array per name, so multi-output functions are refused with their alternatives.
MULTI_OUTPUT: dict[str, str] = {
    "bollinger_bands": "use bollinger_upper, bollinger_middle or bollinger_lower",
    "keltner_channels": "use keltner_upper, ema or keltner_lower",
    "ichimoku": "not available to rules (its chikou span reads future bars)",
}
DATA_PARAMS = frozenset({"data", "close", "high", "low", "volume"})
# Inclusive (min, max) per parameter name; the registry and the hyperopt ranges show the same.
PARAM_LIMITS: dict[str, tuple[float, float]] = {
    "period": (2, 200),
    "fast": (2, 100),
    "slow": (2, 200),
    "signal": (2, 100),
    "multiplier": (0.1, 10.0),
    "std_dev": (0.1, 5.0),
    "k_period": (2, 100),
    "d_period": (2, 100),
    "ema_period": (2, 200),
    "atr_period": (2, 200),
}


def resolve_indicator(name: str, params: Mapping[str, object]) -> Callable[..., Any]:
    """Return the library function for *name*, or raise ValueError if it cannot take *params*."""
    if name in MULTI_OUTPUT:
        msg = f"Indicator {name!r} has several outputs: {MULTI_OUTPUT[name]}"
        raise ValueError(msg)
    func = getattr(ind_lib, name, None)
    if name.startswith("_") or not inspect.isfunction(func) or func.__module__ != ind_lib.__name__:
        msg = f"Unknown indicator: {name}"
        raise ValueError(msg)
    sig = inspect.signature(func)
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"Indicator {name!r} parameter {key!r} must be a number, got {value!r}"
            raise ValueError(msg)
        param = sig.parameters.get(key)
        if param is not None and param.annotation in (int, "int") and not isinstance(value, int):
            msg = f"Indicator {name!r} parameter {key!r} must be an integer, got {value!r}"
            raise ValueError(msg)
        lo, hi = PARAM_LIMITS.get(key, (value, value))
        if not lo <= value <= hi:
            msg = f"Indicator {name!r} parameter {key!r} must be in [{lo}, {hi}], got {value!r}"
            raise ValueError(msg)
    data_args = dict.fromkeys(DATA_PARAMS.intersection(sig.parameters))
    try:
        sig.bind(**data_args, **params)
    except TypeError as exc:
        msg = f"Indicator {name!r}: {exc}"
        raise ValueError(msg) from None
    return func


class StrategyContext:
    """Read-only context provided to strategies.

    In production this is backed by real data providers and portfolio tracker.
    For testing it can be constructed with in-memory bar data.
    """

    def __init__(self, max_bars: int | None = None) -> None:
        if max_bars is not None and max_bars < 1:
            msg = f"max_bars must be >= 1, got {max_bars}"
            raise ValueError(msg)
        self._max_bars = max_bars
        # symbol -> timeframe -> list of OHLCV bars (newest last, one per timestamp)
        self._bars: dict[str, dict[str, list[OHLCV]]] = {}
        # Indicator cache: (symbol, timeframe, name, params) -> ndarray
        self._indicator_cache: dict[tuple[str, str, str, tuple[tuple[str, Any], ...]], ndarray] = {}
        # symbol -> Position | None
        self._positions: dict[str, Position | None] = {}
        # symbol -> list of open orders
        self._open_orders: dict[str, list[OrderRequest]] = {}
        # Portfolio value
        self._portfolio_value: Decimal = Decimal("100000")

    # -- Bar data access -----------------------------------------------------

    @property
    def max_bars(self) -> int | None:
        """Bars kept per symbol/timeframe; ``None`` keeps every bar."""
        return self._max_bars

    def add_bar(self, symbol: str, timeframe: Timeframe, bar: OHLCV) -> None:
        """Store a bar, upserting by timestamp (used by the engine).

        A bar with a stored timestamp replaces it; any other is inserted in time order (a
        late gap fill lands where it belongs) and the series is trimmed to ``max_bars``.
        """
        series = self._bars.setdefault(symbol, {}).setdefault(str(timeframe), [])
        ts = bar.timestamp
        if not series or ts > series[-1].timestamp:
            series.append(bar)
        else:
            i = bisect_left(series, ts, key=lambda b: b.timestamp)
            if series[i].timestamp == ts:
                series[i] = bar
            elif self._max_bars is not None and i == 0 and len(series) >= self._max_bars:
                return
            else:
                series.insert(i, bar)
        if self._max_bars is not None and len(series) > self._max_bars:
            del series[: len(series) - self._max_bars]
        tf = str(timeframe)
        keys_to_drop = [k for k in self._indicator_cache if k[0] == symbol and k[1] == tf]
        for k in keys_to_drop:
            del self._indicator_cache[k]

    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> list[OHLCV]:
        """Return the last *count* bars for *symbol* / *timeframe* (oldest first)."""
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        return all_bars[-count:]

    def latest_bar(self, symbol: str, timeframe: Timeframe) -> OHLCV | None:
        """Return the most recent bar, or ``None`` if no data."""
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        return all_bars[-1] if all_bars else None

    # -- Indicator computation (cached) --------------------------------------

    def indicator(self, name: str, symbol: str, timeframe: Timeframe, **params: Any) -> ndarray:
        """Compute (or return cached) indicator values over every stored bar.

        *name* is a single-output function in ``hydra.indicators.library``; it gets the
        close/high/low/volume arrays its signature names. For a function returning a tuple
        (``macd``, ``stochastic``, ``supertrend``) this is the first output.
        """
        key = (symbol, str(timeframe), name, tuple(sorted(params.items())))
        if key in self._indicator_cache:
            return self._indicator_cache[key]

        func = resolve_indicator(name, params)
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        if not all_bars:
            empty = np.array([], dtype=np.float64)
            self._indicator_cache[key] = empty
            return empty

        close = np.array([float(b.close) for b in all_bars], dtype=np.float64)
        arrays = {
            "data": close,
            "close": close,
            "high": np.array([float(b.high) for b in all_bars], dtype=np.float64),
            "low": np.array([float(b.low) for b in all_bars], dtype=np.float64),
            "volume": np.array([float(b.volume) for b in all_bars], dtype=np.float64),
        }
        data = {p: arrays[p] for p in inspect.signature(func).parameters if p in arrays}
        out = func(**data, **params)
        result: ndarray = out[0] if isinstance(out, tuple) else out

        self._indicator_cache[key] = result
        return result

    # -- Position / order access ---------------------------------------------

    def position(self, symbol: str) -> Position | None:
        """Return current position for *symbol*, or ``None``."""
        return self._positions.get(symbol)

    def set_position(self, symbol: str, pos: Position | None) -> None:
        """Set position (used by the engine)."""
        self._positions[symbol] = pos

    def open_orders(self, symbol: str) -> list[OrderRequest]:
        """Return open orders for *symbol*."""
        return self._open_orders.get(symbol, [])

    def portfolio_value(self) -> Decimal:
        """Return total portfolio value."""
        return self._portfolio_value

    def set_portfolio_value(self, value: Decimal) -> None:
        """Set portfolio value (used by the engine)."""
        self._portfolio_value = value
