"""Historical data gap detection and backfilling via CCXT REST.

Uses paginated ``fetch_ohlcv()`` calls with per-exchange rate limit compliance
to download missing bars and fill detected gaps in stored data.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, ExchangeId, Timeframe
from hydra.data.bars import ceil_to_tf, floor_to_tf, last_closed_open, timeframe_seconds
from hydra.data.normalizer import DataNormalizer
from hydra.data.storage import MarketDataRepository

logger = get_logger(__name__)

type Sleep = Callable[[float], Awaitable[None]]

MAX_PAGE_SIZE = 1000
_RETRY_BASE_S = 1.0
_RETRY_MAX_S = 60.0
_DDOS_MIN_WAIT_S = 60.0


def error_is(exc: BaseException, *names: str) -> bool:
    """True when ``exc`` is (a subclass of) a class with one of ``names`` (ccxt stays lazy)."""
    return any(cls.__name__ in names for cls in type(exc).__mro__)


async def fetch_ohlcv_page(
    exchange: Any,
    symbol: str,
    timeframe: Timeframe,
    since_ms: int,
    limit: int,
    *,
    max_retries: int = 5,
    sleep: Sleep = asyncio.sleep,
) -> list[list[Any]]:
    """One ``fetch_ohlcv`` call, retrying ccxt network and rate-limit errors with backoff.

    Raises the last error once ``max_retries`` retries are spent; never returns a partial
    result silently.
    """
    attempt = 0
    while True:
        try:
            rows: list[list[Any]] = await exchange.fetch_ohlcv(
                symbol, str(timeframe), since=since_ms, limit=limit
            )
            return rows
        except Exception as exc:
            if not error_is(exc, "NetworkError") or attempt >= max_retries:
                raise
            attempt += 1
            wait = min(_RETRY_BASE_S * 2 ** (attempt - 1), _RETRY_MAX_S)
            wait += wait * secrets.randbelow(250) / 1000
            if error_is(exc, "DDoSProtection"):
                wait = max(wait, _DDOS_MIN_WAIT_S)
            logger.warning(
                "fetch_ohlcv failed, retrying",
                symbol=symbol,
                timeframe=str(timeframe),
                attempt=attempt,
                wait_s=round(wait, 2),
                error=type(exc).__name__,
            )
            await sleep(wait)


async def iter_closed_bar_pages(
    exchange: Any,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    exchange_id: ExchangeId = "binance",
    now: datetime | None = None,
    page_size: int = MAX_PAGE_SIZE,
    max_retries: int = 5,
    sleep: Sleep = asyncio.sleep,
    normalizer: DataNormalizer | None = None,
) -> AsyncIterator[list[OHLCV]]:
    """Yield pages of closed bars with open time in ``[start, end]``, oldest first.

    ``start`` is rounded up and ``end`` down to the bar grid. Rows outside the range and the
    forming candle (close time after ``now``) are dropped, and a page that does not advance
    the cursor ends the download.
    """
    norm = normalizer or DataNormalizer()
    tf_ms = timeframe_seconds(timeframe) * 1000
    now = now or datetime.now(UTC)
    first = ceil_to_tf(start, timeframe)
    last = min(floor_to_tf(end, timeframe), last_closed_open(now, timeframe))
    start_ms = int(first.timestamp() * 1000)
    end_ms = int(last.timestamp() * 1000)
    limit = min(page_size, MAX_PAGE_SIZE)
    since_ms = start_ms
    while since_ms <= end_ms:
        raw = await fetch_ohlcv_page(
            exchange, symbol, timeframe, since_ms, limit, max_retries=max_retries, sleep=sleep
        )
        if not raw:
            break
        page = [
            norm.normalize_ohlcv(row, exchange_id)
            for row in raw
            if since_ms <= int(row[0]) <= end_ms
        ]
        if page:
            yield page
        newest_ms = int(raw[-1][0])
        if newest_ms < since_ms:
            break
        since_ms = newest_ms + tf_ms


async def fetch_closed_bars(
    exchange: Any,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    exchange_id: ExchangeId = "binance",
    now: datetime | None = None,
    page_size: int = MAX_PAGE_SIZE,
    max_retries: int = 5,
    sleep: Sleep = asyncio.sleep,
) -> list[OHLCV]:
    """All closed bars in ``[start, end]`` (see ``iter_closed_bar_pages``), deduplicated."""
    by_ts: dict[datetime, OHLCV] = {}
    async for page in iter_closed_bar_pages(
        exchange,
        symbol,
        timeframe,
        start,
        end,
        exchange_id=exchange_id,
        now=now,
        page_size=page_size,
        max_retries=max_retries,
        sleep=sleep,
    ):
        for bar in page:
            by_ts[bar.timestamp] = bar
    return [by_ts[ts] for ts in sorted(by_ts)]


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
        """Return the missing bar ranges in ``[start, end]``, oldest first.

        ``start`` is rounded up and ``end`` down to the bar grid; the holes are computed in SQL,
        so a multi-year 1m range is never loaded into memory.
        """
        start = ceil_to_tf(start, timeframe)
        end = floor_to_tf(end, timeframe)
        if start > end:
            return []
        ranges = await self._repo.missing_ranges(exchange_id, symbol, timeframe, start, end)
        return [GapRange(start=s, end=e) for s, e in ranges]

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
            End of range to check.  Defaults to the newest closed bar.

        Returns
        -------
        int
            Total number of bars backfilled.
        """
        now = datetime.now(UTC)
        if start is None:
            start = now - timedelta(days=30)
        if end is None:
            end = last_closed_open(now, timeframe)

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
        page_size: int = MAX_PAGE_SIZE,
        *,
        now: datetime | None = None,
        max_retries: int = 5,
        sleep: Sleep = asyncio.sleep,
        source: str = "feed",
    ) -> int:
        """Download closed bars via paginated CCXT REST calls and upsert them.

        The forming candle is never stored, and a stored bar whose values changed (for
        example one frozen while it was still forming) is corrected.

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
            Number of bars per REST request (capped at 1000).
        now:
            Current time; bars that close after it are dropped. Defaults to the wall clock.
        max_retries:
            Retries per page for ccxt network and rate-limit errors before raising.
        sleep:
            Backoff sleep (injectable for tests).
        source:
            Attribution written on every new bar. The manual backfills keep the default
            'feed'; only the auto-fetch path passes 'auto', which also maintains the
            data-cache ledger so the dataset stays reclaimable.

        Returns
        -------
        int
            Number of closed bars downloaded and passed to the repository.
        """
        exchange = await self._get_exchange(exchange_id)
        total = 0
        logger.info(
            "Bulk download started",
            exchange=exchange_id,
            symbol=symbol,
            timeframe=str(timeframe),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        async for page in iter_closed_bar_pages(
            exchange,
            symbol,
            timeframe,
            start,
            end,
            exchange_id=exchange_id,
            now=now,
            page_size=page_size,
            max_retries=max_retries,
            sleep=sleep,
            normalizer=self._normalizer,
        ):
            await self._repo.upsert_closed_bars(
                [(exchange_id, symbol, timeframe, b) for b in page], source=source
            )
            total += len(page)
            logger.debug(
                "Bulk download page",
                exchange=exchange_id,
                symbol=symbol,
                page_bars=len(page),
                total_so_far=total,
            )
        logger.info(
            "Bulk download complete",
            exchange=exchange_id,
            symbol=symbol,
            total_bars=total,
        )
        return total
