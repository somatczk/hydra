"""Alpaca daily bars through the fetch engine on real TimescaleDB (S1-b).

Needs HYDRA_DB_* (see tests/infra.py). The exchange is ``httpx.MockTransport`` behind the
real ``AlpacaClient``: a US_EQUITY dataset refetches its whole window on every call, so a
split re-scale reaches every stored row while the cache ledger's row count stays put.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest

from hydra.core.types import Timeframe
from hydra.data.alpaca import AlpacaClient
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.on_demand import OnDemandFetchService
from hydra.data.storage import MarketDataRepository, list_cache_entries

pytestmark = pytest.mark.integration

NOW = datetime(2024, 4, 10, 15, tzinfo=UTC)
# the NYSE sessions of 2024-03-25..2024-04-05 (Good Friday, 03-29, was closed); EDT midnights
SESSIONS = ["03-25", "03-26", "03-27", "03-28", "04-01", "04-02", "04-03", "04-04", "04-05"]


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


class _Bars:
    """The bars endpoint: every session from ``start`` on, closes scaled by ``scale``."""

    def __init__(self) -> None:
        self.scale = 1.0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        start = request.url.params["start"]
        bars = [
            {
                "t": f"2024-{day}T04:00:00Z",
                "o": 170.0 * self.scale,
                "h": 172.0 * self.scale,
                "l": 168.0 * self.scale,
                "c": (168.5 + i) * self.scale,
                "v": 1000 + i,
            }
            for i, day in enumerate(SESSIONS)
            if f"2024-{day}T04:00:00Z" >= start
        ]
        return httpx.Response(200, json={"bars": bars, "next_page_token": None})


async def _closes(pool: Any) -> dict[datetime, Decimal]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, close FROM ts.ohlcv_1m "
            "WHERE exchange = 'alpaca' AND symbol = 'AAPL/USD' AND timeframe = '1d' "
            "ORDER BY timestamp"
        )
    return {r["timestamp"]: r["close"] for r in rows}


async def test_a_split_rescale_refetch_updates_every_row_and_keeps_the_ledger_count(
    pool: Any,
) -> None:
    bars = _Bars()
    client = AlpacaClient("k", "s", transport=httpx.MockTransport(bars), clock=lambda: NOW)

    async def provider(exchange_id: str) -> AlpacaClient:
        return client

    engine = OnDemandFetchService(provider, now=NOW)
    repo = MarketDataRepository.from_pool(pool)
    start, end = datetime(2024, 3, 25, tzinfo=UTC), datetime(2024, 4, 5, tzinfo=UTC)
    labels = [datetime.fromisoformat(f"2024-{day}T00:00:00+00:00") for day in SESSIONS]

    await engine.ensure_available(repo, "alpaca", "AAPL/USD", Timeframe.D1, start, end)
    assert await _closes(pool) == {ts: Decimal(str(168.5 + i)) for i, ts in enumerate(labels)}
    [entry] = await list_cache_entries(pool)
    assert (entry.exchange, entry.rows, entry.first_ts, entry.last_ts) == (
        "alpaca",
        9,
        labels[0],
        labels[-1],
    )

    bars.scale = 0.25  # a 4:1 split lands: the provider re-scales all history
    await engine.ensure_available(repo, "alpaca", "AAPL/USD", Timeframe.D1, start, end)
    assert await _closes(pool) == {
        ts: Decimal(str((168.5 + i) * 0.25)) for i, ts in enumerate(labels)
    }
    [entry] = await list_cache_entries(pool)
    assert entry.rows == 9
    # no force flag on either call: the second refetched the whole window by itself
    assert [r.url.params["start"] for r in bars.requests] == [
        "2024-03-25T00:00:00Z",
        "2024-03-25T00:00:00Z",
    ]
