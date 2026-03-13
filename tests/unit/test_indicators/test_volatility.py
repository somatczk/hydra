"""Tests for volatility indicators: ATR, Bollinger Bands, Keltner Channels."""

from __future__ import annotations

import numpy as np

from hydra.indicators.library import atr, bollinger_bands, keltner_channels


class TestATR:
    """Average True Range tests."""

    def test_atr_positive(self) -> None:
        """ATR should be > 0 for volatile data."""
        rng = np.random.default_rng(42)
        n = 30
        close = np.cumsum(rng.standard_normal(n)) + 100
        high = close + rng.uniform(0.5, 2.0, n)
        low = close - rng.uniform(0.5, 2.0, n)
        result = atr(high, low, close, period=14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(v > 0 for v in valid)

    def test_atr_zero_for_flat_market(self) -> None:
        """ATR should be close to zero for a perfectly flat market."""
        n = 30
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        close = np.full(n, 100.0)
        result = atr(high, low, close, period=14)
        valid = result[~np.isnan(result)]
        np.testing.assert_allclose(valid, 0.0, atol=1e-10)

    def test_atr_insufficient_data(self) -> None:
        high = np.array([101.0, 102.0])
        low = np.array([99.0, 100.0])
        close = np.array([100.0, 101.0])
        result = atr(high, low, close, period=14)
        assert all(np.isnan(result))


class TestBollingerBands:
    """Bollinger Bands tests."""

    def test_bb_ordering(self) -> None:
        """Upper > middle > lower for any normal data."""
        rng = np.random.default_rng(42)
        data = np.cumsum(rng.standard_normal(50)) + 100
        upper, middle, lower = bollinger_bands(data, period=20, std_dev=2.0)
        for i in range(19, 50):
            if not np.isnan(upper[i]):
                assert upper[i] >= middle[i]
                assert middle[i] >= lower[i]

    def test_bb_width_proportional_to_std_dev(self) -> None:
        """BB width should scale with std_dev parameter."""
        rng = np.random.default_rng(42)
        data = np.cumsum(rng.standard_normal(50)) + 100
        u1, _m1, l1 = bollinger_bands(data, period=20, std_dev=1.0)
        u2, _m2, l2 = bollinger_bands(data, period=20, std_dev=2.0)
        # Width at index 30 (well within valid range)
        i = 30
        width1 = u1[i] - l1[i]
        width2 = u2[i] - l2[i]
        # Width with std_dev=2 should be ~2x width with std_dev=1
        np.testing.assert_allclose(width2, 2.0 * width1, rtol=1e-10)

    def test_bb_middle_equals_sma(self) -> None:
        """The middle band should equal the SMA."""
        from hydra.indicators.library import sma

        rng = np.random.default_rng(42)
        data = np.cumsum(rng.standard_normal(50)) + 100
        _, middle, _ = bollinger_bands(data, period=20)
        expected_sma = sma(data, 20)
        np.testing.assert_allclose(middle, expected_sma)

    def test_bb_nan_for_insufficient_data(self) -> None:
        data = np.array([1.0, 2.0, 3.0])
        upper, middle, lower = bollinger_bands(data, period=20)
        assert all(np.isnan(upper))
        assert all(np.isnan(middle))
        assert all(np.isnan(lower))


class TestKeltnerChannels:
    """Keltner Channels tests."""

    def test_kc_ordering(self) -> None:
        """Upper > middle > lower."""
        rng = np.random.default_rng(42)
        n = 50
        close = np.cumsum(rng.standard_normal(n)) + 100
        high = close + rng.uniform(0.5, 2.0, n)
        low = close - rng.uniform(0.5, 2.0, n)
        upper, middle, lower = keltner_channels(high, low, close, ema_period=20, atr_period=10)
        for i in range(25, n):
            if not (np.isnan(upper[i]) or np.isnan(lower[i])):
                assert upper[i] >= middle[i]
                assert middle[i] >= lower[i]

    def test_kc_output_length(self) -> None:
        n = 40
        rng = np.random.default_rng(42)
        close = rng.uniform(95, 105, n)
        high = close + 2
        low = close - 2
        upper, middle, lower = keltner_channels(high, low, close)
        assert len(upper) == n
        assert len(middle) == n
        assert len(lower) == n
