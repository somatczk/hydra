from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=PortfolioSummary)
async def get_summary() -> PortfolioSummary:
    """Total value, unrealized PnL, realized PnL, fees."""
    return PortfolioSummary()


@router.get("/positions", response_model=list[Position])
async def get_positions() -> list[dict]:
    """All open positions across exchanges."""
    return _POSITIONS


@router.get("/equity-curve", response_model=list[EquityPoint])
async def get_equity_curve() -> list[dict]:
    """Time series data for the equity chart."""
    return _EQUITY_CURVE


@router.get("/daily-pnl", response_model=list[DailyPnl])
async def get_daily_pnl() -> list[dict]:
    """Daily PnL series."""
    return _DAILY_PNL


@router.get("/monthly-returns", response_model=list[MonthlyReturn])
async def get_monthly_returns() -> list[dict]:
    """Monthly return percentages."""
    return _MONTHLY_RETURNS


@router.get("/attribution", response_model=list[AttributionItem])
async def get_attribution() -> list[dict]:
    """PnL broken down by strategy."""
    return _ATTRIBUTION
