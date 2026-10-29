"""The vectorized indicators equal their loop originals bit for bit.

The ``_ref_*`` functions are the loop implementations as they were before vectorizing, copied
verbatim. Equality is exact: values, NaN positions and the sign of zeros.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
from numpy import ndarray

from hydra.indicators.library import (
    atr,
    bollinger_bands,
    cci,
    ichimoku,
    mfi,
    obv,
    rolling_max,
    rolling_mid,
    rolling_min,
    sma,
    supertrend,
    williams_r,
)


def _ref_supertrend(
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


def _ref_ichimoku(
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


def _ref_cci(
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


def _ref_williams_r(
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


def _ref_bollinger_bands(
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


def _ref_rolling_max(data: ndarray, period: int) -> ndarray:
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


def _ref_rolling_min(data: ndarray, period: int) -> ndarray:
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


def _ref_obv(close: ndarray, volume: ndarray) -> ndarray:
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


def _ref_mfi(
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


KINDS = ["walk", "rounded", "constant", "flat_runs", "nan"]


def _series(n: int, kind: str) -> tuple[ndarray, ndarray, ndarray, ndarray]:
    rng = np.random.default_rng([n, KINDS.index(kind)])
    close = 40000 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    high = close + np.abs(rng.normal(0, 400, n))
    low = close - np.abs(rng.normal(0, 400, n))
    volume = np.abs(rng.normal(500, 200, n))
    if kind == "rounded":  # ties in comparisons and in rolling extremes
        close, high, low = np.round(close, -3), np.round(high, -3), np.round(low, -3)
    elif kind == "constant":  # zero std, zero MAD, hh == ll
        close[:], high[:], low[:] = 100.0, 100.0, 100.0
    elif kind == "flat_runs" and n > 30:
        close[5:12] = close[4]
        high[5:30], low[5:30] = high[4], low[4]
        volume[3] = 0.0
    elif kind == "nan":
        for arr in (close, high, low, volume):
            if n:
                arr[rng.integers(0, n, n // 15 + 1)] = np.nan
    return high, low, close, volume


def _same(got: object, want: object) -> None:
    got_t = got if isinstance(got, tuple) else (got,)
    want_t = want if isinstance(want, tuple) else (want,)
    assert len(got_t) == len(want_t)
    for g, w in zip(got_t, want_t, strict=True):
        assert isinstance(g, ndarray)
        assert g.dtype == np.float64
        assert g.shape == w.shape
        assert np.array_equal(g, w, equal_nan=True)
        known = ~np.isnan(w)
        assert np.array_equal(np.signbit(g[known]), np.signbit(w[known]))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("period", [1, 2, 3, 8, 9, 14, 20, 128, 200])
def test_matches_the_loop_reference(kind: str, period: int) -> None:
    for n in (0, 1, 2, period - 1, period, period + 1, 250):
        h, lo, c, v = _series(n, kind)
        _same(bollinger_bands(c, period, 2.0), _ref_bollinger_bands(c, period, 2.0))
        _same(rolling_max(c, period), _ref_rolling_max(c, period))
        _same(rolling_min(c, period), _ref_rolling_min(c, period))
        _same(
            rolling_mid(c, period),
            (_ref_rolling_max(c, period) + _ref_rolling_min(c, period)) / 2.0,
        )
        _same(williams_r(h, lo, c, period), _ref_williams_r(h, lo, c, period))
        _same(cci(h, lo, c, period), _ref_cci(h, lo, c, period))
        _same(mfi(h, lo, c, v, period), _ref_mfi(h, lo, c, v, period))
        _same(obv(c, v), _ref_obv(c, v))
        _same(supertrend(h, lo, c, period, 3.0), _ref_supertrend(h, lo, c, period, 3.0))
        new_ichi = ichimoku(h, lo, c, period, period + 1, 2 * period)
        ref_ichi = _ref_ichimoku(h, lo, c, period, period + 1, 2 * period)
        assert new_ichi.keys() == ref_ichi.keys()
        for key in ref_ichi:
            _same(new_ichi[key], ref_ichi[key])


def test_the_reference_values_are_not_all_nan() -> None:
    h, lo, c, v = _series(250, "walk")
    for out in (
        bollinger_bands(c, 20)[2],
        rolling_max(c, 20),
        williams_r(h, lo, c, 14),
        cci(h, lo, c, 20),
        mfi(h, lo, c, v, 14),
        obv(c, v),
        supertrend(h, lo, c, 10)[0],
        ichimoku(h, lo, c)["senkou_span_b"],
    ):
        assert np.isfinite(out).sum() > 150


def test_period_zero_matches_the_loop_reference() -> None:
    h, lo, c, v = _series(30, "walk")
    with warnings.catch_warnings(record=True) as ref_warnings:
        warnings.simplefilter("always")
        ref_bb, ref_cci = _ref_bollinger_bands(c, 0), _ref_cci(h, lo, c, 0)
    assert ref_warnings  # the loops warned on their empty windows; the vectorized code does not
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _same(bollinger_bands(c, 0), ref_bb)
        _same(cci(h, lo, c, 0), ref_cci)
    _same(rolling_max(c, 0), _ref_rolling_max(c, 0))
    _same(rolling_min(c, 0), _ref_rolling_min(c, 0))
    _same(mfi(h, lo, c, v, 0), _ref_mfi(h, lo, c, v, 0))
    for new, ref in ((williams_r, _ref_williams_r), (ichimoku, _ref_ichimoku)):
        with pytest.raises(ValueError):
            ref(h, lo, c, 0)
        with pytest.raises(ValueError):
            new(h, lo, c, 0)
