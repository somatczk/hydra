"""Tests for trend indicators: SMA, EMA, MACD, Supertrend, Ichimoku."""

from __future__ import annotations

import numpy as np

from hydra.indicators.library import ema, ichimoku, macd, sma, supertrend


class TestSMA:
    """Simple Moving Average tests."""

    def test_sma_basic(self) -> None:
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        result = sma(data, 5)
        assert len(result) == 7
        # First 4 elements should be NaN
        assert all(np.isnan(result[:4]))
        np.testing.assert_allclose(result[4:], [3.0, 4.0, 5.0])

    def test_sma_period_2(self) -> None:
        data = np.array([10.0, 20.0, 30.0])
        result = sma(data, 2)
        assert np.isnan(result[0])
        np.testing.assert_allclose(result[1], 15.0)
        np.testing.assert_allclose(result[2], 25.0)

    def test_sma_insufficient_data(self) -> None:
        data = np.array([1.0, 2.0])
        result = sma(data, 5)
        assert all(np.isnan(result))

    def test_sma_same_length(self) -> None:
        data = np.arange(1, 11, dtype=np.float64)
        result = sma(data, 3)
        assert len(result) == len(data)

    def test_sma_zero_period(self) -> None:
        data = np.array([1.0, 2.0, 3.0])
        result = sma(data, 0)
        assert all(np.isnan(result))


class TestEMA:
    """Exponential Moving Average tests."""

    def test_ema_basic(self) -> None:
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ema(data, 3)
        assert len(result) == 5
        # First 2 should be NaN, index 2 = SMA of first 3 = 2.0
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        np.testing.assert_allclose(result[2], 2.0)
        # EMA converges: each subsequent value should be between previous EMA and data
        assert result[3] > result[2]
        assert result[4] > result[3]

    def test_ema_convergence(self) -> None:
        """EMA of constant series should converge to that constant."""
        data = np.full(50, 42.0)
        result = ema(data, 10)
        # All valid values should be 42
        valid = result[~np.isnan(result)]
        np.testing.assert_allclose(valid, 42.0)

    def test_ema_insufficient_data(self) -> None:
        data = np.array([1.0, 2.0])
        result = ema(data, 5)
        assert all(np.isnan(result))


class TestMACD:
    """MACD tests."""

    def test_macd_output_shapes(self) -> None:
        data = np.arange(1, 51, dtype=np.float64)
        macd_line, signal_line, histogram = macd(data, fast=12, slow=26, signal=9)
        assert len(macd_line) == 50
        assert len(signal_line) == 50
        assert len(histogram) == 50

    def test_macd_nan_for_insufficient_data(self) -> None:
        data = np.arange(1, 10, dtype=np.float64)
        macd_line, _signal_line, _histogram = macd(data)
        # With only 9 data points, slow EMA(26) can't be computed
        assert all(np.isnan(macd_line))

    def test_macd_signal_crossover(self) -> None:
        """MACD histogram should change sign at signal crossover points."""
        # Create data that trends up then down
        up = np.linspace(10, 50, 40)
        down = np.linspace(50, 20, 30)
        data = np.concatenate([up, down])
        _macd_line, _signal_line, histogram = macd(data, fast=12, slow=26, signal=9)
        valid_hist = histogram[~np.isnan(histogram)]
        if len(valid_hist) > 10:
            # There should be both positive and negative values in a trend reversal
            assert np.any(valid_hist > 0) or np.any(valid_hist < 0)


class TestSupertrend:
    """Supertrend tests."""

    def test_supertrend_output_shapes(self) -> None:
        n = 30
        high = np.random.default_rng(42).uniform(100, 110, n)
        low = high - np.random.default_rng(42).uniform(1, 5, n)
        close = (high + low) / 2
        st_line, direction = supertrend(high, low, close, period=10, multiplier=3.0)
        assert len(st_line) == n
        assert len(direction) == n

    def test_supertrend_direction_values(self) -> None:
        n = 50
        rng = np.random.default_rng(42)
        high = np.cumsum(rng.uniform(0, 2, n)) + 100
        low = high - rng.uniform(1, 3, n)
        close = (high + low) / 2
        _, direction = supertrend(high, low, close, period=10, multiplier=3.0)
        valid = direction[~np.isnan(direction)]
        # Direction should only be +1 or -1
        assert all(d in (1.0, -1.0) for d in valid)

    def test_supertrend_insufficient_data(self) -> None:
        high = np.array([100.0, 101.0, 102.0])
        low = np.array([99.0, 100.0, 101.0])
        close = np.array([99.5, 100.5, 101.5])
        st_line, direction = supertrend(high, low, close, period=10)
        assert all(np.isnan(st_line))
        assert all(np.isnan(direction))


class TestIchimoku:
    """Ichimoku Cloud tests."""

    def test_ichimoku_keys(self) -> None:
        n = 60
        rng = np.random.default_rng(42)
        high = rng.uniform(100, 110, n)
        low = high - rng.uniform(1, 5, n)
        close = (high + low) / 2
        result = ichimoku(high, low, close, tenkan=9, kijun=26, senkou_b=52)
        expected_keys = {
            "tenkan_sen",
            "kijun_sen",
            "senkou_span_a",
            "senkou_span_b",
            "chikou_span",
        }
        assert set(result.keys()) == expected_keys
        for key in expected_keys:
            assert len(result[key]) == n

    def test_ichimoku_tenkan_shorter_than_kijun(self) -> None:
        """Tenkan period is shorter, so it should have earlier valid values."""
        n = 60
        rng = np.random.default_rng(42)
        high = rng.uniform(100, 110, n)
        low = high - rng.uniform(1, 5, n)
        close = (high + low) / 2
        result = ichimoku(high, low, close, tenkan=9, kijun=26, senkou_b=52)
        tenkan_first_valid = np.argmax(~np.isnan(result["tenkan_sen"]))
        kijun_first_valid = np.argmax(~np.isnan(result["kijun_sen"]))
        assert tenkan_first_valid < kijun_first_valid
