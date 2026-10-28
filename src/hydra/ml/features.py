"""Feature engineering for ML model training and real-time inference.

One frozen :class:`FeatureSpec` pins everything a model consumes: the per-timeframe
window lengths the dataset builder reads, the target horizon, the exact feature
names and a spec version that bumps whenever the feature set changes.
``build_features`` and ``build_live_features`` share one computation path, so the
last training row and the live vector are identical by construction. Higher
timeframe features are timestamp-joined onto the 1h grid (a 4h/1d bar is visible
only once it has closed), never count-aligned. All computations use numpy; no
pandas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.types import OHLCV
from hydra.data.bars import timeframe_seconds
from hydra.indicators.library import (
    atr,
    bollinger_bands,
    macd,
    mfi,
    obv,
    rsi,
    sma,
    vwap,
)

PRIMARY_TF = "1h"
HIGHER_TFS = ("4h", "1d")

# ---------------------------------------------------------------------------
# FeatureMatrix dataclass
# ---------------------------------------------------------------------------


@dataclass
class FeatureMatrix:
    """Container for an ML feature matrix with metadata."""

    features: ndarray  # shape (n_samples, n_features)
    feature_names: list[str]  # column names
    timestamps: list[datetime]  # aligned timestamps
    spec: FeatureSpec  # the pinned spec the matrix was built with
    target: ndarray | None = None  # sign target (+1/-1; NaN where no forward bar exists)
    forward_returns: ndarray | None = None  # the raw forward return each target came from


# ---------------------------------------------------------------------------
# The frozen feature spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Everything a model consumes, pinned: windows, horizon, names, version.

    ``reference_quantiles`` is the placeholder (``None``) until the trainer fills
    it with one decile-edge tuple per feature, which the drift check reuses as its
    bins. Frozen and hashable so a dataset is reproducible from (rows, spec).
    """

    spec_version: int
    horizon: int
    window_1h: int
    window_4h: int
    window_1d: int
    feature_names: tuple[str, ...]
    reference_quantiles: tuple[tuple[float, ...] | None, ...] | None = None

    def __post_init__(self) -> None:
        if self.spec_version < 1:
            msg = "spec_version must be >= 1"
            raise ValueError(msg)
        if self.horizon < 1:
            msg = "horizon must be >= 1 (bars of the primary timeframe)"
            raise ValueError(msg)
        if min(self.window_1h, self.window_4h, self.window_1d) < 1:
            msg = "window lengths must be >= 1"
            raise ValueError(msg)
        if not self.feature_names:
            msg = "feature_names must not be empty"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """The jsonb form stored on a registry row."""
        quantiles = self.reference_quantiles
        return {
            "spec_version": self.spec_version,
            "horizon": self.horizon,
            "window_1h": self.window_1h,
            "window_4h": self.window_4h,
            "window_1d": self.window_1d,
            "feature_names": list(self.feature_names),
            "reference_quantiles": (
                None
                if quantiles is None
                else [list(q) if q is not None else None for q in quantiles]
            ),
        }


# ---------------------------------------------------------------------------
# Feature name lists (the single source the builders and the spec share)
# ---------------------------------------------------------------------------


def _technical_indicator_names(tf_label: str) -> list[str]:
    return [
        f"rsi_14_{tf_label}",
        f"macd_hist_{tf_label}",
        f"bb_pctb_{tf_label}",
        f"atr_norm_{tf_label}",
        f"rsi_14_{tf_label}_lag1",
        f"macd_hist_{tf_label}_lag1",
        f"rsi_14_{tf_label}_lag2",
        f"macd_hist_{tf_label}_lag2",
        f"rsi_14_{tf_label}_lag3",
        f"macd_hist_{tf_label}_lag3",
        f"rsi_14_{tf_label}_roc3",
        f"macd_hist_{tf_label}_roc3",
    ]


def _price_action_names(tf_label: str) -> list[str]:
    names = [f"log_return_{tf_label}"]
    if tf_label == PRIMARY_TF:
        names.append("realized_vol_24h")
    elif tf_label == "1d":
        names.append("realized_vol_7d")
    names.extend(
        [
            f"price_vs_sma20_{tf_label}",
            f"price_vs_sma50_{tf_label}",
            f"candle_body_ratio_{tf_label}",
            f"upper_wick_ratio_{tf_label}",
            f"lower_wick_ratio_{tf_label}",
        ]
    )
    return names


def _volume_names(tf_label: str) -> list[str]:
    return [
        f"vol_sma20_ratio_{tf_label}",
        f"vwap_dev_{tf_label}",
        f"obv_roc10_{tf_label}",
        f"mfi_14_{tf_label}",
    ]


def _temporal_names() -> list[str]:
    return ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]


def _derived_names() -> list[str]:
    return ["rsi_vol_ratio", "atr_pctile_rank_100", "bb_width_change"]


def _all_feature_names() -> list[str]:
    """The names in the exact block order the builder computes them."""
    names: list[str] = []
    for tf_label in (PRIMARY_TF, *HIGHER_TFS):
        names.extend(_technical_indicator_names(tf_label))
    for tf_label in (PRIMARY_TF, *HIGHER_TFS):
        names.extend(_price_action_names(tf_label))
    names.extend(_volume_names(PRIMARY_TF))
    names.extend(_temporal_names())
    names.extend(_derived_names())
    return names


DEFAULT_FEATURE_SPEC = FeatureSpec(
    spec_version=3,
    horizon=4,
    window_1h=2000,
    window_4h=500,
    window_1d=120,
    feature_names=tuple(_all_feature_names()),
)
"""The pinned spec training and serving share; bump spec_version on any change."""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_close_array(bars: list[OHLCV]) -> ndarray:
    """Extract close prices as float64 array."""
    return np.array([float(b.close) for b in bars], dtype=np.float64)


def _extract_ohlcv_arrays(
    bars: list[OHLCV],
) -> tuple[ndarray, ndarray, ndarray, ndarray, ndarray]:
    """Extract open, high, low, close, volume as float64 arrays."""
    o = np.array([float(b.open) for b in bars], dtype=np.float64)
    h = np.array([float(b.high) for b in bars], dtype=np.float64)
    lo = np.array([float(b.low) for b in bars], dtype=np.float64)
    c = np.array([float(b.close) for b in bars], dtype=np.float64)
    v = np.array([float(b.volume) for b in bars], dtype=np.float64)
    return o, h, lo, c, v


def _log_returns(close: ndarray) -> ndarray:
    """Compute log returns: ln(close[t] / close[t-1]).

    Returns array of length len(close) - 1.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        returns: ndarray = np.log(close[1:] / close[:-1])
    return returns


def _realized_vol(returns: ndarray, window: int) -> ndarray:
    """Rolling realized volatility = rolling std * sqrt(window).

    Returns array of the same length as *returns*, with NaN where the
    rolling window is incomplete.
    """
    n = len(returns)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return result
    for i in range(window - 1, n):
        result[i] = np.std(returns[i - window + 1 : i + 1], ddof=1) * math.sqrt(window)
    return result


def _sin_cos_encode(values: ndarray, period: float) -> tuple[ndarray, ndarray]:
    """Cyclical encoding via sin/cos."""
    angle = 2.0 * np.pi * values / period
    return np.sin(angle), np.cos(angle)


def _percentile_rank(data: ndarray, window: int) -> ndarray:
    """Rolling percentile rank of the current value within its window.

    Returns values in [0, 1].
    """
    n = len(data)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(window - 1, n):
        w = data[i - window + 1 : i + 1]
        valid = w[~np.isnan(w)]
        if len(valid) == 0:
            continue
        result[i] = np.sum(valid <= data[i]) / len(valid)
    return result


def build_target(close: ndarray, horizon: int = 1) -> tuple[ndarray, ndarray]:
    """The sign target and the forward returns it is taken from.

    Both arrays have length ``len(close) - horizon``; the signs are +1, -1 or 0
    (a zero forward return), which the trainer drops.
    """
    if horizon < 1:
        msg = f"horizon must be >= 1, got {horizon}"
        raise ValueError(msg)
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd = close[horizon:] / close[:-horizon] - 1.0
    return np.sign(fwd).astype(np.float64), fwd


def reference_deciles(values: ndarray) -> tuple[float, ...] | None:
    """The 9 internal decile edges of the non-NaN values; None when there is nothing to bin."""
    clean = values[~np.isnan(values)]
    if len(clean) == 0:
        return None
    return tuple(float(q) for q in np.quantile(clean, np.arange(1, 10) / 10.0))


def _require_ascending(bars: list[OHLCV], tf_label: str) -> None:
    """The timestamp join assumes strictly increasing bar timestamps."""
    for prev, cur in pairwise(bars):
        if cur.timestamp <= prev.timestamp:
            msg = (
                f"bars_{tf_label} not strictly increasing at {cur.timestamp.isoformat()} "
                f"(previous {prev.timestamp.isoformat()})"
            )
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Internal: per-timeframe feature builders
# ---------------------------------------------------------------------------


def _technical_indicator_features(
    close: ndarray,
    high: ndarray,
    low: ndarray,
    tf_label: str,
) -> tuple[ndarray, list[str]]:
    """Compute technical indicator features for one timeframe.

    Returns a 2-D array (n_bars, n_features) and the corresponding names.
    """
    n = len(close)
    cols: list[ndarray] = []

    # RSI(14)
    rsi_14 = rsi(close, 14)
    cols.append(rsi_14)

    # MACD histogram
    _, _, macd_hist = macd(close)
    cols.append(macd_hist)

    # Bollinger Band %B
    bb_upper, _bb_mid, bb_lower = bollinger_bands(close, 20, 2.0)
    bb_range = bb_upper - bb_lower
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_pctb = np.where(bb_range != 0, (close - bb_lower) / bb_range, np.nan)
    cols.append(bb_pctb.astype(np.float64))

    # ATR(14) normalised by price
    atr_14 = atr(high, low, close, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_norm = np.where(close != 0, atr_14 / close, np.nan)
    cols.append(atr_norm.astype(np.float64))

    # Lagged values: t-1, t-2, t-3 for RSI and MACD histogram
    for lag in (1, 2, 3):
        lagged_rsi = np.full(n, np.nan, dtype=np.float64)
        if lag < n:
            lagged_rsi[lag:] = rsi_14[:-lag]
        cols.append(lagged_rsi)

        lagged_macd = np.full(n, np.nan, dtype=np.float64)
        if lag < n:
            lagged_macd[lag:] = macd_hist[:-lag]
        cols.append(lagged_macd)

    # Rate of change over 3 periods for RSI and MACD histogram
    rsi_roc = np.full(n, np.nan, dtype=np.float64)
    if n > 3:
        rsi_roc[3:] = rsi_14[3:] - rsi_14[:-3]
    cols.append(rsi_roc)

    macd_roc = np.full(n, np.nan, dtype=np.float64)
    if n > 3:
        macd_roc[3:] = macd_hist[3:] - macd_hist[:-3]
    cols.append(macd_roc)

    return np.column_stack(cols), _technical_indicator_names(tf_label)


def _price_action_features(
    open_arr: ndarray,
    high: ndarray,
    low: ndarray,
    close: ndarray,
    tf_label: str,
) -> tuple[ndarray, list[str]]:
    """Compute price-action features for one timeframe.

    For log returns, the first element is NaN (no prior bar).
    """
    n = len(close)
    cols: list[ndarray] = []

    # Log returns
    lr = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        lr[1:] = _log_returns(close)
    cols.append(lr)

    # Realized volatility (only for 1h and 1d as specified)
    if tf_label in (PRIMARY_TF, "1d"):
        ret = _log_returns(close)
        window = 24 if tf_label == PRIMARY_TF else 7
        rv = _realized_vol(ret, window)
        rv_padded = np.full(n, np.nan, dtype=np.float64)
        if len(rv) > 0:
            rv_padded[1:] = rv
        cols.append(rv_padded)

    # Price vs SMA(20) and SMA(50) ratio
    sma_20 = sma(close, 20)
    sma_50 = sma(close, 50)
    with np.errstate(divide="ignore", invalid="ignore"):
        price_vs_sma20 = np.where(sma_20 != 0, close / sma_20 - 1.0, np.nan)
        price_vs_sma50 = np.where(sma_50 != 0, close / sma_50 - 1.0, np.nan)
    cols.append(price_vs_sma20.astype(np.float64))
    cols.append(price_vs_sma50.astype(np.float64))

    # Candle body ratio: abs(close - open) / (high - low), zero-range guard
    hl_range = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        body_ratio = np.where(hl_range != 0, np.abs(close - open_arr) / hl_range, 0.0)
    cols.append(body_ratio.astype(np.float64))

    # Upper wick ratio: (high - max(open, close)) / (high - low)
    upper_wick = high - np.maximum(open_arr, close)
    with np.errstate(divide="ignore", invalid="ignore"):
        upper_wick_ratio = np.where(hl_range != 0, upper_wick / hl_range, 0.0)
    cols.append(upper_wick_ratio.astype(np.float64))

    # Lower wick ratio: (min(open, close) - low) / (high - low)
    lower_wick = np.minimum(open_arr, close) - low
    with np.errstate(divide="ignore", invalid="ignore"):
        lower_wick_ratio = np.where(hl_range != 0, lower_wick / hl_range, 0.0)
    cols.append(lower_wick_ratio.astype(np.float64))

    return np.column_stack(cols), _price_action_names(tf_label)


def _volume_features(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    volume: ndarray,
    tf_label: str,
) -> tuple[ndarray, list[str]]:
    """Compute volume-based features for one timeframe."""
    n = len(close)
    cols: list[ndarray] = []

    # Volume SMA(20) ratio
    vol_sma = sma(volume, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma != 0, volume / vol_sma, np.nan)
    cols.append(vol_ratio.astype(np.float64))

    # VWAP deviation
    vwap_vals = vwap(high, low, close, volume)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap_dev = np.where(vwap_vals != 0, (close - vwap_vals) / vwap_vals, np.nan)
    cols.append(vwap_dev.astype(np.float64))

    # OBV rate of change over 10 periods
    obv_vals = obv(close, volume)
    obv_roc = np.full(n, np.nan, dtype=np.float64)
    if n > 10:
        with np.errstate(divide="ignore", invalid="ignore"):
            obv_roc[10:] = np.where(
                obv_vals[:-10] != 0,
                (obv_vals[10:] - obv_vals[:-10]) / np.abs(obv_vals[:-10]),
                np.nan,
            )
    cols.append(obv_roc)

    # MFI(14)
    mfi_vals = mfi(high, low, close, volume, 14)
    cols.append(mfi_vals)

    return np.column_stack(cols), _volume_names(tf_label)


def _temporal_features(bars: list[OHLCV]) -> tuple[ndarray, list[str]]:
    """Compute cyclically-encoded temporal features from bar timestamps."""
    hours = np.array([b.timestamp.hour for b in bars], dtype=np.float64)
    dows = np.array([b.timestamp.weekday() for b in bars], dtype=np.float64)

    hour_sin, hour_cos = _sin_cos_encode(hours, 24.0)
    dow_sin, dow_cos = _sin_cos_encode(dows, 7.0)

    cols = np.column_stack([hour_sin, hour_cos, dow_sin, dow_cos])
    return cols.astype(np.float64), _temporal_names()


def _derived_features(
    close_1h: ndarray,
    high_1h: ndarray,
    low_1h: ndarray,
    volume_1h: ndarray,
) -> tuple[ndarray, list[str]]:
    """Compute derived / interaction features from 1h data."""
    n = len(close_1h)
    cols: list[ndarray] = []

    # RSI * Volume ratio (momentum + volume confirmation)
    rsi_14 = rsi(close_1h, 14)
    vol_sma_20 = sma(volume_1h, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma_20 != 0, volume_1h / vol_sma_20, np.nan)
    rsi_vol = rsi_14 * vol_ratio
    cols.append(rsi_vol.astype(np.float64))

    # ATR percentile rank over last 100 bars
    atr_14 = atr(high_1h, low_1h, close_1h, 14)
    atr_pctrank = _percentile_rank(atr_14, 100)
    cols.append(atr_pctrank)

    # Bollinger Band width change (current - previous)
    bb_upper, _bb_mid, bb_lower = bollinger_bands(close_1h, 20, 2.0)
    bb_width = bb_upper - bb_lower
    bb_width_change = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        bb_width_change[1:] = bb_width[1:] - bb_width[:-1]
    cols.append(bb_width_change)

    return np.column_stack(cols), _derived_names()


# ---------------------------------------------------------------------------
# The timestamp join (SM-14: never count-align timeframes)
# ---------------------------------------------------------------------------


def _align_to_primary(
    block: ndarray,
    src_bars: list[OHLCV],
    primary_ts: list[datetime],
    tf_label: str,
) -> ndarray:
    """Timestamp-join a higher-timeframe block onto the 1h grid.

    A 4h/1d bar contributes only to 1h bars that open at or after its close
    (its open time plus one timeframe step), so no 1h row ever sees a
    still-forming higher-timeframe bar — regardless of gaps on either grid.
    """
    n = len(primary_ts)
    result = np.full((n, block.shape[1]), np.nan, dtype=np.float64)
    if not src_bars or n == 0:
        return result
    offset = timeframe_seconds(tf_label)
    cutoff = np.array([t.timestamp() - offset for t in primary_ts])
    src_open = np.array([b.timestamp.timestamp() for b in src_bars])
    idx = np.searchsorted(src_open, cutoff, side="right") - 1
    have = idx >= 0
    result[have] = block[idx[have]]
    return result


# ---------------------------------------------------------------------------
# Public builders (one computation path for training and serving)
# ---------------------------------------------------------------------------


def build_features(
    spec: FeatureSpec,
    bars_1h: list[OHLCV],
    bars_4h: list[OHLCV],
    bars_1d: list[OHLCV],
) -> FeatureMatrix:
    """Build the feature matrix for one set of bars, one row per 1h bar.

    The computed names must match the spec's pinned ``feature_names`` exactly;
    a mismatch means the builder changed without a spec-version bump.
    """
    feature_block, feature_names = _compute_all_features(spec, bars_1h, bars_4h, bars_1d)
    return FeatureMatrix(
        features=feature_block,
        feature_names=feature_names,
        timestamps=[b.timestamp for b in bars_1h],
        spec=spec,
        target=None,
        forward_returns=None,
    )


def build_live_features(
    spec: FeatureSpec,
    bars_1h: list[OHLCV],
    bars_4h: list[OHLCV],
    bars_1d: list[OHLCV],
) -> ndarray:
    """Build the single feature vector for real-time inference.

    Uses the exact same computation path as ``build_features`` and returns the
    last row, guaranteeing training/serving parity.
    """
    feature_block, _ = _compute_all_features(spec, bars_1h, bars_4h, bars_1d)
    live: ndarray = feature_block[-1]
    return live


def _compute_all_features(
    spec: FeatureSpec,
    bars_1h: list[OHLCV],
    bars_4h: list[OHLCV],
    bars_1d: list[OHLCV],
) -> tuple[ndarray, list[str]]:
    """Compute the full feature matrix (n_1h_bars, n_features) and its names."""
    if not bars_1h:
        msg = "bars_1h is empty: nothing to build features from"
        raise ValueError(msg)
    for bars, label in ((bars_1h, PRIMARY_TF), (bars_4h, "4h"), (bars_1d, "1d")):
        _require_ascending(bars, label)

    n = len(bars_1h)
    primary_ts = [b.timestamp for b in bars_1h]
    per_tf = ((PRIMARY_TF, bars_1h), ("4h", bars_4h), ("1d", bars_1d))

    all_blocks: list[ndarray] = []
    all_names: list[str] = []

    def add_block(block: ndarray, names: list[str], label: str, bars: list[OHLCV]) -> None:
        if label == PRIMARY_TF:
            all_blocks.append(block)
        else:
            all_blocks.append(_align_to_primary(block, bars, primary_ts, label))
        all_names.extend(names)

    # ---- 1. Technical indicators (multi-timeframe) ----
    for label, bars in per_tf:
        if not bars:
            names = _technical_indicator_names(label)
            all_blocks.append(np.full((n, len(names)), np.nan))
            all_names.extend(names)
            continue
        _o, h, lo, c, _v = _extract_ohlcv_arrays(bars)
        block, names = _technical_indicator_features(c, h, lo, label)
        add_block(block, names, label, bars)

    # ---- 2. Price action features (multi-timeframe) ----
    for label, bars in per_tf:
        if not bars:
            names = _price_action_names(label)
            all_blocks.append(np.full((n, len(names)), np.nan))
            all_names.extend(names)
            continue
        o, h, lo, c, _v = _extract_ohlcv_arrays(bars)
        block, names = _price_action_features(o, h, lo, c, label)
        add_block(block, names, label, bars)

    # ---- 3. Volume features (1h only, primary timeframe) ----
    _o, h, lo, c, v = _extract_ohlcv_arrays(bars_1h)
    block, names = _volume_features(h, lo, c, v, PRIMARY_TF)
    all_blocks.append(block)
    all_names.extend(names)

    # ---- 4. Temporal features ----
    block, names = _temporal_features(bars_1h)
    all_blocks.append(block)
    all_names.extend(names)

    # ---- 5. Derived / interaction features ----
    _o, h, lo, c, v = _extract_ohlcv_arrays(bars_1h)
    block, names = _derived_features(c, h, lo, v)
    all_blocks.append(block)
    all_names.extend(names)

    if all_names != list(spec.feature_names):
        msg = (
            f"feature spec v{spec.spec_version} pins {len(spec.feature_names)} feature names "
            f"but the builder computes {len(all_names)} differently; bump spec_version"
        )
        raise ValueError(msg)

    return np.hstack(all_blocks).astype(np.float64), all_names
