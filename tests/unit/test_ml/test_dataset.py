"""Tests for hydra.ml.dataset — the honest dataset builder and its provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import numpy as np
import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.errors import NoMarketDataError
from hydra.data.storage import MarketDataRepository
from hydra.ml.dataset import DatasetBuilder, DatasetProvenance
from hydra.ml.features import FeatureSpec, _all_feature_names

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


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


def _hourly(closes: list[float], start: datetime = T0) -> list[OHLCV]:
    return [_bar(start + i * H, close) for i, close in enumerate(closes)]


class _FakeRepo:
    """Stands in for MarketDataRepository's read methods only."""

    def __init__(
        self,
        bars_by_tf: dict[Timeframe, list[OHLCV]] | None = None,
        stored_range: tuple[datetime, datetime, int] | None = None,
        source_counts: dict[Timeframe, dict[str, int]] | None = None,
    ) -> None:
        self.bars_by_tf = bars_by_tf or {}
        self.stored_range = stored_range
        self.source_counts = source_counts or {}
        self.latest_calls: list[tuple[str, str, Timeframe, int]] = []
        self.source_calls: list[tuple[str, str, Timeframe]] = []

    async def get_latest_bars(
        self, exchange: str, symbol: str, timeframe: Timeframe, limit: int
    ) -> list[OHLCV]:
        self.latest_calls.append((exchange, symbol, timeframe, limit))
        return self.bars_by_tf.get(timeframe, [])[-limit:]

    async def get_bar_range(
        self,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[datetime, datetime, int] | None:
        return self.stored_range

    async def get_source_counts(
        self,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, int]:
        self.source_calls.append((exchange, symbol, timeframe))
        return dict(self.source_counts.get(timeframe, {}))


def _as_repo(fake: _FakeRepo) -> MarketDataRepository:
    return cast("MarketDataRepository", fake)


def _spec(horizon: int = 1, w1: int = 500, w4: int = 100, w1d: int = 30) -> FeatureSpec:
    return FeatureSpec(
        spec_version=3,
        horizon=horizon,
        window_1h=w1,
        window_4h=w4,
        window_1d=w1d,
        feature_names=tuple(_all_feature_names()),
    )


def _three_tf_bars(n_1h: int = 300) -> dict[Timeframe, list[OHLCV]]:
    rng = np.random.default_rng(7)
    closes_1h = 100.0 * np.cumprod(1.0 + rng.normal(0, 0.01, n_1h))
    bars = {
        Timeframe.H1: _hourly([float(c) for c in closes_1h]),
        Timeframe.H4: [
            _bar(T0 + timedelta(hours=4 * i), float(closes_1h[min(4 * i, n_1h - 1)]))
            for i in range(n_1h // 4)
        ],
        Timeframe.D1: [
            _bar(T0 + timedelta(days=i), float(closes_1h[min(24 * i, n_1h - 1)]))
            for i in range(n_1h // 24)
        ],
    }
    return bars


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


class TestDatasetBuild:
    async def test_reads_the_spec_windows_per_timeframe(self) -> None:
        repo = _FakeRepo(_three_tf_bars())
        builder = DatasetBuilder(_as_repo(repo))
        await builder.build(_spec(w1=123, w4=45, w1d=6), "binance", "BTCUSDT")
        assert repo.latest_calls == [
            ("binance", "BTCUSDT", Timeframe.H1, 123),
            ("binance", "BTCUSDT", Timeframe.H4, 45),
            ("binance", "BTCUSDT", Timeframe.D1, 6),
        ]

    async def test_matrix_shape_and_target_alignment(self) -> None:
        closes = [100.0, 101.0, 100.0, 102.0, 102.0, 103.0]
        repo = _FakeRepo({Timeframe.H1: _hourly(closes)})
        matrix, provenance = await DatasetBuilder(_as_repo(repo)).build(
            _spec(horizon=2), "binance", "BTCUSDT"
        )
        assert matrix.features.shape == (len(closes), 67)
        assert matrix.timestamps == [b.timestamp for b in _hourly(closes)]
        assert matrix.spec.horizon == 2
        # the last `horizon` rows have no forward bar: their target is NaN
        assert np.all(np.isnan(matrix.target[-2:]))
        assert np.all(np.isnan(matrix.forward_returns[-2:]))
        # hand-checked signs and forward returns for horizon 2
        expected_fwd = [closes[i + 2] / closes[i] - 1.0 for i in range(len(closes) - 2)]
        np.testing.assert_allclose(matrix.forward_returns[:-2], expected_fwd, rtol=1e-12)
        np.testing.assert_array_equal(matrix.target[:-2], np.sign(np.asarray(expected_fwd)))
        # the zero forward return (100 -> 100 after two bars) is sign 0, for the trainer to drop
        assert matrix.target[0] == 0.0
        assert provenance.spec_version == 3
        assert provenance.horizon == 2

    async def test_missing_primary_bars_raise_no_market_data(self) -> None:
        repo = _FakeRepo({})
        with pytest.raises(NoMarketDataError, match="No market data"):
            await DatasetBuilder(_as_repo(repo)).build(_spec(), "binance", "BTCUSDT")

    async def test_missing_higher_tf_is_all_nan_but_disclosed(self) -> None:
        repo = _FakeRepo({Timeframe.H1: _hourly([100.0 + i for i in range(300)])})
        matrix, provenance = await DatasetBuilder(_as_repo(repo)).build(
            _spec(), "binance", "BTCUSDT"
        )
        for name in ("rsi_14_4h", "rsi_14_1d"):
            assert np.all(np.isnan(matrix.features[:, matrix.feature_names.index(name)]))
        by_tf = {c.timeframe: c for c in provenance.coverage}
        assert by_tf["4h"].bar_count == 0
        assert by_tf["1d"].bar_count == 0
        assert by_tf["4h"].first_ts is None and by_tf["4h"].last_ts is None


# ---------------------------------------------------------------------------
# Provenance (the P0-B discipline)
# ---------------------------------------------------------------------------


class TestProvenance:
    async def test_ranges_gaps_and_source_mix_reported(self) -> None:
        bars = _three_tf_bars(300)
        # punch a hole in the 1h series: remove one hour in the middle
        gappy_1h = bars[Timeframe.H1][:100] + bars[Timeframe.H1][101:]
        repo = _FakeRepo(
            {**bars, Timeframe.H1: gappy_1h},
            source_counts={
                Timeframe.H1: {"feed": 250, "auto": 49},
                Timeframe.H4: {"feed": 75},
                Timeframe.D1: {"auto": 12},
            },
        )
        _, provenance = await DatasetBuilder(_as_repo(repo)).build(_spec(), "binance", "BTCUSDT")
        assert isinstance(provenance, DatasetProvenance)
        assert provenance.exchange == "binance"
        assert provenance.symbol == "BTCUSDT"

        by_tf = {c.timeframe: c for c in provenance.coverage}
        assert set(by_tf) == {"1h", "4h", "1d"}

        h1 = by_tf["1h"]
        assert h1.bar_count == 299
        assert h1.first_ts == T0
        assert h1.last_ts == T0 + 299 * H
        assert h1.gap_count == 1  # the punched hole
        assert h1.sources == {"feed": 250, "auto": 49}

        h4 = by_tf["4h"]
        assert h4.bar_count == 75
        assert h4.first_ts == T0
        assert h4.last_ts == T0 + 74 * 4 * H
        assert h4.gap_count == 0
        assert h4.sources == {"feed": 75}

        d1 = by_tf["1d"]
        assert d1.bar_count == 12
        assert d1.gap_count == 0
        assert d1.sources == {"auto": 12}

        assert provenance.to_dict() == {
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "spec_version": 3,
            "horizon": 1,
            "coverage": [by_tf["1h"].to_dict(), by_tf["4h"].to_dict(), by_tf["1d"].to_dict()],
        }

    async def test_source_counts_are_scoped_to_the_read_range(self) -> None:
        bars = _three_tf_bars(300)
        repo = _FakeRepo(bars)
        await DatasetBuilder(_as_repo(repo)).build(_spec(w1=50, w4=10, w1d=3), "binance", "BTCUSDT")
        # one source-mix query per timeframe, over the bars actually read
        assert repo.source_calls == [
            ("binance", "BTCUSDT", Timeframe.H1),
            ("binance", "BTCUSDT", Timeframe.H4),
            ("binance", "BTCUSDT", Timeframe.D1),
        ]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    async def test_same_rows_and_spec_hash_produce_byte_identical_matrix(self) -> None:
        bars = _three_tf_bars(300)
        matrix_a, provenance_a = await DatasetBuilder(_as_repo(_FakeRepo(bars))).build(
            _spec(), "binance", "BTCUSDT"
        )
        matrix_b, provenance_b = await DatasetBuilder(_as_repo(_FakeRepo(bars))).build(
            _spec(), "binance", "BTCUSDT"
        )
        assert hash(matrix_a.spec) == hash(matrix_b.spec)
        assert matrix_a.features.tobytes() == matrix_b.features.tobytes()
        assert matrix_a.target is not None and matrix_b.target is not None
        assert matrix_a.target.tobytes() == matrix_b.target.tobytes()
        assert matrix_a.forward_returns is not None and matrix_b.forward_returns is not None
        assert matrix_a.forward_returns.tobytes() == matrix_b.forward_returns.tobytes()
        assert provenance_a == provenance_b

    async def test_a_shorter_window_changes_the_matrix(self) -> None:
        bars = _three_tf_bars(300)
        matrix_full, _ = await DatasetBuilder(_as_repo(_FakeRepo(bars))).build(
            _spec(), "binance", "BTCUSDT"
        )
        matrix_short, _ = await DatasetBuilder(_as_repo(_FakeRepo(bars))).build(
            _spec(w1=200), "binance", "BTCUSDT"
        )
        assert matrix_full.features.tobytes() != matrix_short.features.tobytes()
