"""Historical data gap detection and backfilling via CCXT REST.

Uses paginated ``fetch_ohlcv()`` calls with per-exchange rate limit compliance
to download missing bars and fill detected gaps in stored data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, ExchangeId, Timeframe
from hydra.data.normalizer import DataNormalizer
from hydra.data.storage import MarketDataRepository

logger = get_logger(__name__)

# Timeframe durations used for gap detection
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


@dataclass
class GapRange:
    """A contiguous range of missing bars."""

    start: datetime
    end: datetime


class ExchangeBackfillService:
    """Detect gaps in stored data and backfill from exchange REST APIs.

    Parameters
    ----------
    repository:
        ``MarketDataRepository`` for querying existing bars and storing
        backfilled data.
    normalizer:
        ``DataNormalizer`` for converting raw CCXT data.
    exchange_factories:
        Mapping of exchange id to a callable that returns a ccxt async
        exchange instance.  This allows the caller to inject pre-configured
        exchange objects.
    """

    def __init__(
        self,
        repository: MarketDataRepository,
        normalizer: DataNormalizer | None = None,
        exchange_factories: dict[str, Any] | None = None,
    ) -> None:
        self._repo = repository
        self._normalizer = normalizer or DataNormalizer()
        self._exchange_factories = exchange_factories or {}
        self._exchanges: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Exchange management
    # ------------------------------------------------------------------

    async def _get_exchange(self, exchange_id: ExchangeId) -> Any:
        """Get or create a CCXT exchange instance."""
        if exchange_id not in self._exchanges:
            factory = self._exchange_factories.get(exchange_id)
            if factory is None:
                msg = f"No exchange factory registered for {exchange_id}"
                raise ValueError(msg)
            self._exchanges[exchange_id] = factory()
        return self._exchanges[exchange_id]

    async def close(self) -> None:
        """Close all exchange connections."""
        for exchange in self._exchanges.values():
            if hasattr(exchange, "close"):
                await exchange.close()
        self._exchanges.clear()

    # ------------------------------------------------------------------
    # Gap detection
    # ------------------------------------------------------------------

    async def detect_gaps(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[GapRange]:
        """Query storage and return ranges of missing bars.

        Compares the expected number of bars between *start* and *end*
        against what is actually stored, and returns contiguous gap ranges.

        Parameters
        ----------
        exchange_id:
            Exchange to check.
        symbol:
            Trading pair.
        timeframe:
            Bar timeframe.
        start:
            Inclusive start of the range (UTC).
        end:
            Inclusive end of the range (UTC).

        Returns
        -------
        list[GapRange]
            Sorted list of gap ranges (ascending by start time).
        """
        tf_seconds = _TIMEFRAME_SECONDS.get(str(timeframe))
        if tf_seconds is None:
            msg = f"Unknown timeframe: {timeframe}"
            raise ValueError(msg)

        bars = await self._repo.get_bars(
            exchange_id=exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )

        existing_timestamps = {bar.timestamp for bar in bars}

        # Generate expected timestamps
        tf_delta = timedelta(seconds=tf_seconds)
        gaps: list[GapRange] = []
        current = start
        gap_start: datetime | None = None

        while current <= end:
            if current not in existing_timestamps:
                if gap_start is None:
                    gap_start = current
            else:
                if gap_start is not None:
                    gaps.append(GapRange(start=gap_start, end=current - tf_delta))
                    gap_start = None
            current += tf_delta

        # Close trailing gap
        if gap_start is not None:
            gaps.append(GapRange(start=gap_start, end=end))

        return gaps

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def backfill_gaps(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """Detect gaps and fetch missing bars via CCXT REST.

        Parameters
        ----------
        exchange_id:
            Exchange to backfill from.
        symbol:
            Trading pair.
        timeframe:
            Bar timeframe.
        start:
            Start of range to check.  Defaults to 30 days ago.
        end:
            End of range to check.  Defaults to now.

        Returns
        -------
        int
            Total number of bars backfilled.
        """
        now = datetime.now(UTC)
        if start is None:
            start = now - timedelta(days=30)
        if end is None:
            end = now

        gaps = await self.detect_gaps(exchange_id, symbol, timeframe, start, end)

        if not gaps:
            logger.info(
                "No gaps detected",
                exchange=exchange_id,
                symbol=symbol,
                timeframe=str(timeframe),
            )
            return 0

        total = 0
        for gap in gaps:
            count = await self.bulk_download(
                exchange_id=exchange_id,
                symbol=symbol,
                timeframe=timeframe,
                start=gap.start,
                end=gap.end,
            )
            total += count

        logger.info(
            "Backfill complete",
            exchange=exchange_id,
            symbol=symbol,
            timeframe=str(timeframe),
            gaps_filled=len(gaps),
            bars_total=total,
        )
        return total

    async def bulk_download(
        self,
        exchange_id: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        page_size: int = 500,
    ) -> int:
        """Fetch historical OHLCV data via paginated CCXT REST calls.

        Parameters
        ----------
        exchange_id:
            Exchange to download from.
        symbol:
            Trading pair.
        timeframe:
            Bar timeframe.
        start:
            Inclusive start timestamp (UTC).
        end:
            Inclusive end timestamp (UTC).
        page_size:
            Number of bars per REST request (exchange-dependent max).

        Returns
        -------
        int
            Number of bars downloaded and stored.
        """
        exchange = await self._get_exchange(exchange_id)
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        total_stored = 0

        logger.info(
            "Bulk download started",
            exchange=exchange_id,
            symbol=symbol,
            timeframe=str(timeframe),
            start=start.isoformat(),
            end=end.isoformat(),
        )

        while since_ms <= end_ms:
            raw_bars = await exchange.fetch_ohlcv(
                symbol,
                str(timeframe),
                since=since_ms,
                limit=page_size,
            )

            if not raw_bars:
                break

            bars_to_store: list[tuple[str, str, Timeframe, OHLCV]] = []
            for raw in raw_bars:
                bar_ts_ms = raw[0]
                if bar_ts_ms > end_ms:
                    break

                ohlcv = self._normalizer.normalize_ohlcv(raw, exchange_id)
                bars_to_store.append((exchange_id, symbol, timeframe, ohlcv))

            if bars_to_store:
                await self._repo.store_bars(bars_to_store)
                total_stored += len(bars_to_store)

            # Advance past the last bar we received
            last_ts_ms = raw_bars[-1][0]
            tf_seconds = _TIMEFRAME_SECONDS.get(str(timeframe), 60)
            since_ms = last_ts_ms + tf_seconds * 1000

            # Rate limit compliance — brief pause between pages
            await asyncio.sleep(0.1)

            logger.debug(
                "Bulk download page",
                exchange=exchange_id,
                symbol=symbol,
                page_bars=len(raw_bars),
                total_so_far=total_stored,
            )

        logger.info(
            "Bulk download complete",
            exchange=exchange_id,
            symbol=symbol,
            total_bars=total_stored,
        )
        return total_stored
