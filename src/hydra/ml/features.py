"""Feature engineering for ML model training and real-time inference.

Builds feature matrices from multi-timeframe OHLCV data and auxiliary sources
(funding rates, fear & greed index). All computations use numpy; no pandas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy import ndarray

from hydra.core.types import OHLCV
from hydra.indicators.custom import funding_rate_sma
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

# ---------------------------------------------------------------------------
# FeatureMatrix dataclass
# ---------------------------------------------------------------------------


@dataclass
class FeatureMatrix:
    """Container for an ML feature matrix with metadata."""

    features: ndarray  # shape (n_samples, n_features)
    feature_names: list[str]  # column names
    timestamps: list[datetime]  # aligned timestamps
    target: ndarray | None  # optional: forward returns for training


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
        return np.log(close[1:] / close[:-1])


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


def build_target(close_1h: ndarray, horizon: int = 1) -> ndarray:
    """Build classification target from forward returns.

    Returns array of length ``len(close_1h) - horizon`` with values +1, -1, or 0.
    """
    fwd = close_1h[horizon:] / close_1h[:-horizon] - 1.0
    return np.sign(fwd).astype(np.float64)


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
    names: list[str] = []

    # RSI(14)
    rsi_14 = rsi(close, 14)
    cols.append(rsi_14)
    names.append(f"rsi_14_{tf_label}")

    # MACD histogram
    _, _, macd_hist = macd(close)
    cols.append(macd_hist)
    names.append(f"macd_hist_{tf_label}")

    # Bollinger Band %B
    bb_upper, _bb_mid, bb_lower = bollinger_bands(close, 20, 2.0)
    bb_range = bb_upper - bb_lower
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_pctb = np.where(bb_range != 0, (close - bb_lower) / bb_range, np.nan)
    cols.append(bb_pctb.astype(np.float64))
    names.append(f"bb_pctb_{tf_label}")

    # ATR(14) normalised by price
    atr_14 = atr(high, low, close, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_norm = np.where(close != 0, atr_14 / close, np.nan)
    cols.append(atr_norm.astype(np.float64))
    names.append(f"atr_norm_{tf_label}")

    # Lagged values: t-1, t-2, t-3 for RSI and MACD histogram
    for lag in (1, 2, 3):
        lagged_rsi = np.full(n, np.nan, dtype=np.float64)
        if lag < n:
            lagged_rsi[lag:] = rsi_14[:-lag]
        cols.append(lagged_rsi)
        names.append(f"rsi_14_{tf_label}_lag{lag}")

        lagged_macd = np.full(n, np.nan, dtype=np.float64)
        if lag < n:
            lagged_macd[lag:] = macd_hist[:-lag]
        cols.append(lagged_macd)
        names.append(f"macd_hist_{tf_label}_lag{lag}")

    # Rate of change over 3 periods for RSI and MACD histogram
    rsi_roc = np.full(n, np.nan, dtype=np.float64)
    if n > 3:
        rsi_roc[3:] = rsi_14[3:] - rsi_14[:-3]
    cols.append(rsi_roc)
    names.append(f"rsi_14_{tf_label}_roc3")

    macd_roc = np.full(n, np.nan, dtype=np.float64)
    if n > 3:
        macd_roc[3:] = macd_hist[3:] - macd_hist[:-3]
    cols.append(macd_roc)
    names.append(f"macd_hist_{tf_label}_roc3")

    return np.column_stack(cols), names


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
    names: list[str] = []

    # Log returns
    lr = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        lr[1:] = _log_returns(close)
    cols.append(lr)
    names.append(f"log_return_{tf_label}")

    # Realized volatility (only for 1h and 1d as specified)
    if tf_label == "1h":
        ret = _log_returns(close)
        rv_24 = _realized_vol(ret, 24)
        rv_padded = np.full(n, np.nan, dtype=np.float64)
        if len(rv_24) > 0:
            rv_padded[1:] = rv_24
        cols.append(rv_padded)
        names.append("realized_vol_24h")
    elif tf_label == "1d":
        ret = _log_returns(close)
        rv_7 = _realized_vol(ret, 7)
        rv_padded = np.full(n, np.nan, dtype=np.float64)
        if len(rv_7) > 0:
            rv_padded[1:] = rv_7
        cols.append(rv_padded)
        names.append("realized_vol_7d")

    # Price vs SMA(20) and SMA(50) ratio
    sma_20 = sma(close, 20)
    sma_50 = sma(close, 50)
    with np.errstate(divide="ignore", invalid="ignore"):
        price_vs_sma20 = np.where(sma_20 != 0, close / sma_20 - 1.0, np.nan)
        price_vs_sma50 = np.where(sma_50 != 0, close / sma_50 - 1.0, np.nan)
    cols.append(price_vs_sma20.astype(np.float64))
    names.append(f"price_vs_sma20_{tf_label}")
    cols.append(price_vs_sma50.astype(np.float64))
    names.append(f"price_vs_sma50_{tf_label}")

    # Candle body ratio: abs(close - open) / (high - low), zero-range guard
    hl_range = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        body_ratio = np.where(hl_range != 0, np.abs(close - open_arr) / hl_range, 0.0)
    cols.append(body_ratio.astype(np.float64))
    names.append(f"candle_body_ratio_{tf_label}")

    # Upper wick ratio: (high - max(open, close)) / (high - low)
    upper_wick = high - np.maximum(open_arr, close)
    with np.errstate(divide="ignore", invalid="ignore"):
        upper_wick_ratio = np.where(hl_range != 0, upper_wick / hl_range, 0.0)
    cols.append(upper_wick_ratio.astype(np.float64))
    names.append(f"upper_wick_ratio_{tf_label}")

    # Lower wick ratio: (min(open, close) - low) / (high - low)
    lower_wick = np.minimum(open_arr, close) - low
    with np.errstate(divide="ignore", invalid="ignore"):
        lower_wick_ratio = np.where(hl_range != 0, lower_wick / hl_range, 0.0)
    cols.append(lower_wick_ratio.astype(np.float64))
    names.append(f"lower_wick_ratio_{tf_label}")

    return np.column_stack(cols), names


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
    names: list[str] = []

    # Volume SMA(20) ratio
    vol_sma = sma(volume, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma != 0, volume / vol_sma, np.nan)
    cols.append(vol_ratio.astype(np.float64))
    names.append(f"vol_sma20_ratio_{tf_label}")

    # VWAP deviation
    vwap_vals = vwap(high, low, close, volume)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap_dev = np.where(vwap_vals != 0, (close - vwap_vals) / vwap_vals, np.nan)
    cols.append(vwap_dev.astype(np.float64))
    names.append(f"vwap_dev_{tf_label}")

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
    names.append(f"obv_roc10_{tf_label}")

    # MFI(14)
    mfi_vals = mfi(high, low, close, volume, 14)
    cols.append(mfi_vals)
    names.append(f"mfi_14_{tf_label}")

    return np.column_stack(cols), names


def _crypto_features(
    n: int,
    funding_rates: list[float] | None,
    fear_greed_index: list[float] | None,
) -> tuple[ndarray, list[str]]:
    """Compute crypto-specific features."""
    cols: list[ndarray] = []
    names: list[str] = []

    # Funding rate (current)
    if funding_rates is not None and len(funding_rates) > 0:
        fr = np.array(funding_rates, dtype=np.float64)
        # Align to length n: right-align, pad with NaN
        fr_aligned = np.full(n, np.nan, dtype=np.float64)
        take = min(len(fr), n)
        fr_aligned[n - take :] = fr[len(fr) - take :]
        cols.append(fr_aligned)
        names.append("funding_rate")

        # Funding rate SMA(24)
        fr_sma = funding_rate_sma(fr, 24)
        fr_sma_aligned = np.full(n, np.nan, dtype=np.float64)
        fr_sma_aligned[n - take :] = fr_sma[len(fr_sma) - take :]
        cols.append(fr_sma_aligned)
        names.append("funding_rate_sma24")
    else:
        cols.append(np.full(n, np.nan, dtype=np.float64))
        names.append("funding_rate")
        cols.append(np.full(n, np.nan, dtype=np.float64))
        names.append("funding_rate_sma24")

    # Fear & Greed Index normalised to [0, 1]
    if fear_greed_index is not None and len(fear_greed_index) > 0:
        fg = np.array(fear_greed_index, dtype=np.float64) / 100.0
        fg_aligned = np.full(n, np.nan, dtype=np.float64)
        take = min(len(fg), n)
        fg_aligned[n - take :] = fg[len(fg) - take :]
        cols.append(fg_aligned)
        names.append("fear_greed_norm")
    else:
        cols.append(np.full(n, np.nan, dtype=np.float64))
        names.append("fear_greed_norm")

    return np.column_stack(cols), names


def _temporal_features(bars: list[OHLCV]) -> tuple[ndarray, list[str]]:
    """Compute cyclically-encoded temporal features from bar timestamps."""
    hours = np.array([b.timestamp.hour for b in bars], dtype=np.float64)
    dows = np.array([b.timestamp.weekday() for b in bars], dtype=np.float64)

    hour_sin, hour_cos = _sin_cos_encode(hours, 24.0)
    dow_sin, dow_cos = _sin_cos_encode(dows, 7.0)

    cols = np.column_stack([hour_sin, hour_cos, dow_sin, dow_cos])
    names = ["hour_sin", "hour_cos", "dow_sin", "dow_cos"]
    # Ensure float64
    return cols.astype(np.float64), names


def _derived_features(
    close_1h: ndarray,
    high_1h: ndarray,
    low_1h: ndarray,
    volume_1h: ndarray,
) -> tuple[ndarray, list[str]]:
    """Compute derived / interaction features from 1h data."""
    n = len(close_1h)
    cols: list[ndarray] = []
    names: list[str] = []

    # RSI * Volume ratio (momentum + volume confirmation)
    rsi_14 = rsi(close_1h, 14)
    vol_sma_20 = sma(volume_1h, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = np.where(vol_sma_20 != 0, volume_1h / vol_sma_20, np.nan)
    rsi_vol = rsi_14 * vol_ratio
    cols.append(rsi_vol.astype(np.float64))
    names.append("rsi_vol_ratio")

    # ATR percentile rank over last 100 bars
    atr_14 = atr(high_1h, low_1h, close_1h, 14)
    atr_pctrank = _percentile_rank(atr_14, 100)
    cols.append(atr_pctrank)
    names.append("atr_pctile_rank_100")

    # Bollinger Band width change (current - previous)
    bb_upper, _bb_mid, bb_lower = bollinger_bands(close_1h, 20, 2.0)
    bb_width = bb_upper - bb_lower
    bb_width_change = np.full(n, np.nan, dtype=np.float64)
    if n > 1:
        bb_width_change[1:] = bb_width[1:] - bb_width[:-1]
    cols.append(bb_width_change)
    names.append("bb_width_change")

    return np.column_stack(cols), names


# ---------------------------------------------------------------------------
# FeatureEngineering class
# ---------------------------------------------------------------------------


class FeatureEngineering:
    """Build feature matrices for ML model training and inference."""

    def build_features(
        self,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
        funding_rates: list[float] | None = None,
        fear_greed_index: list[float] | None = None,
    ) -> FeatureMatrix:
        """Build complete feature matrix for training from historical data.

        The primary timeframe is 1h. Higher timeframes (4h, 1d) are
        resampled / aligned to the 1h bar count using forward-fill so that
        every row represents one 1h bar.
        """
        feature_block, feature_names = self._compute_all_features(
            bars_1h, bars_4h, bars_1d, funding_rates, fear_greed_index
        )
        timestamps = [b.timestamp for b in bars_1h]
        return FeatureMatrix(
            features=feature_block,
            feature_names=feature_names,
            timestamps=timestamps,
            target=None,
        )

    def build_live_features(
        self,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
        funding_rates: list[float] | None = None,
        fear_greed_index: list[float] | None = None,
    ) -> ndarray:
        """Build single feature vector for real-time inference.

        Uses the exact same computation path as ``build_features`` and
        returns the last row, guaranteeing consistency.
        """
        feature_block, _ = self._compute_all_features(
            bars_1h, bars_4h, bars_1d, funding_rates, fear_greed_index
        )
        return feature_block[-1]

    # ------------------------------------------------------------------
    # Private: single computation path shared by both public methods
    # ------------------------------------------------------------------

    def _compute_all_features(
        self,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
        funding_rates: list[float] | None,
        fear_greed_index: list[float] | None,
    ) -> tuple[ndarray, list[str]]:
        """Compute the full feature matrix (n_1h_bars, n_features).

        Higher timeframe features are computed on their native resolution
        and then right-aligned / forward-filled to 1h length.
        """
        n = len(bars_1h)

        all_blocks: list[ndarray] = []
        all_names: list[str] = []

        # ---- 1. Technical indicators (multi-timeframe) ----
        for bars, tf_label, ratio in [
            (bars_1h, "1h", 1),
            (bars_4h, "4h", 4),
            (bars_1d, "1d", 24),
        ]:
            _o, h, lo, c, _v = _extract_ohlcv_arrays(bars)
            block, names = _technical_indicator_features(c, h, lo, tf_label)
            aligned = self._align_to_1h(block, len(bars), n, ratio)
            all_blocks.append(aligned)
            all_names.extend(names)

        # ---- 2. Price action features (multi-timeframe) ----
        for bars, tf_label, ratio in [
            (bars_1h, "1h", 1),
            (bars_4h, "4h", 4),
            (bars_1d, "1d", 24),
        ]:
            o, h, lo, c, _v = _extract_ohlcv_arrays(bars)
            block, names = _price_action_features(o, h, lo, c, tf_label)
            aligned = self._align_to_1h(block, len(bars), n, ratio)
            all_blocks.append(aligned)
            all_names.extend(names)

        # ---- 3. Volume features (1h only, primary timeframe) ----
        _o, h, lo, c, v = _extract_ohlcv_arrays(bars_1h)
        block, names = _volume_features(h, lo, c, v, "1h")
        all_blocks.append(block)
        all_names.extend(names)

        # ---- 4. Crypto-specific features ----
        block, names = _crypto_features(n, funding_rates, fear_greed_index)
        all_blocks.append(block)
        all_names.extend(names)

        # ---- 5. Temporal features ----
        block, names = _temporal_features(bars_1h)
        all_blocks.append(block)
        all_names.extend(names)

        # ---- 6. Derived / interaction features ----
        _o, h, lo, c, v = _extract_ohlcv_arrays(bars_1h)
        block, names = _derived_features(c, h, lo, v)
        all_blocks.append(block)
        all_names.extend(names)

        feature_matrix = np.hstack(all_blocks).astype(np.float64)
        return feature_matrix, all_names

    @staticmethod
    def _align_to_1h(
        block: ndarray,
        src_len: int,
        target_len: int,
        ratio: int,
    ) -> ndarray:
        """Right-align a higher-timeframe feature block to 1h length.

        Each higher-TF bar is forward-filled for *ratio* 1h bars.
        If ratio == 1 the block is returned as-is (already 1h).
        """
        if ratio == 1:
            return block

        n_features = block.shape[1] if block.ndim == 2 else 1
        result = np.full((target_len, n_features), np.nan, dtype=np.float64)

        # Forward-fill: repeat each source row `ratio` times, right-aligned
        expanded_len = src_len * ratio
        if expanded_len >= target_len:
            # More data than needed; take the tail
            expanded = np.repeat(block, ratio, axis=0)
            result[:] = expanded[expanded_len - target_len :]
        else:
            # Less data; fill from the end
            expanded = np.repeat(block, ratio, axis=0)
            result[target_len - expanded_len :] = expanded

        return result
