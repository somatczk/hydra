"""Read-only context provided to strategies at runtime.

The context wraps data providers, portfolio tracker, and caches indicator
computations so that multiple strategies sharing the same symbol/timeframe
do not redundantly recompute indicators.
"""

from __future__ import annotations

import functools
import inspect
import logging
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.features import FEATURES
from hydra.core.features import PREFIX as FEATURE_PREFIX
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
    "zscore": (1, 200),
    "pct_change": (1, 200),
    "diff": (1, 200),
}
_TRANSFORMS = ("zscore", "pct_change", "diff")
_FIELDS = ("close", "high", "low", "volume")
_signature = functools.cache(inspect.signature)


def _frozen(columns: Iterable[ndarray]) -> dict[str, ndarray]:
    """The arrays indicators read (``data`` is the close); read-only, ``close`` returns them."""
    arrays = dict(zip(_FIELDS, columns, strict=True))
    for arr in arrays.values():
        arr.flags.writeable = False
    return {"data": arrays["close"], **arrays}


def feature_transform(
    data: ndarray, zscore: int = 0, pct_change: int = 0, diff: int = 0
) -> ndarray:
    """A feature's bar-aligned values, optionally transformed over the last N bars (no lookahead).

    A window holding a NaN (before the first value, or a stale bar) yields NaN.
    """
    n = len(data)
    out = np.full(n, np.nan)
    if diff:
        if n > diff:
            out[diff:] = data[diff:] - data[:-diff]
    elif pct_change:
        if n > pct_change:
            with np.errstate(divide="ignore", invalid="ignore"):
                out[pct_change:] = data[pct_change:] / data[:-pct_change] - 1.0
    elif zscore:
        if n >= zscore:
            windows = np.lib.stride_tricks.sliding_window_view(data, zscore)
            std = windows.std(axis=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                out[zscore - 1 :] = (data[zscore - 1 :] - windows.mean(axis=1)) / std
    else:
        out[:] = data
    out[~np.isfinite(out)] = np.nan
    return out


def _resolve_feature(name: str, params: Mapping[str, object]) -> Callable[..., Any]:
    ref = name.removeprefix(FEATURE_PREFIX)
    if ref not in FEATURES:
        msg = f"Unknown feature: {ref}"
        raise ValueError(msg)
    for key, value in params.items():
        if key not in _TRANSFORMS:
            msg = (
                f"Feature {name!r} has no parameter {key!r} (transforms: {', '.join(_TRANSFORMS)})"
            )
            raise ValueError(msg)
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"Feature {name!r} parameter {key!r} must be an integer, got {value!r}"
            raise ValueError(msg)
        lo, hi = PARAM_LIMITS[key]
        if not lo <= value <= hi:
            msg = f"Feature {name!r} parameter {key!r} must be in [{lo}, {hi}], got {value!r}"
            raise ValueError(msg)
    if len(params) > 1:
        msg = f"Feature {name!r} takes one transform, got {sorted(params)}"
        raise ValueError(msg)
    return feature_transform


def resolve_indicator(name: str, params: Mapping[str, object]) -> Callable[..., Any]:
    """Return the library function for *name*, or raise ValueError if it cannot take *params*."""
    if name.startswith(FEATURE_PREFIX):
        return _resolve_feature(name, params)
    if name in MULTI_OUTPUT:
        msg = f"Indicator {name!r} has several outputs: {MULTI_OUTPUT[name]}"
        raise ValueError(msg)
    func = getattr(ind_lib, name, None)
    if name.startswith("_") or not inspect.isfunction(func) or func.__module__ != ind_lib.__name__:
        msg = f"Unknown indicator: {name}"
        raise ValueError(msg)
    sig = _signature(func)
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


def indicator_params(name: str, params: Mapping[str, object]) -> dict[str, object]:
    """*name*'s own parameters, *params* over the signature defaults; ValueError as resolve does."""
    sig = _signature(resolve_indicator(name, params))
    return {k: params.get(k, p.default) for k, p in sig.parameters.items() if k not in DATA_PARAMS}


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
        # (symbol, timeframe) -> read-only close/high/low/volume arrays of the stored bars
        self._arrays: dict[tuple[str, str], dict[str, ndarray]] = {}
        # symbol -> Position | None
        self._positions: dict[str, Position | None] = {}
        # symbol -> list of open orders
        self._open_orders: dict[str, list[OrderRequest]] = {}
        # Portfolio value
        self._portfolio_value: Decimal = Decimal("100000")
        # ponytail: (ref, symbol) -> rows by available_at, never trimmed; trim if they grow
        self._features: dict[tuple[str, str], list[tuple[datetime, datetime, float]]] = {}

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
        appended = not series or ts > series[-1].timestamp
        if appended:
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
        arrays = self._arrays.pop((symbol, tf), None)
        if appended and arrays is not None:
            # A new latest bar: extend the arrays rather than convert every Decimal again.
            self._arrays[(symbol, tf)] = _frozen(
                np.append(arrays[f], float(getattr(bar, f)))[-len(series) :] for f in _FIELDS
            )
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

    # -- Features (ts.features rows, point in time) ---------------------------

    def add_feature(
        self, ref: str, symbol: str, *, ts: datetime, available_at: datetime, value: float
    ) -> None:
        """Store one feature row (used by the engine); *symbol* is '' for a market feature.

        A row with a stored ``(available_at, ts)`` replaces it. Drops the cached values that
        read it: every symbol's for a market feature.
        """
        rows = self._features.setdefault((ref, symbol), [])
        row = (available_at, ts, float(value))
        i = bisect_left(rows, row[:2], key=lambda r: r[:2])
        if i < len(rows) and rows[i][:2] == row[:2]:
            rows[i] = row
        else:
            rows.insert(i, row)
        name = FEATURE_PREFIX + ref
        stale = [
            k for k in self._indicator_cache if k[2] == name and (not symbol or k[0] == symbol)
        ]
        for k in stale:
            del self._indicator_cache[k]

    def _feature_values(self, ref: str, symbol: str, timeframe: Timeframe) -> ndarray:
        """*ref* as of each bar's close: the newest event published by then, NaN when stale."""
        fd = FEATURES[ref]
        rows = self._features.get((ref, symbol if fd.scope == "symbol" else ""), [])
        bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        step = timedelta(seconds=Timeframe(timeframe).seconds)
        out = np.full(len(bars), np.nan)
        best: tuple[datetime, datetime, float] | None = None
        j = 0
        for i, bar in enumerate(bars):
            close = bar.timestamp + step
            while j < len(rows) and rows[j][0] <= close:
                if best is None or (rows[j][1], rows[j][0]) > (best[1], best[0]):
                    best = rows[j]
                j += 1
            if best is not None and close - best[1] <= fd.max_age:
                out[i] = best[2]
        return out

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
        if name.startswith(FEATURE_PREFIX):
            ref = name.removeprefix(FEATURE_PREFIX)
            values: ndarray = func(self._feature_values(ref, symbol, timeframe), **params)
            self._indicator_cache[key] = values
            return values
        all_bars = self._bars.get(symbol, {}).get(str(timeframe), [])
        if not all_bars:
            empty = np.array([], dtype=np.float64)
            self._indicator_cache[key] = empty
            return empty

        arrays = self._arrays.get((symbol, str(timeframe)))
        if arrays is None:
            arrays = self._arrays[(symbol, str(timeframe))] = _frozen(
                np.array([float(getattr(b, f)) for b in all_bars]) for f in _FIELDS
            )
        data = {p: arrays[p] for p in _signature(func).parameters if p in arrays}
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
