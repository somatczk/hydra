"""The USD⌂-M funding-rate writer for ``ts.funding_rates`` (binanceusdm, P4-B).

The 0001-era hypertable ships empty, and P4-B ships the writer, not the data.
The cold-start decision (the critic's §4-6): the on-box battery backfills each
perp symbol once from public fapi (no keys), and P4-C's executor accrues
funding only from ingested rows — absent rows mean an honest skip, never a
fabricated or interpolated rate.

``ensure_funding_rates`` walks ``fetch_funding_rate_history`` pages with a
since/until cursor and writes what the exchange actually settled — the
settlement timestamps fapi reports (most symbols 00/08/16 UTC, some
01/09/17 or 03/11/19), never an assumed grid — paced by the on-demand
engine's fixed-window :class:`~hydra.data.on_demand.WeightBudget`, and
inserts only rows the table does not have, so every re-run is idempotent.
``FundingRefresher`` is the lifespan's third refresher-pattern task: each
cycle it tops up every running perp session's symbols since each symbol's
newest stored row — with no running perp sessions the cycle is a no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import ExchangeId
from hydra.data.on_demand import WeightBudget

logger = get_logger(__name__)

EXCHANGE_ID: ExchangeId = "binanceusdm"
"""The USD⌂-M perps venue: the only venue whose funding rates this writer stores."""

FUNDING_PAGE_LIMIT = 1000
"""Binance fapi GET /fapi/v1/fundingRate caps one response at 1000 rows."""

FUNDING_PAGE_WEIGHT = 1
"""One funding-history page: one request on fapi's shared 500-per-5-min IP limit."""

FUNDING_WEIGHT_PER_MIN = 100
"""The writer's page budget per 60 s window (the shared fapi limit, re-derived)."""

REFRESH_INTERVAL_S = 60.0 * 60
RETRY_INTERVAL_S = 60.0 * 60

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]
type ClientFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class FundingRate:
    """One settled funding rate; ``(exchange, symbol, timestamp)`` identifies it."""

    symbol: str
    rate: Decimal
    timestamp: datetime
    next_funding_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class FundingWriteReport:
    """What one ``ensure_funding_rates`` call did."""

    rows_fetched: int
    rows_inserted: int
    weight_spent: int


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def parse_funding_rate_history(
    entries: Sequence[Mapping[str, Any]], *, symbol: str
) -> list[FundingRate]:
    """ccxt ``fetch_funding_rate_history`` entries -> rows for *symbol* (the request's id).

    Binance's history entries carry no next funding time, so ``next_funding_time``
    is None for this venue; a venue that reports one (``nextFundingTime``) keeps it.
    """
    rows: list[FundingRate] = []
    for entry in entries:
        timestamp = entry.get("timestamp")
        rate = entry.get("fundingRate")
        if not isinstance(timestamp, int | float) or rate is None:
            continue
        next_ms = entry.get("nextFundingTime")
        rows.append(
            FundingRate(
                symbol=symbol,
                rate=Decimal(str(rate)),
                timestamp=_from_ms(int(timestamp)),
                next_funding_time=_from_ms(int(next_ms))
                if isinstance(next_ms, int | float)
                else None,
            )
        )
    return rows


async def fetch_funding_page(
    client: Any,
    symbol: str,
    since_ms: int,
    end_ms: int,
    *,
    limit: int = FUNDING_PAGE_LIMIT,
) -> list[FundingRate]:
    """One ``fetch_funding_rate_history`` page for ``[since_ms, end_ms]`` (oldest first).

    fapi clamps to ``until`` server-side; rows a misbehaving response returns past
    ``end_ms`` are dropped — the cursor never leaves the requested window.
    """
    entries: Sequence[Mapping[str, Any]] = await client.fetch_funding_rate_history(
        symbol, since=since_ms, limit=limit, params={"until": end_ms}
    )
    return [
        row
        for row in parse_funding_rate_history(entries, symbol=symbol)
        if _ms(row.timestamp) <= end_ms
    ]


_UPSERT_FUNDING_SQL = """
    INSERT INTO ts.funding_rates (exchange, symbol, rate, next_funding_time, timestamp)
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::numeric[], $4::timestamptz[], $5::timestamptz[]
    ) AS u(exchange, symbol, rate, next_funding_time, timestamp)
    WHERE NOT EXISTS (
        SELECT 1 FROM ts.funding_rates f
        WHERE f.exchange = u.exchange AND f.symbol = u.symbol AND f.timestamp = u.timestamp
    )
    RETURNING 1
"""

_NEWEST_FUNDING_SQL = (
    "SELECT max(timestamp) FROM ts.funding_rates WHERE exchange = $1 AND symbol = $2"
)

_RUNNING_PERP_SYMBOLS_SQL = """
    SELECT DISTINCT symbol
    FROM trading_sessions
    CROSS JOIN LATERAL unnest(symbols) AS symbol
    WHERE status = 'running' AND exchange_id = $1
"""


async def upsert_funding_rates(
    pool: Any, rows: Sequence[FundingRate], *, exchange: str = EXCHANGE_ID
) -> int:
    """Insert the rows ``ts.funding_rates`` does not have; existing rows stay as stored.

    Funding history is immutable, so an existing ``(exchange, symbol, timestamp)``
    row is never rewritten and a re-fetch inserts nothing. The 0001 schema has no
    unique constraint on the hypertable, so a per-symbol advisory transaction lock
    keeps two concurrent writers (the engine's refresher and a backtest preflight)
    from double-inserting; a writer that does not get the lock defers to the one
    that holds it.
    """
    unique: dict[datetime, FundingRate] = {}
    for row in rows:
        unique.setdefault(row.timestamp, row)
    if not unique:
        return 0
    batch = sorted(unique.values(), key=lambda row: row.timestamp)
    async with pool.acquire() as conn, conn.transaction():
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtext($1))",
            f"funding_rates:{exchange}:{batch[0].symbol}",
        )
        if not locked:
            return 0
        inserted = await conn.fetch(
            _UPSERT_FUNDING_SQL,
            [exchange] * len(batch),
            [row.symbol for row in batch],
            [row.rate for row in batch],
            [row.next_funding_time for row in batch],
            [row.timestamp for row in batch],
        )
    return len(inserted)


async def newest_funding_timestamp(
    pool: Any, symbol: str, *, exchange: str = EXCHANGE_ID
) -> datetime | None:
    """The newest stored funding row's timestamp for the symbol, or None when there is none."""
    async with pool.acquire() as conn:
        timestamp = await conn.fetchval(_NEWEST_FUNDING_SQL, exchange, symbol)
    return timestamp


async def running_perp_symbols(pool: Any) -> list[str]:
    """Every symbol a running USD⌂-M perp session trades (the refresher's worklist)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RUNNING_PERP_SYMBOLS_SQL, EXCHANGE_ID)
    return sorted(str(row["symbol"]) for row in rows)


async def ensure_funding_rates(
    pool: Any,
    client: Any,
    symbol: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    page_size: int = FUNDING_PAGE_LIMIT,
    budget: WeightBudget | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> FundingWriteReport:
    """Store the settled funding rows of ``[start, end]`` (or since the newest stored row).

    ``start=None`` resumes from the symbol's newest stored row — the top-up the
    refresher runs; with no stored rows and no ``start`` nothing is fetched: the
    cold start is the battery's one-shot backfill, never a quiet guess. Every page
    is charged to the fixed-window weight budget before the request goes out, and
    only missing rows are inserted, so re-running any window is idempotent.
    """
    fetch_start = start
    if fetch_start is None:
        newest = await newest_funding_timestamp(pool, symbol)
        if newest is None:
            return FundingWriteReport(0, 0, 0)
        fetch_start = newest + timedelta(milliseconds=1)
    fetch_end = end or (now or datetime.now(UTC))
    since_ms = _ms(fetch_start)
    end_ms = _ms(fetch_end)
    if since_ms > end_ms:
        return FundingWriteReport(0, 0, 0)
    if budget is None:
        budget = WeightBudget(FUNDING_WEIGHT_PER_MIN, clock=clock, sleep=sleep)
    rows_fetched = rows_inserted = weight = 0
    while since_ms <= end_ms:
        await budget.acquire(FUNDING_PAGE_WEIGHT)
        weight += FUNDING_PAGE_WEIGHT
        page = await fetch_funding_page(client, symbol, since_ms, end_ms, limit=page_size)
        if not page:
            break
        rows_fetched += len(page)
        rows_inserted += await upsert_funding_rates(pool, page)
        newest_ms = max(_ms(row.timestamp) for row in page)
        if newest_ms < since_ms:
            break
        since_ms = newest_ms + 1
    if rows_inserted:
        logger.info(
            "funding rates ensured",
            symbol=symbol,
            rows_fetched=rows_fetched,
            rows_inserted=rows_inserted,
            weight_spent=weight,
        )
    return FundingWriteReport(
        rows_fetched=rows_fetched,
        rows_inserted=rows_inserted,
        weight_spent=weight,
    )


def default_client() -> Any:
    """A fresh public Binance USD⌂-M REST client (no keys) — the refresher's default.

    The binanceusdm class's own defaults load only linear swap markets (verified
    against the installed ccxt describe()), so no options are needed here.
    """
    import ccxt.async_support as ccxt_async

    return ccxt_async.binanceusdm({"enableRateLimit": True})


class FundingRefresher:
    """Background funding top-up loop (the ``UniverseRefresher`` pattern).

    Each cycle tops up the funding rows of every running perp session's symbols
    since each symbol's newest stored row; with no running perp sessions the
    cycle is a no-op. A failed cycle is a warning retried after ``retry_s``; the
    loop itself never raises into the lifespan. ``sleep`` is injectable so tests
    run the schedule without wall time.
    """

    def __init__(
        self,
        pool: Any,
        *,
        client_factory: ClientFactory | None = None,
        interval_s: float = REFRESH_INTERVAL_S,
        retry_s: float = RETRY_INTERVAL_S,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._pool = pool
        self._client_factory = client_factory or default_client
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._budget = WeightBudget(FUNDING_WEIGHT_PER_MIN, clock=clock, sleep=sleep)
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Schedule the loop; the first cycle runs immediately, in the background."""
        if self._task is not None:
            raise RuntimeError("FundingRefresher.start() called twice")
        self._task = asyncio.create_task(self._run(), name="funding-refresher")

    async def stop(self) -> None:
        """Cancel the loop and wait for it; safe to call twice or before ``start``."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "funding refresh failed; retrying in %.0f s", self._retry_s, exc_info=True
                )
                await self._sleep(self._retry_s)
                continue
            await self._sleep(self._interval_s)

    async def _cycle(self) -> None:
        """One top-up pass over the running perp sessions' symbols."""
        symbols = await running_perp_symbols(self._pool)
        if not symbols:
            return
        client = self._client_factory()
        try:
            for symbol in symbols:
                try:
                    report = await ensure_funding_rates(
                        self._pool, client, symbol, budget=self._budget
                    )
                except Exception:
                    logger.warning(
                        "funding refresh failed for %s; retrying next cycle",
                        symbol,
                        exc_info=True,
                    )
                    continue
                if report.rows_inserted:
                    logger.info(
                        "funding rates refreshed",
                        symbol=symbol,
                        rows_inserted=report.rows_inserted,
                        weight_spent=report.weight_spent,
                    )
        finally:
            await client.close()
