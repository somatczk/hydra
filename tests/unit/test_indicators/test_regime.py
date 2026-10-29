"""Regime and time-series momentum indicators: ADX/DI, ROC, lag, bandwidth, ATR %, Donchian."""

from __future__ import annotations

import inspect

import numpy as np
import pytest
import talib

from hydra.indicators import library as ind_lib
from hydra.indicators.library import (
    adx,
    atr_pct,
    bb_width,
    lag,
    minus_di,
    plus_di,
    roc,
    rolling_high,
    rolling_low,
)

nan = np.nan
# (name, params, warm-up NaN prefix length)
NEW = [
    ("adx", {"period": 14}, 27),
    ("plus_di", {"period": 14}, 14),
    ("minus_di", {"period": 14}, 14),
    ("roc", {"period": 10}, 10),
    ("lag", {"period": 60}, 60),
    ("bb_width", {"period": 20, "std_dev": 2.0}, 19),
    ("atr_pct", {"period": 14}, 14),
    ("rolling_high", {"period": 20}, 20),
    ("rolling_low", {"period": 20}, 20),
]


def _walk(n: int, seed: int = 7) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.standard_normal(n))
    return {
        "data": close,
        "close": close,
        "high": close + rng.uniform(0.1, 2.0, n),
        "low": close - rng.uniform(0.1, 2.0, n),
    }


def _call(name: str, arrays: dict[str, np.ndarray], params: dict[str, float]) -> np.ndarray:
    func = getattr(ind_lib, name)
    data = {p: arrays[p] for p in inspect.signature(func).parameters if p in arrays}
    out: np.ndarray = func(**data, **params)
    return out


def test_dmi_matches_wilder_exact() -> None:
    """TA-Lib's Wilder seed: DI sums bars 1..p-1 then one Wilder step; ADX averages p DX."""
    high = np.array([10, 12, 13, 12, 15, 16, 15, 17], dtype=np.float64)
    low = np.array([8, 9, 11, 10, 12, 14, 13, 15], dtype=np.float64)
    close = np.array([9, 11, 12, 11, 14, 15, 14, 16], dtype=np.float64)
    pdi = [nan, nan, nan, 75 / 2, 975 / 17, 1050 / 19, 10500 / 271, 90600 / 1813]
    mdi = [nan, nan, nan, 75 / 4, 150 / 17, 120 / 19, 5250 / 271, 3000 / 259]
    adx_ = [nan, nan, nan, nan, nan, 2420 / 39, 6140 / 117, 606880 / 10881]
    np.testing.assert_allclose(plus_di(high, low, close, 3), pdi, rtol=1e-12)
    np.testing.assert_allclose(minus_di(high, low, close, 3), mdi, rtol=1e-12)
    np.testing.assert_allclose(adx(high, low, close, 3), adx_, rtol=1e-12)


def test_dmi_on_a_clean_trend() -> None:
    """Every bar a +1 higher high and higher low with TR 2: +DI 50, -DI 0, ADX 100."""
    close = np.arange(100, 140, dtype=np.float64)
    high, low = close + 1, close - 1
    np.testing.assert_allclose(plus_di(high, low, close, 5)[5:], 50.0)
    np.testing.assert_allclose(minus_di(high, low, close, 5)[5:], 0.0)
    np.testing.assert_allclose(adx(high, low, close, 5)[9:], 100.0)
    np.testing.assert_allclose(minus_di(high[::-1], low[::-1], close[::-1], 5)[5:], 50.0)


def test_roc_and_lag_exact() -> None:
    data = np.array([100, 110, 99, 121], dtype=np.float64)
    np.testing.assert_allclose(roc(data, 2), [nan, nan, -1.0, 10.0], rtol=1e-12)
    np.testing.assert_array_equal(lag(data, 2), [nan, nan, 100, 110])
    np.testing.assert_array_equal(lag(data, 4), [nan] * 4)


def test_bb_width_and_atr_pct_exact() -> None:
    np.testing.assert_allclose(
        bb_width(np.array([1, 2, 3], dtype=np.float64), 3, 2.0),
        [nan, nan, 200 * np.sqrt(2 / 3)],
        rtol=1e-12,
    )
    high = np.array([10, 12, 13, 12, 15, 16, 15, 17], dtype=np.float64)
    low = np.array([8, 9, 11, 10, 12, 14, 13, 15], dtype=np.float64)
    close = np.array([9, 11, 12, 11, 14, 15, 14, 16], dtype=np.float64)
    result = atr_pct(high, low, close, 3)
    assert result[3] == pytest.approx(7 / 3 / 11 * 100, rel=1e-12)
    np.testing.assert_allclose(result, talib.NATR(high, low, close, 3), rtol=1e-12)


def test_donchian_on_highs_and_lows_excludes_the_current_bar() -> None:
    high = np.array([1, 3, 2, 5, 4], dtype=np.float64)
    low = np.array([0, 2, 1, 4, 3], dtype=np.float64)
    np.testing.assert_array_equal(rolling_high(high, 2), [nan, nan, 3, 3, 5])
    np.testing.assert_array_equal(rolling_low(low, 2), [nan, nan, 0, 1, 1])


@pytest.mark.parametrize(("name", "params", "warmup"), NEW, ids=[n for n, _, _ in NEW])
def test_warmup_is_a_nan_prefix(name: str, params: dict[str, float], warmup: int) -> None:
    result = _call(name, _walk(150), params)
    assert result.shape == (150,)
    assert result.dtype == np.float64
    assert np.isnan(result[:warmup]).all()
    assert np.isfinite(result[warmup:]).all()


@pytest.mark.parametrize(("name", "params", "warmup"), NEW, ids=[n for n, _, _ in NEW])
def test_no_lookahead(name: str, params: dict[str, float], warmup: int) -> None:
    """Rewriting every bar after t never changes a value at or before t."""
    base = _walk(150)
    full = _call(name, base, params)
    for t in (warmup, 90, 148):
        rng = np.random.default_rng(t)
        moved = {k: v.copy() for k, v in base.items()}
        shock = rng.uniform(0.5, 1.5, 150 - t - 1)
        for k in moved:
            moved[k][t + 1 :] *= shock
        assert np.array_equal(_call(name, moved, params)[: t + 1], full[: t + 1], equal_nan=True)
