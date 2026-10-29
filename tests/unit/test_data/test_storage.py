"""Tests for hydra.data.storage — MarketDataRepository with mocked asyncpg."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.storage import (
    DataCacheEntry,
    MarketDataRepository,
    cache_totals,
    delete_auto_dataset,
    list_cache_entries,
)

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

    async def test_store_bars_returns_rows_actually_inserted(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """One unnest INSERT; the count is the RETURNING rows, not the attempt count."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[_fake_record({"?column?": 1})])

        bars = [
            ("binance", "BTCUSDT", Timeframe.M1, _make_bar(0)),
            ("binance", "BTCUSDT", Timeframe.M1, _make_bar(1)),
        ]

        result = await repo.store_bars(bars)

        assert result == 1
        conn.fetch.assert_called_once()
        query = conn.fetch.call_args[0][0]
        assert "unnest(" in query
        assert "ON CONFLICT (exchange, symbol, timeframe, timestamp) DO NOTHING" in query
        assert "RETURNING 1" in query

    async def test_store_bars_empty_list(self, repo: MarketDataRepository) -> None:
        """Empty bar list returns 0 without hitting the database."""
        result = await repo.store_bars([])
        assert result == 0

    async def test_store_bars_column_arrays(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """Each query parameter is one column array, in table order."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        bar0, bar1 = _make_bar(0), _make_bar(1, close="42200.50")
        await repo.store_bars(
            [
                ("binance", "BTCUSDT", Timeframe.M1, bar0),
                ("binance", "BTCUSDT", Timeframe.M1, bar1),
            ]
        )

        cols = conn.fetch.call_args[0][1:]
        assert len(cols) == 10
        assert cols[0] == ["binance", "binance"]
        assert cols[1] == ["BTCUSDT", "BTCUSDT"]
        assert cols[2] == ["1m", "1m"]
        assert cols[3] == [bar0.timestamp, bar1.timestamp]
        assert cols[7] == [Decimal("42100.00"), Decimal("42200.50")]
        assert cols[9] == ["feed", "feed"]


def _transactional(mock_pool: MagicMock) -> AsyncMock:
    conn = mock_pool._conn
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _key(minute: int) -> _FakeRecord:
    return _fake_record(
        {
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "timestamp": datetime(2024, 1, 1, 0, minute, tzinfo=UTC),
        }
    )


class TestUpsertClosedBars:
    """Tests for MarketDataRepository.upsert_closed_bars()."""

    async def test_counts_inserted_and_updated_from_returned_keys(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        # minute 0 existed and changed, minute 1 is new, minute 2 existed unchanged
        conn.fetch = AsyncMock(side_effect=[[_key(0), _key(2)], [_key(0), _key(1)]])

        result = await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(3)]
        )

        assert result == (1, 1)
        existing_sql, upsert_sql = (c[0][0] for c in conn.fetch.call_args_list)
        assert "JOIN unnest(" in existing_sql
        assert "DO UPDATE SET" in upsert_sql
        assert "IS DISTINCT FROM" in upsert_sql
        conn.transaction.assert_called_once()

    async def test_duplicate_keys_in_one_batch_keep_the_last(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """ON CONFLICT DO UPDATE cannot touch a row twice, so the batch is deduplicated."""
        conn = _transactional(mock_pool)
        conn.fetch = AsyncMock(side_effect=[[], [_key(0)]])

        result = await repo.upsert_closed_bars(
            [
                ("binance", "BTCUSDT", Timeframe.M1, _make_bar(0, close="1")),
                ("binance", "BTCUSDT", Timeframe.M1, _make_bar(0, close="2")),
            ]
        )

        assert result == (1, 0)
        cols = conn.fetch.call_args_list[1][0][1:]
        assert cols[7] == [Decimal("2")]

    async def test_empty_batch(self, repo: MarketDataRepository) -> None:
        assert await repo.upsert_closed_bars([]) == (0, 0)


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------


class TestSourceAttribution:
    """source: threaded into both INSERTs, immutable through conflicts, 'feed' by default."""

    async def test_store_bars_sql_carries_source(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        await repo.store_bars([("binance", "BTCUSDT", Timeframe.M1, _make_bar(0))])

        sql = conn.fetch.call_args[0][0]
        assert "close, volume, source" in sql
        assert "$10::text[]" in sql
        assert conn.fetch.call_args[0][10] == ["feed"]

    async def test_upsert_default_source_is_feed(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.fetch = AsyncMock(side_effect=[[_key(0)], [_key(0)]])

        await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(3)]
        )

        assert conn.fetch.call_args_list[1][0][10] == ["feed", "feed", "feed"]

    async def test_upsert_conflict_set_list_never_touches_source(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.fetch = AsyncMock(side_effect=[[], [_key(0)]])

        await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(0))], source="auto"
        )

        sql = conn.fetch.call_args_list[1][0][0]
        assert "close, volume, source" in sql
        assert "$10::text[]" in sql
        assert conn.fetch.call_args_list[1][0][10] == ["auto"]
        set_list = sql.split("DO UPDATE SET")[1].split("WHERE")[0]
        assert "source" not in set_list


# ---------------------------------------------------------------------------
# Cache-ledger writes (source='auto' only, same transaction)
# ---------------------------------------------------------------------------


def _record(exchange: str, symbol: str, timeframe: str, minute: int) -> _FakeRecord:
    return _fake_record(
        {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": datetime(2024, 1, 1, 0, minute, tzinfo=UTC),
        }
    )


class TestCacheLedgerWrites:
    """``source='auto'`` maintains ``data_cache_entries`` for every dataset that gained rows."""

    async def test_auto_insert_upserts_one_entry_per_dataset(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(side_effect=[[], [_key(0), _key(1)]])

        result = await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(2)], source="auto"
        )

        assert result == (2, 0)
        assert conn.execute.await_count == 1
        sql, *params = conn.execute.call_args[0]
        assert "data_cache_entries" in sql
        assert "ON CONFLICT (exchange, symbol, timeframe)" in sql
        assert "rows = entry.rows + EXCLUDED.rows" in sql
        assert params == [
            "binance",
            "BTCUSDT",
            "1m",
            _make_bar(0).timestamp,
            _make_bar(1).timestamp,
            2,
        ]

    async def test_overlapping_page_carries_page_bounds_and_new_rows_only(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """Existing keys 3-4 get corrected; 5-7 are new: the entry gains 3 rows, spans 3-7."""
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(
            side_effect=[[_key(3), _key(4)], [_key(3), _key(4), _key(5), _key(6), _key(7)]]
        )

        result = await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(3, 8)],
            source="auto",
        )

        assert result == (3, 2)
        *_, first_ts, last_ts, inserted_rows = conn.execute.call_args[0]
        assert (first_ts, last_ts, inserted_rows) == (
            _make_bar(3).timestamp,
            _make_bar(7).timestamp,
            3,
        )

    async def test_feed_default_writes_no_entry(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(side_effect=[[], [_key(0), _key(1)]])

        await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(2)]
        )

        conn.execute.assert_not_called()

    async def test_auto_upsert_with_no_new_rows_writes_no_entry(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        conn.fetch = AsyncMock(side_effect=[[_key(0)], [_key(0)]])

        result = await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(0, close="9"))], source="auto"
        )

        assert result == (0, 1)
        conn.execute.assert_not_called()

    async def test_mixed_batch_writes_one_entry_per_market(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        records = [
            _record("binance", "BTCUSDT", "1m", 0),
            _record("binance", "BTCUSDT", "1m", 1),
            _record("binance", "ETHUSDT", "1m", 5),
            _record("binance", "ETHUSDT", "1m", 6),
        ]
        conn.fetch = AsyncMock(side_effect=[[], records])

        await repo.upsert_closed_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _make_bar(m)) for m in range(2)]
            + [("binance", "ETHUSDT", Timeframe.M1, _make_bar(m)) for m in (5, 6)],
            source="auto",
        )

        assert conn.execute.await_count == 2
        btc = conn.execute.call_args_list[0][0]
        eth = conn.execute.call_args_list[1][0]
        assert btc[1:4] == ("binance", "BTCUSDT", "1m")
        assert btc[4:] == (_make_bar(0).timestamp, _make_bar(1).timestamp, 2)
        assert eth[1:4] == ("binance", "ETHUSDT", "1m")
        assert eth[4:] == (_make_bar(5).timestamp, _make_bar(6).timestamp, 2)


# ---------------------------------------------------------------------------
# Cache-ledger reads and eviction seam
# ---------------------------------------------------------------------------


class TestDataCacheFunctions:
    """The pool-level seam C2's sweeper consumes: delete, list, totals."""

    async def test_delete_auto_dataset_counts_deleted_rows_and_drops_the_entry(
        self, mock_pool: MagicMock
    ) -> None:
        conn = _transactional(mock_pool)
        conn.execute = AsyncMock(side_effect=["DELETE 7", "DELETE 1"])

        deleted = await delete_auto_dataset(mock_pool, "binance", "BTCUSDT", Timeframe.M1)

        assert deleted == 7
        bars_sql, *bars_params = conn.execute.call_args_list[0][0]
        entry_sql, *entry_params = conn.execute.call_args_list[1][0]
        assert "source = 'auto'" in bars_sql
        assert bars_params == ["binance", "BTCUSDT", "1m"]
        assert "data_cache_entries" in entry_sql
        assert entry_params == ["binance", "BTCUSDT", "1m"]
        conn.transaction.assert_called_once()

    async def test_list_cache_entries_orders_by_last_used(self, mock_pool: MagicMock) -> None:
        conn = mock_pool._conn
        now = datetime(2024, 1, 1, tzinfo=UTC)
        conn.fetch = AsyncMock(
            return_value=[
                _fake_record(
                    {
                        "exchange": "binance",
                        "symbol": "BTCUSDT",
                        "timeframe": "1m",
                        "first_ts": now,
                        "last_ts": now + timedelta(minutes=9),
                        "rows": 10,
                        "status": "ready",
                        "created_at": now,
                        "last_fetched_at": now,
                        "last_used_at": now,
                    }
                )
            ]
        )

        entries = await list_cache_entries(mock_pool)

        assert "ORDER BY last_used_at ASC" in conn.fetch.call_args[0][0]
        assert entries == [
            DataCacheEntry(
                exchange="binance",
                symbol="BTCUSDT",
                timeframe="1m",
                first_ts=now,
                last_ts=now + timedelta(minutes=9),
                rows=10,
                status="ready",
                created_at=now,
                last_fetched_at=now,
                last_used_at=now,
            )
        ]

    async def test_touch_cache_entry_bumps_last_used_at(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = mock_pool._conn
        conn.execute = AsyncMock(return_value="UPDATE 1")

        await repo.touch_cache_entry("binance", "BTCUSDT", Timeframe.H1)

        sql, *params = conn.execute.call_args[0]
        assert "UPDATE data_cache_entries SET last_used_at = now()" in sql
        assert "WHERE exchange = $1 AND symbol = $2 AND timeframe = $3" in sql
        assert params == ["binance", "BTCUSDT", "1h"]

    async def test_cache_totals_sums_rows_and_datasets(self, mock_pool: MagicMock) -> None:
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[_fake_record({"rows": 42, "datasets": 3})])
        assert await cache_totals(mock_pool) == (42, 3)

    async def test_cache_totals_empty_ledger(self, mock_pool: MagicMock) -> None:
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])
        assert await cache_totals(mock_pool) == (0, 0)


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

        query, *params = conn.fetch.call_args[0]
        assert "timestamp >= $4" in query
        assert "timestamp <= $5" in query
        assert params == ["binance", "BTCUSDT", "1m", start, end]

    async def test_get_bars_empty_result(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        """get_bars returns empty list when no data matches."""
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[])

        bars = await repo.get_bars("binance", "BTCUSDT", Timeframe.M1)
        assert bars == []


# ---------------------------------------------------------------------------
# ON CONFLICT deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Duplicate bars are handled in SQL, never by the caller."""

    async def test_duplicate_insert_uses_on_conflict(
        self, repo: MarketDataRepository, mock_pool: MagicMock
    ) -> None:
        conn = mock_pool._conn
        conn.fetch = AsyncMock(return_value=[_fake_record({"?column?": 1})])

        bar = _make_bar(0)
        inserted = await repo.store_bars(
            [
                ("binance", "BTCUSDT", Timeframe.M1, bar),
                ("binance", "BTCUSDT", Timeframe.M1, bar),  # duplicate
            ]
        )

        assert inserted == 1
        query = conn.fetch.call_args[0][0]
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


class _Conn:
    """A connection that records every statement it runs."""

    def __init__(
        self, name: str, log: list[tuple[str, str, tuple[object, ...]]], free: bool
    ) -> None:
        self.name = name
        self._log = log
        self._free = free

    async def fetchval(self, query: str, *args: object) -> bool:
        self._log.append((self.name, query, args))
        return self._free

    async def execute(self, query: str, *args: object) -> str:
        self._log.append((self.name, query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: object) -> None:
        self._log.append((self.name, query, args))


class _CountingPool:
    """Hands out a new named connection per ``acquire()``."""

    def __init__(self, free: bool = True) -> None:
        self.log: list[tuple[str, str, tuple[object, ...]]] = []
        self.acquired = 0
        self._free = free

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        self.acquired += 1
        yield _Conn(f"conn{self.acquired}", self.log, self._free)


_TRY = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))"
_LOCK = "SELECT pg_advisory_lock(hashtextextended($1, 0))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtextextended($1, 0))"


class TestDatasetLock:
    async def test_every_call_inside_runs_on_the_lock_connection(self) -> None:
        pool = _CountingPool()
        repo = MarketDataRepository.from_pool(pool)
        t0 = datetime(2024, 1, 1, tzinfo=UTC)

        async with repo.dataset_lock("binance", "BTCUSDT", Timeframe.M1) as held:
            await held.set_cache_status("binance", "BTCUSDT", Timeframe.M1, "fetching", t0, t0)
            assert await held.get_bar_range("binance", "BTCUSDT", Timeframe.M1) is None

        assert pool.acquired == 1
        assert {name for name, _, _ in pool.log} == {"conn1"}
        key = ("hydra.on_demand:binance:BTCUSDT:1m",)
        # a free lock is taken by the try alone: no wait, no blocking call
        assert pool.log[0][1:] == (_TRY, key)
        assert pool.log[-1][1:] == (_UNLOCK, key)
        assert len(pool.log) == 4

    async def test_a_held_lock_reports_the_wait_then_blocks_for_it(self) -> None:
        pool = _CountingPool(free=False)
        repo = MarketDataRepository.from_pool(pool)
        waits: list[int] = []

        async with repo.dataset_lock("binance", "BTCUSDT", Timeframe.M1, lambda: waits.append(1)):
            assert [q for _, q, _ in pool.log] == [_TRY, _LOCK]
            assert waits == [1]

        assert [q for _, q, _ in pool.log] == [_TRY, _LOCK, _UNLOCK]

    async def test_the_lock_is_released_when_the_body_raises(self) -> None:
        pool = _CountingPool()
        repo = MarketDataRepository.from_pool(pool)

        with pytest.raises(RuntimeError, match="fetch failed"):
            async with repo.dataset_lock("binance", "ETHUSDT", Timeframe.H1):
                raise RuntimeError("fetch failed")

        assert [q for _, q, _ in pool.log] == [_TRY, _UNLOCK]
        assert pool.log[0][2] == ("hydra.on_demand:binance:ETHUSDT:1h",)
