"""asyncpg-based market data repository for OHLCV bars.

Provides high-performance bulk insert and query operations against
TimescaleDB with connection pooling and ON CONFLICT deduplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, Timeframe
from hydra.data.bars import timeframe_seconds

logger = get_logger(__name__)

_STORE_BARS_SQL = """
    INSERT INTO ts.ohlcv_1m (
        exchange, symbol, timeframe, timestamp, open, high, low, close, volume, source
    )
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::timestamptz[],
        $5::numeric[], $6::numeric[], $7::numeric[], $8::numeric[], $9::numeric[],
        $10::text[]
    )
    ON CONFLICT (exchange, symbol, timeframe, timestamp) DO NOTHING
    RETURNING 1
"""

_UPSERT_CLOSED_BARS_SQL = """
    INSERT INTO ts.ohlcv_1m (
        exchange, symbol, timeframe, timestamp, open, high, low, close, volume, source
    )
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::timestamptz[],
        $5::numeric[], $6::numeric[], $7::numeric[], $8::numeric[], $9::numeric[],
        $10::text[]
    )
    ON CONFLICT (exchange, symbol, timeframe, timestamp) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume
    WHERE (ohlcv_1m.open, ohlcv_1m.high, ohlcv_1m.low, ohlcv_1m.close, ohlcv_1m.volume)
        IS DISTINCT FROM
        (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
    RETURNING exchange, symbol, timeframe, timestamp
"""

# source is never in the DO UPDATE set: attribution is immutable while values correct.
_UPSERT_CACHE_ENTRY_SQL = """
    INSERT INTO data_cache_entries AS entry
        (exchange, symbol, timeframe, first_ts, last_ts, rows)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (exchange, symbol, timeframe) DO UPDATE SET
        first_ts = LEAST(entry.first_ts, EXCLUDED.first_ts),
        last_ts = GREATEST(entry.last_ts, EXCLUDED.last_ts),
        rows = entry.rows + EXCLUDED.rows,
        last_fetched_at = now()
"""

# The fetch engine's race rail: only status flips, never the range or the row count.
_SET_CACHE_STATUS_SQL = """
    INSERT INTO data_cache_entries AS entry
        (exchange, symbol, timeframe, first_ts, last_ts, rows, status)
    VALUES ($1, $2, $3, $4, $5, 0, $6)
    ON CONFLICT (exchange, symbol, timeframe) DO UPDATE SET status = EXCLUDED.status
"""

# The sweeper's LRU freshness write, issued by the backtest loaders (best effort).
_TOUCH_CACHE_ENTRY_SQL = """
    UPDATE data_cache_entries SET last_used_at = now()
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
"""

_EXISTING_KEYS_SQL = """
    SELECT o.exchange, o.symbol, o.timeframe, o.timestamp
    FROM ts.ohlcv_1m AS o
    JOIN unnest($1::text[], $2::text[], $3::text[], $4::timestamptz[])
        AS k(exchange, symbol, timeframe, timestamp)
      ON o.exchange = k.exchange AND o.symbol = k.symbol
     AND o.timeframe = k.timeframe AND o.timestamp = k.timestamp
"""


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _key(record: Any) -> tuple[str, str, str, datetime]:
    return (
        record["exchange"],
        record["symbol"],
        record["timeframe"],
        _utc(record["timestamp"]),
    )


def _columns(bars: list[tuple[str, str, Timeframe, OHLCV]], source: str) -> list[list[Any]]:
    """One array per ``ts.ohlcv_1m`` column, in insert order, for the unnest queries."""
    cols: list[list[Any]] = [[] for _ in range(10)]
    for exchange_id, symbol, timeframe, ohlcv in bars:
        row = (
            exchange_id,
            symbol,
            str(timeframe),
            ohlcv.timestamp,
            ohlcv.open,
            ohlcv.high,
            ohlcv.low,
            ohlcv.close,
            ohlcv.volume,
            source,
        )
        for col, value in zip(cols, row, strict=True):
            col.append(value)
    return cols


def _page_bounds(
    bars: list[tuple[str, str, Timeframe, OHLCV]],
) -> dict[tuple[str, str, str], tuple[datetime, datetime]]:
    """Per-dataset ``(min, max)`` open time over one upsert batch (the fetched page)."""
    bounds: dict[tuple[str, str, str], tuple[datetime, datetime]] = {}
    for exchange_id, symbol, timeframe, ohlcv in bars:
        key = (exchange_id, symbol, str(timeframe))
        first, last = bounds.get(key, (ohlcv.timestamp, ohlcv.timestamp))
        bounds[key] = (min(first, ohlcv.timestamp), max(last, ohlcv.timestamp))
    return bounds


class MarketDataRepository:
    """Async repository for OHLCV bars.

    Uses ``asyncpg`` connection pooling for high throughput.

    Parameters
    ----------
    dsn:
        PostgreSQL DSN, e.g. ``"postgresql://hydra:pw@localhost:5432/hydra"``.
    min_pool_size:
        Minimum number of connections in the pool.
    max_pool_size:
        Maximum number of connections in the pool.
    """

    def __init__(
        self,
        dsn: str,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: Any = None

    @classmethod
    def from_pool(cls, pool: Any) -> MarketDataRepository:
        """Create a repository wrapping an existing asyncpg pool."""
        repo = cls.__new__(cls)
        repo._dsn = ""
        repo._min_pool_size = 0
        repo._max_pool_size = 0
        repo._pool = pool
        return repo

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create the asyncpg connection pool."""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
        )
        logger.info("MarketDataRepository connected", dsn=self._dsn)

    async def close(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("MarketDataRepository connection pool closed")

    def _ensure_pool(self) -> Any:
        """Return the pool or raise if not connected."""
        if self._pool is None:
            msg = "MarketDataRepository is not connected. Call connect() first."
            raise RuntimeError(msg)
        return self._pool

    # ------------------------------------------------------------------
    # OHLCV bars
    # ------------------------------------------------------------------

    async def store_bars(
        self,
        bars: list[tuple[str, str, Timeframe, OHLCV]],
    ) -> int:
        """Bulk insert OHLCV bars, keeping any existing row (ON CONFLICT DO NOTHING).

        Parameters
        ----------
        bars:
            List of ``(exchange_id, symbol, timeframe, ohlcv)`` tuples.

        Returns
        -------
        int
            Number of rows actually inserted (excludes duplicates).
        """
        if not bars:
            return 0
        async with self._ensure_pool().acquire() as conn:
            records = await conn.fetch(_STORE_BARS_SQL, *_columns(bars, "feed"))
        inserted = len(records)
        logger.info("store_bars", attempted=len(bars), inserted=inserted)
        return inserted

    async def upsert_closed_bars(
        self,
        bars: list[tuple[str, str, Timeframe, OHLCV]],
        source: Literal["feed", "auto"] = "feed",
    ) -> tuple[int, int]:
        """Insert closed bars, correcting stored rows whose values differ.

        Only closed bars may be passed: a forming candle written here would be overwritten
        later, but anything that read it in between saw a partial bar.

        New rows carry ``source``; an existing row's ``source`` is immutable, so a feed
        write can never claim an auto-fetched row and vice versa. With ``source='auto'``,
        every dataset that gained rows updates its ``data_cache_entries`` ledger row in
        the same transaction, so the ledger never drifts from the committed bars.

        Returns
        -------
        tuple[int, int]
            ``(inserted, updated)``; identical existing rows count as neither.
        """
        if not bars:
            return (0, 0)
        latest: dict[tuple[str, str, str, datetime], tuple[str, str, Timeframe, OHLCV]] = {}
        for exchange_id, symbol, timeframe, ohlcv in bars:
            latest[(exchange_id, symbol, str(timeframe), ohlcv.timestamp)] = (
                exchange_id,
                symbol,
                timeframe,
                ohlcv,
            )
        unique = list(latest.values())
        cols = _columns(unique, source)
        page_bounds = _page_bounds(unique) if source == "auto" else {}
        async with self._ensure_pool().acquire() as conn, conn.transaction():
            existing = await conn.fetch(_EXISTING_KEYS_SQL, *cols[:4])
            records = await conn.fetch(_UPSERT_CLOSED_BARS_SQL, *cols)
            existing_keys = {_key(r) for r in existing}
            updated = 0
            inserted_by: dict[tuple[str, str, str], int] = {}
            for record in records:
                if _key(record) in existing_keys:
                    updated += 1
                else:
                    key = (record["exchange"], record["symbol"], record["timeframe"])
                    inserted_by[key] = inserted_by.get(key, 0) + 1
            if source == "auto":
                for key, inserted_rows in sorted(inserted_by.items()):
                    first, last = page_bounds[key]
                    await conn.execute(_UPSERT_CACHE_ENTRY_SQL, *key, first, last, inserted_rows)
        inserted = len(records) - updated
        logger.info(
            "upsert_closed_bars",
            attempted=len(unique),
            inserted=inserted,
            updated=updated,
            source=source,
        )
        return (inserted, updated)

    async def set_cache_status(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        status: Literal["fetching", "ready"],
        first_ts: datetime,
        last_ts: datetime,
    ) -> None:
        """Mark a dataset's cache-ledger row ``fetching`` or ``ready``.

        The on-demand engine writes ``fetching`` before its first page and ``ready``
        when it stops, so the sweeper never evicts a dataset mid-write. A dataset
        without a ledger row gets one over the requested range with ``rows = 0``; an
        existing row keeps its range and count untouched.
        """
        async with self._ensure_pool().acquire() as conn:
            await conn.execute(
                _SET_CACHE_STATUS_SQL,
                exchange_id,
                symbol,
                str(timeframe),
                first_ts,
                last_ts,
                status,
            )

    async def touch_cache_entry(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
    ) -> None:
        """Bump the dataset's ``last_used_at`` — the LRU freshness write.

        No row exists until the first auto fetch; an unmatched key is a silent
        no-op, and the caller (the backtest loader) treats any failure as
        best-effort.
        """
        async with self._ensure_pool().acquire() as conn:
            await conn.execute(_TOUCH_CACHE_ENTRY_SQL, exchange_id, symbol, str(timeframe))

    async def get_bars(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[OHLCV]:
        """Bars with open time in ``[start, end]`` (either bound optional), in ascending order."""
        pool = self._ensure_pool()

        query = """
            SELECT open, high, low, close, volume, timestamp
            FROM ts.ohlcv_1m
            WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
              AND ($4::timestamptz IS NULL OR timestamp >= $4::timestamptz)
              AND ($5::timestamptz IS NULL OR timestamp <= $5::timestamptz)
            ORDER BY timestamp ASC
        """

        async with pool.acquire() as conn:
            records = await conn.fetch(query, exchange_id, symbol, str(timeframe), start, end)

        return [
            OHLCV(
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=Decimal(str(r["volume"])),
                timestamp=r["timestamp"].replace(tzinfo=UTC)
                if r["timestamp"].tzinfo is None
                else r["timestamp"],
            )
            for r in records
        ]

    async def get_latest_bars(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        end: datetime | None = None,
    ) -> list[OHLCV]:
        """Return the ``limit`` most recent bars at or before ``end``, in ascending order."""
        pool = self._ensure_pool()

        query = """
            SELECT open, high, low, close, volume, timestamp
            FROM ts.ohlcv_1m
            WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
              AND ($4::timestamptz IS NULL OR timestamp <= $4::timestamptz)
            ORDER BY timestamp DESC
            LIMIT $5
        """

        async with pool.acquire() as conn:
            records = await conn.fetch(query, exchange_id, symbol, str(timeframe), end, limit)

        return [
            OHLCV(
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=Decimal(str(r["volume"])),
                timestamp=r["timestamp"].replace(tzinfo=UTC)
                if r["timestamp"].tzinfo is None
                else r["timestamp"],
            )
            for r in reversed(records)
        ]

    async def get_bar_range(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[datetime, datetime, int] | None:
        """Return ``(first_ts, last_ts, count)`` of stored bars in range, or None if empty."""
        pool = self._ensure_pool()

        query = """
            SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts, COUNT(*) AS n
            FROM ts.ohlcv_1m
            WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
              AND ($4::timestamptz IS NULL OR timestamp >= $4::timestamptz)
              AND ($5::timestamptz IS NULL OR timestamp <= $5::timestamptz)
        """

        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, exchange_id, symbol, str(timeframe), start, end)

        if row is None or not row["n"]:
            return None
        first, last = row["first_ts"], row["last_ts"]
        return (
            first.replace(tzinfo=UTC) if first.tzinfo is None else first,
            last.replace(tzinfo=UTC) if last.tzinfo is None else last,
            int(row["n"]),
        )

    async def missing_ranges(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Inclusive ``(first, last)`` open times of every hole in ``[start, end]``.

        ``start`` and ``end`` must be on the bar grid. Only the holes leave the database, so
        memory is O(gaps), not O(bars); an empty range returns ``[(start, end)]``.
        """
        pool = self._ensure_pool()
        step = timeframe_seconds(timeframe)

        query = """
            WITH present AS (
                SELECT timestamp AS ts FROM ts.ohlcv_1m
                WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
                  AND timestamp BETWEEN $4::timestamptz AND $5::timestamptz
                UNION ALL SELECT $4::timestamptz - make_interval(secs => $6)
                UNION ALL SELECT $5::timestamptz + make_interval(secs => $6)
            ), steps AS (
                SELECT ts, lead(ts) OVER (ORDER BY ts) AS nxt FROM present
            )
            SELECT ts + make_interval(secs => $6) AS gap_start,
                   nxt - make_interval(secs => $6) AS gap_end
            FROM steps
            WHERE nxt - ts > make_interval(secs => $6)
            ORDER BY ts
        """

        async with pool.acquire() as conn:
            records = await conn.fetch(query, exchange_id, symbol, str(timeframe), start, end, step)

        return [
            (
                r["gap_start"].replace(tzinfo=UTC)
                if r["gap_start"].tzinfo is None
                else r["gap_start"],
                r["gap_end"].replace(tzinfo=UTC) if r["gap_end"].tzinfo is None else r["gap_end"],
            )
            for r in records
        ]


# ------------------------------------------------------------------
# Data-cache ledger (auto-fetched datasets; the sweeper's seam)
# ------------------------------------------------------------------

_DELETE_AUTO_BARS_SQL = """
    DELETE FROM ts.ohlcv_1m
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3 AND source = 'auto'
"""

_DELETE_CACHE_ENTRY_SQL = """
    DELETE FROM data_cache_entries
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
"""

_CACHE_ENTRIES_SQL = """
    SELECT exchange, symbol, timeframe, first_ts, last_ts, rows, status,
           created_at, last_fetched_at, last_used_at
    FROM data_cache_entries
    ORDER BY last_used_at ASC
"""

_CACHE_TOTALS_SQL = """
    SELECT COALESCE(SUM(rows), 0) AS rows, COUNT(*) AS datasets FROM data_cache_entries
"""


@dataclass(frozen=True)
class DataCacheEntry:
    """One cached dataset's ledger row: range, size, state and freshness."""

    exchange: str
    symbol: str
    timeframe: str
    first_ts: datetime
    last_ts: datetime
    rows: int
    status: str
    created_at: datetime
    last_fetched_at: datetime
    last_used_at: datetime


async def delete_auto_dataset(
    pool: Any, exchange_id: str, symbol: str, timeframe: Timeframe
) -> int:
    """Delete one dataset's auto-fetched bars and its ledger row; feed rows never match.

    Returns the number of bar rows deleted; the ledger row goes even when zero bars did.
    """
    async with pool.acquire() as conn, conn.transaction():
        status = await conn.execute(_DELETE_AUTO_BARS_SQL, exchange_id, symbol, str(timeframe))
        await conn.execute(_DELETE_CACHE_ENTRY_SQL, exchange_id, symbol, str(timeframe))
    return int(status.split()[-1])


async def list_cache_entries(pool: Any) -> list[DataCacheEntry]:
    """Every ledger row, least-recently-used first (the sweeper's eviction order)."""
    async with pool.acquire() as conn:
        records = await conn.fetch(_CACHE_ENTRIES_SQL)
    return [
        DataCacheEntry(
            exchange=r["exchange"],
            symbol=r["symbol"],
            timeframe=r["timeframe"],
            first_ts=_utc(r["first_ts"]),
            last_ts=_utc(r["last_ts"]),
            rows=r["rows"],
            status=r["status"],
            created_at=_utc(r["created_at"]),
            last_fetched_at=_utc(r["last_fetched_at"]),
            last_used_at=_utc(r["last_used_at"]),
        )
        for r in records
    ]


async def cache_totals(pool: Any) -> tuple[int, int]:
    """``(total cached rows, dataset count)`` over the ledger."""
    async with pool.acquire() as conn:
        records = await conn.fetch(_CACHE_TOTALS_SQL)
    if not records:
        return (0, 0)
    return (int(records[0]["rows"]), int(records[0]["datasets"]))
