from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Known base currencies ordered longest-first so "BETH" is not split as "B/ETHUSDT".
_BASE_CURRENCIES = sorted(
    ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "DOT", "AVAX", "LINK"],
    key=len,
    reverse=True,
)
_PAIR_RE = re.compile(rf"^({'|'.join(_BASE_CURRENCIES)})(\w+)$")


def _format_pair(symbol: str) -> str:
    """Convert raw symbol (e.g. ``BTCUSDT``) to pair format (``BTC/USDT``)."""
    m = _PAIR_RE.match(symbol)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # Fallback: insert slash at position 3 (BTC-length) -- best effort
    return f"{symbol[:3]}/{symbol[3:]}" if len(symbol) > 3 else symbol


def _pool_from_request(request: Request) -> object | None:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class PortfolioSummary(BaseModel):
    total_value: float = 12450.00
    unrealized_pnl: float = 198.00
    realized_pnl: float = 1842.30
    total_fees: float = 87.50
    change_pct: float = 2.4


class Position(BaseModel):
    id: str
    pair: str
    exchange: str
    side: str
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    pnl_pct: float


class EquityPoint(BaseModel):
    timestamp: str
    value: float


class DailyPnl(BaseModel):
    date: str
    pnl: float


class MonthlyReturn(BaseModel):
    month: str
    return_pct: float


class AttributionItem(BaseModel):
    strategy: str
    pnl: float
    pct_of_total: float


class TradeRecord(BaseModel):
    id: int
    pair: str
    side: str
    price: float
    size: float
    fee: float
    pnl: float
    timestamp: str


# ---------------------------------------------------------------------------
# Placeholder data
# ---------------------------------------------------------------------------

_POSITIONS: list[dict] = [
    {
        "id": "pos-1",
        "pair": "BTC/USDT",
        "exchange": "binance",
        "side": "Long",
        "size": 0.15,
        "entry_price": 67420.0,
        "current_price": 68100.0,
        "unrealized_pnl": 102.0,
        "pnl_pct": 1.01,
    },
    {
        "id": "pos-2",
        "pair": "BTC/USDT",
        "exchange": "bybit",
        "side": "Short",
        "size": 0.08,
        "entry_price": 68800.0,
        "current_price": 68100.0,
        "unrealized_pnl": 56.0,
        "pnl_pct": 1.02,
    },
    {
        "id": "pos-3",
        "pair": "BTC/USDT",
        "exchange": "binance",
        "side": "Long",
        "size": 0.20,
        "entry_price": 67900.0,
        "current_price": 68100.0,
        "unrealized_pnl": 40.0,
        "pnl_pct": 0.29,
    },
]

_EQUITY_CURVE: list[dict] = [
    {"timestamp": "2026-03-01T00:00:00Z", "value": 10000.0},
    {"timestamp": "2026-03-03T00:00:00Z", "value": 10250.0},
    {"timestamp": "2026-03-05T00:00:00Z", "value": 10480.0},
    {"timestamp": "2026-03-07T00:00:00Z", "value": 10320.0},
    {"timestamp": "2026-03-09T00:00:00Z", "value": 10890.0},
    {"timestamp": "2026-03-11T00:00:00Z", "value": 11450.0},
    {"timestamp": "2026-03-13T00:00:00Z", "value": 12100.0},
    {"timestamp": "2026-03-14T00:00:00Z", "value": 12450.0},
]

_DAILY_PNL: list[dict] = [
    {"date": "2026-03-08", "pnl": 120.0},
    {"date": "2026-03-09", "pnl": -45.0},
    {"date": "2026-03-10", "pnl": 310.0},
    {"date": "2026-03-11", "pnl": 180.0},
    {"date": "2026-03-12", "pnl": -60.0},
    {"date": "2026-03-13", "pnl": 210.0},
    {"date": "2026-03-14", "pnl": 285.50},
]

_MONTHLY_RETURNS: list[dict] = [
    {"month": "2026-01", "return_pct": 5.2},
    {"month": "2026-02", "return_pct": 8.1},
    {"month": "2026-03", "return_pct": 2.4},
]

_ATTRIBUTION: list[dict] = [
    {"strategy": "LSTM Momentum", "pnl": 1240.50, "pct_of_total": 52.3},
    {"strategy": "Mean Reversion RSI", "pnl": 580.20, "pct_of_total": 24.5},
    {"strategy": "Breakout Scanner", "pnl": -120.0, "pct_of_total": -5.1},
    {"strategy": "Manual Trades", "pnl": 141.60, "pct_of_total": 6.0},
]

_TRADES_PLACEHOLDER: list[dict] = [
    {
        "id": 1,
        "pair": "BTC/USDT",
        "side": "BUY",
        "price": 67420.0,
        "size": 0.15,
        "fee": 4.04,
        "pnl": 102.0,
        "timestamp": "2026-03-14T12:30:00Z",
    },
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=PortfolioSummary)
async def get_summary(request: Request) -> PortfolioSummary | dict:
    """Total value, unrealized PnL, realized PnL, fees."""
    pool = _pool_from_request(request)
    if pool is None:
        return PortfolioSummary()

    try:
        async with pool.acquire() as conn:
            snapshot = await conn.fetchrow(
                "SELECT total_value, unrealized_pnl, realized_pnl "
                "FROM seed_balance_snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            fees_row = await conn.fetchval("SELECT COALESCE(SUM(fee), 0) FROM seed_trades")

            if snapshot is None:
                return PortfolioSummary()

            total_value = float(snapshot["total_value"])
            unrealized_pnl = float(snapshot["unrealized_pnl"])
            realized_pnl = float(snapshot["realized_pnl"])
            total_fees = float(fees_row)

            # Derive change_pct from unrealized pnl relative to total value
            change_pct = round(unrealized_pnl / total_value * 100, 2) if total_value else 0.0

            return {
                "total_value": round(total_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "total_fees": round(total_fees, 2),
                "change_pct": change_pct,
            }
    except Exception:
        logger.exception("Failed to fetch portfolio summary from DB")
        return PortfolioSummary()


@router.get("/positions", response_model=list[Position])
async def get_positions(request: Request) -> list[dict]:
    """All open positions across exchanges."""
    pool = _pool_from_request(request)
    if pool is None:
        return _POSITIONS

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, strategy_id, exchange_id, symbol, direction, quantity, "
                "avg_entry_price, unrealized_pnl, realized_pnl FROM seed_positions"
            )

        positions: list[dict] = []
        for row in rows:
            entry_price = float(row["avg_entry_price"])
            quantity = float(row["quantity"])
            unrealized_pnl = float(row["unrealized_pnl"])
            # No live feed yet -- use entry price as current price
            current_price = entry_price
            pnl_pct = (
                round(unrealized_pnl / (entry_price * quantity) * 100, 2)
                if entry_price * quantity
                else 0.0
            )

            positions.append(
                {
                    "id": f"pos-{row['id']}",
                    "pair": _format_pair(row["symbol"]),
                    "exchange": row["exchange_id"],
                    "side": "Long" if row["direction"] == "LONG" else "Short",
                    "size": quantity,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_pct": pnl_pct,
                }
            )
        return positions
    except Exception:
        logger.exception("Failed to fetch positions from DB")
        return _POSITIONS


@router.get("/equity-curve", response_model=list[EquityPoint])
async def get_equity_curve(request: Request) -> list[dict]:
    """Time series data for the equity chart."""
    pool = _pool_from_request(request)
    if pool is None:
        return _EQUITY_CURVE

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT timestamp, total_value FROM seed_balance_snapshots ORDER BY timestamp"
            )

        return [
            {
                "timestamp": row["timestamp"].isoformat(),
                "value": float(row["total_value"]),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch equity curve from DB")
        return _EQUITY_CURVE


@router.get("/daily-pnl", response_model=list[DailyPnl])
async def get_daily_pnl(request: Request) -> list[dict]:
    """Daily PnL series."""
    pool = _pool_from_request(request)
    if pool is None:
        return _DAILY_PNL

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT date_trunc('day', timestamp) AS date, SUM(pnl) AS pnl "
                "FROM seed_trades GROUP BY date ORDER BY date"
            )

        return [
            {
                "date": row["date"].strftime("%Y-%m-%d"),
                "pnl": round(float(row["pnl"]), 2),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch daily PnL from DB")
        return _DAILY_PNL


@router.get("/monthly-returns", response_model=list[MonthlyReturn])
async def get_monthly_returns(request: Request) -> list[dict]:
    """Monthly return percentages derived from balance snapshots."""
    pool = _pool_from_request(request)
    if pool is None:
        return _MONTHLY_RETURNS

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT "
                "  to_char(date_trunc('month', timestamp), 'YYYY-MM') AS month, "
                "  (array_agg(total_value ORDER BY timestamp ASC))[1] AS first_val, "
                "  (array_agg(total_value ORDER BY timestamp DESC))[1] AS last_val "
                "FROM seed_balance_snapshots "
                "GROUP BY date_trunc('month', timestamp) "
                "ORDER BY date_trunc('month', timestamp)"
            )

        results: list[dict] = []
        for row in rows:
            first_val = float(row["first_val"])
            last_val = float(row["last_val"])
            return_pct = round((last_val - first_val) / first_val * 100, 2) if first_val else 0.0
            results.append({"month": row["month"], "return_pct": return_pct})
        return results
    except Exception:
        logger.exception("Failed to fetch monthly returns from DB")
        return _MONTHLY_RETURNS


@router.get("/attribution", response_model=list[AttributionItem])
async def get_attribution(request: Request) -> list[dict]:
    """PnL broken down by strategy."""
    pool = _pool_from_request(request)
    if pool is None:
        return _ATTRIBUTION

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.name, SUM(t.pnl) AS pnl "
                "FROM seed_trades t "
                "JOIN seed_strategies s ON t.strategy_id = s.id "
                "GROUP BY s.name"
            )

        total_pnl = sum(float(r["pnl"]) for r in rows)
        return [
            {
                "strategy": row["name"],
                "pnl": round(float(row["pnl"]), 2),
                "pct_of_total": (
                    round(float(row["pnl"]) / total_pnl * 100, 1) if total_pnl else 0.0
                ),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch attribution from DB")
        return _ATTRIBUTION


@router.get("/trades", response_model=list[TradeRecord])
async def get_trades(request: Request) -> list[dict]:
    """Recent trades (last 20, newest first)."""
    pool = _pool_from_request(request)
    if pool is None:
        return _TRADES_PLACEHOLDER

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, symbol, side, price, quantity, fee, pnl, timestamp "
                "FROM seed_trades ORDER BY timestamp DESC LIMIT 20"
            )

        return [
            {
                "id": row["id"],
                "pair": _format_pair(row["symbol"]),
                "side": row["side"],
                "price": float(row["price"]),
                "size": float(row["quantity"]),
                "fee": round(float(row["fee"]), 8),
                "pnl": round(float(row["pnl"]), 2),
                "timestamp": row["timestamp"].isoformat(),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch trades from DB")
        return _TRADES_PLACEHOLDER
