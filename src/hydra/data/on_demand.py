"""The on-demand fetch engine: a backtest never fails "data unavailable".

``OnDemandFetchService.ensure_available`` detects a dataset's missing bar ranges in
SQL (grid-rounded, O(gaps) memory), fetches exactly those gaps from the exchange REST
API page by page — never the forming candle — and stores every page through
``upsert_closed_bars(source='auto')``, so attribution is immutable and the
data-cache ledger stays in the same transaction as the bars. Requests are paced by a
fixed-window weight budget (the static split: feeds stay uncapped), the fetch is
patient up to a deadline, and writes for one dataset are serialized by an asyncio
lock (the single-writer contract). Gaps the exchange genuinely lacks are accepted and
reported — the run proceeds on honest gappy data; only a range with zero stored bars
raises the P0-B ``NoMarketDataError``.

The REST client is injected (``data`` may not import ``execution``); the caller owns
its lifecycle. No caller is wired yet — the jobs/worker wiring is P6-B2, the
submit/preview surfaces P6-B3.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from hydra.core.config import DataConfig
from hydra.core.logging import get_logger
from hydra.core.types import ExchangeId, Timeframe
from hydra.data.backfill import (
    MAX_PAGE_SIZE,
    ExchangeBackfillService,
    GapRange,
    Sleep,
    error_is,
    iter_closed_bar_pages,
)
from hydra.data.bars import ceil_to_tf, floor_to_tf, timeframe_seconds
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.data.storage import MarketDataRepository

logger = get_logger(__name__)

type Clock = Callable[[], float]
type ClientFactory = Callable[[ExchangeId], Awaitable[Any]]
type ProgressCallback = Callable[[dict[str, Any]], None]

KLINE_PAGE_WEIGHT = 2
"""Binance spot GET /api/v3/klines request weight, regardless of the limit."""

LOAD_MARKETS_WEIGHT = 20
"""One load_markets on a client whose markets are not loaded yet."""

_WINDOW_S = 60.0


class WeightBudget:
    """Fixed-window request-weight budget: at most ``weight_per_min`` per 60 s window.

    The first acquire opens a window; a request that would exceed the window's limit
    waits exactly to the window boundary and the next request opens a fresh one. A
    single request heavier than the whole budget proceeds without waiting — waiting
    could never fit it. This paces the engine's own egress only (the static split);
    feeds and warm-ups stay uncapped as today.
    """

    def __init__(self, weight_per_min: int, clock: Clock, sleep: Sleep) -> None:
        self._limit = weight_per_min
        self._clock = clock
        self._sleep = sleep
        self._window_start: float | None = None
        self._spent_in_window = 0

    async def acquire(self, weight: int) -> None:
        now = self._clock()
        if self._window_start is None or now - self._window_start >= _WINDOW_S:
            self._window_start, self._spent_in_window = now, 0
        if self._spent_in_window > 0 and self._spent_in_window + weight > self._limit:
            wait_s = _WINDOW_S - (now - self._window_start)
            if wait_s > 0:
                await self._sleep(wait_s)
            self._window_start, self._spent_in_window = self._clock(), 0
        self._spent_in_window += weight


@dataclass
class _Call:
    """Per-call counters; the weight budget itself is engine-wide."""

    started: float
    bars: int = 0
    weight: int = 0


@dataclass(frozen=True, slots=True)
class FetchReport:
    """What one ``ensure_available`` call did.

    ``gap_report`` lists the ranges still missing after the fetch — gaps the exchange
    genuinely lacks, accepted so the run proceeds on honest gappy data.
    """

    bars_fetched: int
    weight_spent: int
    elapsed_ms: int
    ranges_fetched: int
    gap_report: list[GapRange] = field(default_factory=list)


class OnDemandFetchService:
    """Fetch exactly the missing bars of a dataset, budgeted and patient.

    Parameters
    ----------
    client_factory:
        Async factory for the ccxt REST client of an exchange; the caller owns the
        client's lifecycle (a worker-lifetime pool pays ``load_markets`` once).
    weight_per_min:
        The engine's request-weight budget per 60 s window (default 900).
    patience_s:
        How long rate-limit/network failures are retried before ``DataFetchError``.
    max_bars:
        Per-call cap on the bars a fetch may store — the disk safety net.
    page_size:
        Bars requested per ``fetch_ohlcv`` page (capped at 1000).
    clock, sleep:
        Injectable monotonic clock and sleep (tests drive both).
    now:
        Fixed "current time" for the forming-candle filter; the wall clock by default.
    """

    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        weight_per_min: int = 900,
        patience_s: int = 900,
        max_bars: int = 2_000_000,
        page_size: int = MAX_PAGE_SIZE,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        now: datetime | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._patience_s = patience_s
        self._max_bars = max_bars
        self._page_size = min(page_size, MAX_PAGE_SIZE)
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._budget = WeightBudget(weight_per_min, clock=clock, sleep=sleep)
        self._locks: dict[tuple[ExchangeId, str, Timeframe], asyncio.Lock] = {}

    @classmethod
    def from_config(
        cls,
        config: DataConfig,
        client_factory: ClientFactory,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> OnDemandFetchService:
        """Build the engine from the ``data`` config section (tests inject clock/sleep)."""
        return cls(
            client_factory,
            weight_per_min=config.fetch_weight_per_min,
            patience_s=config.fetch_patience_s,
            max_bars=config.fetch_max_bars,
            clock=clock,
            sleep=sleep,
        )

    async def ensure_available(
        self,
        repo: MarketDataRepository,
        exchange: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> FetchReport:
        """Make the bars of ``[start, end]`` available, fetching exactly the gaps.

        ``force=True`` refetches the whole grid-rounded range (``fetch_fresh``
        semantics); values are corrected through the upsert, attribution never flips.
        Concurrent calls for the same dataset serialize on a per-key lock, so the
        second caller re-detects gaps after the first one's writes land.
        """
        call = _Call(started=self._clock())
        grid_start = ceil_to_tf(start, timeframe)
        grid_end = floor_to_tf(end, timeframe)
        lock_key = (exchange, symbol, timeframe)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            gaps = await self._detect_or_force(
                repo, exchange, symbol, timeframe, grid_start, grid_end, force=force
            )
            if not gaps:
                self._emit(on_progress, "fetched", call, gaps)
                return self._report(call, ranges_fetched=0, gap_report=[])

            owed = sum(_gap_bars(gap, timeframe) for gap in gaps)
            if owed > self._max_bars:
                raise DataFetchError(
                    "too_much_data",
                    exchange,
                    symbol,
                    timeframe,
                    weight_spent=0,
                    elapsed_s=self._clock() - call.started,
                    remaining_ranges=list(gaps),
                )

            logger.info(
                "on-demand fetch started",
                exchange=exchange,
                symbol=symbol,
                timeframe=str(timeframe),
                start=grid_start.isoformat(),
                end=grid_end.isoformat(),
                gaps=len(gaps),
                bars_owed=owed,
            )
            self._emit(on_progress, "fetching", call, gaps)
            await repo.set_cache_status(
                exchange, symbol, timeframe, "fetching", grid_start, grid_end
            )
            try:
                client = await self._client_factory(exchange)
                if not getattr(client, "markets", None):
                    await self._acquire(call, LOAD_MARKETS_WEIGHT)
                    await client.load_markets()
                await self._fetch_gaps(
                    repo, client, exchange, symbol, timeframe, gaps, call, on_progress
                )
            finally:
                await repo.set_cache_status(
                    exchange, symbol, timeframe, "ready", grid_start, grid_end
                )

            remaining = await self._detect_or_force(
                repo, exchange, symbol, timeframe, grid_start, grid_end, force=False
            )
            if remaining == [GapRange(start=grid_start, end=grid_end)]:
                available = await repo.get_bar_range(exchange, symbol, timeframe)
                first, last = (available[0], available[1]) if available else (None, None)
                raise NoMarketDataError(exchange, symbol, timeframe, start, end, first, last)
            logger.info(
                "on-demand fetch complete",
                exchange=exchange,
                symbol=symbol,
                timeframe=str(timeframe),
                bars_fetched=call.bars,
                weight_spent=call.weight,
                elapsed_ms=int((self._clock() - call.started) * 1000),
                remaining_gaps=len(remaining),
            )
            self._emit(on_progress, "fetched", call, gaps)
            return self._report(call, ranges_fetched=len(gaps), gap_report=remaining)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _detect_or_force(
        self,
        repo: MarketDataRepository,
        exchange: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        grid_start: datetime,
        grid_end: datetime,
        *,
        force: bool,
    ) -> list[GapRange]:
        if grid_start > grid_end:
            return []
        if force:
            return [GapRange(start=grid_start, end=grid_end)]
        detector = ExchangeBackfillService(repository=repo)
        return await detector.detect_gaps(exchange, symbol, timeframe, grid_start, grid_end)

    async def _fetch_gaps(
        self,
        repo: MarketDataRepository,
        client: Any,
        exchange: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        gaps: list[GapRange],
        call: _Call,
        on_progress: ProgressCallback | None,
    ) -> None:
        tf_step = timedelta(seconds=timeframe_seconds(timeframe))
        for index, gap in enumerate(gaps):
            resume_from = gap.start
            while True:
                try:
                    async with aclosing(
                        iter_closed_bar_pages(
                            client,
                            symbol,
                            timeframe,
                            resume_from,
                            gap.end,
                            exchange_id=exchange,
                            now=self._now,
                            page_size=self._page_size,
                            sleep=self._sleep,
                        )
                    ) as pages:
                        while True:
                            await self._acquire(call, KLINE_PAGE_WEIGHT)
                            try:
                                page = await anext(pages)
                            except StopAsyncIteration:
                                break
                            await repo.upsert_closed_bars(
                                [(exchange, symbol, timeframe, bar) for bar in page],
                                source="auto",
                            )
                            call.bars += len(page)
                            resume_from = page[-1].timestamp + tf_step
                            self._emit(on_progress, "fetching", call, gaps)
                            # Past the gap no page can be yielded: stop before charging for one.
                            if resume_from > gap.end:
                                break
                except Exception as exc:
                    await self._handle_fetch_error(
                        exc,
                        exchange=exchange,
                        symbol=symbol,
                        timeframe=timeframe,
                        gaps=gaps,
                        index=index,
                        resume_from=resume_from,
                        gap_end=gap.end,
                        call=call,
                        on_progress=on_progress,
                    )
                    continue
                break
            logger.info(
                "on-demand gap fetched",
                exchange=exchange,
                symbol=symbol,
                timeframe=str(timeframe),
                gap_start=gap.start.isoformat(),
                gap_end=gap.end.isoformat(),
            )

    async def _handle_fetch_error(
        self,
        exc: Exception,
        *,
        exchange: ExchangeId,
        symbol: str,
        timeframe: Timeframe,
        gaps: list[GapRange],
        index: int,
        resume_from: datetime,
        gap_end: datetime,
        call: _Call,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Raise past patience, else return to retry the gap from the resume point.

        ``iter_closed_bar_pages`` already retried the page with backoff; a retry here
        is another full attempt, so the deadline is checked on every escape.
        """
        if not error_is(exc, "NetworkError"):
            raise exc
        elapsed = self._clock() - call.started
        if elapsed < self._patience_s:
            logger.warning(
                "on-demand fetch retrying",
                exchange=exchange,
                symbol=symbol,
                timeframe=str(timeframe),
                error=type(exc).__name__,
                elapsed_s=round(elapsed, 2),
                patience_s=self._patience_s,
            )
            return
        reason: Literal["rate_limited", "network"] = (
            "rate_limited" if error_is(exc, "RateLimitExceeded") else "network"
        )
        remaining = [GapRange(start=resume_from, end=gap_end), *gaps[index + 1 :]]
        logger.warning(
            "on-demand fetch failed",
            exchange=exchange,
            symbol=symbol,
            timeframe=str(timeframe),
            reason=reason,
            weight_spent=call.weight,
            elapsed_s=round(elapsed, 2),
            remaining_ranges=len(remaining),
        )
        self._emit(on_progress, "failed", call, gaps)
        raise DataFetchError(
            reason,
            exchange,
            symbol,
            timeframe,
            weight_spent=call.weight,
            elapsed_s=elapsed,
            remaining_ranges=remaining,
        ) from exc

    async def _acquire(self, call: _Call, weight: int) -> None:
        await self._budget.acquire(weight)
        call.weight += weight

    def _emit(
        self,
        on_progress: ProgressCallback | None,
        phase: Literal["fetching", "fetched", "failed"],
        call: _Call,
        gaps: list[GapRange],
    ) -> None:
        if on_progress is None:
            return
        on_progress(
            {
                "state": phase,
                "ranges": [[gap.start.isoformat(), gap.end.isoformat()] for gap in gaps],
                "bars_fetched": call.bars,
                "weight_spent": call.weight,
                "elapsed_ms": int((self._clock() - call.started) * 1000),
            }
        )

    def _report(
        self, call: _Call, *, ranges_fetched: int, gap_report: list[GapRange]
    ) -> FetchReport:
        return FetchReport(
            bars_fetched=call.bars,
            weight_spent=call.weight,
            elapsed_ms=int((self._clock() - call.started) * 1000),
            ranges_fetched=ranges_fetched,
            gap_report=gap_report,
        )


def _gap_bars(gap: GapRange, timeframe: Timeframe) -> int:
    """Bars in one grid-aligned gap (inclusive bounds)."""
    return int((gap.end - gap.start).total_seconds() // timeframe_seconds(timeframe)) + 1
