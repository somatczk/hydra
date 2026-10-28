"""storage.get_source_counts on real TimescaleDB (A3, from #212's review).

The dataset provenance's source-mix query, asserted over seeded source rows:
curated ``feed`` bars and auto-fetched bars coexist in one dataset and the mix
reads back exactly as seeded, both through the repository query directly and
through the DatasetBuilder's provenance (the seam the ml_train job reports).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from hydra.backtest.jobs import MlTrainRunRequest, ml_feature_spec
from hydra.core.types import OHLCV, Timeframe
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.ml.dataset import DatasetBuilder

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


def _bar(ts: datetime, close: int) -> OHLCV:
    return OHLCV(
        open=Decimal("100.0"),
        high=Decimal("103.0"),
        low=Decimal("99.5"),
        close=Decimal(str(close)),
        volume=Decimal("1"),
        timestamp=ts,
    )


async def _seed_mix(repo: MarketDataRepository) -> tuple[datetime, datetime]:
    """30 curated feed bars, then 12 auto-fetched bars, one 1h dataset."""
    feed = [("binance", "BTCUSDT", Timeframe.H1, _bar(T0 + i * H, 100 + i)) for i in range(30)]
    auto_start = T0 + 30 * H
    auto = [
        ("binance", "BTCUSDT", Timeframe.H1, _bar(auto_start + i * H, 200 + i)) for i in range(12)
    ]
    assert await repo.store_bars(feed) == 30
    inserted, updated = await repo.upsert_closed_bars(auto, source="auto")
    assert (inserted, updated) == (12, 0)
    return auto_start, auto_start + 11 * H


async def test_source_counts_return_the_seeded_source_mix(pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    auto_start, _ = await _seed_mix(repo)

    counts = await repo.get_source_counts("binance", "BTCUSDT", Timeframe.H1)
    assert counts == {"feed": 30, "auto": 12}

    # the range window only ever sees the rows inside it
    late = await repo.get_source_counts("binance", "BTCUSDT", Timeframe.H1, auto_start, None)
    assert late == {"auto": 12}
    early = await repo.get_source_counts("binance", "BTCUSDT", Timeframe.H1, None, auto_start - H)
    assert early == {"feed": 30}
    assert await repo.get_source_counts("binance", "NOPEUSDT", Timeframe.H1) == {}


async def test_the_dataset_provenance_reports_the_same_mix(pool: Any) -> None:
    """The builder's coverage sources are the same query over the bars it actually read."""
    repo = MarketDataRepository.from_pool(pool)
    await _seed_mix(repo)
    spec = ml_feature_spec(
        MlTrainRunRequest(
            name="alpha",
            strategy_id="saw",
            symbol="BTCUSDT",
            window_1h=42,
            window_4h=1,
            window_1d=1,
        )
    )

    _matrix, provenance = await DatasetBuilder(repo).build(spec, "binance", "BTCUSDT")

    assert provenance.symbol == "BTCUSDT"
    coverage, *higher = provenance.coverage
    assert coverage.timeframe == "1h"
    assert coverage.bar_count == 42
    assert coverage.sources == {"feed": 30, "auto": 12}
    assert coverage.first_ts == T0
    assert coverage.last_ts == T0 + 41 * H
    assert coverage.gap_count == 0
    # the higher timeframes were absent and disclosed as such
    assert [c.bar_count for c in higher] == [0, 0]
