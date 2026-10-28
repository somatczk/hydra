"""The funding-rate writer on real TimescaleDB (P4-B).

Needs HYDRA_DB_* (see tests/infra.py). The upsert writes exactly the fetched
rows into the 0001-era hypertable (which has no unique constraint), a re-fetch
inserts nothing, two concurrent writers never double-insert (the per-symbol
advisory lock), and the top-up resumes after the newest stored row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from hydra.data.funding import (
    FundingRate,
    FundingWriteReport,
    ensure_funding_rates,
    newest_funding_timestamp,
    running_perp_symbols,
    upsert_funding_rates,
)
from hydra.data.migrations.runner import upgrade_to_head

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, 0, tzinfo=UTC)
H8 = timedelta(hours=8)

ROWS = [
    FundingRate(symbol="BTCUSDT", rate=Decimal("0.00010000"), timestamp=T0),
    FundingRate(symbol="BTCUSDT", rate=Decimal("-0.00005000"), timestamp=T0 + H8),
    FundingRate(symbol="BTCUSDT", rate=Decimal("0.00020000"), timestamp=T0 + 2 * H8),
]


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


async def _stored(pool: Any, symbol: str = "BTCUSDT") -> list[tuple[Decimal, datetime]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT rate, timestamp FROM ts.funding_rates "
            "WHERE exchange = 'binanceusdm' AND symbol = $1 ORDER BY timestamp",
            symbol,
        )
    return [(row["rate"], row["timestamp"]) for row in rows]


class _FakeClient:
    """A ccxt-shaped fake history (no egress): five settlements, eight hours apart."""

    def __init__(self, first: datetime) -> None:
        self.first = first
        self.calls: list[dict[str, Any]] = []

    async def fetch_funding_rate_history(
        self,
        symbol: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        until = (params or {}).get("until")
        self.calls.append({"symbol": symbol, "since": since, "until": until})
        rates = ("0.0001", "-0.00005", "0.0002", "0.0001", "0.0003")
        entries = []
        for index, rate in enumerate(rates):
            moment = self.first + index * H8
            ms = int(moment.timestamp() * 1000)
            if (since is not None and ms < since) or (until is not None and ms > until):
                continue
            entries.append(
                {
                    "info": {"symbol": symbol, "fundingRate": rate, "fundingTime": ms},
                    "symbol": "BTC/USDT:USDT",
                    "fundingRate": float(rate),
                    "timestamp": ms,
                }
            )
        return entries[: limit or len(entries)]


class TestUpsert:
    async def test_three_rows_written_exactly_and_a_refetch_inserts_nothing(
        self, pool: Any
    ) -> None:
        assert await upsert_funding_rates(pool, ROWS) == 3
        assert await _stored(pool) == [
            (Decimal("0.00010000"), T0),
            (Decimal("-0.00005000"), T0 + H8),
            (Decimal("0.00020000"), T0 + 2 * H8),
        ]
        assert await upsert_funding_rates(pool, ROWS) == 0
        assert len(await _stored(pool)) == 3

    async def test_only_the_missing_row_of_a_batch_inserts(self, pool: Any) -> None:
        await upsert_funding_rates(pool, ROWS)
        batch = [*ROWS, FundingRate("BTCUSDT", Decimal("0.00015"), T0 + 3 * H8)]
        assert await upsert_funding_rates(pool, batch) == 1
        assert [rate for rate, _ in await _stored(pool)] == [
            Decimal("0.00010000"),
            Decimal("-0.00005000"),
            Decimal("0.00020000"),
            Decimal("0.00015"),
        ]

    async def test_two_concurrent_writers_never_double_insert(self, pool: Any) -> None:
        inserted = await asyncio.gather(
            upsert_funding_rates(pool, ROWS), upsert_funding_rates(pool, ROWS)
        )
        assert sorted(inserted) == [0, 3]
        assert len(await _stored(pool)) == 3

    async def test_a_mixed_symbol_batch_locks_and_writes_every_symbol(self, pool: Any) -> None:
        batch = [
            FundingRate("ETHUSDT", Decimal("0.00010000"), T0),
            FundingRate("BTCUSDT", Decimal("-0.00005000"), T0),
            FundingRate("BTCUSDT", Decimal("0.00020000"), T0 + H8),
        ]
        assert await upsert_funding_rates(pool, batch) == 3
        assert await _stored(pool, "BTCUSDT") == [
            (Decimal("-0.00005000"), T0),
            (Decimal("0.00020000"), T0 + H8),
        ]
        assert await _stored(pool, "ETHUSDT") == [(Decimal("0.00010000"), T0)]

    async def test_two_concurrent_mixed_writers_never_double_insert(self, pool: Any) -> None:
        batch = [
            FundingRate("BTCUSDT", Decimal("0.00010000"), T0),
            FundingRate("ETHUSDT", Decimal("-0.00005000"), T0),
        ]
        inserted = await asyncio.gather(
            upsert_funding_rates(pool, batch), upsert_funding_rates(pool, batch)
        )
        assert sorted(inserted) == [0, 2]
        assert len(await _stored(pool, "BTCUSDT")) == 1
        assert len(await _stored(pool, "ETHUSDT")) == 1

    async def test_the_newest_timestamp_is_the_resume_cursor(self, pool: Any) -> None:
        assert await newest_funding_timestamp(pool, "BTCUSDT") is None
        await upsert_funding_rates(pool, ROWS[:2])
        assert await newest_funding_timestamp(pool, "BTCUSDT") == T0 + H8
        assert await newest_funding_timestamp(pool, "ETHUSDT") is None


class TestEnsure:
    async def test_a_backfill_writes_every_settlement_of_the_range(self, pool: Any) -> None:
        client = _FakeClient(T0)
        report = await ensure_funding_rates(pool, client, "BTCUSDT", start=T0, end=T0 + 4 * H8)
        assert report.rows_fetched == 5
        assert report.rows_inserted == 5
        assert report.weight_spent == 1
        assert len(await _stored(pool)) == 5
        first = await _stored(pool)
        assert first[0] == (Decimal("0.00010000"), T0)

    async def test_a_rerun_of_the_same_range_is_idempotent(self, pool: Any) -> None:
        client = _FakeClient(T0)
        await ensure_funding_rates(pool, client, "BTCUSDT", start=T0, end=T0 + 4 * H8)
        report = await ensure_funding_rates(pool, client, "BTCUSDT", start=T0, end=T0 + 4 * H8)
        assert report.rows_fetched == 5
        assert report.rows_inserted == 0
        assert len(await _stored(pool)) == 5

    async def test_the_top_up_resumes_after_the_newest_stored_row(self, pool: Any) -> None:
        await upsert_funding_rates(pool, ROWS[:2])
        client = _FakeClient(T0)
        report = await ensure_funding_rates(pool, client, "BTCUSDT", now=T0 + 4 * H8)
        assert report == FundingWriteReport(rows_fetched=3, rows_inserted=3, weight_spent=1)
        resume = T0 + H8 + timedelta(milliseconds=1)
        assert client.calls[0]["since"] == int(resume.timestamp() * 1000)
        assert len(await _stored(pool)) == 5


class TestRunningPerpSymbols:
    async def test_only_running_binanceusdm_sessions_are_the_worklist(self, pool: Any) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO trading_sessions "
                "(id, strategy_id, trading_mode, status, exchange_id, symbols, timeframe) "
                "VALUES ('perp-1', 'sma', 'paper', 'running', 'binanceusdm', "
                "ARRAY['ETHUSDT','BTCUSDT']::text[], '1m'), "
                "('perp-2', 'sma', 'paper', 'stopped', 'binanceusdm', "
                "ARRAY['SOLUSDT']::text[], '1m'), "
                "('spot-1', 'sma', 'paper', 'running', 'binance', "
                "ARRAY['DOGEUSDT']::text[], '1m')"
            )
        assert await running_perp_symbols(pool) == ["BTCUSDT", "ETHUSDT"]
