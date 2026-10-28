"""The persisted top-100 universe: one ``system_config`` row, read, refreshed, validated.

The row key is ``universe``; the value carries the full provenance — the exchange, the
quote asset, the ranking source, ``ranked_at``, the count and the ranked symbols with
rank and rolling-24h quote volume. ``read_universe`` serves the stored truth however
stale (stale-OK reads) and is a 503 on every surface before the first refresh lands —
never a fabricated or partial list. ``validate_symbol`` is the write-time membership
gate (enforced at the callers from P6-A2) with the MarketRules slash-stripping
normalization; with no stored row it fails closed with the same 503.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from hydra.data.universe import (
    EXCHANGE_ID,
    QUOTE_ASSET,
    SOURCE,
    UniverseEntry,
    UniverseRefresher,
    compute_universe,
)
from hydra.execution.client_factory import SharedRestClients
from hydra.services.errors import db_errors, require_pool, service_error

logger = logging.getLogger(__name__)

UNIVERSE_KEY = "universe"
UPDATED_BY = "system:universe"

type ComputeUniverse = Callable[[], Awaitable[list[UniverseEntry]]]

_UPSERT_UNIVERSE = (
    "INSERT INTO system_config (key, value, updated_at, updated_by) "
    "VALUES ($1, $2::jsonb, now(), $3) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by"
)


def _stored_value(row: Any) -> dict[str, Any]:
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)
    return cast(dict[str, Any], value)


async def refresh_universe(pool: Any, compute: ComputeUniverse) -> dict[str, Any]:
    """Compute a fresh ranking via *compute* and upsert it as the ``universe`` row."""
    pool = require_pool(pool)
    entries = await compute()
    ranked_at = datetime.now(UTC).isoformat()
    value: dict[str, Any] = {
        "exchange_id": EXCHANGE_ID,
        "quote_asset": QUOTE_ASSET,
        "source": SOURCE,
        "ranked_at": ranked_at,
        "count": len(entries),
        "symbols": [
            {"rank": entry.rank, "symbol": entry.symbol, "quote_volume": entry.quote_volume}
            for entry in entries
        ],
    }
    async with db_errors("refreshing the universe"), pool.acquire() as conn:
        await conn.execute(_UPSERT_UNIVERSE, UNIVERSE_KEY, json.dumps(value), UPDATED_BY)
    logger.info("Universe refreshed: %d symbols ranked at %s", len(entries), ranked_at)
    return value


async def read_universe(pool: Any) -> dict[str, Any]:
    """The stored universe plus its ``age_hours`` in hours since ``ranked_at``.

    503 ``universe_unavailable`` when the row is missing (before the first refresh) or
    the process has no pool; the ranking is served as stored however stale.
    """
    pool = require_pool(pool)
    async with db_errors("reading the universe"), pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM system_config WHERE key = $1", UNIVERSE_KEY)
    if row is None:
        raise service_error(
            503,
            "universe_unavailable",
            "The top-100 universe has not been computed yet",
            "unavailable",
        )
    value = _stored_value(row)
    ranked_at = datetime.fromisoformat(str(value["ranked_at"]))
    age_hours = (datetime.now(UTC) - ranked_at).total_seconds() / 3600
    return {
        "exchange_id": value["exchange_id"],
        "quote_asset": value["quote_asset"],
        "source": value["source"],
        "ranked_at": value["ranked_at"],
        "age_hours": round(age_hours, 2),
        "count": value["count"],
        "symbols": value["symbols"],
    }


async def validate_symbol(pool: Any, symbol: str) -> None:
    """The write-time membership gate: return iff *symbol* is a current member.

    The comparison normalizes like ``MarketRulesCache`` (``BTC/USDT`` → ``BTCUSDT``).
    A non-member is a 422 ``symbol_not_in_universe`` naming the ranking's ``ranked_at``
    (write-time only: running sessions and stored strategies are unaffected); no stored
    row fails closed with the 503 above — nothing is writable without a universe.
    """
    normalized = symbol.replace("/", "").upper()
    data = await read_universe(pool)
    members = {str(entry["symbol"]) for entry in data["symbols"]}
    if normalized not in members:
        raise service_error(
            422,
            "symbol_not_in_universe",
            f"{symbol} is not in the current top-100 universe "
            f"(ranked at {data['ranked_at']}); running sessions and stored "
            "strategies are unaffected",
            "invalid",
            symbol=normalized,
            universe_ranked_at=data["ranked_at"],
            universe_count=data["count"],
        )


def default_compute(rest_pool: SharedRestClients | None = None) -> ComputeUniverse:
    """The production compute: one pooled public Binance client per refresh.

    The client comes from the shared refcounted REST pool (the ``markets_loader``
    pattern), so each refresh rides one handle — acquired, used for the single
    ``load_markets`` + ``fetch_tickers`` pair, released (and closed, as the last
    reference) — and never a bespoke client.
    """
    pool = SharedRestClients() if rest_pool is None else rest_pool

    async def _compute() -> list[UniverseEntry]:
        handle = await pool.handle(EXCHANGE_ID)
        try:
            return await compute_universe(handle.client)
        finally:
            await handle.release()

    return _compute


def default_refresher(pool: Any) -> UniverseRefresher:
    """The production refresher the lifespan builds: pooled compute → the stored row."""
    compute = default_compute()

    async def refresh() -> None:
        await refresh_universe(pool, compute)

    return UniverseRefresher(refresh)
