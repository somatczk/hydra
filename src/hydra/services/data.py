"""Stored market-data reads: the bar coverage behind the ``data_coverage`` tool and
the cache inventory behind ``data_inventory``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from hydra.core.config import DataConfig
from hydra.core.types import Timeframe
from hydra.data.storage import MarketDataRepository, cache_totals, list_cache_entries
from hydra.services.errors import db_errors, require_pool, service_error

logger = logging.getLogger(__name__)

_GIB = 1 << 30

_SOURCES_SQL = """
    SELECT source, COUNT(*) AS n
    FROM ts.ohlcv_1m
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
    GROUP BY source
"""

_CACHE_ENTRY_SQL = """
    SELECT status, rows, first_ts, last_ts, last_used_at, last_fetched_at
    FROM data_cache_entries
    WHERE exchange = $1 AND symbol = $2 AND timeframe = $3
"""


async def bar_coverage(
    pool: Any,
    exchange_id: str,
    symbol: str,
    timeframe: Timeframe,
    row_bytes_estimate: int,
) -> dict[str, Any]:
    """The stored range for one market: ``first_ts``/``last_ts`` (ISO, UTC), ``bar_count``,
    the ``feed``/``auto`` source split, ``estimated_bytes`` (rows x the estimate) and the
    auto-cache ledger row (``cache``; null when the market was never auto-fetched).

    The ledger row's ``coverage`` range spans the fetch grid its market was asked to
    fill — it can predate the first bar the ledger lists — and its ``rows`` counts only
    bars a fetch inserted, so the range is a requested-coverage range, never an
    "auto-cached since" bound. 404 ``no_stored_data`` when nothing is stored for the
    exchange/symbol/timeframe; 503 when the process has no pool or the database cannot
    answer.
    """
    pool = require_pool(pool)
    repo = MarketDataRepository.from_pool(pool)
    async with db_errors("reading stored bar coverage"):
        stored = await repo.get_bar_range(exchange_id, symbol, timeframe)
        if stored is None:
            raise service_error(
                404,
                "no_stored_data",
                f"No stored bars for {exchange_id} {symbol} {timeframe}",
                "no_data",
                exchange_id=exchange_id,
                symbol=symbol,
                timeframe=str(timeframe),
            )
        async with pool.acquire() as conn:
            source_rows = await conn.fetch(_SOURCES_SQL, exchange_id, symbol, str(timeframe))
            entry = await conn.fetchrow(_CACHE_ENTRY_SQL, exchange_id, symbol, str(timeframe))
    first, last, count = stored
    sources = {str(row["source"]): int(row["n"]) for row in source_rows}
    sources.setdefault("feed", 0)
    sources.setdefault("auto", 0)
    return {
        "exchange_id": exchange_id,
        "symbol": symbol,
        "timeframe": str(timeframe),
        "first_ts": first.isoformat(),
        "last_ts": last.isoformat(),
        "bar_count": count,
        "sources": sources,
        "estimated_bytes": count * row_bytes_estimate,
        "cache": (
            {
                "status": entry["status"],
                "rows": int(entry["rows"]),
                "coverage": {
                    "first_ts": entry["first_ts"].isoformat(),
                    "last_ts": entry["last_ts"].isoformat(),
                },
                "last_used_at": entry["last_used_at"].isoformat(),
                "last_fetched_at": entry["last_fetched_at"].isoformat(),
            }
            if entry is not None
            else None
        ),
    }


async def cache_inventory(
    pool: Any, data_config: DataConfig, last_sweep: float | None
) -> dict[str, Any]:
    """Every cached dataset, least-recently-used first, with the cache totals.

    The LRU order is the sweeper's eviction order, so the eviction candidates lead
    the list. Each dataset's ``coverage`` range spans the fetch grid its market was
    asked to fill — it can predate the first bar the ledger lists — and its ``rows``
    counts only bars a fetch inserted, so a rows=0 dataset is an honestly reported
    "no bars" (a requested range the exchange never served), never a hidden one.
    ``last_sweep`` is the wall-clock time the sweeper last completed a cycle (null
    before its first cycle). 503 when the process has no pool or the database cannot
    answer.
    """
    pool = require_pool(pool)
    async with db_errors("reading the data cache inventory"):
        entries = await list_cache_entries(pool)
        total_rows, _datasets = await cache_totals(pool)
    return {
        "datasets": [
            {
                "exchange": entry.exchange,
                "symbol": entry.symbol,
                "timeframe": entry.timeframe,
                "coverage": {
                    "first_ts": entry.first_ts.isoformat(),
                    "last_ts": entry.last_ts.isoformat(),
                },
                "rows": entry.rows,
                "estimated_bytes": entry.rows * data_config.row_bytes_estimate,
                "status": entry.status,
                "last_used_at": entry.last_used_at.isoformat(),
            }
            for entry in entries
        ],
        "totals": {
            "bytes": total_rows * data_config.row_bytes_estimate,
            "rows": total_rows,
            "pairs": len({(entry.exchange, entry.symbol) for entry in entries}),
            "budget_bytes": int(data_config.cache_max_gb * _GIB),
            "last_sweep": (
                datetime.fromtimestamp(last_sweep, UTC).isoformat()
                if last_sweep is not None
                else None
            ),
        },
    }
