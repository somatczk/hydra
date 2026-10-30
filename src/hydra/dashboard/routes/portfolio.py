from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from hydra.dashboard.routes._db import api_error, db_errors_to_503, require_pool

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


def _aware(ts: datetime) -> datetime:
    """Query dates without an offset are UTC (asyncpg refuses naive values for timestamptz)."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _format_pair(symbol: str) -> str:
    """Convert raw symbol (e.g. ``BTCUSDT``) to pair format (``BTC/USDT``)."""
    m = _PAIR_RE.match(symbol)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # Fallback: insert slash at position 3 (BTC-length) -- best effort
    return f"{symbol[:3]}/{symbol[3:]}" if len(symbol) > 3 else symbol


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
    paper_capital_pending: float | None = None


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
# Endpoints
# ---------------------------------------------------------------------------


_TRADE_TOTALS_SQL = (
    "SELECT COALESCE(SUM(pnl), 0) AS realized, COALESCE(SUM(fee), 0) AS fees, "
    "COALESCE(SUM(pnl - fee) FILTER (WHERE timestamp >= "
    "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS daily "
    "FROM trades WHERE ($1::text IS NULL OR source = $1)"
)

_RUNNING_UNREALIZED_SQL = (
    "SELECT COALESCE(SUM(p.unrealized_pnl), 0) FROM positions p "
    "JOIN trading_sessions s ON s.id = p.session_id "
    "WHERE s.status = 'running' AND ($1::text IS NULL OR p.source = $1)"
)

_SNAPSHOT_COLUMNS = (
    "b.timestamp, b.session_id, b.total_value, s.paper_capital AS allocated, s.stopped_at "
    "FROM balance_snapshots b JOIN trading_sessions s ON s.id = b.session_id "
    "WHERE b.source = 'paper' AND s.paper_capital IS NOT NULL "
)

# each session's last sleeve value per hour, from $1 on (all history when NULL)
_SESSION_BUCKETS_SQL = (
    "SELECT DISTINCT ON (b.session_id, date_trunc('hour', b.timestamp)) "
    + _SNAPSHOT_COLUMNS
    + "AND ($1::timestamptz IS NULL OR b.timestamp >= $1) "
    "ORDER BY b.session_id, date_trunc('hour', b.timestamp), b.timestamp DESC, b.id DESC"
)

# each session's last sleeve value before $1, so a bounded curve starts from the right sum
_SESSION_SEED_SQL = (
    "SELECT DISTINCT ON (b.session_id) " + _SNAPSHOT_COLUMNS + "AND b.timestamp < $1 "
    "ORDER BY b.session_id, b.timestamp DESC, b.id DESC"
)

_PAPER_RESETS_SQL = (
    "SELECT ts, details FROM risk_events WHERE scope = 'paper' AND kind = 'paper_reset' "
    "ORDER BY ts, id"
)

_ACCOUNT_SNAPSHOTS_SQL = (
    "SELECT timestamp, total_value FROM balance_snapshots "
    "WHERE session_id IS NULL AND ($1::text IS NULL OR source = $1) "
    "AND ($2::timestamptz IS NULL OR timestamp >= $2) "
    "ORDER BY timestamp, id"
)

# (timestamp, account value, segment); a paper reset starts a new segment
CurvePoint = tuple[datetime, float, int]


async def _ledger_capital(request: Request, pool: Any) -> float:
    """The capital the risk ledger runs on; the system_config row (503 if missing) without it."""
    from hydra.dashboard.routes.system import get_paper_capital, risk_service

    svc = risk_service(request)
    if svc is not None and svc.paper_capital is not None:
        return float(svc.paper_capital)
    return await get_paper_capital(pool)


async def _live_capital_baseline(pool: Any) -> float:
    """system_config 'live_capital': the capital a source=live summary is based on.

    The live account curve is the account-level balance snapshots (rows without a
    session); until one exists the summary's total value falls back to this baseline,
    like paper's falls back to the ledger capital. A missing row is the 403 of the
    live start path (seam #8: one code, one status, everywhere), never a silent 0.
    """
    async with db_errors_to_503("reading live_capital"), pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM system_config WHERE key = 'live_capital'")
    if row is None:
        raise api_error(
            403,
            "live_capital_missing",
            "system_config has no 'live_capital' row; it is set by the arm ceremony",
            "invalid",
        )
    raw = row["value"]
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return float(value)
    except (TypeError, ValueError) as exc:
        raise api_error(
            403,
            "live_capital_missing",
            f"system_config live_capital is unreadable: {exc}",
            "invalid",
        ) from exc


def _capital(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _paper_resets(conn: Any) -> list[tuple[datetime, float | None, float | None]]:
    resets: list[tuple[datetime, float | None, float | None]] = []
    for row in await conn.fetch(_PAPER_RESETS_SQL):
        details = row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        resets.append(
            (row["ts"], _capital(details.get("old_capital")), _capital(details.get("new_capital")))
        )
    return resets


async def _account_curve(
    conn: Any, source: str | None, capital: float | None, since: datetime | None = None
) -> list[CurvePoint]:
    """Account equity over time, at most one point per hour (and one per paper reset).

    Paper mirrors the RiskService ledger: capital in effect at t (from the audited paper_reset
    events, else the ledger's capital) + the sum over sessions of (last sleeve value - allocation).
    A stopped session keeps its result until the next reset, which drops it (carried PnL -> 0).
    Other sources keep one account-level row per snapshot (rows without a session).
    """
    if source != "paper":
        rows = await conn.fetch(_ACCOUNT_SNAPSHOTS_SQL, source, since)
        return [(row["timestamp"], float(row["total_value"]), 0) for row in rows]

    resets = await _paper_resets(conn)
    first_old = next((old for _, old, _ in resets if old is not None), None)
    base: float = first_old if first_old is not None else (capital or 0.0)
    seed = list(await conn.fetch(_SESSION_SEED_SQL, since)) if since is not None else []
    rows = sorted(
        [*seed, *await conn.fetch(_SESSION_BUCKETS_SQL, since)],
        key=lambda r: r["timestamp"],
    )
    last: dict[str, float] = {}
    stopped: dict[str, datetime | None] = {}
    points: list[CurvePoint] = []
    segment = 0
    pending = list(resets)

    def emit(ts: datetime) -> None:
        if since is not None and ts < since:
            return
        value = base + sum(last.values())
        hour = ts.replace(minute=0, second=0, microsecond=0)
        prev = points[-1] if points else None
        if (
            prev is not None
            and prev[2] == segment
            and prev[0].replace(minute=0, second=0, microsecond=0) == hour
        ):
            points[-1] = (ts, value, segment)
        else:
            points.append((ts, value, segment))

    def apply_resets(until: datetime | None) -> None:
        nonlocal base, segment
        while pending and (until is None or pending[0][0] <= until):
            ts, _, new = pending.pop(0)
            if new is not None:
                base = new
            for sid in [s for s, at in stopped.items() if at is not None and at <= ts]:
                last.pop(sid, None)
            segment += 1
            emit(ts)

    for row in rows:
        apply_resets(row["timestamp"])
        sid = row["session_id"]
        last[sid] = float(row["total_value"]) - float(row["allocated"])
        stopped[sid] = row["stopped_at"]
        emit(row["timestamp"])
    apply_resets(None)
    return points


def _max_drawdown_pct(curve: list[CurvePoint]) -> float:
    """Largest peak-to-trough fall within a segment; a capital change is not a drawdown."""
    max_dd = 0.0
    peak = 0.0
    segment = None
    for _, value, seg in curve:
        if seg != segment:
            peak, segment = 0.0, seg
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd


def _time_weighted_return_pct(curve: list[CurvePoint]) -> float:
    """Chained return over segments, so a capital change is not a gain or a loss."""
    growth = 1.0
    first: float | None = None
    prev: tuple[float, int] | None = None
    for _, value, seg in curve:
        if prev is None or seg != prev[1]:
            if prev is not None and first:
                growth *= prev[0] / first
            first = value
        prev = (value, seg)
    if prev is not None and first:
        growth *= prev[0] / first
    return (growth - 1) * 100


async def _pending_capital(request: Request, pool: Any) -> float | None:
    from hydra.dashboard.routes.system import get_paper_capital, pending_capital, risk_service

    if risk_service(request) is None:
        return None
    pending = pending_capital(request, await get_paper_capital(pool))
    return float(pending) if pending is not None else None


@router.get("/summary", response_model=PortfolioSummary)
async def get_summary(
    request: Request, source: str | None = None
) -> PortfolioSummary | dict[str, Any]:
    """Account value and max drawdown from the account curve; PnL and fees from trades.

    Paper: total_value is the ledger the RiskService runs on (capital + sleeves, carried PnL
    dropped at a paper reset); realized PnL and fees count every paper session (running or
    stopped), unrealized PnL running sessions, and daily PnL is net realized since 00:00 UTC.
    source=live: the same totals over the live-sourced rows, with ``live_capital`` as the
    capital baseline until account-level live snapshots exist.
    """
    pool = require_pool(request)
    capital: float | None
    if source == "paper":
        capital = await _ledger_capital(request, pool)
    elif source == "live":
        capital = await _live_capital_baseline(pool)
    else:
        capital = None
    pending = await _pending_capital(request, pool) if source == "paper" else None

    async with db_errors_to_503("reading portfolio summary"), pool.acquire() as conn:
        totals = await conn.fetchrow(_TRADE_TOTALS_SQL, source)
        realized_pnl = float(totals["realized"])
        total_fees = float(totals["fees"])
        daily_pnl = float(totals["daily"])
        unrealized_pnl = float(await conn.fetchval(_RUNNING_UNREALIZED_SQL, source))
        curve = await _account_curve(conn, source, capital)

    total_value = curve[-1][1] if curve else (capital or 0.0)

    change_pct = (
        round((realized_pnl + unrealized_pnl) / total_value * 100, 2) if total_value else 0.0
    )

    return {
        "total_value": round(total_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "daily_pnl": round(daily_pnl, 2),
        "max_drawdown_pct": round(_max_drawdown_pct(curve), 1),
        "total_fees": round(total_fees, 2),
        "change_pct": change_pct,
        "paper_capital_pending": pending,
    }


_DAILY_PNL_SQL = (
    "SELECT date_trunc('day', timestamp AT TIME ZONE 'UTC') AS date, SUM(pnl - fee) AS pnl "
    "FROM trades WHERE ($1::text IS NULL OR source = $1) GROUP BY date ORDER BY date"
)

_DAILY_PNL_30D_SQL = (
    "SELECT date_trunc('day', timestamp AT TIME ZONE 'UTC') AS d, SUM(pnl - fee) AS pnl "
    "FROM trades WHERE ($1::text IS NULL OR source = $1) "
    "AND timestamp >= now() - interval '30 days' GROUP BY d ORDER BY d"
)

_ATTRIBUTION_SQL = (
    "SELECT strategy_id AS name, SUM(pnl - fee) AS pnl FROM trades "
    "WHERE ($1::text IS NULL OR source = $1) GROUP BY strategy_id"
)

_POSITIONS_SQL = (
    "SELECT p.id, p.strategy_id, p.exchange_id, p.symbol, p.direction, p.quantity, "
    "p.avg_entry_price, p.unrealized_pnl, p.realized_pnl, COALESCE(s.timeframe, '1m') AS timeframe "
    "FROM positions p LEFT JOIN trading_sessions s ON s.id = p.session_id "
    "WHERE p.quantity <> 0 AND ($1::text IS NULL OR p.source = $1)"
)

_LATEST_CLOSE_SQL = (
    "SELECT close FROM ts.ohlcv_1m WHERE exchange = $1 AND symbol = $2 AND timeframe = $3 "
    "ORDER BY timestamp DESC LIMIT 1"
)


@router.get("/positions", response_model=list[Position])
async def get_positions(request: Request, source: str | None = None) -> list[dict[str, Any]]:
    """All open positions across exchanges."""
    pool = require_pool(request)

    async with db_errors_to_503("reading positions"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(_POSITIONS_SQL, source)
            if not rows:
                return []

            # Latest stored close on the position's own venue and the session's bar timeframe
            latest_prices: dict[tuple[str, str, str], float] = {}
            try:
                for key in {(r["exchange_id"], r["symbol"], r["timeframe"]) for r in rows}:
                    price_row = await conn.fetchval(_LATEST_CLOSE_SQL, *key)
                    if price_row is not None:
                        latest_prices[key] = float(price_row)
            except asyncpg.UndefinedTableError:
                logger.warning("ts.ohlcv_1m missing; positions use their stored unrealized PnL")

        positions: list[dict[str, Any]] = []
        for row in rows:
            entry_price = float(row["avg_entry_price"])
            quantity = float(row["quantity"])
            direction = row["direction"]
            symbol = row["symbol"]
            sign = 1.0 if direction == "LONG" else -1.0
            price = latest_prices.get((row["exchange_id"], symbol, row["timeframe"]))
            if price is not None:
                current_price = price
                unrealized_pnl = (current_price - entry_price) * quantity * sign
            else:
                unrealized_pnl = float(row["unrealized_pnl"])
                current_price = (
                    entry_price + unrealized_pnl / quantity * sign if quantity else entry_price
                )

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


@router.get("/equity-curve", response_model=list[EquityPoint])
async def get_equity_curve(request: Request, source: str | None = None) -> list[dict[str, Any]]:
    """Account equity over time, hourly (paper: the ledger's capital plus per-session sleeves)."""
    pool = require_pool(request)
    capital = await _ledger_capital(request, pool) if source == "paper" else None
    async with db_errors_to_503("reading equity curve"), pool.acquire() as conn:
        curve = await _account_curve(conn, source, capital)
    return [{"timestamp": ts.isoformat(), "value": value} for ts, value, _ in curve]


@router.get("/daily-pnl", response_model=list[DailyPnl])
async def get_daily_pnl(request: Request, source: str | None = None) -> list[dict[str, Any]]:
    """Net (after fees) realized PnL per UTC day."""
    pool = require_pool(request)

    async with db_errors_to_503("reading daily PnL"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(_DAILY_PNL_SQL, source)

        if not rows:
            return []

        return [
            {
                "date": row["date"].strftime("%Y-%m-%d"),
                "pnl": round(float(row["pnl"]), 2),
            }
            for row in rows
        ]


@router.get("/monthly-returns", response_model=list[MonthlyReturn])
async def get_monthly_returns(request: Request, source: str | None = None) -> list[dict[str, Any]]:
    """Monthly time-weighted return of the account curve (a capital change is not a return)."""
    pool = require_pool(request)
    capital = await _ledger_capital(request, pool) if source == "paper" else None
    async with db_errors_to_503("reading monthly returns"), pool.acquire() as conn:
        curve = await _account_curve(conn, source, capital)

    months: dict[str, list[CurvePoint]] = {}
    for point in curve:
        months.setdefault(point[0].strftime("%Y-%m"), []).append(point)
    return [
        {"month": month, "return_pct": round(_time_weighted_return_pct(points), 2)}
        for month, points in months.items()
    ]


@router.get("/attribution", response_model=list[AttributionItem])
async def get_attribution(request: Request, source: str | None = None) -> list[dict[str, Any]]:
    """Net (after fees) realized PnL broken down by strategy."""
    pool = require_pool(request)

    async with db_errors_to_503("reading attribution"):
        async with pool.acquire() as conn:
            rows = await conn.fetch(_ATTRIBUTION_SQL, source)

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


@router.get("/trades", response_model=TradeListResponse)
async def get_trades(
    request: Request,
    source: str | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    min_pnl: float | None = None,
    max_pnl: float | None = None,
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    """Filtered, paginated trade journal.

    Returns trade records alongside pagination metadata so clients can
    implement page controls without a separate count request.
    """
    pool = require_pool(request)

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
        _add("timestamp >= ${n}", _aware(from_date))
    if to_date is not None:
        _add("timestamp <= ${n}", _aware(to_date))
    if min_pnl is not None:
        _add("pnl >= ${n}", min_pnl)
    if max_pnl is not None:
        _add("pnl <= ${n}", max_pnl)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db_errors_to_503("reading trades"):
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


@router.patch("/trades/{trade_id}", response_model=TradeRecord)
async def update_trade(trade_id: int, body: TradeUpdateRequest, request: Request) -> dict[str, Any]:
    """Update journal notes and/or tags on a trade record."""
    pool = require_pool(request)

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
        raise api_error(422, "no_fields", "No fields provided for update", "invalid")

    params.append(trade_id)
    pk_n = len(params)

    async with db_errors_to_503("updating trade"), pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE trades SET {', '.join(set_parts)} "  # noqa: S608
            f"WHERE id = ${pk_n} "
            f"RETURNING id, symbol, side, price, quantity, fee, pnl, timestamp, "
            f"strategy_id, COALESCE(notes, '') AS notes, "
            f"COALESCE(tags, ARRAY[]::text[]) AS tags",
            *params,
        )

    if row is None:
        raise api_error(404, "trade_not_found", f"Trade {trade_id} not found", "not_found")

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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    format: str = "generic",
    source: str | None = None,
) -> StreamingResponse:
    """Export trades as a downloadable CSV file.

    Supported format values: ``generic``, ``koinly``, ``turbotax``.
    """
    if format not in _CSV_HEADERS:
        raise api_error(
            422,
            "unknown_format",
            f"Unknown format '{format}'. Must be one of: {', '.join(_CSV_HEADERS)}",
            "invalid",
        )

    pool = require_pool(request)

    conditions: list[str] = []
    params: list[Any] = []

    def _add(condition: str, value: Any) -> None:
        params.append(value)
        conditions.append(condition.format(n=len(params)))

    if from_date is not None:
        _add("timestamp >= ${n}", _aware(from_date))
    if to_date is not None:
        _add("timestamp <= ${n}", _aware(to_date))
    if source is not None:
        _add("source = ${n}", source)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with db_errors_to_503("exporting trades"), pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, symbol, side, price, quantity, fee, pnl, timestamp, strategy_id "  # noqa: S608
            f"FROM trades {where_clause} ORDER BY timestamp ASC",
            *params,
        )

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
async def get_live_metrics(
    request: Request, source: str | None = None
) -> LiveMetrics | dict[str, Any]:
    """Rolling 30-day performance metrics computed from trade history."""
    pool = require_pool(request)
    capital = await _ledger_capital(request, pool) if source == "paper" else None
    cutoff = datetime.now(UTC) - timedelta(days=30)

    async with db_errors_to_503("computing live metrics"), pool.acquire() as conn:
        daily_rows = await conn.fetch(_DAILY_PNL_30D_SQL, source)
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

        # Time-weighted return (30d) on the account equity curve
        recent = await _account_curve(conn, source, capital, since=cutoff)
        twr = round(_time_weighted_return_pct(recent), 2) if len(recent) >= 2 else 0.0

        # Trades per day
        if source:
            trade_stats = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM trades "
                "WHERE source = $1 AND timestamp >= now() - interval '30 days'",
                source,
            )
        else:
            trade_stats = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM trades WHERE timestamp >= now() - interval '30 days'"
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
