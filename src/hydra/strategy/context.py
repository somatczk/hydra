"""Read-only context provided to strategies at runtime.

The context wraps data providers, portfolio tracker, and caches indicator
computations so that multiple strategies sharing the same symbol/timeframe
do not redundantly recompute indicators.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.types import OHLCV, OrderRequest, Position, Timeframe
from hydra.indicators import library as ind_lib

logger = logging.getLogger(__name__)


class StrategyContext:
    """Read-only context provided to strategies.

    In production this is backed by real data providers and portfolio tracker.
    For testing it can be constructed with in-memory bar data.
    """

    def __init__(self) -> None:
        # symbol -> timeframe -> list of OHLCV bars (newest last)
        self._bars: dict[str, dict[str, list[OHLCV]]] = {}
        # Indicator cache: key -> ndarray
        self._indicator_cache: dict[str, ndarray] = {}
        # symbol -> Position | None
        self._positions: dict[str, Position | None] = {}
        # symbol -> list of open orders
        self._open_orders: dict[str, list[OrderRequest]] = {}
        # Portfolio value
        self._portfolio_value: Decimal = Decimal("100000")

    # -- Bar data access -----------------------------------------------------

    def add_bar(self, symbol: str, timeframe: Timeframe, bar: OHLCV) -> None:
        """Append a bar to the internal store (used by the engine)."""
        self._bars.setdefault(symbol, {}).setdefault(str(timeframe), []).append(bar)
        # Invalidate indicator cache for this symbol/timeframe
        prefix = f"{symbol}:{timeframe}:"
        keys_to_drop = [k for k in self._indicator_cache if k.startswith(prefix)]
        for k in keys_to_drop:
            del self._indicator_cache[k]

    def bars(self, symbol: str, timeframe: Timeframe, count: int) -> list[OHLCV]:
        """Return the last *count* bars for *symbol* / *timeframe*."""
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        return all_bars[-count:]

    def latest_bar(self, symbol: str, timeframe: Timeframe) -> OHLCV | None:
        """Return the most recent bar, or ``None`` if no data."""
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        return all_bars[-1] if all_bars else None

    # -- Indicator computation (cached) --------------------------------------

    def _cache_key(self, name: str, symbol: str, timeframe: Timeframe, **params: Any) -> str:
        param_str = hashlib.md5(  # noqa: S324
            str(sorted(params.items())).encode()
        ).hexdigest()
        return f"{symbol}:{timeframe}:{name}:{param_str}"

    def indicator(self, name: str, symbol: str, timeframe: Timeframe, **params: Any) -> ndarray:
        """Compute (or return cached) indicator values.

        The *name* must match a function in ``hydra.indicators.library``.
        """
        key = self._cache_key(name, symbol, timeframe, **params)
        if key in self._indicator_cache:
            return self._indicator_cache[key]

        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        if not all_bars:
            result = np.array([], dtype=np.float64)
            self._indicator_cache[key] = result
            return result

        close = np.array([float(b.close) for b in all_bars], dtype=np.float64)
        high = np.array([float(b.high) for b in all_bars], dtype=np.float64)
        low = np.array([float(b.low) for b in all_bars], dtype=np.float64)
        volume = np.array([float(b.volume) for b in all_bars], dtype=np.float64)

        func = getattr(ind_lib, name, None)
        if func is None:
            msg = f"Unknown indicator: {name}"
            raise ValueError(msg)

        # Determine which arrays the indicator needs based on its name
        volume_indicators = {"obv", "vwap", "mfi"}
        hlc_indicators = {
            "atr",
            "stochastic",
            "cci",
            "williams_r",
            "supertrend",
            "ichimoku",
            "keltner_channels",
        }

        if name in volume_indicators:
            if name == "obv":
                result = func(close, volume, **params)
            else:
                result = func(high, low, close, volume, **params)
        elif name in hlc_indicators:
            result = func(high, low, close, **params)
        else:
            result = func(close, **params)

        # Handle tuple returns (store only the first element for simple caching)
        if isinstance(result, tuple):
            result = result[0]
        elif isinstance(result, dict):
            # For ichimoku, return the first value — caller can use params to select
            result = next(iter(result.values()))

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
