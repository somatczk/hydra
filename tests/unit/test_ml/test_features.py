"""Tests for hydra.ml.features — the pinned spec and the timestamp-joined matrix."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.types import OHLCV
from hydra.ml.features import (
    DEFAULT_FEATURE_SPEC,
    FeatureMatrix,
    FeatureSpec,
    _all_feature_names,
    _extract_close_array,
    _log_returns,
    _percentile_rank,
    _realized_vol,
    _sin_cos_encode,
    build_features,
    build_live_features,
    build_target,
    reference_deciles,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


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
        start = T0
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
        start = T0
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


def _bar(ts: datetime, close: str | float, volume: str | float = 10.0) -> OHLCV:
    price = Decimal(str(close))
    return OHLCV(
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(str(volume)),
        timestamp=ts,
    )


@pytest.fixture()
def bars_1h() -> list[OHLCV]:
    return _make_bars(200, interval_hours=1)


@pytest.fixture()
def bars_4h() -> list[OHLCV]:
    return _make_bars(50, interval_hours=4)


@pytest.fixture()
def bars_1d() -> list[OHLCV]:
    return _make_bars(30, interval_hours=24)


# ---------------------------------------------------------------------------
# The pinned spec
# ---------------------------------------------------------------------------


class TestFeatureSpec:
    def test_default_spec_pins_67_unique_names(self) -> None:
        names = DEFAULT_FEATURE_SPEC.feature_names
        assert len(names) == 67
        assert len(set(names)) == 67
        assert names == tuple(_all_feature_names())

    def test_default_spec_matches_the_builder(self) -> None:
        """The pinned names must be exactly what the builder computes (spec/code sync)."""
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_bars(200),
            _make_bars(50, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        assert fm.feature_names == list(DEFAULT_FEATURE_SPEC.feature_names)
        assert fm.features.shape == (200, 67)

    def test_funding_and_fear_greed_blocks_are_deleted(self) -> None:
        names = set(DEFAULT_FEATURE_SPEC.feature_names)
        assert "funding_rate" not in names
        assert "funding_rate_sma24" not in names
        assert "fear_greed_norm" not in names

    def test_spec_is_hashable_and_equal_specs_hash_equal(self) -> None:
        spec_a = FeatureSpec(3, 4, 2000, 500, 120, tuple(_all_feature_names()))
        spec_b = FeatureSpec(3, 4, 2000, 500, 120, tuple(_all_feature_names()))
        assert spec_a == DEFAULT_FEATURE_SPEC
        assert hash(spec_a) == hash(spec_b)

    def test_reference_quantiles_placeholder_is_none(self) -> None:
        assert DEFAULT_FEATURE_SPEC.reference_quantiles is None

    def test_to_dict_roundtrips_the_fields(self) -> None:
        spec = FeatureSpec(
            3,
            2,
            10,
            5,
            3,
            ("a", "b"),
            reference_quantiles=((0.1, 0.2, 0.3), None),
        )
        assert spec.to_dict() == {
            "spec_version": 3,
            "horizon": 2,
            "window_1h": 10,
            "window_4h": 5,
            "window_1d": 3,
            "feature_names": ["a", "b"],
            "reference_quantiles": [[0.1, 0.2, 0.3], None],
        }

    def test_invalid_spec_is_refused(self) -> None:
        names = tuple(_all_feature_names())
        with pytest.raises(ValueError, match="spec_version"):
            FeatureSpec(0, 4, 2000, 500, 120, names)
        with pytest.raises(ValueError, match="horizon"):
            FeatureSpec(3, 0, 2000, 500, 120, names)
        with pytest.raises(ValueError, match="window"):
            FeatureSpec(3, 4, 2000, 0, 120, names)
        with pytest.raises(ValueError, match="feature_names"):
            FeatureSpec(3, 4, 2000, 500, 120, ())


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_extract_close_array(self, bars_1h: list[OHLCV]) -> None:
        close = _extract_close_array(bars_1h)
        assert close.dtype == np.float64
        assert len(close) == len(bars_1h)
        assert close[0] == float(bars_1h[0].close)

    def test_log_returns_manual(self) -> None:
        close = np.array([100.0, 110.0, 105.0], dtype=np.float64)
        lr = _log_returns(close)
        assert len(lr) == 2
        np.testing.assert_allclose(lr[0], math.log(110.0 / 100.0), rtol=1e-12)
        np.testing.assert_allclose(lr[1], math.log(105.0 / 110.0), rtol=1e-12)

    def test_realized_vol_positive(self) -> None:
        rng = np.random.default_rng(7)
        returns = rng.normal(0, 0.01, 50)
        rv = _realized_vol(returns, 10)
        assert np.all(np.isnan(rv[:9]))
        valid = rv[~np.isnan(rv)]
        assert np.all(valid > 0)

    def test_sin_cos_encode_identity(self) -> None:
        values = np.arange(24, dtype=np.float64)
        s, c = _sin_cos_encode(values, 24.0)
        np.testing.assert_allclose(s**2 + c**2, 1.0, atol=1e-12)

    def test_percentile_rank_bounds(self) -> None:
        data = np.arange(100, dtype=np.float64)
        pr = _percentile_rank(data, 20)
        valid = pr[~np.isnan(pr)]
        assert np.all(valid >= 0.0)
        assert np.all(valid <= 1.0)


# ---------------------------------------------------------------------------
# Target generation
# ---------------------------------------------------------------------------


class TestBuildTarget:
    def test_target_values_and_forward_returns(self) -> None:
        close = np.array([100.0, 102.0, 101.0, 103.0, 100.0], dtype=np.float64)
        signs, fwd = build_target(close, horizon=1)
        assert len(signs) == len(fwd) == 4
        np.testing.assert_array_equal(signs, [1.0, -1.0, 1.0, -1.0])
        np.testing.assert_allclose(
            fwd, [0.02, 101.0 / 102.0 - 1, 103.0 / 101.0 - 1, -0.0291262], rtol=1e-6
        )

    def test_target_length(self) -> None:
        close = np.arange(100, dtype=np.float64) + 1
        signs, fwd = build_target(close, horizon=3)
        assert len(signs) == len(fwd) == 97

    def test_target_zero_change(self) -> None:
        close = np.array([100.0, 100.0, 100.0], dtype=np.float64)
        signs, fwd = build_target(close, horizon=1)
        assert np.all(signs == 0.0)
        assert np.all(fwd == 0.0)

    def test_horizon_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="horizon"):
            build_target(np.ones(5), horizon=0)


class TestReferenceDeciles:
    def test_nine_edges_near_equal_population(self) -> None:
        rng = np.random.default_rng(42)
        edges = reference_deciles(rng.standard_normal(100_000))
        assert edges is not None and len(edges) == 9
        assert all(edges[i] < edges[i + 1] for i in range(8))
        counts = np.histogram(rng.standard_normal(100_000), bins=[-np.inf, *edges, np.inf])[0]
        np.testing.assert_allclose(counts / 100_000, 0.1, atol=0.01)

    def test_all_nan_returns_none(self) -> None:
        assert reference_deciles(np.full(10, np.nan)) is None

    def test_empty_returns_none(self) -> None:
        assert reference_deciles(np.array([])) is None


# ---------------------------------------------------------------------------
# Matrix shape and value checks
# ---------------------------------------------------------------------------


class TestFeatureMatrixShape:
    def test_rows_match_bars_and_dtype(self) -> None:
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_bars(200),
            _make_bars(50, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        assert isinstance(fm, FeatureMatrix)
        assert fm.features.shape == (200, 67)
        assert fm.features.dtype == np.float64
        assert fm.feature_names == list(DEFAULT_FEATURE_SPEC.feature_names)
        assert len(fm.timestamps) == 200
        assert fm.timestamps[0] == T0
        assert fm.spec is DEFAULT_FEATURE_SPEC
        assert fm.target is None and fm.forward_returns is None

    def test_empty_primary_bars_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bars_1h is empty"):
            build_features(DEFAULT_FEATURE_SPEC, [], [], [])

    def test_unsorted_bars_are_refused(self) -> None:
        bars = _make_bars(30)
        shuffled = [bars[0], bars[2], bars[1], *bars[3:]]
        with pytest.raises(ValueError, match="not strictly increasing"):
            build_features(DEFAULT_FEATURE_SPEC, shuffled, [], [])

    def test_spec_name_mismatch_is_refused(self) -> None:
        stale = FeatureSpec(2, 4, 2000, 500, 120, tuple(_all_feature_names()[:-1]))
        with pytest.raises(ValueError, match="bump spec_version"):
            build_features(
                stale,
                _make_bars(200),
                _make_bars(50, interval_hours=4),
                _make_bars(30, interval_hours=24),
            )

    def test_rsi_range(self) -> None:
        """Per-TF RSI is in [0, 100] wherever a closed bar's RSI exists.

        The 1d RSI(14) needs 15 closed daily bars, so it first becomes visible
        to a 1h row at hour 360 (the 15th day) — the join never leaks it early.
        """
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_bars(400),
            _make_bars(100, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        for name in ("rsi_14_1h", "rsi_14_4h", "rsi_14_1d"):
            vals = fm.features[:, fm.feature_names.index(name)]
            valid = vals[~np.isnan(vals)]
            assert len(valid) > 0, name
            assert np.all(valid >= 0.0)
            assert np.all(valid <= 100.0)
        d1 = fm.features[:, fm.feature_names.index("rsi_14_1d")]
        assert np.all(np.isnan(d1[:360]))
        assert np.any(~np.isnan(d1[360:]))

    def test_candle_ratios_bounded(self) -> None:
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_bars(200),
            _make_bars(50, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        for name in ("candle_body_ratio_1h", "upper_wick_ratio_1h", "lower_wick_ratio_1h"):
            idx = fm.feature_names.index(name)
            vals = fm.features[:, idx]
            valid = vals[~np.isnan(vals)]
            assert np.all(valid >= 0.0), f"{name} has negative values"
            assert np.all(valid <= 1.0 + 1e-9), f"{name} exceeds 1.0"

    def test_temporal_sin_cos_identity(self) -> None:
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_bars(200),
            _make_bars(50, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        s = fm.features[:, fm.feature_names.index("hour_sin")]
        c = fm.features[:, fm.feature_names.index("hour_cos")]
        np.testing.assert_allclose(s**2 + c**2, 1.0, atol=1e-12)

    def test_constant_price_edge_case(self) -> None:
        fm = build_features(
            DEFAULT_FEATURE_SPEC,
            _make_constant_bars(200),
            _make_constant_bars(50, interval_hours=4),
            _make_constant_bars(30, interval_hours=24),
        )
        idx = fm.feature_names.index("log_return_1h")
        valid = fm.features[:, idx][~np.isnan(fm.features[:, idx])]
        np.testing.assert_allclose(valid, 0.0, atol=1e-12)

    def test_missing_higher_tf_is_all_nan(self) -> None:
        fm = build_features(DEFAULT_FEATURE_SPEC, _make_bars(200), [], [])
        for name in ("rsi_14_4h", "log_return_4h", "rsi_14_1d", "log_return_1d"):
            idx = fm.feature_names.index(name)
            assert np.all(np.isnan(fm.features[:, idx])), name


# ---------------------------------------------------------------------------
# The timestamp join (SM-14: no count-aligned timeframes)
# ---------------------------------------------------------------------------


class TestTimestampJoin:
    """Higher-TF features attach by timestamp, never by bar count."""

    @staticmethod
    def _gappy_setup() -> tuple[list[OHLCV], list[OHLCV]]:
        """48 hourly bars; 4h bars at 0h/4h, a 2-bar hole, then 16h/20h on."""
        bars_1h = [_bar(T0 + i * H, 100.0 + i) for i in range(48)]
        closes = [100.0, 110.0, 132.0, 92.4]
        opens = [T0, T0 + 4 * H, T0 + 16 * H, T0 + 20 * H]
        bars_4h = [_bar(ts, close) for ts, close in zip(opens, closes, strict=True)]
        return bars_1h, bars_4h

    def test_gappy_4h_series_aligns_by_timestamp(self) -> None:
        """log_return_4h at 1h bar T is the log return of the 4h bar closed by T."""
        bars_1h, bars_4h = self._gappy_setup()
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, [])
        idx = fm.feature_names.index("log_return_4h")
        col = fm.features[:, idx]

        # nothing closed yet: the first 4h bar closes at 04:00
        assert np.all(np.isnan(col[0:4]))
        # the 4h bar opened 00:00 is the first bar: its own log return is NaN
        assert np.all(np.isnan(col[4:8]))
        # the bar opened 04:00 (close 110) is visible 08:00-19:00:
        # the 16:00 bar only closes at 20:00, so it must NOT leak in early
        np.testing.assert_allclose(col[8:20], math.log(110.0 / 100.0), rtol=1e-12)
        # the bar opened 16:00 (close 132) is visible 20:00-23:00
        np.testing.assert_allclose(col[20:24], math.log(132.0 / 110.0), rtol=1e-12)
        # the bar opened 20:00 (close 92.4) is visible from 00:00 next day
        np.testing.assert_allclose(col[24:28], math.log(92.4 / 132.0), rtol=1e-12)

    def test_no_lookahead_boundary(self) -> None:
        """A 4h bar's features first appear at the 1h bar exactly 4h after its open."""
        bars_1h, bars_4h = self._gappy_setup()
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, [])
        idx = fm.feature_names.index("log_return_4h")
        col = fm.features[:, idx]
        # the 04:00 bar closes at 08:00: hour 7 still sees the 00:00 bar (NaN),
        # hour 8 sees it for the first time
        assert np.isnan(col[7])
        np.testing.assert_allclose(col[8], math.log(110.0 / 100.0), rtol=1e-12)

    def test_late_starting_series_never_count_aligns(self) -> None:
        """A higher-TF series that starts late contributes only from its first close."""
        bars_1h = [_bar(T0 + i * H, 100.0 + i) for i in range(60)]
        bars_4h = [
            _bar(T0 + 40 * H, 100.0),
            _bar(T0 + 44 * H, 121.0),
            _bar(T0 + 48 * H, 108.9),
        ]
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, [])
        idx = fm.feature_names.index("log_return_4h")
        col = fm.features[:, idx]
        # nothing has closed before 44:00; the 40:00 bar's own log return is NaN
        assert np.all(np.isnan(col[:48]))
        # the 44:00 bar (close 121) closes at 48:00 and is visible from there
        np.testing.assert_allclose(col[48:52], math.log(121.0 / 100.0), rtol=1e-12)
        # the 48:00 bar (close 108.9) closes at 52:00
        np.testing.assert_allclose(col[52:56], math.log(108.9 / 121.0), rtol=1e-12)

    def test_1d_series_with_gaps(self) -> None:
        bars_1h = [_bar(T0 + i * H, 100.0 + i) for i in range(96)]
        bars_1d = [
            _bar(T0, 100.0),
            _bar(T0 + timedelta(days=1), 130.0),
            _bar(T0 + timedelta(days=4), 104.0),
        ]
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, [], bars_1d)
        col = fm.features[:, fm.feature_names.index("log_return_1d")]
        # nothing has closed before hour 24, and the day-0 bar's own log return is NaN
        assert np.all(np.isnan(col[:48]))
        # the day-1 bar (close 130) closes at hour 48 and is visible to the end
        np.testing.assert_allclose(col[48:96], math.log(130.0 / 100.0), rtol=1e-12)
        # the day-4 bar only closes at hour 96, past the window: never visible


# ---------------------------------------------------------------------------
# The SM-14 parity pin: the last training row IS the live vector
# ---------------------------------------------------------------------------


class TestTrainingServingParity:
    def test_last_training_row_equals_live_vector_exactly(self) -> None:
        bars_1h = _make_bars(200)
        bars_4h = _make_bars(50, interval_hours=4)
        bars_1d = _make_bars(30, interval_hours=24)
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)
        live = build_live_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)
        np.testing.assert_allclose(fm.features[-1], live, rtol=0.0, atol=0.0)

    def test_parity_holds_with_gappy_higher_tf(self) -> None:
        """Parity must survive exactly the data shape that breaks count alignment."""
        bars_1h, bars_4h = TestTimestampJoin._gappy_setup()
        bars_1d = [_bar(T0 + timedelta(days=d), 100.0 + d) for d in range(2)]
        fm = build_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)
        live = build_live_features(DEFAULT_FEATURE_SPEC, bars_1h, bars_4h, bars_1d)
        np.testing.assert_allclose(fm.features[-1], live, rtol=0.0, atol=0.0)

    def test_live_vector_shape(self) -> None:
        bars_1h = _make_bars(200)
        live = build_live_features(
            DEFAULT_FEATURE_SPEC,
            bars_1h,
            _make_bars(50, interval_hours=4),
            _make_bars(30, interval_hours=24),
        )
        assert live.ndim == 1
        assert live.shape[0] == 67
