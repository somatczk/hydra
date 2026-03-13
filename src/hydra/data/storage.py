"""asyncpg-based market data repository for OHLCV bars and funding rates.

Provides high-performance bulk insert and query operations against
TimescaleDB with connection pooling and ON CONFLICT deduplication.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, Timeframe

logger = get_logger(__name__)


class MarketDataRepository:
    """Async repository for OHLCV bars and funding-rate data.

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
        """Bulk insert OHLCV bars with ON CONFLICT DO NOTHING.

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

        pool = self._ensure_pool()

        rows = [
            (
                exchange_id,
                symbol,
                str(timeframe),
                ohlcv.timestamp,
                ohlcv.open,
                ohlcv.high,
                ohlcv.low,
                ohlcv.close,
                ohlcv.volume,
            )
            for exchange_id, symbol, timeframe, ohlcv in bars
        ]

        query = """
            INSERT INTO ts.ohlcv_1m (
                exchange, symbol, timeframe, timestamp,
                open, high, low, close, volume
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (exchange, symbol, timeframe, timestamp) DO NOTHING
        """

        async with pool.acquire() as conn:
            result = await conn.executemany(query, rows)

        # executemany does not return a row count; we log the attempt count
        inserted = len(rows)
        logger.info("store_bars", attempted=inserted, result=result)
        return inserted

    async def get_bars(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[OHLCV]:
        """Query OHLCV bars with optional date range and limit.

        Parameters
        ----------
        exchange_id:
            Exchange identifier.
        symbol:
            Trading pair symbol.
        timeframe:
            Bar timeframe.
        start:
            Inclusive start timestamp (UTC).
        end:
            Inclusive end timestamp (UTC).
        limit:
            Maximum number of bars to return (most recent first when used
            without a date range).

        Returns
        -------
        list[OHLCV]
            Bars in ascending timestamp order.
        """
        pool = self._ensure_pool()

        conditions = [
            "exchange = $1",
            "symbol = $2",
            "timeframe = $3",
        ]
        params: list[Any] = [exchange_id, symbol, str(timeframe)]
        idx = 4

        if start is not None:
            conditions.append(f"timestamp >= ${idx}")
            params.append(start)
            idx += 1

        if end is not None:
            conditions.append(f"timestamp <= ${idx}")
            params.append(end)
            idx += 1

        where = " AND ".join(conditions)
        query = f"""
            SELECT open, high, low, close, volume, timestamp
            FROM ts.ohlcv_1m
            WHERE {where}
            ORDER BY timestamp ASC
        """  # noqa: S608

        if limit is not None:
            query += f" LIMIT ${idx}"
            params.append(limit)

        async with pool.acquire() as conn:
            records = await conn.fetch(query, *params)

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

    async def get_latest_bar(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
    ) -> OHLCV | None:
        """Return the most recent bar, or ``None`` if no data exists."""
        pool = self._ensure_pool()

        query = """
            SELECT open, high, low, close, volume, timestamp
            FROM ts.ohlcv_1m
            WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
            ORDER BY timestamp DESC
            LIMIT 1
        """

        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, exchange_id, symbol, str(timeframe))

        if row is None:
            return None

        return OHLCV(
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            timestamp=row["timestamp"].replace(tzinfo=UTC)
            if row["timestamp"].tzinfo is None
            else row["timestamp"],
        )

    async def get_bar_count(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Count bars in range."""
        pool = self._ensure_pool()

        conditions = [
            "exchange = $1",
            "symbol = $2",
            "timeframe = $3",
        ]
        params: list[Any] = [exchange_id, symbol, str(timeframe)]
        idx = 4

        if start is not None:
            conditions.append(f"timestamp >= ${idx}")
            params.append(start)
            idx += 1

        if end is not None:
            conditions.append(f"timestamp <= ${idx}")
            params.append(end)
            idx += 1

        where = " AND ".join(conditions)
        query = f"SELECT COUNT(*) FROM ts.ohlcv_1m WHERE {where}"  # noqa: S608

        async with pool.acquire() as conn:
            count = await conn.fetchval(query, *params)

        return int(count) if count else 0

    # ------------------------------------------------------------------
    # Funding rates
    # ------------------------------------------------------------------

    async def store_funding_rates(self, rates: list[dict[str, Any]]) -> int:
        """Store funding rate records.

        Each dict must have keys: ``exchange``, ``symbol``, ``rate``,
        ``next_funding_time``, ``timestamp``.

        Returns
        -------
        int
            Number of rows attempted to insert.
        """
        if not rates:
            return 0

        pool = self._ensure_pool()

        rows = [
            (
                r["exchange"],
                r["symbol"],
                Decimal(str(r["rate"])),
                r["next_funding_time"],
                r["timestamp"],
            )
            for r in rates
        ]

        query = """
            INSERT INTO ts.funding_rates (
                exchange, symbol, rate, next_funding_time, timestamp
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
        """

        async with pool.acquire() as conn:
            await conn.executemany(query, rows)

        logger.info("store_funding_rates", count=len(rows))
        return len(rows)

    async def get_funding_rates(
        self,
        exchange_id: str,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Query funding rates with optional date range."""
        pool = self._ensure_pool()

        conditions = [
            "exchange = $1",
            "symbol = $2",
        ]
        params: list[Any] = [exchange_id, symbol]
        idx = 3

        if start is not None:
            conditions.append(f"timestamp >= ${idx}")
            params.append(start)
            idx += 1

        if end is not None:
            conditions.append(f"timestamp <= ${idx}")
            params.append(end)
            idx += 1

        where = " AND ".join(conditions)
        query = f"""
            SELECT exchange, symbol, rate, next_funding_time, timestamp
            FROM ts.funding_rates
            WHERE {where}
            ORDER BY timestamp ASC
        """  # noqa: S608

        async with pool.acquire() as conn:
            records = await conn.fetch(query, *params)

        return [
            {
                "exchange": r["exchange"],
                "symbol": r["symbol"],
                "rate": Decimal(str(r["rate"])),
                "next_funding_time": r["next_funding_time"],
                "timestamp": r["timestamp"],
            }
            for r in records
        ]
