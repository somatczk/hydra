"""Market data endpoints: funding rates, order book depth, the top-100 universe and
the auto-fetched data inventory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ccxt.base.errors import BadSymbol, BaseError
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from prometheus_client import REGISTRY
from pydantic import BaseModel

from hydra.core.config import DataConfig
from hydra.core.venues import registered_venue_ids
from hydra.dashboard.routes._db import (
    ServiceErrorRoute,
    api_error,
    db_errors_to_503,
    require_pool,
)
from hydra.services import data as data_svc
from hydra.services import universe as universe_svc

router = APIRouter(prefix="/api/market", tags=["market"], route_class=ServiceErrorRoute)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FundingRatePoint(BaseModel):
    timestamp: str
    rate: float
    annualized_rate: float


class CurrentFundingRate(BaseModel):
    exchange: str
    symbol: str
    rate: float
    annualized_rate: float
    next_funding_time: str | None = None


class OrderBookSnapshot(BaseModel):
    symbol: str
    exchange: str
    bids: list[list[float]]  # [[price, qty], ...]
    asks: list[list[float]]  # [[price, qty], ...]
    spread: float
    timestamp: str | None  # exchange time; None when the exchange does not report one
    fetched_at: str


class UniverseMember(BaseModel):
    rank: int
    symbol: str  # the exchange market id, no slash (BTCUSDT)
    quote_volume: float


class UniverseSnapshot(BaseModel):
    """The stored top-100 USDT-quoted universe, with the ranking's provenance and age."""

    exchange_id: str
    quote_asset: str
    source: str
    ranked_at: str
    age_hours: float
    count: int
    symbols: list[UniverseMember]


# ---------------------------------------------------------------------------
# Funding rate endpoints
# ---------------------------------------------------------------------------


@router.get("/funding-rates", response_model=list[FundingRatePoint])
async def get_funding_rates(
    request: Request,
    symbol: str = "BTCUSDT",
    exchange: str = "binance",
    days: int = 30,
) -> list[dict[str, Any]]:
    """Historical funding rates for a symbol (from ts.funding_rates)."""
    pool = require_pool(request)
    async with db_errors_to_503("reading funding rates"), pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, rate FROM ts.funding_rates "
            "WHERE exchange = $1 AND symbol = $2 "
            "AND timestamp >= now() - make_interval(days => $3) "
            "ORDER BY timestamp",
            exchange,
            symbol,
            days,
        )
    return [
        {
            "timestamp": row["timestamp"].isoformat(),
            "rate": float(row["rate"]),
            "annualized_rate": round(float(row["rate"]) * 3 * 365 * 100, 4),
        }
        for row in rows
    ]


@router.get("/funding-rates/current", response_model=list[CurrentFundingRate])
async def get_current_funding_rates(
    request: Request,
    symbol: str = "BTCUSDT",
) -> list[dict[str, Any]]:
    """Latest funding rate for a symbol across all exchanges."""
    pool = require_pool(request)
    async with db_errors_to_503("reading current funding rates"), pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (exchange) "
            "exchange, symbol, rate, next_funding_time, timestamp "
            "FROM ts.funding_rates "
            "WHERE symbol = $1 "
            "ORDER BY exchange, timestamp DESC",
            symbol,
        )
    return [
        {
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "rate": float(row["rate"]),
            "annualized_rate": round(float(row["rate"]) * 3 * 365 * 100, 4),
            "next_funding_time": (
                row["next_funding_time"].isoformat() if row["next_funding_time"] else None
            ),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Universe: the stored top-100 ranking (refreshed daily by the lifespan)
# ---------------------------------------------------------------------------


@router.get("/universe", response_model=UniverseSnapshot)
async def get_universe(request: Request) -> dict[str, Any]:
    """The stored top-100 universe: USDT-quoted Binance spot pairs by 24h quote volume.

    Serves the stored ranking however stale; before the first refresh has landed it is
    a 503 ``universe_unavailable`` — never a fabricated or partial list. No exchange
    call: the refresher owns refreshing, this reads the stored truth.
    """
    return await universe_svc.read_universe(require_pool(request))


# ---------------------------------------------------------------------------
# Data inventory: the auto-fetched bar cache the sweeper bounds (the data_inventory
# MCP tool's reads over HTTP, for the dashboard's Data page)
# ---------------------------------------------------------------------------


class CoverageRange(BaseModel):
    """The fetch grid a market was asked to fill — never an "auto-cached since" bound."""

    first_ts: str
    last_ts: str


class CacheDataset(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    coverage: CoverageRange
    rows: int
    estimated_bytes: int
    status: str
    last_used_at: str


class CacheTotals(BaseModel):
    bytes: int
    rows: int
    pairs: int
    budget_bytes: int
    last_sweep: str | None = None


class DataInventory(BaseModel):
    """Every auto-cached dataset least-recently-used first, plus the totals.

    ``sweep_interval_s`` is the configured cycle the sweep-stall threshold reads
    (two intervals); the LRU order is the sweeper's eviction order, so the
    eviction candidates lead.
    """

    datasets: list[CacheDataset]
    totals: CacheTotals
    sweep_interval_s: int


def _data_config(request: Request) -> DataConfig:
    """The bound ``data`` section; the section's defaults while the state is unbound."""
    cfg = getattr(request.app.state, "config", None)
    return cfg.data if cfg is not None else DataConfig()


@router.get("/data-inventory", response_model=DataInventory)
async def get_data_inventory(request: Request) -> dict[str, Any]:
    """The auto-fetched bar cache: every dataset least-recently-used first (the
    sweeper's eviction candidates lead) with its requested coverage range, bar
    rows, estimated bytes (rows x ``data.row_bytes_estimate``), fetch status and
    last use, plus the totals — the whole cache's rows and estimated bytes
    against the ``data.cache_max_gb`` budget and the last completed sweep —
    mirroring the ``data_inventory`` MCP tool. 503 when the process has no pool
    or the database cannot answer.
    """
    config = _data_config(request)
    sweep_ts = REGISTRY.get_sample_value("hydra_data_sweep_last_timestamp_seconds")
    inventory = await data_svc.cache_inventory(require_pool(request), config, sweep_ts)
    return {**inventory, "sweep_interval_s": config.sweep_interval_s}


# ---------------------------------------------------------------------------
# Order book: one cached public CCXT client per exchange, stored on app.state
# ---------------------------------------------------------------------------

_ALLOWED_EXCHANGES = registered_venue_ids()
"""The registry's venue ids — bybit spot joins the order book (P7-F); the pinned test
keeps this set equal to ``client_factory.ALLOWED_VENUES``'s ids."""


def _default_exchange_factory(exchange_id: str) -> Any:
    from hydra.execution.client_factory import rest_exchange_factory

    return rest_exchange_factory(exchange_id)


_exchange_factory: Callable[[str], Any] = _default_exchange_factory


def _client_state(app: FastAPI) -> tuple[dict[str, Any], asyncio.Lock]:
    clients: dict[str, Any] | None = getattr(app.state, "market_data_clients", None)
    if clients is None:
        clients = {}
        app.state.market_data_clients = clients
    lock: asyncio.Lock | None = getattr(app.state, "market_data_clients_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.market_data_clients_lock = lock
    return clients, lock


async def _get_market_client(app: FastAPI, exchange_id: str) -> Any:
    clients, lock = _client_state(app)
    async with lock:
        client = clients.get(exchange_id)
        if client is None:
            client = _exchange_factory(exchange_id)
            clients[exchange_id] = client
        return client


async def _evict_market_client(app: FastAPI, exchange_id: str, client: Any) -> None:
    """Drop and close the client that failed; a newer cached replacement is left alone."""
    clients, lock = _client_state(app)
    async with lock:
        if clients.get(exchange_id) is client:
            clients.pop(exchange_id)
    try:
        await client.close()
    except Exception:
        logger.debug("Failed to close market data client for %s", exchange_id, exc_info=True)


async def close_market_clients(app: FastAPI) -> None:
    """Close every cached market data client (called on app shutdown)."""
    clients: dict[str, Any] | None = getattr(app.state, "market_data_clients", None)
    if not clients:
        return
    for exchange_id in list(clients):
        client = clients.pop(exchange_id)
        try:
            await client.close()
        except Exception:
            logger.warning("Failed to close market data client for %s", exchange_id, exc_info=True)


def _exchange_unavailable(exchange: str) -> HTTPException:
    return api_error(
        503,
        "exchange_unavailable",
        f"Order book from {exchange} is unavailable",
        "unavailable",
        exchange=exchange,
    )


@router.get("/orderbook", response_model=OrderBookSnapshot)
async def get_order_book(
    request: Request,
    symbol: str = "BTCUSDT",
    exchange: str = Query("binance"),
    depth: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Order book snapshot (top N bid/ask levels) from the exchange, never synthesised."""
    if exchange not in _ALLOWED_EXCHANGES:
        raise api_error(
            422,
            "exchange_not_allowed",
            f"Order book is only available for: {', '.join(sorted(_ALLOWED_EXCHANGES))}",
            "invalid",
            allowed=sorted(_ALLOWED_EXCHANGES),
        )

    app = request.app
    client = await _get_market_client(app, exchange)
    try:
        ob = await client.fetch_order_book(symbol, limit=depth)
    except BadSymbol as exc:
        raise api_error(
            422, "unknown_symbol", f"Unknown symbol {symbol} on {exchange}", "invalid"
        ) from exc
    except (BaseError, OSError, TimeoutError) as exc:
        logger.warning("Order book fetch failed for %s/%s", exchange, symbol, exc_info=True)
        await _evict_market_client(app, exchange, client)
        raise _exchange_unavailable(exchange) from exc

    bids = [[float(p), float(q)] for p, q, *_ in ob.get("bids", [])[:depth]]
    asks = [[float(p), float(q)] for p, q, *_ in ob.get("asks", [])[:depth]]
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    ts_ms = ob.get("timestamp")
    exchange_ts = (
        datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()
        if isinstance(ts_ms, int | float)
        else None
    )
    return {
        "symbol": symbol,
        "exchange": exchange,
        "bids": bids,
        "asks": asks,
        "spread": round(best_ask - best_bid, 8),
        "timestamp": exchange_ts,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Sentiment & News
# ---------------------------------------------------------------------------


class SentimentPoint(BaseModel):
    timestamp: str
    value: int
    classification: str


class NewsItem(BaseModel):
    title: str
    source: str
    url: str
    published_at: str


@router.get("/sentiment", response_model=list[SentimentPoint])
async def get_sentiment(limit: int = 30) -> list[dict[str, Any]]:
    """Bitcoin Fear & Greed Index (free API, no key required)."""
    from hydra.data.sentiment import fetch_fear_greed

    return await fetch_fear_greed(limit=limit)


@router.get("/news", response_model=list[NewsItem])
async def get_news(limit: int = 20) -> list[dict[str, Any]]:
    """Latest crypto news headlines."""
    from hydra.data.news import fetch_crypto_news

    return await fetch_crypto_news(limit=limit)
