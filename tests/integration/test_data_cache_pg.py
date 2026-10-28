"""Bar attribution and the data-cache ledger on real TimescaleDB (P6-C1).

Needs HYDRA_DB_* (see tests/infra.py): every write goes through the storage seam, so
these tests prove the sweeper's safety rails on committed rows — a feed row's source is
immutable, only auto rows are reclaimable, and the ledger cannot drift from the bars.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.backfill import ExchangeBackfillService
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import (
    MarketDataRepository,
    cache_totals,
    delete_auto_dataset,
    list_cache_entries,
)
from tests.fixtures.binance_kline import FakeRestExchange

pytestmark = pytest.mark.integration

T0 = datetime(2026, 2, 2, 10, tzinfo=UTC)
G0 = datetime(2026, 2, 2, tzinfo=UTC)
M = timedelta(minutes=1)
H = timedelta(hours=1)


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


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


async def _sources(pool: Any, timeframe: str = "1m") -> dict[datetime, str]:
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT timestamp, source FROM ts.ohlcv_1m "
            "WHERE exchange = 'binance' AND symbol = 'BTCUSDT' AND timeframe = $1 "
            "ORDER BY timestamp",
            timeframe,
        )
    return {r["timestamp"]: r["source"] for r in records}


async def test_auto_upsert_attributes_new_rows_and_seeds_the_ledger(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)

    bars = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(5)]
    assert await repo.upsert_closed_bars(bars, source="auto") == (5, 0)

    sources = await _sources(pool)
    assert sources == {T0 + i * M: "auto" for i in range(5)}
    entries = await list_cache_entries(pool)
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.exchange, entry.symbol, entry.timeframe) == ("binance", "BTCUSDT", "1m")
    assert (entry.first_ts, entry.last_ts) == (T0, T0 + 4 * M)
    assert entry.rows == 5
    assert entry.status == "ready"
    assert await cache_totals(pool) == (5, 1)


async def test_feed_upsert_corrects_values_but_never_reattributes(pool: Any) -> None:
    """A feed-default write may correct an auto row's values; its source and entry stay."""
    repo = MarketDataRepository.from_pool(pool)
    await repo.upsert_closed_bars(
        [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(3)], source="auto"
    )
    async with pool.acquire() as conn:
        # a sentinel proves the feed path never rewrites the ledger row
        await conn.execute("UPDATE data_cache_entries SET rows = 4242")

    corrected = [
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M, close="250")) for i in range(3)
    ]
    assert await repo.upsert_closed_bars(corrected) == (0, 3)

    assert set((await _sources(pool)).values()) == {"auto"}
    stored = await repo.get_latest_bars("binance", "BTCUSDT", Timeframe.M1, 10)
    assert [b.close for b in stored] == [Decimal("250")] * 3
    assert [e.rows for e in await list_cache_entries(pool)] == [4242]


async def test_auto_upsert_over_feed_rows_never_flips_the_source(pool: Any) -> None:
    """Feed-written rows keep 'feed' through any auto write; only new bars become 'auto'."""
    repo = MarketDataRepository.from_pool(pool)
    assert (
        await repo.store_bars(
            [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(3)]
        )
        == 3
    )

    same = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(3)]
    assert await repo.upsert_closed_bars(same, source="auto") == (0, 0)
    assert set((await _sources(pool)).values()) == {"feed"}
    assert await list_cache_entries(pool) == []

    corrected = [
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M, close="77")) for i in range(3)
    ]
    assert await repo.upsert_closed_bars(corrected, source="auto") == (0, 3)
    sources = await _sources(pool)
    assert set(sources.values()) == {"feed"}
    stored = await repo.get_latest_bars("binance", "BTCUSDT", Timeframe.M1, 10)
    assert [b.close for b in stored] == [Decimal("77")] * 3
    assert await list_cache_entries(pool) == []

    mixed = [
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0, close="88")),
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + 5 * M)),
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + 6 * M)),
    ]
    assert await repo.upsert_closed_bars(mixed, source="auto") == (2, 1)
    sources = await _sources(pool)
    assert sources[T0] == "feed"
    assert sources[T0 + 5 * M] == "auto"
    assert sources[T0 + 6 * M] == "auto"
    entries = await list_cache_entries(pool)
    assert [e.rows for e in entries] == [2]
    assert (entries[0].first_ts, entries[0].last_ts) == (T0, T0 + 6 * M)


async def test_ledger_merges_overlapping_pages_without_drift(pool: Any) -> None:
    """Two overlapping auto pages: rows add up, the range only grows, and matches the bars."""
    repo = MarketDataRepository.from_pool(pool)
    page_a = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(5)]
    assert await repo.upsert_closed_bars(page_a, source="auto") == (5, 0)
    page_b = [
        ("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M, close="9")) for i in range(3, 8)
    ]
    assert await repo.upsert_closed_bars(page_b, source="auto") == (3, 2)

    entries = await list_cache_entries(pool)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.rows == 8
    assert (entry.first_ts, entry.last_ts) == (T0, T0 + 7 * M)
    async with pool.acquire() as conn:
        committed = await conn.fetchval(
            "SELECT count(*) FROM ts.ohlcv_1m WHERE exchange = 'binance' AND symbol = 'BTCUSDT' "
            "AND timeframe = '1m' AND source = 'auto'"
        )
    assert committed == entry.rows == 8
    assert await cache_totals(pool) == (8, 1)


async def test_delete_auto_dataset_reclaims_only_auto_rows(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    feed = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(0, 10, 2)]
    assert await repo.store_bars(feed) == 5
    auto = [("binance", "BTCUSDT", Timeframe.M1, _bar(T0 + i * M)) for i in range(1, 10, 2)]
    assert await repo.upsert_closed_bars(auto, source="auto") == (5, 0)

    assert await delete_auto_dataset(pool, "binance", "BTCUSDT", Timeframe.M1) == 5

    sources = await _sources(pool)
    assert sorted(sources) == [T0 + i * M for i in range(0, 10, 2)]
    assert set(sources.values()) == {"feed"}
    assert await list_cache_entries(pool) == []
    assert await cache_totals(pool) == (0, 0)
    # re-sweeping a reclaimed dataset is a clean no-op
    assert await delete_auto_dataset(pool, "binance", "BTCUSDT", Timeframe.M1) == 0


async def test_bulk_download_auto_pages_accumulate_the_ledger(pool: Any) -> None:
    """The fetch path's seam end to end: paginated auto pages build one exact ledger row."""
    repo = MarketDataRepository.from_pool(pool)
    rows = [[_ms(G0 + h * H), 100.0, 101.0, 99.0, 100.5, 3.0] for h in range(10)]
    exchange = FakeRestExchange(rows)
    service = ExchangeBackfillService(repo, exchange_factories={"binance": lambda: exchange})

    total = await service.bulk_download(
        "binance",
        "BTCUSDT",
        Timeframe.H1,
        G0,
        G0 + 9 * H,
        page_size=3,
        now=G0 + 10 * H + 5 * M,
        source="auto",
    )

    assert total == 10
    assert len(exchange.calls) == 4
    entries = await list_cache_entries(pool)
    assert len(entries) == 1
    assert entries[0].rows == 10
    assert (entries[0].first_ts, entries[0].last_ts) == (G0, G0 + 9 * H)
    assert set((await _sources(pool, timeframe="1h")).values()) == {"auto"}
    assert await cache_totals(pool) == (10, 1)
