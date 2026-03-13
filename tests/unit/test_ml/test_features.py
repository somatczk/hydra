"""Tests for hydra.ml.features — feature engineering module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.types import OHLCV
from hydra.ml.features import (
    FeatureEngineering,
    FeatureMatrix,
    _extract_close_array,
    _extract_ohlcv_arrays,
    _log_returns,
    _percentile_rank,
    _realized_vol,
    _sin_cos_encode,
    build_target,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bars(
    n: int,
    base_price: float = 50000.0,
    volatility: float = 100.0,
    base_volume: float = 10.0,
    start: datetime | None = None,
    interval_hours: int = 1,
) -> list[OHLCV]:
    """Generate synthetic OHLCV bars with a simple random walk."""
    rng = np.random.default_rng(42)
    if start is None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
    bars: list[OHLCV] = []
    price = base_price
    for i in range(n):
        change = rng.normal(0, volatility)
        o = price
        c = price + change
        h = max(o, c) + abs(rng.normal(0, volatility * 0.3))
        lo = min(o, c) - abs(rng.normal(0, volatility * 0.3))
        v = base_volume + abs(rng.normal(0, base_volume * 0.5))
        bars.append(
            OHLCV(
                open=Decimal(str(round(o, 2))),
                high=Decimal(str(round(h, 2))),
                low=Decimal(str(round(lo, 2))),
                close=Decimal(str(round(c, 2))),
                volume=Decimal(str(round(v, 4))),
                timestamp=start + timedelta(hours=i * interval_hours),
            )
        )
        price = c
    return bars


def _make_constant_bars(
    n: int,
    price: float = 50000.0,
    volume: float = 10.0,
    start: datetime | None = None,
    interval_hours: int = 1,
) -> list[OHLCV]:
    """Generate bars with identical OHLCV values (edge case: no movement)."""
    if start is None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        OHLCV(
            open=Decimal(str(price)),
            high=Decimal(str(price)),
            low=Decimal(str(price)),
            close=Decimal(str(price)),
            volume=Decimal(str(volume)),
            timestamp=start + timedelta(hours=i * interval_hours),
        )
        for i in range(n)
    ]


@pytest.fixture()
def bars_1h() -> list[OHLCV]:
    return _make_bars(200, interval_hours=1)


@pytest.fixture()
def bars_4h() -> list[OHLCV]:
    return _make_bars(50, interval_hours=4)


@pytest.fixture()
def bars_1d() -> list[OHLCV]:
    return _make_bars(30, interval_hours=24)


@pytest.fixture()
def engine() -> FeatureEngineering:
    return FeatureEngineering()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for standalone helper/utility functions."""

    def test_extract_close_array(self, bars_1h: list[OHLCV]) -> None:
        close = _extract_close_array(bars_1h)
        assert close.dtype == np.float64
        assert len(close) == len(bars_1h)
        assert close[0] == float(bars_1h[0].close)

    def test_extract_ohlcv_arrays(self, bars_1h: list[OHLCV]) -> None:
        o, h, lo, c, v = _extract_ohlcv_arrays(bars_1h)
        assert all(arr.dtype == np.float64 for arr in (o, h, lo, c, v))
        assert all(len(arr) == len(bars_1h) for arr in (o, h, lo, c, v))

    def test_log_returns_manual(self) -> None:
        close = np.array([100.0, 110.0, 105.0], dtype=np.float64)
        lr = _log_returns(close)
        assert len(lr) == 2
        np.testing.assert_allclose(lr[0], np.log(110.0 / 100.0), rtol=1e-12)
        np.testing.assert_allclose(lr[1], np.log(105.0 / 110.0), rtol=1e-12)

    def test_realized_vol_positive(self) -> None:
        rng = np.random.default_rng(7)
        returns = rng.normal(0, 0.01, 50)
        rv = _realized_vol(returns, 10)
        # First 9 elements should be NaN
        assert np.all(np.isnan(rv[:9]))
        # After warm-up, values should be positive
        valid = rv[~np.isnan(rv)]
        assert np.all(valid > 0)

    def test_sin_cos_encode_identity(self) -> None:
        values = np.arange(24, dtype=np.float64)
        s, c = _sin_cos_encode(values, 24.0)
        # sin^2 + cos^2 == 1
        np.testing.assert_allclose(s**2 + c**2, 1.0, atol=1e-12)

    def test_percentile_rank_bounds(self) -> None:
        data = np.arange(100, dtype=np.float64)
        pr = _percentile_rank(data, 20)
        valid = pr[~np.isnan(pr)]
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 1.0)


# ---------------------------------------------------------------------------
# Target generation tests
# ---------------------------------------------------------------------------


class TestBuildTarget:
    def test_target_values(self) -> None:
        close = np.array([100.0, 102.0, 101.0, 103.0, 100.0], dtype=np.float64)
        target = build_target(close, horizon=1)
        assert len(target) == 4
        assert target[0] == 1.0  # 102 > 100
        assert target[1] == -1.0  # 101 < 102
        assert target[2] == 1.0  # 103 > 101
        assert target[3] == -1.0  # 100 < 103

    def test_target_length(self) -> None:
        close = np.arange(100, dtype=np.float64) + 1
        target = build_target(close, horizon=3)
        assert len(target) == 97

    def test_target_zero_change(self) -> None:
        close = np.array([100.0, 100.0, 100.0], dtype=np.float64)
        target = build_target(close, horizon=1)
        assert np.all(target == 0.0)


# ---------------------------------------------------------------------------
# FeatureEngineering tests
# ---------------------------------------------------------------------------


class TestFeatureConsistency:
    """build_features last row must equal build_live_features."""

    def test_consistency(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        funding = [0.0001] * 200
        fg = [55.0] * 200
        fm = engine.build_features(bars_1h, bars_4h, bars_1d, funding, fg)
        live = engine.build_live_features(bars_1h, bars_4h, bars_1d, funding, fg)

        last_row = fm.features[-1]
        np.testing.assert_array_equal(last_row, live)

    def test_consistency_no_optionals(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        live = engine.build_live_features(bars_1h, bars_4h, bars_1d)
        np.testing.assert_array_equal(fm.features[-1], live)


class TestFeatureCount:
    """Verify the expected number of features matches feature_names."""

    def test_feature_names_match_columns(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert fm.features.shape[1] == len(fm.feature_names)

    def test_feature_names_unique(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert len(fm.feature_names) == len(set(fm.feature_names))

    def test_rows_match_bars(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert fm.features.shape[0] == len(bars_1h)

    def test_timestamps_match(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert len(fm.timestamps) == len(bars_1h)
        assert fm.timestamps[0] == bars_1h[0].timestamp


class TestTechnicalFeatures:
    """Validate technical indicator feature ranges."""

    def test_rsi_range(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        rsi_idx = fm.feature_names.index("rsi_14_1h")
        rsi_vals = fm.features[:, rsi_idx]
        valid = rsi_vals[~np.isnan(rsi_vals)]
        assert len(valid) > 0
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 100.0)

    def test_bb_pctb_reasonable(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("bb_pctb_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        # For normal data %B is *roughly* in [0, 1] but can exceed
        # Just check it produces finite values
        assert np.all(np.isfinite(valid))

    def test_atr_positive(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("atr_norm_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        assert np.all(valid > 0.0)


class TestPriceActionFeatures:
    """Validate price action features."""

    def test_log_returns_manual_match(self) -> None:
        bars = _make_bars(10, interval_hours=1)
        close = _extract_close_array(bars)
        expected = np.log(close[1:] / close[:-1])
        lr = _log_returns(close)
        np.testing.assert_allclose(lr, expected, rtol=1e-12)

    def test_realized_vol_positive(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("realized_vol_24h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        assert np.all(valid > 0.0)

    def test_candle_ratios_bounded(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        for name in ("candle_body_ratio_1h", "upper_wick_ratio_1h", "lower_wick_ratio_1h"):
            idx = fm.feature_names.index(name)
            vals = fm.features[:, idx]
            valid = vals[~np.isnan(vals)]
            assert np.all(valid >= 0.0), f"{name} has negative values"
            assert np.all(valid <= 1.0 + 1e-9), f"{name} exceeds 1.0"


class TestVolumeFeatures:
    """Validate volume-based features."""

    def test_volume_ratio_positive(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("vol_sma20_ratio_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        assert np.all(valid > 0.0)

    def test_vwap_deviation_reasonable(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("vwap_dev_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        # VWAP deviation should be small for normal data
        assert np.all(np.abs(valid) < 1.0)

    def test_mfi_range(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        idx = fm.feature_names.index("mfi_14_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        assert len(valid) > 0
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 100.0)


class TestTemporalEncoding:
    """Validate sin/cos temporal features."""

    def test_sin_cos_identity(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        hour_sin_idx = fm.feature_names.index("hour_sin")
        hour_cos_idx = fm.feature_names.index("hour_cos")
        s = fm.features[:, hour_sin_idx]
        c = fm.features[:, hour_cos_idx]
        np.testing.assert_allclose(s**2 + c**2, 1.0, atol=1e-12)

    def test_dow_sin_cos_identity(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        s_idx = fm.feature_names.index("dow_sin")
        c_idx = fm.feature_names.index("dow_cos")
        s = fm.features[:, s_idx]
        c = fm.features[:, c_idx]
        np.testing.assert_allclose(s**2 + c**2, 1.0, atol=1e-12)


class TestNoneOptionals:
    """Verify that None optional inputs do not crash."""

    def test_none_funding_rates(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d, funding_rates=None)
        idx = fm.feature_names.index("funding_rate")
        vals = fm.features[:, idx]
        # Should be all NaN when not provided
        assert np.all(np.isnan(vals))

    def test_none_fear_greed(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d, fear_greed_index=None)
        idx = fm.feature_names.index("fear_greed_norm")
        vals = fm.features[:, idx]
        assert np.all(np.isnan(vals))

    def test_all_none_optionals(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        # Should not raise
        fm = engine.build_features(
            bars_1h, bars_4h, bars_1d, funding_rates=None, fear_greed_index=None
        )
        assert fm.features.shape[0] == len(bars_1h)
        assert fm.features.dtype == np.float64


class TestEdgeCases:
    """Edge cases: constant price, zero vol, etc."""

    def test_constant_price_zero_returns(self) -> None:
        bars_1h = _make_constant_bars(200, interval_hours=1)
        bars_4h = _make_constant_bars(50, interval_hours=4)
        bars_1d = _make_constant_bars(30, interval_hours=24)
        eng = FeatureEngineering()
        fm = eng.build_features(bars_1h, bars_4h, bars_1d)

        idx = fm.feature_names.index("log_return_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        # All returns should be zero (or -inf/nan for log(same/same)=0)
        np.testing.assert_allclose(valid, 0.0, atol=1e-12)

    def test_constant_price_zero_volatility(self) -> None:
        bars_1h = _make_constant_bars(200, interval_hours=1)
        bars_4h = _make_constant_bars(50, interval_hours=4)
        bars_1d = _make_constant_bars(30, interval_hours=24)
        eng = FeatureEngineering()
        fm = eng.build_features(bars_1h, bars_4h, bars_1d)

        idx = fm.feature_names.index("realized_vol_24h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        # Volatility of constant returns is zero
        np.testing.assert_allclose(valid, 0.0, atol=1e-12)

    def test_constant_volume(self) -> None:
        bars_1h = _make_constant_bars(200, volume=10.0, interval_hours=1)
        bars_4h = _make_constant_bars(50, volume=10.0, interval_hours=4)
        bars_1d = _make_constant_bars(30, volume=10.0, interval_hours=24)
        eng = FeatureEngineering()
        fm = eng.build_features(bars_1h, bars_4h, bars_1d)

        idx = fm.feature_names.index("vol_sma20_ratio_1h")
        vals = fm.features[:, idx]
        valid = vals[~np.isnan(vals)]
        # Constant volume / SMA(constant volume) == 1.0
        np.testing.assert_allclose(valid, 1.0, atol=1e-12)

    def test_feature_matrix_dtype_float64(self) -> None:
        bars_1h = _make_bars(200, interval_hours=1)
        bars_4h = _make_bars(50, interval_hours=4)
        bars_1d = _make_bars(30, interval_hours=24)
        eng = FeatureEngineering()
        fm = eng.build_features(bars_1h, bars_4h, bars_1d)
        assert fm.features.dtype == np.float64

    def test_live_features_shape(self) -> None:
        bars_1h = _make_bars(200, interval_hours=1)
        bars_4h = _make_bars(50, interval_hours=4)
        bars_1d = _make_bars(30, interval_hours=24)
        eng = FeatureEngineering()
        fm = eng.build_features(bars_1h, bars_4h, bars_1d)
        live = eng.build_live_features(bars_1h, bars_4h, bars_1d)
        assert live.ndim == 1
        assert live.shape[0] == fm.features.shape[1]


class TestFeatureMatrixDataclass:
    """Verify FeatureMatrix structure."""

    def test_target_default_none(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert fm.target is None

    def test_feature_matrix_is_dataclass(
        self,
        engine: FeatureEngineering,
        bars_1h: list[OHLCV],
        bars_4h: list[OHLCV],
        bars_1d: list[OHLCV],
    ) -> None:
        fm = engine.build_features(bars_1h, bars_4h, bars_1d)
        assert isinstance(fm, FeatureMatrix)
        assert isinstance(fm.features, np.ndarray)
        assert isinstance(fm.feature_names, list)
        assert isinstance(fm.timestamps, list)
