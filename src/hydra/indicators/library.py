"""Full technical indicator library (numpy-based, TA-Lib optional).

All functions take numpy arrays and return numpy arrays.
Insufficient data positions are filled with NaN, never raising exceptions.
"""

from __future__ import annotations

import numpy as np
from numpy import ndarray

# ---------------------------------------------------------------------------
# Optional TA-Lib import
# ---------------------------------------------------------------------------

try:
    import talib

    _HAS_TALIB = True
except ImportError:
    _HAS_TALIB = False


# ===========================================================================
# Trend indicators
# ===========================================================================


def sma(data: ndarray, period: int) -> ndarray:
    """Simple Moving Average.

    Returns an array of the same length as *data*, with NaN for the first
    ``period - 1`` elements where the window is incomplete.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    if _HAS_TALIB:
        return talib.SMA(data.astype(np.float64), timeperiod=period)
    result = np.full(len(data), np.nan, dtype=np.float64)
    if len(data) < period:
        return result
    cumsum = np.cumsum(data, dtype=np.float64)
    cumsum[period:] = cumsum[period:] - cumsum[:-period]
    result[period - 1 :] = cumsum[period - 1 :] / period
    return result


def ema(data: ndarray, period: int) -> ndarray:
    """Exponential Moving Average.

    Uses multiplier ``2 / (period + 1)``.  The first valid value is the SMA
    of the first *period* elements.  Earlier positions are NaN.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    if _HAS_TALIB:
        return talib.EMA(data.astype(np.float64), timeperiod=period)
    data = data.astype(np.float64)
    result = np.full(len(data), np.nan, dtype=np.float64)
    if len(data) < period:
        return result
    multiplier = 2.0 / (period + 1)
    # Seed with SMA
    result[period - 1] = np.mean(data[:period])
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def macd(
    data: ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[ndarray, ndarray, ndarray]:
    """MACD: Moving Average Convergence Divergence.

    Returns ``(macd_line, signal_line, histogram)``.
    """
    if _HAS_TALIB:
        m, s, h = talib.MACD(
            data.astype(np.float64),
            fastperiod=fast,
            slowperiod=slow,
            signalperiod=signal,
        )
        return m, s, h
    fast_ema = ema(data, fast)
    slow_ema = ema(data, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line[~np.isnan(macd_line)], signal)
    # Pad signal_line to same length as macd_line
    full_signal = np.full(len(data), np.nan, dtype=np.float64)
    # Find first valid macd index
    first_valid = int(np.argmax(~np.isnan(macd_line)))
    valid_count = len(macd_line) - first_valid
    if len(signal_line) == valid_count:
        full_signal[first_valid:] = signal_line
    histogram = macd_line - full_signal
    return macd_line, full_signal, histogram


def supertrend(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[ndarray, ndarray]:
    """Supertrend indicator.

    Returns ``(supertrend_line, direction)`` where direction is +1 (up/bull)
    or -1 (down/bear).
    """
    high = high.astype(np.float64)
    low = low.astype(np.float64)
    close = close.astype(np.float64)
    n = len(close)

    atr_vals = atr(high, low, close, period)

    mid = (high + low) / 2.0
    upper_band = mid + multiplier * atr_vals
    lower_band = mid - multiplier * atr_vals

    st = np.full(n, np.nan, dtype=np.float64)
    direction = np.full(n, np.nan, dtype=np.float64)

    if n < period:
        return st, direction

    # Initialize at first valid ATR index
    start = period - 1
    st[start] = upper_band[start]
    direction[start] = -1.0

    for i in range(start + 1, n):
        if np.isnan(atr_vals[i]):
            continue
        # Adjust bands
        if not (lower_band[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]):
            lower_band[i] = lower_band[i - 1]

        if not (upper_band[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]):
            upper_band[i] = upper_band[i - 1]

        if direction[i - 1] == 1.0:
            if close[i] < lower_band[i]:
                direction[i] = -1.0
                st[i] = upper_band[i]
            else:
                direction[i] = 1.0
                st[i] = lower_band[i]
        else:
            if close[i] > upper_band[i]:
                direction[i] = 1.0
                st[i] = lower_band[i]
            else:
                direction[i] = -1.0
                st[i] = upper_band[i]

    return st, direction


def ichimoku(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> dict[str, ndarray]:
    """Ichimoku Cloud.

    Returns dict with keys: tenkan_sen, kijun_sen, senkou_span_a,
    senkou_span_b, chikou_span.
    """
    high = high.astype(np.float64)
    low = low.astype(np.float64)
    close = close.astype(np.float64)
    n = len(close)

    def _midline(h: ndarray, lo: ndarray, p: int) -> ndarray:
        result = np.full(n, np.nan, dtype=np.float64)
        for i in range(p - 1, n):
            window_h = h[i - p + 1 : i + 1]
            window_l = lo[i - p + 1 : i + 1]
            result[i] = (np.max(window_h) + np.min(window_l)) / 2.0
        return result

    tenkan_sen = _midline(high, low, tenkan)
    kijun_sen = _midline(high, low, kijun)

    # Senkou Span A: average of tenkan and kijun, shifted forward by kijun periods
    senkou_span_a = np.full(n, np.nan, dtype=np.float64)
    avg = (tenkan_sen + kijun_sen) / 2.0
    shift = kijun
    if n > shift:
        senkou_span_a[shift:] = avg[: n - shift]

    # Senkou Span B: midline of senkou_b period, shifted forward by kijun periods
    senkou_span_b_raw = _midline(high, low, senkou_b)
    senkou_span_b_arr = np.full(n, np.nan, dtype=np.float64)
    if n > shift:
        senkou_span_b_arr[shift:] = senkou_span_b_raw[: n - shift]

    # Chikou Span: close shifted back by kijun periods
    chikou_span = np.full(n, np.nan, dtype=np.float64)
    if n > shift:
        chikou_span[: n - shift] = close[shift:]

    return {
        "tenkan_sen": tenkan_sen,
        "kijun_sen": kijun_sen,
        "senkou_span_a": senkou_span_a,
        "senkou_span_b": senkou_span_b_arr,
        "chikou_span": chikou_span,
    }


# ===========================================================================
# Momentum indicators
# ===========================================================================


def rsi(data: ndarray, period: int = 14) -> ndarray:
    """Relative Strength Index (Wilder's smoothing).

    Returns values in [0, 100]. NaN for insufficient data.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    if _HAS_TALIB:
        return talib.RSI(data.astype(np.float64), timeperiod=period)
    data = data.astype(np.float64)
    n = len(data)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return result

    deltas = np.diff(data)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - 100.0 / (1.0 + rs)

    return result


def stochastic(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[ndarray, ndarray]:
    """Stochastic Oscillator (%K, %D).

    %K measures where the close is relative to the period's range.
    %D is the SMA of %K.
    """
    high = high.astype(np.float64)
    low = low.astype(np.float64)
    close = close.astype(np.float64)
    n = len(close)
    k = np.full(n, np.nan, dtype=np.float64)

    for i in range(k_period - 1, n):
        hh = np.max(high[i - k_period + 1 : i + 1])
        ll = np.min(low[i - k_period + 1 : i + 1])
        if hh == ll:
            k[i] = 50.0  # no range
        else:
            k[i] = 100.0 * (close[i] - ll) / (hh - ll)

    d = sma(k, d_period)
    return k, d


def cci(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    period: int = 20,
) -> ndarray:
    """Commodity Channel Index."""
    tp = (high.astype(np.float64) + low.astype(np.float64) + close.astype(np.float64)) / 3.0
    n = len(tp)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(period - 1, n):
        window = tp[i - period + 1 : i + 1]
        mean = np.mean(window)
        mad = np.mean(np.abs(window - mean))
        if mad == 0:
            result[i] = 0.0
        else:
            result[i] = (tp[i] - mean) / (0.015 * mad)
    return result


def williams_r(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    period: int = 14,
) -> ndarray:
    """Williams %R. Returns values in [-100, 0]."""
    high = high.astype(np.float64)
    low = low.astype(np.float64)
    close = close.astype(np.float64)
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(period - 1, n):
        hh = np.max(high[i - period + 1 : i + 1])
        ll = np.min(low[i - period + 1 : i + 1])
        if hh == ll:
            result[i] = -50.0
        else:
            result[i] = -100.0 * (hh - close[i]) / (hh - ll)
    return result


# ===========================================================================
# Volatility indicators
# ===========================================================================


def atr(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    period: int = 14,
) -> ndarray:
    """Average True Range (Wilder smoothing)."""
    high = high.astype(np.float64)
    low = low.astype(np.float64)
    close = close.astype(np.float64)
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < 2:
        return result

    tr = np.full(n, np.nan, dtype=np.float64)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    if n < period:
        return result

    result[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period

    return result


def bollinger_bands(
    data: ndarray,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[ndarray, ndarray, ndarray]:
    """Bollinger Bands: upper, middle, lower."""
    middle = sma(data, period)
    data_f = data.astype(np.float64)
    n = len(data_f)
    std = np.full(n, np.nan, dtype=np.float64)
    for i in range(period - 1, n):
        std[i] = np.std(data_f[i - period + 1 : i + 1], ddof=0)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def keltner_channels(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> tuple[ndarray, ndarray, ndarray]:
    """Keltner Channels: upper, middle, lower."""
    middle = ema(close, ema_period)
    atr_vals = atr(high, low, close, atr_period)
    upper = middle + multiplier * atr_vals
    lower = middle - multiplier * atr_vals
    return upper, middle, lower


# ===========================================================================
# Volume indicators
# ===========================================================================


def obv(close: ndarray, volume: ndarray) -> ndarray:
    """On Balance Volume."""
    close = close.astype(np.float64)
    volume = volume.astype(np.float64)
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return result
    result[0] = volume[0]
    for i in range(1, n):
        if close[i] > close[i - 1]:
            result[i] = result[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            result[i] = result[i - 1] - volume[i]
        else:
            result[i] = result[i - 1]
    return result


def vwap(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    volume: ndarray,
) -> ndarray:
    """Volume Weighted Average Price (intraday, cumulative)."""
    tp = (high.astype(np.float64) + low.astype(np.float64) + close.astype(np.float64)) / 3.0
    vol = volume.astype(np.float64)
    cum_tp_vol = np.cumsum(tp * vol)
    cum_vol = np.cumsum(vol)
    result = np.where(cum_vol != 0, cum_tp_vol / cum_vol, np.nan)
    return result.astype(np.float64)


def mfi(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    volume: ndarray,
    period: int = 14,
) -> ndarray:
    """Money Flow Index. Returns values in [0, 100]."""
    tp = (high.astype(np.float64) + low.astype(np.float64) + close.astype(np.float64)) / 3.0
    raw_mf = tp * volume.astype(np.float64)
    n = len(tp)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return result

    for i in range(period, n):
        pos_flow = 0.0
        neg_flow = 0.0
        for j in range(i - period + 1, i + 1):
            if tp[j] > tp[j - 1]:
                pos_flow += raw_mf[j]
            elif tp[j] < tp[j - 1]:
                neg_flow += raw_mf[j]
        if neg_flow == 0:
            result[i] = 100.0
        else:
            ratio = pos_flow / neg_flow
            result[i] = 100.0 - 100.0 / (1.0 + ratio)

    return result
