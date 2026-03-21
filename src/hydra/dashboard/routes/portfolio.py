from __future__ import annotations

import logging
import re
from typing import Any

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


def _pool_from_request(request: Request) -> Any:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class PortfolioSummary(BaseModel):
    total_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    total_fees: float = 0.0
    change_pct: float = 0.0


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
    symbol: str
    side: str
    price: float
    quantity: float
    fee: float
    pnl: float
    timestamp: str


# ---------------------------------------------------------------------------
# Placeholder data
# ---------------------------------------------------------------------------

_EMPTY_SUMMARY = PortfolioSummary(
    total_value=0, unrealized_pnl=0, realized_pnl=0, total_fees=0, change_pct=0
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _get_paper_capital(request: Request) -> float:
    """Read the global paper_capital from system config (the central wallet)."""
    from hydra.dashboard.routes.system import get_paper_capital

    return get_paper_capital(request)


@router.get("/summary", response_model=PortfolioSummary)
async def get_summary(request: Request, source: str | None = None) -> PortfolioSummary | dict:
    """Total value, unrealized PnL, realized PnL, fees.

    For paper mode, total_value uses the central-wallet model:
    global paper_capital is the "exchange balance", each strategy draws
    from it.  total_value = paper_capital + realized_pnl + unrealized_pnl.
    """
    pool = _pool_from_request(request)
    if pool is None:
        return _EMPTY_SUMMARY

    try:
        async with pool.acquire() as conn:
            # Determine running session IDs for the requested source
            running_ids: list[str] = []
            if source:
                rows = await conn.fetch(
                    "SELECT id FROM trading_sessions "
                    "WHERE status = 'running' AND trading_mode = $1",
                    source,
                )
                running_ids = [r["id"] for r in rows]

            # --- Realized PnL & fees: only from running sessions ---
            if running_ids:
                realized_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        "WHERE source = $1 AND session_id = ANY($2)",
                        source,
                        running_ids,
                    )
                )
                total_fees = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(fee), 0) FROM trades "
                        "WHERE source = $1 AND session_id = ANY($2)",
                        source,
                        running_ids,
                    )
                )
                daily_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        "WHERE source = $1 AND session_id = ANY($2) "
                        "AND timestamp >= date_trunc('day', now())",
                        source,
                        running_ids,
                    )
                )
            elif source:
                realized_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE source = $1",
                        source,
                    )
                )
                total_fees = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(fee), 0) FROM trades WHERE source = $1",
                        source,
                    )
                )
                daily_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        "WHERE source = $1 AND timestamp >= date_trunc('day', now())",
                        source,
                    )
                )
            else:
                realized_pnl = float(
                    await conn.fetchval("SELECT COALESCE(SUM(pnl), 0) FROM trades")
                )
                total_fees = float(await conn.fetchval("SELECT COALESCE(SUM(fee), 0) FROM trades"))
                daily_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        "WHERE timestamp >= date_trunc('day', now())"
                    )
                )

            # --- Unrealized PnL: from open positions of running sessions ---
            if running_ids:
                unrealized_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions "
                        "WHERE source = $1 AND session_id = ANY($2)",
                        source,
                        running_ids,
                    )
                )
            elif source:
                unrealized_pnl = float(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions WHERE source = $1",
                        source,
                    )
                )
            else:
                unrealized_pnl = float(
                    await conn.fetchval("SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions")
                )

            # --- Total value: central-wallet model ---
            # Paper mode: total = global paper_capital + PnL (strategies
            # draw from the wallet; profit/loss adjusts it).
            # Live/other: fall back to latest balance snapshot.
            if source == "paper":
                wallet = _get_paper_capital(request)
                total_value = wallet + realized_pnl + unrealized_pnl - total_fees
            else:
                snapshot = await conn.fetchrow(
                    "SELECT total_value FROM balance_snapshots ORDER BY timestamp DESC LIMIT 1"
                )
                total_value = float(snapshot["total_value"]) if snapshot else 0.0

            # Max drawdown from equity curve
            if source:
                equity_rows = await conn.fetch(
                    "SELECT total_value FROM balance_snapshots "
                    "WHERE source = $1 ORDER BY timestamp",
                    source,
                )
            else:
                equity_rows = await conn.fetch(
                    "SELECT total_value FROM balance_snapshots ORDER BY timestamp"
                )
            max_drawdown_pct = 0.0
            peak = 0.0
            for row in equity_rows:
                val = float(row["total_value"])
                if val > peak:
                    peak = val
                if peak > 0:
                    dd = (peak - val) / peak * 100
                    if dd > max_drawdown_pct:
                        max_drawdown_pct = dd

            change_pct = (
                round((realized_pnl + unrealized_pnl) / total_value * 100, 2)
                if total_value
                else 0.0
            )

            return {
                "total_value": round(total_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "daily_pnl": round(daily_pnl, 2),
                "max_drawdown_pct": round(max_drawdown_pct, 1),
                "total_fees": round(total_fees, 2),
                "change_pct": change_pct,
            }
    except Exception:
        logger.exception("Failed to fetch portfolio summary from DB")
        return _EMPTY_SUMMARY


@router.get("/positions", response_model=list[Position])
async def get_positions(request: Request, source: str | None = None) -> list[dict]:
    """All open positions across exchanges."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT id, strategy_id, exchange_id, symbol, direction, quantity, "
                    "avg_entry_price, unrealized_pnl, realized_pnl FROM positions "
                    "WHERE source = $1",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, strategy_id, exchange_id, symbol, direction, quantity, "
                    "avg_entry_price, unrealized_pnl, realized_pnl FROM positions"
                )

            if not rows:
                return []

            # Fetch latest prices from OHLCV for all position symbols
            symbols = list({row["symbol"] for row in rows})
            latest_prices: dict[str, float] = {}
            try:
                for sym in symbols:
                    price_row = await conn.fetchval(
                        "SELECT close FROM ts.ohlcv_1m WHERE symbol = $1 "
                        "ORDER BY timestamp DESC LIMIT 1",
                        sym,
                    )
                    if price_row is not None:
                        latest_prices[sym] = float(price_row)
            except Exception:
                pass  # Table may not exist; fall back to entry price

        positions: list[dict] = []
        for row in rows:
            entry_price = float(row["avg_entry_price"])
            quantity = float(row["quantity"])
            direction = row["direction"]
            symbol = row["symbol"]
            current_price = latest_prices.get(symbol, entry_price)

            # Recompute unrealized PnL from current price
            if direction == "LONG":
                unrealized_pnl = (current_price - entry_price) * quantity
            else:
                unrealized_pnl = (entry_price - current_price) * quantity

            pnl_pct = (
                round(unrealized_pnl / (entry_price * quantity) * 100, 2)
                if entry_price * quantity
                else 0.0
            )

            positions.append(
                {
                    "id": f"pos-{row['id']}",
                    "pair": _format_pair(symbol),
                    "exchange": row["exchange_id"],
                    "side": "Long" if direction == "LONG" else "Short",
                    "size": quantity,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "pnl_pct": pnl_pct,
                }
            )
        return positions
    except Exception:
        logger.exception("Failed to fetch positions from DB")
        return []


@router.get("/equity-curve", response_model=list[EquityPoint])
async def get_equity_curve(request: Request, source: str | None = None) -> list[dict]:
    """Time series data for the equity chart."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT timestamp, total_value FROM balance_snapshots "
                    "WHERE source = $1 ORDER BY timestamp",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT timestamp, total_value FROM balance_snapshots ORDER BY timestamp"
                )

        if not rows:
            return []

        return [
            {
                "timestamp": row["timestamp"].isoformat(),
                "value": float(row["total_value"]),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch equity curve from DB")
        return []


@router.get("/daily-pnl", response_model=list[DailyPnl])
async def get_daily_pnl(request: Request, source: str | None = None) -> list[dict]:
    """Daily PnL series."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT date_trunc('day', timestamp) AS date, SUM(pnl) AS pnl "
                    "FROM trades WHERE source = $1 GROUP BY date ORDER BY date",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT date_trunc('day', timestamp) AS date, SUM(pnl) AS pnl "
                    "FROM trades GROUP BY date ORDER BY date"
                )

        if not rows:
            return []

        return [
            {
                "date": row["date"].strftime("%Y-%m-%d"),
                "pnl": round(float(row["pnl"]), 2),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch daily PnL from DB")
        return []


@router.get("/monthly-returns", response_model=list[MonthlyReturn])
async def get_monthly_returns(request: Request, source: str | None = None) -> list[dict]:
    """Monthly return percentages derived from balance snapshots."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT "
                    "  to_char(date_trunc('month', timestamp), 'YYYY-MM') AS month, "
                    "  (array_agg(total_value ORDER BY timestamp ASC))[1] AS first_val, "
                    "  (array_agg(total_value ORDER BY timestamp DESC))[1] AS last_val "
                    "FROM balance_snapshots "
                    "WHERE source = $1 "
                    "GROUP BY date_trunc('month', timestamp) "
                    "ORDER BY date_trunc('month', timestamp)",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT "
                    "  to_char(date_trunc('month', timestamp), 'YYYY-MM') AS month, "
                    "  (array_agg(total_value ORDER BY timestamp ASC))[1] AS first_val, "
                    "  (array_agg(total_value ORDER BY timestamp DESC))[1] AS last_val "
                    "FROM balance_snapshots "
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
        return []


@router.get("/attribution", response_model=list[AttributionItem])
async def get_attribution(request: Request, source: str | None = None) -> list[dict]:
    """PnL broken down by strategy."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT strategy_id AS name, SUM(pnl) AS pnl "
                    "FROM trades WHERE source = $1 GROUP BY strategy_id",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT strategy_id AS name, SUM(pnl) AS pnl FROM trades GROUP BY strategy_id"
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
        return []


@router.get("/trades", response_model=list[TradeRecord])
async def get_trades(request: Request, source: str | None = None) -> list[dict]:
    """Recent trades (last 20, newest first)."""
    pool = _pool_from_request(request)
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            if source:
                rows = await conn.fetch(
                    "SELECT id, symbol, side, price, quantity, fee, pnl, timestamp "
                    "FROM trades WHERE source = $1 ORDER BY timestamp DESC LIMIT 20",
                    source,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, symbol, side, price, quantity, fee, pnl, timestamp "
                    "FROM trades ORDER BY timestamp DESC LIMIT 20"
                )

        if not rows:
            return []

        return [
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "price": float(row["price"]),
                "quantity": float(row["quantity"]),
                "fee": round(float(row["fee"]), 8),
                "pnl": round(float(row["pnl"]), 2),
                "timestamp": row["timestamp"].isoformat(),
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to fetch trades from DB")
        return []
