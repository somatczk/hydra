"""Tests for hydra.data.backfill — gap detection and backfill service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.backfill import ExchangeBackfillService, GapRange

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(ts: datetime) -> OHLCV:
    """Build a simple OHLCV bar at the given timestamp."""
    return OHLCV(
        open=Decimal("42000"),
        high=Decimal("42500"),
        low=Decimal("41800"),
        close=Decimal("42100"),
        volume=Decimal("100"),
        timestamp=ts,
    )


def _ts(minute: int) -> datetime:
    """Shortcut to create a UTC datetime at a specific minute on 2024-01-01."""
    return datetime(2024, 1, 1, 0, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# detect_gaps
# ---------------------------------------------------------------------------


class TestDetectGaps:
    """Tests for ExchangeBackfillService.detect_gaps()."""

    @pytest.fixture
    def mock_repo(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def service(self, mock_repo: AsyncMock) -> ExchangeBackfillService:
        return ExchangeBackfillService(repository=mock_repo)

    async def test_finds_gap_in_middle(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """A gap in the middle of a sequence is detected."""
        # Return bars for minutes 0, 1, 4 (missing 2, 3)
        mock_repo.get_bars.return_value = [
            _make_bar(_ts(0)),
            _make_bar(_ts(1)),
            _make_bar(_ts(4)),
        ]

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert len(gaps) == 1
        assert gaps[0].start == _ts(2)
        assert gaps[0].end == _ts(3)

    async def test_finds_gap_at_start(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """A gap at the beginning of the range is detected."""
        mock_repo.get_bars.return_value = [
            _make_bar(_ts(3)),
            _make_bar(_ts(4)),
        ]

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert len(gaps) == 1
        assert gaps[0].start == _ts(0)
        assert gaps[0].end == _ts(2)

    async def test_finds_gap_at_end(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """A gap at the end of the range is detected."""
        mock_repo.get_bars.return_value = [
            _make_bar(_ts(0)),
            _make_bar(_ts(1)),
        ]

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert len(gaps) == 1
        assert gaps[0].start == _ts(2)
        assert gaps[0].end == _ts(4)

    async def test_returns_empty_for_complete_data(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """No gaps when all bars are present."""
        mock_repo.get_bars.return_value = [
            _make_bar(_ts(0)),
            _make_bar(_ts(1)),
            _make_bar(_ts(2)),
            _make_bar(_ts(3)),
            _make_bar(_ts(4)),
        ]

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert gaps == []

    async def test_entirely_missing_range(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """An entirely empty range returns one big gap."""
        mock_repo.get_bars.return_value = []

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert len(gaps) == 1
        assert gaps[0].start == _ts(0)
        assert gaps[0].end == _ts(4)

    async def test_multiple_gaps(
        self, service: ExchangeBackfillService, mock_repo: AsyncMock
    ) -> None:
        """Multiple disjoint gaps are detected."""
        # Present: 0, 2, 4 — missing: 1, 3
        mock_repo.get_bars.return_value = [
            _make_bar(_ts(0)),
            _make_bar(_ts(2)),
            _make_bar(_ts(4)),
        ]

        gaps = await service.detect_gaps(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(4),
        )

        assert len(gaps) == 2
        assert gaps[0] == GapRange(start=_ts(1), end=_ts(1))
        assert gaps[1] == GapRange(start=_ts(3), end=_ts(3))


# ---------------------------------------------------------------------------
# bulk_download
# ---------------------------------------------------------------------------


class TestBulkDownload:
    """Tests for ExchangeBackfillService.bulk_download()."""

    async def test_downloads_and_stores(self) -> None:
        """Paginated fetch_ohlcv data is normalized and stored."""
        mock_repo = AsyncMock()
        mock_repo.store_bars = AsyncMock(return_value=3)

        # Simulate CCXT returning 3 bars
        raw_bars = [
            [1704067200000, 42000, 42500, 41800, 42100, 100],  # 00:00
            [1704067260000, 42100, 42600, 41900, 42200, 110],  # 00:01
            [1704067320000, 42200, 42700, 42000, 42300, 120],  # 00:02
        ]

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv = AsyncMock(side_effect=[raw_bars, []])

        service = ExchangeBackfillService(
            repository=mock_repo,
            exchange_factories={"binance": lambda: mock_exchange},
        )

        count = await service.bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 0, 2, tzinfo=UTC),
        )

        assert count == 3
        assert mock_repo.store_bars.call_count == 1

        # Verify the bars passed to store_bars
        stored = mock_repo.store_bars.call_args[0][0]
        assert len(stored) == 3
        assert stored[0][0] == "binance"
        assert stored[0][1] == "BTCUSDT"
        assert stored[0][2] == Timeframe.M1

    async def test_respects_end_boundary(self) -> None:
        """Bars beyond the end timestamp are not stored."""
        mock_repo = AsyncMock()
        mock_repo.store_bars = AsyncMock(return_value=1)

        raw_bars = [
            [1704067200000, 42000, 42500, 41800, 42100, 100],  # 00:00
            [1704067260000, 42100, 42600, 41900, 42200, 110],  # 00:01
            [1704067320000, 42200, 42700, 42000, 42300, 120],  # 00:02 — beyond end
        ]

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv = AsyncMock(side_effect=[raw_bars, []])

        service = ExchangeBackfillService(
            repository=mock_repo,
            exchange_factories={"binance": lambda: mock_exchange},
        )

        count = await service.bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        )

        # Only 2 bars should be stored (00:00 and 00:01)
        assert count == 2

    async def test_no_exchange_factory_raises(self) -> None:
        """Missing exchange factory raises ValueError."""
        mock_repo = AsyncMock()
        service = ExchangeBackfillService(repository=mock_repo)

        with pytest.raises(ValueError, match="No exchange factory"):
            await service.bulk_download(
                exchange_id="binance",
                symbol="BTCUSDT",
                timeframe=Timeframe.M1,
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
            )

    async def test_empty_response_stops_pagination(self) -> None:
        """An empty response from the exchange stops the download loop."""
        mock_repo = AsyncMock()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv = AsyncMock(return_value=[])

        service = ExchangeBackfillService(
            repository=mock_repo,
            exchange_factories={"binance": lambda: mock_exchange},
        )

        count = await service.bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
        )

        assert count == 0
        mock_repo.store_bars.assert_not_called()
