"""Tests for volume indicators: OBV, VWAP, MFI."""

from __future__ import annotations

import numpy as np
import pytest

from hydra.indicators.library import mfi, obv, vwap


class TestOBV:
    """On Balance Volume over a rolling window."""

    def test_sums_the_signed_volume_of_the_last_period_moves(self) -> None:
        close = np.array([10.0, 11.0, 10.5, 10.5, 12.0, 11.0])
        volume = np.array([100.0, 200.0, 150.0, 300.0, 250.0, 50.0])
        result = obv(close, volume, period=3)
        # signed steps from bar 1: +200, -150, 0, +250, -50
        assert np.isnan(result[:3]).all()
        assert result[3:].tolist() == [
            200.0 - 150.0 + 0.0,
            -150.0 + 0.0 + 250.0,
            0.0 + 250.0 - 50.0,
        ]

    def test_the_value_does_not_depend_on_bars_before_the_window(self) -> None:
        close = np.array([10.0, 11.0, 10.5, 10.5, 12.0, 11.0])
        volume = np.array([100.0, 200.0, 150.0, 300.0, 250.0, 50.0])
        assert obv(close, volume, period=3)[-1] == obv(close[-4:], volume[-4:], period=3)[-1]

    def test_too_short_or_empty_is_all_nan(self) -> None:
        assert np.isnan(obv(np.array([1.0, 2.0]), np.array([5.0, 5.0]), period=2)).all()
        assert len(obv(np.array([], dtype=np.float64), np.array([], dtype=np.float64))) == 0


class TestVWAP:
    """Volume Weighted Average Price over a rolling window."""

    def test_weights_the_typical_price_of_the_last_period_bars(self) -> None:
        high = np.array([105.0, 108.0, 107.0, 110.0])
        low = np.array([95.0, 98.0, 97.0, 100.0])
        close = np.array([100.0, 103.0, 102.0, 105.0])
        volume = np.array([1000.0, 1500.0, 800.0, 2000.0])
        result = vwap(high, low, close, volume, period=2)
        tp = (high + low + close) / 3.0
        assert np.isnan(result[0])
        for i in range(1, 4):
            want = (tp[i - 1] * volume[i - 1] + tp[i] * volume[i]) / (volume[i - 1] + volume[i])
            assert result[i] == pytest.approx(want, rel=1e-15)
        assert result[3] == pytest.approx((102.0 * 800.0 + 105.0 * 2000.0) / 2800.0, rel=1e-15)

    def test_a_window_without_volume_is_nan(self) -> None:
        price = np.array([100.0, 101.0, 102.0])
        result = vwap(price, price, price, np.array([5.0, 0.0, 0.0]), period=2)
        assert result[1] == 100.0 and np.isnan(result[2])

    def test_a_single_bar_window_is_the_typical_price(self) -> None:
        result = vwap(np.array([110.0]), np.array([90.0]), np.array([100.0]), np.array([1000.0]), 1)
        assert result[0] == pytest.approx(100.0)


def test_obv_and_vwap_do_not_depend_on_how_much_history_is_loaded() -> None:
    """A sweep (20,000 bars) and a live session (2,000) ending on one bar read the same line."""
    rng = np.random.default_rng(7)
    n = 20_000
    close = 40_000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    volume = np.abs(rng.normal(500, 200, n))
    short = slice(n - 2_000, n)
    for period in (2, 20, 200):
        long_obv = obv(close, volume, period)
        short_obv = obv(close[short], volume[short], period)
        assert np.array_equal(long_obv[-1_500:], short_obv[-1_500:])
        long_vwap = vwap(high, low, close, volume, period)
        short_vwap = vwap(high[short], low[short], close[short], volume[short], period)
        assert np.array_equal(long_vwap[-1_500:], short_vwap[-1_500:])
        assert np.isfinite(short_vwap[-1_500:]).all() and np.isfinite(short_obv[-1_500:]).all()


class TestMFI:
    """Money Flow Index tests."""

    def test_mfi_range(self) -> None:
        """MFI should be in [0, 100]."""
        rng = np.random.default_rng(42)
        n = 50
        close = np.cumsum(rng.standard_normal(n)) + 100
        high = close + rng.uniform(0.5, 2.0, n)
        low = close - rng.uniform(0.5, 2.0, n)
        volume = rng.uniform(1000, 5000, n)
        result = mfi(high, low, close, volume, period=14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(0 <= v <= 100 for v in valid)

    def test_mfi_insufficient_data(self) -> None:
        high = np.array([101.0, 102.0])
        low = np.array([99.0, 100.0])
        close = np.array([100.0, 101.0])
        volume = np.array([1000.0, 1500.0])
        result = mfi(high, low, close, volume, period=14)
        assert all(np.isnan(result))
