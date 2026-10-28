"""The offline universe: fixed entries so no lifespan-booting test needs Binance egress.

``offline_refresher_factory`` is installed on ``app.state.universe_refresher_factory``
(the ``health_monitor_factory`` pattern) by every test that runs the real lifespan with
a real pool. It builds a real ``UniverseRefresher`` whose refresh persists these fixed
entries through the real service, so the refresher lifecycle, the SQL upsert and the
read surfaces run end to end with zero exchange egress.
"""

from __future__ import annotations

from typing import Any

from hydra.data.universe import UniverseEntry, UniverseRefresher

OFFLINE_UNIVERSE: tuple[UniverseEntry, ...] = (
    UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=1_250_000_000.0),
    UniverseEntry(rank=2, symbol="ETHUSDT", quote_volume=640_000_000.0),
    UniverseEntry(rank=3, symbol="SOLUSDT", quote_volume=210_000_000.0),
)


async def offline_compute() -> list[UniverseEntry]:
    """The fixed ranking every offline refresher persists."""
    return list(OFFLINE_UNIVERSE)


def offline_refresher_factory(app: Any) -> UniverseRefresher:
    """A real refresher around the offline compute — no Binance egress."""
    from hydra.services.universe import refresh_universe

    async def refresh() -> None:
        await refresh_universe(getattr(app.state, "db_pool", None), offline_compute)

    return UniverseRefresher(refresh)
