"""scripts/download_history.py against a real TimescaleDB; only the Binance REST client is faked.

Needs HYDRA_DB_* (see tests/infra.py): gap detection and persistence run for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from scripts.download_history import _download, _parse_args

from hydra.core.types import OHLCV, Timeframe
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository
from hydra.execution import client_factory
from tests.fixtures.binance_kline import FakeRestExchange

pytestmark = pytest.mark.integration

G0 = datetime(2026, 2, 2, tzinfo=UTC)
H = timedelta(hours=1)
ARGS = ["--timeframe", "1h", "--start", "2026-02-02", "--end", "2026-02-03", "--dsn"]


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


@pytest.fixture()
async def seeded_dsn(fresh_database: str) -> AsyncIterator[str]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    repo = MarketDataRepository(fresh_database, min_pool_size=1, max_pool_size=1)
    await repo.connect()
    one = Decimal(1)
    await repo.store_bars(
        [
            ("binance", "BTCUSDT", Timeframe.H1, OHLCV(one, one, one, one, one, G0 + h * H))
            for h in (2, 6, 7)
        ]
    )
    await repo.close()
    yield fresh_database


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRestExchange:
    rows = [[_ms(G0 + h * H), 100.0, 101.0, 99.0, 100.5, 3.0] for h in range(25)]
    exchange = FakeRestExchange(rows)
    monkeypatch.setattr(client_factory, "rest_exchange_factory", lambda _eid: exchange)
    return exchange


async def _count(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        n: int = await conn.fetchval(
            "SELECT count(*) FROM ts.ohlcv_1m"
            " WHERE exchange = 'binance' AND symbol = 'BTCUSDT' AND timeframe = '1h'"
        )
        return n
    finally:
        await conn.close()


async def test_download_history_fills_every_hole(seeded_dsn: str, fake: FakeRestExchange) -> None:
    assert await _download(_parse_args([*ARGS, seeded_dsn])) == 22
    assert await _count(seeded_dsn) == 25
    assert [since for _, _, since, _ in fake.calls] == [
        _ms(G0),
        _ms(G0 + 3 * H),
        _ms(G0 + 8 * H),
    ]


async def test_download_history_rerun_is_noop(seeded_dsn: str, fake: FakeRestExchange) -> None:
    await _download(_parse_args([*ARGS, seeded_dsn]))
    calls = len(fake.calls)
    assert await _download(_parse_args([*ARGS, seeded_dsn])) == 0
    assert len(fake.calls) == calls
    assert await _count(seeded_dsn) == 25


async def test_download_history_dry_run(
    seeded_dsn: str, fake: FakeRestExchange, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _download(_parse_args([*ARGS, seeded_dsn, "--dry-run"])) == 0
    out = capsys.readouterr().out
    assert "missing 2026-02-02T00:00:00+00:00 -> 2026-02-02T01:00:00+00:00 (2 bars)" in out
    assert "missing 2026-02-02T03:00:00+00:00 -> 2026-02-02T05:00:00+00:00 (3 bars)" in out
    assert "missing 2026-02-02T08:00:00+00:00 -> 2026-02-03T00:00:00+00:00 (17 bars)" in out
    assert fake.calls == []
    assert await _count(seeded_dsn) == 3
