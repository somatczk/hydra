"""Market data endpoints: funding rates, order book depth."""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/market", tags=["market"])
logger = logging.getLogger(__name__)


def _pool_from_request(request: Request) -> Any:
    return getattr(request.app.state, "db_pool", None)


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


class OrderBookLevel(BaseModel):
    price: float
    quantity: float


class OrderBookSnapshot(BaseModel):
    symbol: str
    exchange: str
    bids: list[list[float]]  # [[price, qty], ...]
    asks: list[list[float]]  # [[price, qty], ...]
    spread: float
    timestamp: str
    mock: bool = False


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
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
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
    except Exception:
        logger.exception("Failed to fetch funding rates")
        return []


@router.get("/funding-rates/current", response_model=list[CurrentFundingRate])
async def get_current_funding_rates(
    request: Request,
    symbol: str = "BTCUSDT",
) -> list[dict[str, Any]]:
    """Latest funding rate for a symbol across all exchanges."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
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
    except Exception:
        logger.exception("Failed to fetch current funding rates")
        return []


# ---------------------------------------------------------------------------
# Order book helpers + REST snapshot endpoint
# ---------------------------------------------------------------------------

_MOCK_MID_PRICES: dict[str, float] = {
    "BTCUSDT": 65000.0,
    "ETHUSDT": 3500.0,
    "SOLUSDT": 150.0,
}
_MOCK_TICK_SIZES: dict[str, float] = {
    "BTCUSDT": 0.1,
    "ETHUSDT": 0.01,
    "SOLUSDT": 0.001,
}


def _build_mock_order_book(symbol: str, depth: int = 20) -> dict[str, Any]:
    """Generate a plausible mock order book when no live feed is available."""
    mid = _MOCK_MID_PRICES.get(symbol, 1000.0)
    tick = _MOCK_TICK_SIZES.get(symbol, 0.01)

    bids: list[list[float]] = []
    asks: list[list[float]] = []

    for i in range(depth):
        bid_price = round(mid - tick * (i + 1), 8)
        ask_price = round(mid + tick * (i + 1), 8)
        bid_qty = round(random.uniform(0.01, 2.0), 4)  # noqa: S311  # nosec B311
        ask_qty = round(random.uniform(0.01, 2.0), 4)  # noqa: S311  # nosec B311
        bids.append([bid_price, bid_qty])
        asks.append([ask_price, ask_qty])

    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    spread = round(best_ask - best_bid, 8)

    return {
        "symbol": symbol,
        "exchange": "mock",
        "bids": bids,
        "asks": asks,
        "spread": spread,
        "timestamp": datetime.now(UTC).isoformat(),
        "mock": True,
    }


async def _fetch_live_order_book(
    symbol: str, exchange: str, depth: int = 20
) -> dict[str, Any] | None:
    """Attempt to fetch a live order book snapshot via CCXT.

    Returns ``None`` if CCXT is unavailable or the call fails.
    """
    try:
        import ccxt.async_support as ccxt

        exchange_cls = getattr(ccxt, exchange, None)
        if exchange_cls is None:
            return None

        ex = exchange_cls({"enableRateLimit": True})
        try:
            ob = await ex.fetch_order_book(symbol, limit=depth)
            bids = [[float(p), float(q)] for p, q in ob.get("bids", [])[:depth]]
            asks = [[float(p), float(q)] for p, q in ob.get("asks", [])[:depth]]
            best_bid = bids[0][0] if bids else 0.0
            best_ask = asks[0][0] if asks else 0.0
            return {
                "symbol": symbol,
                "exchange": exchange,
                "bids": bids,
                "asks": asks,
                "spread": round(best_ask - best_bid, 8),
                "timestamp": datetime.now(UTC).isoformat(),
                "mock": False,
            }
        finally:
            await ex.close()
    except Exception:
        return None


@router.get("/orderbook", response_model=OrderBookSnapshot)
async def get_order_book(
    symbol: str = "BTCUSDT",
    exchange: str = "binance",
    depth: int = 20,
) -> dict[str, Any]:
    """Order book snapshot (top N bid/ask levels).

    Tries a live CCXT fetch first; falls back to mock data with ``mock: true``
    so the frontend always has something to render on initial load before the
    WebSocket connects.
    """
    live = await _fetch_live_order_book(symbol, exchange, depth=depth)
    if live is not None:
        return live
    logger.debug(
        "Order book live feed unavailable for %s/%s — returning mock data", symbol, exchange
    )
    return _build_mock_order_book(symbol, depth=depth)


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
