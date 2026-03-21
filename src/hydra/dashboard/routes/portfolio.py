from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
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
    strategy_id: str = ""
    notes: str = ""
    tags: list[str] = []


class TradeListResponse(BaseModel):
    trades: list[TradeRecord]
    total: int
    page: int
    limit: int
    pages: int


class TradeUpdateRequest(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None


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
                if source:
                    snapshot = await conn.fetchrow(
                        "SELECT total_value FROM balance_snapshots "
                        "WHERE source = $1 ORDER BY timestamp DESC LIMIT 1",
                        source,
                    )
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


@router.get("/trades", response_model=TradeListResponse)
async def get_trades(
    request: Request,
    source: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    min_pnl: float | None = None,
    max_pnl: float | None = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Filtered, paginated trade journal.

    Returns trade records alongside pagination metadata so clients can
    implement page controls without a separate count request.
    """
    pool = _pool_from_request(request)
    if pool is None:
        return {"trades": [], "total": 0, "page": page, "limit": limit, "pages": 0}

    # Clamp inputs to sane bounds
    page = max(1, page)
    limit = max(1, min(limit, 500))
    offset = (page - 1) * limit

    # Build dynamic WHERE clause with positional parameters
    conditions: list[str] = []
    params: list[Any] = []

    def _add(condition: str, value: Any) -> None:
        params.append(value)
        conditions.append(condition.format(n=len(params)))

    if source is not None:
        _add("source = ${n}", source)
    if strategy_id is not None:
        _add("strategy_id = ${n}", strategy_id)
    if symbol is not None:
        _add("symbol = ${n}", symbol)
    if from_date is not None:
        _add("timestamp >= ${n}", datetime.fromisoformat(from_date))
    if to_date is not None:
        _add("timestamp <= ${n}", datetime.fromisoformat(to_date))
    if min_pnl is not None:
        _add("pnl >= ${n}", min_pnl)
    if max_pnl is not None:
        _add("pnl <= ${n}", max_pnl)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with pool.acquire() as conn:
            total: int = await conn.fetchval(
                f"SELECT COUNT(*) FROM trades {where_clause}",  # noqa: S608
                *params,
            )

            # Append LIMIT / OFFSET params after the WHERE params
            params.append(limit)
            params.append(offset)
            limit_n = len(params) - 1
            offset_n = len(params)

            rows = await conn.fetch(
                f"SELECT id, symbol, side, price, quantity, fee, pnl, timestamp, "  # noqa: S608
                f"strategy_id, COALESCE(notes, '') AS notes, "
                f"COALESCE(tags, ARRAY[]::text[]) AS tags "
                f"FROM trades {where_clause} "
                f"ORDER BY timestamp DESC "
                f"LIMIT ${limit_n} OFFSET ${offset_n}",
                *params,
            )

        trades = [
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "price": float(row["price"]),
                "quantity": float(row["quantity"]),
                "fee": round(float(row["fee"]), 8),
                "pnl": round(float(row["pnl"]), 2),
                "timestamp": row["timestamp"].isoformat(),
                "strategy_id": row["strategy_id"] or "",
                "notes": row["notes"],
                "tags": list(row["tags"]),
            }
            for row in rows
        ]

        pages = (total + limit - 1) // limit if total > 0 else 0

        return {
            "trades": trades,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
        }
    except Exception:
        logger.exception("Failed to fetch trades from DB")
        return {"trades": [], "total": 0, "page": page, "limit": limit, "pages": 0}


@router.patch("/trades/{trade_id}", response_model=TradeRecord)
async def update_trade(trade_id: int, body: TradeUpdateRequest, request: Request) -> dict:
    """Update journal notes and/or tags on a trade record."""
    pool = _pool_from_request(request)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Build SET clause from provided fields only
    set_parts: list[str] = []
    params: list[Any] = []

    def _add_set(clause: str, value: Any) -> None:
        params.append(value)
        set_parts.append(clause.format(n=len(params)))

    if body.notes is not None:
        _add_set("notes = ${n}", body.notes)
    if body.tags is not None:
        _add_set("tags = ${n}", body.tags)

    if not set_parts:
        raise HTTPException(status_code=422, detail="No fields provided for update")

    params.append(trade_id)
    pk_n = len(params)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE trades SET {', '.join(set_parts)} "  # noqa: S608
                f"WHERE id = ${pk_n} "
                f"RETURNING id, symbol, side, price, quantity, fee, pnl, timestamp, "
                f"strategy_id, COALESCE(notes, '') AS notes, "
                f"COALESCE(tags, ARRAY[]::text[]) AS tags",
                *params,
            )
    except Exception as exc:
        logger.exception("Failed to update trade %d", trade_id)
        raise HTTPException(status_code=500, detail="Database error") from exc

    if row is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "price": float(row["price"]),
        "quantity": float(row["quantity"]),
        "fee": round(float(row["fee"]), 8),
        "pnl": round(float(row["pnl"]), 2),
        "timestamp": row["timestamp"].isoformat(),
        "strategy_id": row["strategy_id"] or "",
        "notes": row["notes"],
        "tags": list(row["tags"]),
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_HEADERS: dict[str, list[str]] = {
    "generic": ["Date", "Symbol", "Side", "Quantity", "Price", "Fee", "PnL", "Strategy"],
    "koinly": [
        "Date",
        "Sent Amount",
        "Sent Currency",
        "Received Amount",
        "Received Currency",
        "Fee Amount",
        "Fee Currency",
        "Net Worth Amount",
        "Net Worth Currency",
        "Label",
        "Description",
        "TxHash",
    ],
    "turbotax": [
        "Description of Property",
        "Date Acquired",
        "Date Sold",
        "Proceeds",
        "Cost Basis",
        "Gain or Loss",
    ],
}


def _row_generic(row: Any) -> list[str]:
    return [
        row["timestamp"].isoformat(),
        row["symbol"],
        row["side"],
        str(float(row["quantity"])),
        str(float(row["price"])),
        str(round(float(row["fee"]), 8)),
        str(round(float(row["pnl"]), 2)),
        row["strategy_id"] or "",
    ]


def _row_koinly(row: Any) -> list[str]:
    """Map a trade row to Koinly CSV format."""
    side = row["side"].upper()
    qty = float(row["quantity"])
    price = float(row["price"])
    fee = round(float(row["fee"]), 8)
    pnl = round(float(row["pnl"]), 2)
    symbol = row["symbol"]
    m = _PAIR_RE.match(symbol)
    base = m.group(1) if m else symbol[:3]
    quote = m.group(2) if m else symbol[3:]
    proceeds = round(qty * price, 8)

    if side == "BUY":
        sent_amt, sent_cur = str(proceeds), quote
        recv_amt, recv_cur = str(qty), base
    else:
        sent_amt, sent_cur = str(qty), base
        recv_amt, recv_cur = str(proceeds), quote

    return [
        row["timestamp"].isoformat(),
        sent_amt,
        sent_cur,
        recv_amt,
        recv_cur,
        str(fee),
        quote,
        str(abs(pnl)),
        quote,
        "trade",
        f"{side} {qty} {base}",
        "",
    ]


def _row_turbotax(row: Any) -> list[str]:
    """Map a trade row to TurboTax CSV format."""
    qty = float(row["quantity"])
    price = float(row["price"])
    fee = round(float(row["fee"]), 8)
    pnl = round(float(row["pnl"]), 2)
    side = row["side"].upper()
    proceeds = round(qty * price, 8)
    cost_basis = round(proceeds - pnl - fee, 8)
    symbol = row["symbol"]
    m = _PAIR_RE.match(symbol)
    base = m.group(1) if m else symbol[:3]
    ts = row["timestamp"].isoformat()

    return [
        f"{qty} {base} ({side})",
        ts,  # Date Acquired -- approximated as trade date
        ts,
        str(proceeds),
        str(cost_basis),
        str(pnl),
    ]


@router.get("/export/csv")
async def export_trades_csv(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    format: str = "generic",
) -> StreamingResponse:
    """Export trades as a downloadable CSV file.

    Supported format values: ``generic``, ``koinly``, ``turbotax``.
    """
    if format not in _CSV_HEADERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown format '{format}'. Must be one of: {', '.join(_CSV_HEADERS)}",
        )

    pool = _pool_from_request(request)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    conditions: list[str] = []
    params: list[Any] = []

    def _add(condition: str, value: Any) -> None:
        params.append(value)
        conditions.append(condition.format(n=len(params)))

    if from_date is not None:
        _add("timestamp >= ${n}", datetime.fromisoformat(from_date))
    if to_date is not None:
        _add("timestamp <= ${n}", datetime.fromisoformat(to_date))

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, symbol, side, price, quantity, fee, pnl, timestamp, strategy_id "  # noqa: S608
                f"FROM trades {where_clause} ORDER BY timestamp ASC",
                *params,
            )
    except Exception as exc:
        logger.exception("Failed to fetch trades for CSV export")
        raise HTTPException(status_code=500, detail="Database error") from exc

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADERS[format])

    row_fn = {"generic": _row_generic, "koinly": _row_koinly, "turbotax": _row_turbotax}[format]
    for row in rows:
        writer.writerow(row_fn(row))

    csv_content = buf.getvalue()
    headers = {"Content-Disposition": f'attachment; filename="hydra_trades_{format}.csv"'}
    return StreamingResponse(iter([csv_content]), media_type="text/csv", headers=headers)


# ---------------------------------------------------------------------------
# Live performance metrics
# ---------------------------------------------------------------------------


class LiveMetrics(BaseModel):
    rolling_sharpe_30d: float = 0.0
    time_weighted_return_30d: float = 0.0
    avg_trade_duration_hours: float = 0.0
    trades_per_day: float = 0.0


@router.get("/live-metrics", response_model=LiveMetrics)
async def get_live_metrics(request: Request, source: str | None = None) -> LiveMetrics | dict:
    """Rolling 30-day performance metrics computed from trade history."""
    pool = _pool_from_request(request)
    if pool is None:
        return LiveMetrics()

    try:
        async with pool.acquire() as conn:
            # Daily PnL for last 30 days
            if source:
                daily_rows = await conn.fetch(
                    "SELECT date_trunc('day', timestamp) AS d, SUM(pnl) AS pnl "
                    "FROM trades WHERE source = $1 "
                    "AND timestamp >= now() - interval '30 days' "
                    "GROUP BY d ORDER BY d",
                    source,
                )
            else:
                daily_rows = await conn.fetch(
                    "SELECT date_trunc('day', timestamp) AS d, SUM(pnl) AS pnl "
                    "FROM trades WHERE timestamp >= now() - interval '30 days' "
                    "GROUP BY d ORDER BY d"
                )
            daily_pnls = [float(r["pnl"]) for r in daily_rows]

            # Sharpe ratio (30d daily)
            import math

            sharpe = 0.0
            if len(daily_pnls) >= 2:
                import numpy as np

                arr = np.array(daily_pnls)
                mean = float(np.mean(arr))
                std = float(np.std(arr, ddof=1))
                if std > 0:
                    sharpe = round(mean / std * math.sqrt(252), 2)

            # Time-weighted return (30d)
            if source:
                equity_rows = await conn.fetch(
                    "SELECT total_value FROM balance_snapshots "
                    "WHERE source = $1 AND timestamp >= now() - interval '30 days' "
                    "ORDER BY timestamp",
                    source,
                )
            else:
                equity_rows = await conn.fetch(
                    "SELECT total_value FROM balance_snapshots "
                    "WHERE timestamp >= now() - interval '30 days' "
                    "ORDER BY timestamp"
                )
            twr = 0.0
            if len(equity_rows) >= 2:
                first = float(equity_rows[0]["total_value"])
                last = float(equity_rows[-1]["total_value"])
                if first > 0:
                    twr = round((last - first) / first * 100, 2)

            # Trades per day
            if source:
                trade_stats = await conn.fetchrow(
                    "SELECT COUNT(*) AS cnt FROM trades "
                    "WHERE source = $1 AND timestamp >= now() - interval '30 days'",
                    source,
                )
            else:
                trade_stats = await conn.fetchrow(
                    "SELECT COUNT(*) AS cnt FROM trades "
                    "WHERE timestamp >= now() - interval '30 days'"
                )
            cnt = trade_stats["cnt"] if trade_stats else 0
            days = max(len(daily_pnls), 1)
            trades_per_day = round(cnt / days, 1) if days > 0 else 0.0

            return {
                "rolling_sharpe_30d": sharpe,
                "time_weighted_return_30d": twr,
                "avg_trade_duration_hours": 0.0,  # Requires entry/exit pairing
                "trades_per_day": trades_per_day,
            }
    except Exception:
        logger.exception("Failed to compute live metrics")
        return LiveMetrics()
