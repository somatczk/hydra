"""Technical indicator library (TA-Lib where it has the indicator, numpy otherwise).

All functions take numpy arrays and return numpy arrays.
Insufficient data positions are filled with NaN, never raising exceptions.
A function returning a tuple is reachable from rules by its first output only; the other
outputs have their own single-output function (``macd_hist``, ``bollinger_lower``, ...).
"""

from __future__ import annotations

import numpy as np
import talib
from numpy import ndarray

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
    return talib.SMA(data.astype(np.float64), timeperiod=period)


def ema(data: ndarray, period: int) -> ndarray:
    """Exponential Moving Average.

    Uses multiplier ``2 / (period + 1)``.  The first valid value is the SMA
    of the first *period* elements.  Earlier positions are NaN.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    return talib.EMA(data.astype(np.float64), timeperiod=period)


def macd(
    data: ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[ndarray, ndarray, ndarray]:
    """MACD: Moving Average Convergence Divergence.

    Returns ``(macd_line, signal_line, histogram)``; the rule name ``macd`` is the MACD line.
    """
    m, s, h = talib.MACD(
        data.astype(np.float64),
        fastperiod=fast,
        slowperiod=slow,
        signalperiod=signal,
    )
    return m, s, h


def macd_signal(data: ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> ndarray:
    """MACD signal line (EMA of the MACD line)."""
    return macd(data, fast, slow, signal)[1]


def macd_hist(data: ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> ndarray:
    """MACD histogram (MACD line minus signal line)."""
    return macd(data, fast, slow, signal)[2]


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

    if n <= period:
        return st, direction

    # Initialize at first valid ATR index
    start = period
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


def supertrend_direction(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> ndarray:
    """Supertrend direction: +1 bullish, -1 bearish."""
    return supertrend(high, low, close, period, multiplier)[1]


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
    return talib.RSI(data.astype(np.float64), timeperiod=period)


def stochastic(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[ndarray, ndarray]:
    """Fast stochastic (%K, %D), TA-Lib ``STOCHF`` (TradingView's Stoch 14,1,3).

    %K is where the close sits in the ``k_period`` high-low range (0 on a zero range);
    %D is the SMA of %K over ``d_period``.
    """
    k, d = talib.STOCHF(
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        fastk_period=k_period,
        fastd_period=d_period,
    )
    return k, d


def stochastic_d(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    k_period: int = 14,
    d_period: int = 3,
) -> ndarray:
    """Fast stochastic %D (SMA of %K)."""
    return stochastic(high, low, close, k_period, d_period)[1]


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
    """Average True Range, Wilder's (TA-Lib ``ATR``): first value at index ``period``,
    the mean of TR[1..period], then Wilder smoothing.
    """
    return talib.ATR(
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        timeperiod=period,
    )


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


def bollinger_upper(data: ndarray, period: int = 20, std_dev: float = 2.0) -> ndarray:
    """Upper Bollinger Band."""
    upper, _, _ = bollinger_bands(data, period, std_dev)
    return upper


def bollinger_middle(data: ndarray, period: int = 20, std_dev: float = 2.0) -> ndarray:
    """Middle Bollinger Band (SMA)."""
    _, middle, _ = bollinger_bands(data, period, std_dev)
    return middle


def bollinger_lower(data: ndarray, period: int = 20, std_dev: float = 2.0) -> ndarray:
    """Lower Bollinger Band."""
    _, _, lower = bollinger_bands(data, period, std_dev)
    return lower


def rolling_max(data: ndarray, period: int) -> ndarray:
    """Rolling maximum of the *period* values before the current one, window [t-period, t-1].

    The upper Donchian channel on closes: ``close crosses_above`` it on a new high.
    NaN for the first ``period`` elements.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    data_f = data.astype(np.float64)
    n = len(data_f)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(period, n):
        result[i] = np.max(data_f[i - period : i])
    return result


def rolling_min(data: ndarray, period: int) -> ndarray:
    """Rolling minimum of the *period* values before the current one, window [t-period, t-1].

    The lower Donchian channel on closes: ``close crosses_below`` it on a new low.
    NaN for the first ``period`` elements.
    """
    if period <= 0:
        return np.full_like(data, np.nan, dtype=np.float64)
    data_f = data.astype(np.float64)
    n = len(data_f)
    result = np.full(n, np.nan, dtype=np.float64)
    for i in range(period, n):
        result[i] = np.min(data_f[i - period : i])
    return result


def rolling_mid(data: ndarray, period: int) -> ndarray:
    """Rolling midpoint: (rolling_max + rolling_min) / 2, over the same [t-period, t-1] window.

    The Donchian channel midline on closes. NaN for the first ``period`` elements.
    """
    result: ndarray = (rolling_max(data, period) + rolling_min(data, period)) / 2.0
    return result


def close(data: ndarray) -> ndarray:
    """Return close prices (identity — for use as a condition indicator)."""
    return data


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


def keltner_upper(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> ndarray:
    """Upper Keltner Channel (the middle line is ``ema``)."""
    return keltner_channels(high, low, close, ema_period, atr_period, multiplier)[0]


def keltner_lower(
    high: ndarray,
    low: ndarray,
    close: ndarray,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> ndarray:
    """Lower Keltner Channel."""
    return keltner_channels(high, low, close, ema_period, atr_period, multiplier)[2]


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


# ===========================================================================
# Regime and time-series momentum (single output, causal: the value at t reads bars <= t)
# ===========================================================================


def adx(high: ndarray, low: ndarray, close: ndarray, period: int = 14) -> ndarray:
    """Average Directional Index, Wilder's (TA-Lib ``ADX``), trend strength in [0, 100].

    The first value, at index ``2 * period - 1``, is the mean of the first ``period`` DX
    values; then Wilder smoothing. DI is seeded as in ``plus_di``.
    """
    return talib.ADX(
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        timeperiod=period,
    )


def plus_di(high: ndarray, low: ndarray, close: ndarray, period: int = 14) -> ndarray:
    """+DI, Wilder's (TA-Lib ``PLUS_DI``): 100 * smoothed +DM / smoothed TR, in [0, 100].

    First value at index ``period``: the sums of +DM and TR over bars 1..period-1, then one
    Wilder step (``s - s / period + x``) with bar ``period``; Wilder steps after that.
    """
    return talib.PLUS_DI(
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        timeperiod=period,
    )


def minus_di(high: ndarray, low: ndarray, close: ndarray, period: int = 14) -> ndarray:
    """-DI, Wilder's (TA-Lib ``MINUS_DI``), seeded like ``plus_di``, in [0, 100]."""
    return talib.MINUS_DI(
        high.astype(np.float64),
        low.astype(np.float64),
        close.astype(np.float64),
        timeperiod=period,
    )


def roc(data: ndarray, period: int) -> ndarray:
    """Rate of change in percent: ``(data[t] / data[t - period] - 1) * 100`` (TA-Lib ``ROC``).

    NaN for the first ``period`` elements.
    """
    return talib.ROC(data.astype(np.float64), timeperiod=period)


def lag(data: ndarray, period: int) -> ndarray:
    """The close ``period`` bars ago, ``data[t - period]``; NaN for the first ``period``.

    Time-series momentum as a rule: ``close greater_than "lag:period=60"``.
    """
    data_f = data.astype(np.float64)
    result = np.full(len(data_f), np.nan, dtype=np.float64)
    if 0 < period < len(data_f):
        result[period:] = data_f[:-period]
    return result


def bb_width(data: ndarray, period: int = 20, std_dev: float = 2.0) -> ndarray:
    """Bollinger bandwidth in percent of the middle band: ``(upper - lower) / middle * 100``.

    The same bands as ``bollinger_upper``/``bollinger_lower``; NaN for the first ``period - 1``.
    """
    upper, middle, lower = bollinger_bands(data, period, std_dev)
    result: ndarray = (upper - lower) / middle * 100.0
    return result


def atr_pct(high: ndarray, low: ndarray, close: ndarray, period: int = 14) -> ndarray:
    """ATR in percent of the close, ``atr / close * 100`` (TA-Lib's ``NATR``).

    NaN for the first ``period`` elements.
    """
    result: ndarray = atr(high, low, close, period) / close.astype(np.float64) * 100.0
    return result


def rolling_high(high: ndarray, period: int) -> ndarray:
    """Highest high of the *period* bars before the current one, window [t-period, t-1].

    The upper Donchian channel on highs: ``close crosses_above`` it on a breakout.
    NaN for the first ``period`` elements.
    """
    return rolling_max(high, period)


def rolling_low(low: ndarray, period: int) -> ndarray:
    """Lowest low of the *period* bars before the current one, window [t-period, t-1].

    The lower Donchian channel on lows. NaN for the first ``period`` elements.
    """
    return rolling_min(low, period)
