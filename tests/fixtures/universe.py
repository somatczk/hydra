"""The offline universe: fixed entries so no lifespan-booting test needs Binance egress.

``offline_refresher_factory`` is installed on ``app.state.universe_refresher_factory``
(the ``health_monitor_factory`` pattern) by every test that runs the real lifespan with
a real pool. It builds a real ``UniverseRefresher`` whose refresh persists these fixed
entries through the real service, so the refresher lifecycle, the SQL upsert and the
read surfaces run end to end with zero exchange egress.

``seeded_universe_pool`` is the unit-test twin: a pool stub that answers only the
stored-universe read the write gate (P6-A2) performs, for tests that exercise the
builder's save/update and ``default_pair`` paths without a database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hydra.data.universe import (
    EXCHANGE_ID,
    QUOTE_ASSET,
    SOURCE,
    UniverseEntry,
    UniverseRefresher,
)

OFFLINE_UNIVERSE: tuple[UniverseEntry, ...] = (
    UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=1_250_000_000.0),
    UniverseEntry(rank=2, symbol="ETHUSDT", quote_volume=640_000_000.0),
    UniverseEntry(rank=3, symbol="SOLUSDT", quote_volume=210_000_000.0),
)

OFFLINE_RANKED_AT = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
"""A fixed ranking timestamp so the 422 envelope's ``ranked_at`` is assertable."""


async def offline_compute() -> list[UniverseEntry]:
    """The fixed ranking every offline refresher persists."""
    return list(OFFLINE_UNIVERSE)


def offline_universe_value(ranked_at: datetime = OFFLINE_RANKED_AT) -> dict[str, Any]:
    """The ``system_config`` ``universe`` row value the offline ranking persists."""
    return {
        "exchange_id": EXCHANGE_ID,
        "quote_asset": QUOTE_ASSET,
        "source": SOURCE,
        "ranked_at": ranked_at.isoformat(),
        "count": len(OFFLINE_UNIVERSE),
        "symbols": [
            {"rank": entry.rank, "symbol": entry.symbol, "quote_volume": entry.quote_volume}
            for entry in OFFLINE_UNIVERSE
        ],
    }


class _UniverseConn:
    """Serves the stored ``universe`` row; every other read answers empty."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM system_config" in query
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []


class UniversePool:
    """A pool answering only the write gate's stored-universe read (the DB boundary)."""

    def __init__(self, conn: _UniverseConn) -> None:
        self.conn = conn

    def acquire(self) -> UniversePool:
        return self

    async def __aenter__(self) -> _UniverseConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def seeded_universe_pool(ranked_at: datetime = OFFLINE_RANKED_AT) -> UniversePool:
    """A pool serving the offline ranking as the stored ``universe`` row."""
    row = {"value": json.dumps(offline_universe_value(ranked_at))}
    return UniversePool(_UniverseConn(row))


def offline_refresher_factory(app: Any) -> UniverseRefresher:
    """A real refresher around the offline compute — no Binance egress."""
    from hydra.services.universe import refresh_universe

    async def refresh() -> None:
        await refresh_universe(getattr(app.state, "db_pool", None), offline_compute)

    return UniverseRefresher(refresh)
