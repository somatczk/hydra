"""Market data endpoints: funding rates, order book depth."""

from __future__ import annotations

import logging
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
