"""Tests for volume indicators: OBV, VWAP, MFI."""

from __future__ import annotations

import numpy as np

from hydra.indicators.library import mfi, obv, vwap


class TestOBV:
    """On Balance Volume tests."""

    def test_obv_increases_on_up_close(self) -> None:
        """OBV should increase when close > prev close."""
        close = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
        volume = np.array([100.0, 200.0, 150.0, 300.0, 250.0])
        result = obv(close, volume)
        # Each bar close > prev close, so OBV accumulates
        for i in range(1, len(result)):
            assert result[i] > result[i - 1]

    def test_obv_decreases_on_down_close(self) -> None:
        """OBV should decrease when close < prev close."""
        close = np.array([14.0, 13.0, 12.0, 11.0, 10.0])
        volume = np.array([100.0, 200.0, 150.0, 300.0, 250.0])
        result = obv(close, volume)
        for i in range(1, len(result)):
            assert result[i] < result[i - 1]

    def test_obv_flat_on_equal_close(self) -> None:
        """OBV unchanged when close == prev close."""
        close = np.array([10.0, 10.0, 10.0])
        volume = np.array([100.0, 200.0, 300.0])
        result = obv(close, volume)
        np.testing.assert_allclose(result[0], 100.0)
        np.testing.assert_allclose(result[1], 100.0)
        np.testing.assert_allclose(result[2], 100.0)

    def test_obv_empty(self) -> None:
        close = np.array([], dtype=np.float64)
        volume = np.array([], dtype=np.float64)
        result = obv(close, volume)
        assert len(result) == 0


class TestVWAP:
    """Volume Weighted Average Price tests."""

    def test_vwap_between_high_and_low(self) -> None:
        """VWAP should be between high and low for each bar (cumulative)."""
        high = np.array([105.0, 108.0, 107.0, 110.0])
        low = np.array([95.0, 98.0, 97.0, 100.0])
        close = np.array([100.0, 103.0, 102.0, 105.0])
        volume = np.array([1000.0, 1500.0, 800.0, 2000.0])
        result = vwap(high, low, close, volume)
        # VWAP is cumulative; at each point it should be between
        # the overall min low and max high seen so far
        for i in range(len(result)):
            assert result[i] >= np.min(low[: i + 1])
            assert result[i] <= np.max(high[: i + 1])

    def test_vwap_single_bar(self) -> None:
        """VWAP of a single bar should be the typical price."""
        high = np.array([110.0])
        low = np.array([90.0])
        close = np.array([100.0])
        volume = np.array([1000.0])
        result = vwap(high, low, close, volume)
        expected = (110 + 90 + 100) / 3.0
        np.testing.assert_allclose(result[0], expected)


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
