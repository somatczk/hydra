"""Closed-bar persistence against a real TimescaleDB: upsert counts and the backtester's read path.

Needs HYDRA_DB_HOST/PORT/NAME/USER/PASSWORD (as exported by CI); tests/infra.py creates and drops
a throwaway database per test and fails (not skips) when the env is set but unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from hydra.backtest.data import load_bars
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, Event
from hydra.core.types import OHLCV, Timeframe
from hydra.data.backfill import ExchangeBackfillService, GapRange
from hydra.data.bars import last_closed_open
from hydra.data.ingestion import ExchangeFeedManager
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from tests.fixtures.binance_kline import BinanceKlineDriver, FakeRestExchange, FakeWsExchange

pytestmark = pytest.mark.integration

T0 = datetime(2026, 2, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


def _bar(ts: datetime, close: str, volume: str) -> OHLCV:
    return OHLCV(
        open=Decimal("100.0"),
        high=Decimal("103.0"),
        low=Decimal("99.5"),
        close=Decimal(close),
        volume=Decimal(volume),
        timestamp=ts,
    )


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def test_upsert_corrects_a_frozen_forming_candle(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    frozen = _bar(T0, "100.2", "0.3")
    assert await repo.store_bars([("binance", "BTCUSDT", Timeframe.H1, frozen)]) == 1

    final = _bar(T0, "102.7", "55")
    assert await repo.upsert_closed_bars([("binance", "BTCUSDT", Timeframe.H1, final)]) == (0, 1)
    assert await repo.upsert_closed_bars([("binance", "BTCUSDT", Timeframe.H1, final)]) == (0, 0)

    stored = await repo.get_latest_bars("binance", "BTCUSDT", Timeframe.H1, 10)
    assert [(b.close, b.volume) for b in stored] == [(Decimal("102.7"), Decimal("55"))]


async def test_upsert_counts_inserts_and_updates(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await repo.store_bars([("binance", "BTCUSDT", Timeframe.H1, _bar(T0, "1", "1"))])
    result = await repo.upsert_closed_bars(
        [
            ("binance", "BTCUSDT", Timeframe.H1, _bar(T0, "2", "1")),
            ("binance", "BTCUSDT", Timeframe.H1, _bar(T0 + H, "3", "1")),
            ("binance", "BTCUSDT", Timeframe.H1, _bar(T0 + H, "4", "1")),
        ]
    )
    assert result == (1, 1)
    closes = [b.close for b in await repo.get_latest_bars("binance", "BTCUSDT", Timeframe.H1, 10)]
    assert closes == [Decimal("2"), Decimal("4")]


async def test_store_bars_counts_only_new_rows(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    bars = [("binance", "BTCUSDT", Timeframe.H1, _bar(T0 + i * H, "100", "1")) for i in range(3)]
    assert await repo.store_bars(bars) == 3
    assert await repo.store_bars(bars) == 0


async def test_feed_persists_closed_bars_where_the_backtester_reads(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    ws = FakeWsExchange()
    bus = InMemoryEventBus()
    published: list[BarEvent] = []

    async def record(event: Event) -> None:
        assert isinstance(event, BarEvent)
        published.append(event)

    await bus.subscribe("bar", record)
    clock = (T0 + 3 * H + 5 * timedelta(seconds=1)).timestamp()
    feed = ExchangeFeedManager(
        event_bus=bus,
        exchange_factories={"binance": lambda: ws},
        repository=repo,
        rest_factory=lambda _eid: FakeRestExchange(),
        watchdog_interval_s=3600,
        clock=lambda: clock,
    )
    await feed.connect("binance", ["BTCUSDT"], [Timeframe.H1])
    driver = BinanceKlineDriver()
    for i, (close, final) in enumerate([(101.25, False), (102.5, True), (99.75, False)]):
        open_ms = int((T0 + (1 if i == 2 else 0) * H).timestamp() * 1000)
        ws.feed(
            driver.push_and_read(
                "BTCUSDT",
                "BTC/USDT",
                "1h",
                open_ms,
                (100.0, 103.0, 99.5, close, 12.5),
                closed=final,
            )
        )
    await _until(lambda: len(published) == 1)
    await feed.disconnect_all()

    bars, provenance = await load_bars(repo, "binance", "BTCUSDT", Timeframe.H1, T0, T0 + 2 * H)
    assert published[0].ohlcv == bars[0]
    assert [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in bars] == [
        (T0, Decimal("100.0"), Decimal("103.0"), Decimal("99.5"), Decimal("102.5"), Decimal("12.5"))
    ]
    assert provenance.bar_count == 1


async def test_warm_up_reads_back_stored_rows_after_rest_fill(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    now = T0 + 30 * H + 20 * timedelta(minutes=1)
    end = last_closed_open(now, Timeframe.H1)
    seeded = [_bar(end - (120 - i) * H, "100", "1") for i in range(118)]
    await repo.store_bars([("binance", "BTCUSDT", Timeframe.H1, b) for b in seeded])
    rest_rows = [
        [int((end - k * H).timestamp() * 1000), 100.0, 103.0, 99.5, 101.0 + k, 2.0]
        for k in range(2, -1, -1)
    ]
    feed = ExchangeFeedManager(
        event_bus=InMemoryEventBus(),
        repository=repo,
        rest_factory=lambda _eid: FakeRestExchange(rest_rows),
        history_window=100,
        clock=lambda: now.timestamp(),
    )

    warm = await feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)

    bars = warm[("BTCUSDT", Timeframe.H1)]
    assert len(bars) == 100
    assert bars[-1].timestamp == end
    assert [b.close for b in bars[-3:]] == [Decimal("103.0"), Decimal("102.0"), Decimal("101.0")]
    db = await repo.get_latest_bars("binance", "BTCUSDT", Timeframe.H1, 100, end)
    assert db == bars


G0 = datetime(2026, 2, 2, tzinfo=UTC)


async def _store_hours(
    repo: MarketDataRepository, hours: list[int], exchange: str = "binance"
) -> None:
    await repo.store_bars(
        [(exchange, "BTCUSDT", Timeframe.H1, _bar(G0 + h * H, "1", "1")) for h in hours]
    )


async def test_missing_ranges_edges_and_holes(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await _store_hours(repo, [2, 6, 7])
    gaps = await repo.missing_ranges("binance", "BTCUSDT", Timeframe.H1, G0, G0 + 9 * H)
    assert gaps == [(G0, G0 + H), (G0 + 3 * H, G0 + 5 * H), (G0 + 8 * H, G0 + 9 * H)]


async def test_missing_ranges_empty_store(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    gaps = await repo.missing_ranges("binance", "BTCUSDT", Timeframe.H1, G0, G0 + 9 * H)
    assert gaps == [(G0, G0 + 9 * H)]


async def test_missing_ranges_complete(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await _store_hours(repo, list(range(10)))
    assert await repo.missing_ranges("binance", "BTCUSDT", Timeframe.H1, G0, G0 + 9 * H) == []


async def test_missing_ranges_ignores_other_keys(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await _store_hours(repo, list(range(10)), exchange="bybit")
    minutes = [
        ("binance", "BTCUSDT", Timeframe.M1, _bar(G0 + m * timedelta(minutes=1), "1", "1"))
        for m in range(600)
    ]
    await repo.store_bars(minutes)
    gaps = await repo.missing_ranges("binance", "BTCUSDT", Timeframe.H1, G0, G0 + 9 * H)
    assert gaps == [(G0, G0 + 9 * H)]


async def test_detect_gaps_unaligned_start(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await _store_hours(repo, list(range(10)))
    service = ExchangeBackfillService(repo)
    start = G0 + timedelta(seconds=17)
    assert await service.detect_gaps("binance", "BTCUSDT", Timeframe.H1, start, G0 + 9 * H) == []


async def test_detect_gaps_finds_history_before_stored_data(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    day = timedelta(days=1)
    first = datetime(2024, 1, 1, tzinfo=UTC)
    await repo.store_bars(
        [("binance", "BTCUSDT", Timeframe.D1, _bar(first + i * day, "1", "1")) for i in range(2)]
    )
    gaps = await ExchangeBackfillService(repo).detect_gaps(
        "binance", "BTCUSDT", Timeframe.D1, datetime(2020, 1, 1, tzinfo=UTC), first + day
    )
    assert gaps == [GapRange(datetime(2020, 1, 1, tzinfo=UTC), datetime(2023, 12, 31, tzinfo=UTC))]
