"""Tests for momentum indicators: RSI, Stochastic, CCI, Williams %R."""

from __future__ import annotations

import numpy as np
import talib

from hydra.indicators.library import cci, rsi, stochastic, stochastic_d, williams_r


class TestRSI:
    """Relative Strength Index tests."""

    def test_rsi_range(self) -> None:
        """RSI values should be in [0, 100]."""
        rng = np.random.default_rng(42)
        data = np.cumsum(rng.standard_normal(100)) + 100
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(0 <= v <= 100 for v in valid)

    def test_rsi_constant_series(self) -> None:
        """RSI of a constant series: no gains or losses, result should be NaN or ~50.

        With Wilder smoothing when avg_gain=0 and avg_loss=0, the first RSI is 100
        (div by zero guard), but subsequent values have both zero, leading to 100.
        The important invariant is that valid values are within [0, 100].
        """
        data = np.full(30, 50.0)
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        # All valid values should be within [0, 100]
        assert all(0 <= v <= 100 for v in valid)

    def test_rsi_monotonically_increasing(self) -> None:
        """RSI of a strictly increasing series should be very high (near 100)."""
        data = np.arange(1, 32, dtype=np.float64)
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(v > 90 for v in valid)

    def test_rsi_monotonically_decreasing(self) -> None:
        """RSI of a strictly decreasing series should be very low (near 0)."""
        data = np.arange(30, 0, -1, dtype=np.float64)
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(v < 10 for v in valid)

    def test_rsi_insufficient_data(self) -> None:
        data = np.array([1.0, 2.0, 3.0])
        result = rsi(data, 14)
        assert all(np.isnan(result))


class TestStochastic:
    """Stochastic oscillator tests."""

    def test_stochastic_k_range(self) -> None:
        """%K should be in [0, 100]."""
        rng = np.random.default_rng(42)
        n = 50
        high = rng.uniform(100, 110, n)
        low = high - rng.uniform(1, 5, n)
        close = low + rng.uniform(0, 1, n) * (high - low)
        k, _d = stochastic(high, low, close, k_period=14, d_period=3)
        valid_k = k[~np.isnan(k)]
        assert len(valid_k) > 0
        assert all(0 <= v <= 100 for v in valid_k)

    def test_stochastic_d_is_sma_of_k(self) -> None:
        """%D should be the SMA of %K."""
        rng = np.random.default_rng(42)
        n = 50
        high = rng.uniform(100, 110, n)
        low = high - rng.uniform(1, 5, n)
        close = low + rng.uniform(0, 1, n) * (high - low)
        k, d = stochastic(high, low, close, k_period=14, d_period=3)
        # Verify %D is the SMA of %K with period 3
        for i in range(15, n):  # after enough valid K values
            if not np.isnan(d[i]) and not any(np.isnan(k[i - 2 : i + 1])):
                expected = np.mean(k[i - 2 : i + 1])
                np.testing.assert_allclose(d[i], expected, atol=1e-10)


class TestCCI:
    """Commodity Channel Index tests."""

    def test_cci_output_length(self) -> None:
        n = 40
        high = np.random.default_rng(42).uniform(100, 110, n)
        low = high - np.random.default_rng(42).uniform(1, 5, n)
        close = (high + low) / 2
        result = cci(high, low, close, period=20)
        assert len(result) == n

    def test_cci_nan_for_insufficient_data(self) -> None:
        high = np.array([100.0, 101.0])
        low = np.array([99.0, 100.0])
        close = np.array([99.5, 100.5])
        result = cci(high, low, close, period=20)
        assert all(np.isnan(result))


class TestWilliamsR:
    """Williams %R tests."""

    def test_williams_r_range(self) -> None:
        """Williams %R should be in [-100, 0]."""
        rng = np.random.default_rng(42)
        n = 50
        high = rng.uniform(100, 110, n)
        low = high - rng.uniform(1, 5, n)
        close = low + rng.uniform(0, 1, n) * (high - low)
        result = williams_r(high, low, close, period=14)
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert all(-100 <= v <= 0 for v in valid)

    def test_williams_r_at_high(self) -> None:
        """When close is at the period high, Williams %R should be 0."""
        n = 20
        high = np.full(n, 110.0)
        low = np.full(n, 90.0)
        close = np.full(n, 110.0)  # Close at high
        result = williams_r(high, low, close, period=14)
        valid = result[~np.isnan(result)]
        np.testing.assert_allclose(valid, 0.0)

    def test_williams_r_at_low(self) -> None:
        """When close is at the period low, Williams %R should be -100."""
        n = 20
        high = np.full(n, 110.0)
        low = np.full(n, 90.0)
        close = np.full(n, 90.0)  # Close at low
        result = williams_r(high, low, close, period=14)
        valid = result[~np.isnan(result)]
        np.testing.assert_allclose(valid, -100.0)


def test_stochastic_is_fast_stochf_exact() -> None:
    high = np.array([3, 5, 4, 6, 7], dtype=np.float64)
    low = np.array([1, 2, 2, 3, 5], dtype=np.float64)
    close = np.array([2, 4, 3, 5, 6], dtype=np.float64)
    k, d = stochastic(high, low, close, k_period=3, d_period=2)
    np.testing.assert_array_equal(k, [np.nan, np.nan, np.nan, 75.0, 80.0])
    np.testing.assert_array_equal(d, [np.nan, np.nan, np.nan, 62.5, 77.5])
    assert np.array_equal(stochastic_d(high, low, close, 3, 2), d, equal_nan=True)


def test_stochastic_matches_stochf_on_a_random_walk_and_zero_range_is_0() -> None:
    rng = np.random.default_rng(11)
    close = 100 + np.cumsum(rng.standard_normal(200))
    high, low = close + rng.uniform(0, 2, 200), close - rng.uniform(0, 2, 200)
    k, d = stochastic(high, low, close, 14, 3)
    ref_k, ref_d = talib.STOCHF(high, low, close, 14, 3, 0)
    assert np.array_equal(k, ref_k, equal_nan=True)
    assert np.array_equal(d, ref_d, equal_nan=True)
    flat = np.full(20, 5.0)
    assert stochastic(flat, flat, flat, 14, 3)[0][-1] == 0.0
