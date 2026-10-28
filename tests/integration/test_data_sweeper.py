"""The disk-budget sweeper on real TimescaleDB (P6-C2).

Needs HYDRA_DB_* (see tests/infra.py). Every seed goes through the storage seam,
so these prove the sweeper's safety rails on committed rows: exactly the
least-recently-used datasets go, a running session's symbol survives on every
timeframe, feed-written bars never match the delete predicate, a crashed fetch
is recovered and the eviction input is recomputed from the bars.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from prometheus_client import REGISTRY

from hydra.backtest.data import load_bars, load_latest_bars
from hydra.core.config import DataConfig
from hydra.core.types import OHLCV, Timeframe
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository, list_cache_entries
from hydra.data.sweeper import DataCacheSweeper

pytestmark = pytest.mark.integration

T0 = datetime(2026, 2, 2, 10, tzinfo=UTC)
M = timedelta(minutes=1)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


def _bar(ts: datetime, close: str = "100") -> OHLCV:
    return OHLCV(
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.5"),
        close=Decimal(close),
        volume=Decimal("1"),
        timestamp=ts,
    )


def _cfg(**overrides: Any) -> DataConfig:
    return DataConfig(**{"fetch_patience_s": 100, "sweep_interval_s": 3600}, **overrides)


async def _seed_auto(pool: Any, symbol: str, rows: int, timeframe: Timeframe) -> None:
    repo = MarketDataRepository.from_pool(pool)
    bars = [
        ("binance", symbol, timeframe, _bar(T0 + timedelta(minutes=10 * i))) for i in range(rows)
    ]
    assert await repo.upsert_closed_bars(bars, source="auto") == (rows, 0)


async def _age_last_used(pool: Any, symbol: str, minutes: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE data_cache_entries "
            "SET last_used_at = now() - make_interval(mins => $3) "
            "WHERE exchange = 'binance' AND symbol = $1 AND timeframe = $2",
            symbol,
            "1m",
            minutes,
        )


async def _bar_count(pool: Any, symbol: str, timeframe: str = "1m", source: str = "auto") -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM ts.ohlcv_1m "
                "WHERE exchange = 'binance' AND symbol = $1 AND timeframe = $2 AND source = $3",
                symbol,
                timeframe,
                source,
            )
        )


async def _seed_running_session(pool: Any, symbol: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO trading_sessions "
            "(id, strategy_id, trading_mode, status, exchange_id, symbols, timeframe) "
            "VALUES ('sweeper-guard', 'sma', 'paper', 'running', 'binance', "
            "ARRAY[$1]::text[], '1m')",
            symbol,
        )


async def test_the_lru_evicts_exactly_the_two_oldest_datasets(pool: Any) -> None:
    for symbol, rows in (("BTCUSDT", 1000), ("ETHUSDT", 2000), ("SOLUSDT", 3000)):
        await _seed_auto(pool, symbol, rows, Timeframe.M1)
    await _age_last_used(pool, "BTCUSDT", 60)
    await _age_last_used(pool, "ETHUSDT", 30)
    await _age_last_used(pool, "SOLUSDT", 5)

    sweeper = DataCacheSweeper(pool, _cfg(cache_max_gb=500_000 / 2**30))
    assert await sweeper.sweep_once(pool) == (3000, 2)

    entries = await list_cache_entries(pool)
    assert [(e.symbol, e.rows) for e in entries] == [("SOLUSDT", 3000)]
    assert await _bar_count(pool, "BTCUSDT") == 0
    assert await _bar_count(pool, "ETHUSDT") == 0
    assert await _bar_count(pool, "SOLUSDT") == 3000
    assert REGISTRY.get_sample_value("hydra_data_sweep_deleted_rows_total") == 3000.0
    assert REGISTRY.get_sample_value("hydra_data_cache_rows") == 3000.0


async def test_a_running_sessions_symbol_survives_on_every_timeframe(pool: Any) -> None:
    await _seed_auto(pool, "DOGEUSDT", 500, Timeframe.M1)
    await _seed_auto(pool, "DOGEUSDT", 700, Timeframe.H1)
    await _seed_auto(pool, "BTCUSDT", 600, Timeframe.M1)
    await _age_last_used(pool, "DOGEUSDT", 60)
    await _age_last_used(pool, "BTCUSDT", 5)
    await _seed_running_session(pool, "DOGEUSDT")

    sweeper = DataCacheSweeper(pool, _cfg(cache_max_gb=0.0))
    assert await sweeper.sweep_once(pool) == (600, 1)

    remaining = {(e.symbol, e.timeframe) for e in await list_cache_entries(pool)}
    assert remaining == {("DOGEUSDT", "1m"), ("DOGEUSDT", "1h")}
    assert await _bar_count(pool, "DOGEUSDT") == 500
    assert await _bar_count(pool, "DOGEUSDT", timeframe="1h") == 700
    assert await _bar_count(pool, "BTCUSDT") == 0


async def test_feed_rows_survive_the_sweep_exactly(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    feed = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + 2 * i * M)) for i in range(50)]
    auto = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + (2 * i + 1) * M)) for i in range(50)]
    assert await repo.store_bars(feed) == 50
    assert await repo.upsert_closed_bars(auto, source="auto") == (50, 0)

    sweeper = DataCacheSweeper(pool, _cfg(cache_max_gb=0.0))
    assert await sweeper.sweep_once(pool) == (50, 1)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, source FROM ts.ohlcv_1m "
            "WHERE exchange = 'binance' AND symbol = 'BTCUSDT' ORDER BY timestamp"
        )
    assert [r["source"] for r in rows] == ["feed"] * 50
    assert all(r["timestamp"].minute % 2 == 0 for r in rows)
    assert await list_cache_entries(pool) == []


async def test_a_stale_fetching_row_is_swept_and_a_fresh_one_is_skipped(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await _seed_auto(pool, "BTCUSDT", 10, Timeframe.M1)
    await _seed_auto(pool, "ETHUSDT", 20, Timeframe.M1)
    async with pool.acquire() as conn:
        # a fetch that crashed mid-run: no page for 4 minutes (patience 100s x 2 = 200s)
        await conn.execute(
            "UPDATE data_cache_entries SET status = 'fetching', "
            "last_fetched_at = now() - interval '4 minutes' WHERE symbol = 'BTCUSDT'"
        )
    await repo.set_cache_status("binance", "ETHUSDT", Timeframe.M1, "fetching", T0, T0 + 10 * M)

    sweeper = DataCacheSweeper(pool, _cfg(cache_max_gb=0.0))
    assert await sweeper.sweep_once(pool) == (10, 1)

    assert [e.symbol for e in await list_cache_entries(pool)] == ["ETHUSDT"]
    assert await _bar_count(pool, "BTCUSDT") == 0
    assert await _bar_count(pool, "ETHUSDT") == 20


async def test_the_eviction_input_is_recomputed_from_the_bars_not_the_ledger(pool: Any) -> None:
    await _seed_auto(pool, "BTCUSDT", 50, Timeframe.M1)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE data_cache_entries SET rows = 100 WHERE symbol = 'BTCUSDT'")

    # the ledger claims 100 x 160 = 16_000 bytes (over the 10_000 budget); the bars hold 50 x 160
    sweeper = DataCacheSweeper(pool, _cfg(cache_max_gb=10_000 / 2**30))
    assert await sweeper.sweep_once(pool) == (0, 0)

    assert [e.symbol for e in await list_cache_entries(pool)] == ["BTCUSDT"]
    assert REGISTRY.get_sample_value("hydra_data_cache_rows") == 50.0
    assert REGISTRY.get_sample_value("hydra_data_cache_bytes") == 50 * 160.0
    assert REGISTRY.get_sample_value("hydra_data_cache_budget_bytes") == 10_000.0


async def test_the_backtest_loaders_bump_last_used_at(pool: Any) -> None:
    await _seed_auto(pool, "BTCUSDT", 5, Timeframe.M1)
    await _seed_auto(pool, "ETHUSDT", 5, Timeframe.M15)
    stale = datetime.now(UTC) - timedelta(hours=2)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE data_cache_entries SET last_used_at = $1::timestamptz", stale)

    repo = MarketDataRepository.from_pool(pool)
    bars, prov = await load_bars(repo, "binance", "BTCUSDT", Timeframe.M1, T0, T0 + 60 * M)
    assert (len(bars), prov.bar_count) == (5, 5)
    await load_latest_bars(repo, "binance", "ETHUSDT", Timeframe.M15, 3)

    used = {e.symbol: e.last_used_at for e in await list_cache_entries(pool)}
    assert used["BTCUSDT"] > stale
    assert used["ETHUSDT"] > stale
    assert all(u > datetime.now(UTC) - timedelta(minutes=1) for u in used.values())
