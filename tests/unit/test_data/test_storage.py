"""Tests for hydra.data.storage — MarketDataRepository with mocked asyncpg."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.storage import MarketDataRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    minute: int = 0,
    close: str = "42100.00",
) -> OHLCV:
    """Build a simple OHLCV bar at a specific minute."""
    return OHLCV(
        open=Decimal("42000.00"),
        high=Decimal("42500.00"),
        low=Decimal("41800.00"),
        close=Decimal(close),
        volume=Decimal("100.00"),
        timestamp=datetime(2024, 1, 1, 0, minute, tzinfo=UTC),
    )


def _make_record(minute: int = 0, close: str = "42100.00") -> dict:
    """Build a dict mimicking an asyncpg Record for OHLCV queries."""
    return {
        "open": Decimal("42000.00"),
        "high": Decimal("42500.00"),
        "low": Decimal("41800.00"),
        "close": Decimal(close),
        "volume": Decimal("100.00"),
        "timestamp": datetime(2024, 1, 1, 0, minute, tzinfo=UTC),
    }


class _FakeRecord(dict):
    """dict subclass that supports both item and attribute access like asyncpg.Record."""

    def __getitem__(self, key):  # type: ignore[override]
        return super().__getitem__(key)


def _fake_record(data: dict) -> _FakeRecord:
    return _FakeRecord(data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool() -> MagicMock:
    """Create a mock asyncpg pool with a connection context manager."""
    pool = MagicMock()
    conn = AsyncMock()

    # pool.acquire() must return an async context manager (not a coroutine).
    # Using MagicMock for the pool ensures acquire() is a regular call
    # returning the context manager directly.
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    pool._conn = conn  # stash for test access
    return pool


@pytest.fixture
def repo(mock_pool: MagicMock) -> MarketDataRepository:
    """Create a repository with a pre-injected mock pool."""
    r = MarketDataRepository(dsn="postgresql://test:test@localhost/test")
    r._pool = mock_pool
    return r


# ---------------------------------------------------------------------------
# store_bars / get_bars roundtrip
# ---------------------------------------------------------------------------


class TestStoreBars:
    """Tests for MarketDataRepository.store_bars()."""

    async def test_store_bars_calls_executemany(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """store_bars builds correct rows and calls executemany."""
        conn = mock_pool._conn
        conn.executemany = AsyncMock(return_value="INSERT 0 2")

        bars = [
            ("binance", "BTCUSDT", Timeframe.M1, _make_bar(0)),
            ("binance", "BTCUSDT", Timeframe.M1, _make_bar(1)),
        ]

        result = await repo.store_bars(bars)

        assert result == 2
        conn.executemany.assert_called_once()

        # Verify the query contains ON CONFLICT
        call_args = conn.executemany.call_args
        query = call_args[0][0]
        assert "ON CONFLICT" in query
        assert "DO NOTHING" in query

    async def test_store_bars_empty_list(self, repo: MarketDataRepository) -> None:
        """Empty bar list returns 0 without hitting the database."""
        result = await repo.store_bars([])
        assert result == 0

    async def test_store_bars_row_data(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """Verify the row tuples sent to executemany."""
        conn = mock_pool._conn
        conn.executemany = AsyncMock()

        bar = _make_bar(0)
        await repo.store_bars([("binance", "BTCUSDT", Timeframe.M1, bar)])

        rows = conn.executemany.call_args[0][1]
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "binance"
        assert row[1] == "BTCUSDT"
        assert row[2] == "1m"
        assert row[3] == bar.timestamp
        assert row[4] == bar.open
        assert row[5] == bar.high


# ---------------------------------------------------------------------------
# get_bars
# ---------------------------------------------------------------------------


class TestGetBars:
    """Tests for MarketDataRepository.get_bars()."""

    async def test_get_bars_returns_ohlcv_list(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bars converts records to OHLCV objects."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(
            return_value=[
                _fake_record(_make_record(0)),
                _fake_record(_make_record(1)),
            ]
        )

        bars = await repo.get_bars("binance", "BTCUSDT", Timeframe.M1)

        assert len(bars) == 2
        assert all(isinstance(b, OHLCV) for b in bars)
        assert bars[0].timestamp < bars[1].timestamp

    async def test_get_bars_with_date_range(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bars passes start/end to the query."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 2, tzinfo=UTC)

        await repo.get_bars("binance", "BTCUSDT", Timeframe.M1, start=start, end=end)

        call_args = conn.fetch.call_args
        query = call_args[0][0]
        assert "timestamp >=" in query
        assert "timestamp <=" in query

    async def test_get_bars_with_limit(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bars includes LIMIT clause when specified."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        await repo.get_bars("binance", "BTCUSDT", Timeframe.M1, limit=100)

        call_args = conn.fetch.call_args
        query = call_args[0][0]
        assert "LIMIT" in query

    async def test_get_bars_empty_result(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bars returns empty list when no data matches."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        bars = await repo.get_bars("binance", "BTCUSDT", Timeframe.M1)
        assert bars == []


# ---------------------------------------------------------------------------
# get_latest_bar
# ---------------------------------------------------------------------------


class TestGetLatestBar:
    """Tests for MarketDataRepository.get_latest_bar()."""

    async def test_returns_most_recent(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_latest_bar returns the most recent bar."""
        conn = mock_pool._conn
        conn.fetchrow = AsyncMock(return_value=_fake_record(_make_record(5)))

        bar = await repo.get_latest_bar("binance", "BTCUSDT", Timeframe.M1)

        assert bar is not None
        assert isinstance(bar, OHLCV)
        assert bar.timestamp == datetime(2024, 1, 1, 0, 5, tzinfo=UTC)

    async def test_returns_none_when_empty(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_latest_bar returns None when no data exists."""
        conn = mock_pool._conn
        conn.fetchrow = AsyncMock(return_value=None)

        bar = await repo.get_latest_bar("binance", "BTCUSDT", Timeframe.M1)
        assert bar is None

    async def test_query_orders_desc(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_latest_bar query orders by timestamp DESC LIMIT 1."""
        conn = mock_pool._conn
        conn.fetchrow = AsyncMock(return_value=None)

        await repo.get_latest_bar("binance", "BTCUSDT", Timeframe.M1)

        query = conn.fetchrow.call_args[0][0]
        assert "ORDER BY timestamp DESC" in query
        assert "LIMIT 1" in query


# ---------------------------------------------------------------------------
# get_bar_count
# ---------------------------------------------------------------------------


class TestGetBarCount:
    """Tests for MarketDataRepository.get_bar_count()."""

    async def test_returns_count(self, repo: MarketDataRepository, mock_pool: MagicMock) -> None:
        """get_bar_count returns integer count."""
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=42)

        count = await repo.get_bar_count("binance", "BTCUSDT", Timeframe.M1)
        assert count == 42

    async def test_returns_zero_when_empty(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bar_count returns 0 when no data matches."""
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=0)

        count = await repo.get_bar_count("binance", "BTCUSDT", Timeframe.M1)
        assert count == 0

    async def test_with_date_range(self, repo: MarketDataRepository, mock_pool: MagicMock) -> None:
        """get_bar_count passes start/end to the query."""
        conn = mock_pool._conn
        conn.fetchval = AsyncMock(return_value=10)

        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 1, 2, tzinfo=UTC)

        count = await repo.get_bar_count("binance", "BTCUSDT", Timeframe.M1, start=start, end=end)

        query = conn.fetchval.call_args[0][0]
        assert "timestamp >=" in query
        assert "timestamp <=" in query
        assert count == 10


# ---------------------------------------------------------------------------
# ON CONFLICT deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Test that duplicate bars are handled via ON CONFLICT DO NOTHING."""

    async def test_duplicate_insert_uses_on_conflict(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """The INSERT query includes ON CONFLICT DO NOTHING."""
        conn = mock_pool._conn
        conn.executemany = AsyncMock()

        bar = _make_bar(0)
        await repo.store_bars(
            [
                ("binance", "BTCUSDT", Timeframe.M1, bar),
                ("binance", "BTCUSDT", Timeframe.M1, bar),  # duplicate
            ]
        )

        query = conn.executemany.call_args[0][0]
        assert "ON CONFLICT" in query
        assert "DO NOTHING" in query


# ---------------------------------------------------------------------------
# Not connected
# ---------------------------------------------------------------------------


class TestNotConnected:
    """Test that operations fail gracefully when not connected."""

    async def test_store_bars_raises_when_not_connected(self) -> None:
        """store_bars raises RuntimeError if pool is not initialized."""
        repo = MarketDataRepository(dsn="postgresql://test:test@localhost/test")

        with pytest.raises(RuntimeError, match="not connected"):
            await repo.store_bars([("binance", "BTCUSDT", Timeframe.M1, _make_bar(0))])

    async def test_get_bars_raises_when_not_connected(self) -> None:
        """get_bars raises RuntimeError if pool is not initialized."""
        repo = MarketDataRepository(dsn="postgresql://test:test@localhost/test")

        with pytest.raises(RuntimeError, match="not connected"):
            await repo.get_bars("binance", "BTCUSDT", Timeframe.M1)
