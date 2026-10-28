"""The disk-budget sweeper: auto-fetched bars held under a hard byte cap (P6-C2).

Every ``sweep_interval_s`` the ledger is read least-recently-used first and each
dataset's size is recomputed from the bars themselves — ``SELECT count(*)`` over
``source='auto'`` — so a drifted ledger row can never protect data past the
user's budget (the W1 single-writer addendum: the recount is the sweeper's own
truth, the ledger's ``rows`` is advisory). Whole datasets are deleted through
``delete_auto_dataset`` — the delete predicate is ``source='auto'``, so a
feed-written bar can never match — until the cache fits ``cache_max_gb``;
``cache_max_gb=0`` reads as delete-after-run (every eligible dataset goes).

Three rails decide eligibility: a symbol of any running trading session is
protected on every timeframe; a dataset the fetch engine is writing
(``status='fetching'``) is skipped; a fetching dataset whose last fetched page
is older than ``2 x fetch_patience_s`` is a crashed fetch (the engine's
``finally`` never ran) and is swept like any other. A failed or hung cycle
(``asyncio.wait_for`` over the interval) counts one
``hydra_data_sweep_errors_total``, logs a warning and yields to the next
cycle: the sweeper never raises into the lifespan and never blocks a run.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from hydra.core import metrics
from hydra.core.config import DataConfig
from hydra.core.logging import get_logger
from hydra.core.types import Timeframe
from hydra.data.storage import DataCacheEntry, delete_auto_dataset, list_cache_entries

logger = get_logger(__name__)

_GIB = 1 << 30

_RUNNING_SESSION_SYMBOLS_SQL = """
    SELECT DISTINCT exchange_id, symbol
    FROM trading_sessions
    CROSS JOIN LATERAL unnest(symbols) AS symbol
    WHERE status = 'running'
"""

_AUTO_ROW_COUNT_SQL = """
    SELECT count(*) AS rows
    FROM ts.ohlcv_1m
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3 AND source = 'auto'
"""


async def _running_session_pairs(pool: Any) -> set[tuple[str, str]]:
    """Every ``(exchange_id, symbol)`` a running session trades, symbol-level."""
    async with pool.acquire() as conn:
        records = await conn.fetch(_RUNNING_SESSION_SYMBOLS_SQL)
    return {(r["exchange_id"], r["symbol"]) for r in records}


async def _auto_rows(pool: Any, entry: DataCacheEntry) -> int:
    """The dataset's size recomputed from the auto bars (never the ledger's count)."""
    async with pool.acquire() as conn:
        rows = await conn.fetchval(
            _AUTO_ROW_COUNT_SQL, entry.exchange, entry.symbol, entry.timeframe
        )
    return int(rows)


def _is_stale_fetch(entry: DataCacheEntry, patience_s: int) -> bool:
    """True when a fetching row's last page predates 2 x the engine's patience (a crash)."""
    return datetime.now(UTC) - entry.last_fetched_at > timedelta(seconds=2 * patience_s)


class DataCacheSweeper:
    """Background LRU sweep of the auto-fetched bar cache (the HealthMonitor task pattern).

    The first cycle runs immediately in the background task — never inline in
    ``start()``, so a big eviction cannot block startup — then every
    ``sweep_interval_s``. Without a pool (dev) ``start()`` is a no-op.
    """

    def __init__(self, pool: Any, config: DataConfig) -> None:
        self._pool = pool
        self._config = config
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Schedule the loop; a no-op without a pool or when already running."""
        if self._pool is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="data-cache-sweeper")

    async def stop(self) -> None:
        """Cancel the loop and wait for it; safe before ``start`` or called twice."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        interval = self._config.sweep_interval_s
        while True:
            try:
                await asyncio.wait_for(self._cycle(), interval)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                metrics.record_data_sweep_error()
                logger.warning(
                    "data-cache sweep hung past its interval; retrying next cycle",
                    interval_s=interval,
                )
            except Exception:
                metrics.record_data_sweep_error()
                logger.warning("data-cache sweep failed; retrying next cycle", exc_info=True)
            await asyncio.sleep(interval)

    async def _cycle(self) -> tuple[int, int]:
        deleted_rows, deleted_datasets = await self.sweep_once(self._pool)
        metrics.mark_data_sweep()
        logger.info(
            "data-cache sweep complete",
            deleted_rows=deleted_rows,
            deleted_datasets=deleted_datasets,
        )
        return (deleted_rows, deleted_datasets)

    async def sweep_once(self, pool: Any) -> tuple[int, int]:
        """One eviction pass; returns the ``(bar rows, datasets)`` actually deleted."""
        cfg = self._config
        budget_bytes = int(cfg.cache_max_gb * _GIB)
        protected = await _running_session_pairs(pool)
        entries = await list_cache_entries(pool)
        eligible: list[DataCacheEntry] = []
        total_rows = 0
        for entry in entries:
            total_rows += await _auto_rows(pool, entry)
            if (entry.exchange, entry.symbol) in protected:
                continue
            if entry.status == "fetching" and not _is_stale_fetch(entry, cfg.fetch_patience_s):
                continue
            eligible.append(entry)

        deleted_rows = 0
        deleted_datasets = 0
        evicted: set[tuple[str, str, str]] = set()
        for entry in eligible:
            if cfg.cache_max_gb > 0 and total_rows * cfg.row_bytes_estimate <= budget_bytes:
                break
            removed = await delete_auto_dataset(
                pool, entry.exchange, entry.symbol, Timeframe(entry.timeframe)
            )
            deleted_rows += removed
            total_rows -= removed
            deleted_datasets += 1
            evicted.add((entry.exchange, entry.symbol, entry.timeframe))

        if cfg.cache_max_gb > 0 and total_rows * cfg.row_bytes_estimate > budget_bytes:
            logger.warning(
                "data cache still over budget with nothing eligible",
                estimated_bytes=total_rows * cfg.row_bytes_estimate,
                budget_bytes=budget_bytes,
            )
        remaining_pairs = {
            (e.exchange, e.symbol)
            for e in entries
            if (e.exchange, e.symbol, e.timeframe) not in evicted
        }
        metrics.set_data_cache(
            total_rows, total_rows * cfg.row_bytes_estimate, len(remaining_pairs), budget_bytes
        )
        metrics.record_data_sweep_deleted(deleted_rows)
        return (deleted_rows, deleted_datasets)
