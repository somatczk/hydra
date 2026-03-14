from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "LSTM Momentum": "ML-based momentum strategy using LSTM predictions on 1h BTC/USDT",
    "Mean Reversion": "RSI-based mean reversion with Bollinger Band confirmation",
    "Funding Arbitrage": "Cross-exchange funding rate arbitrage between perpetual swaps",
    "Breakout Scanner": "Volume-weighted breakout detection across multiple timeframes",
}
_DEFAULT_DESCRIPTION = "Custom strategy"


def _pool_from_request(request: Request) -> object | None:
    """Return the asyncpg connection pool from app state, or ``None``."""
    return getattr(request.app.state, "db_pool", None)


def _status_from_enabled(enabled: bool) -> str:
    """Map the ``enabled`` boolean to a human-readable status string."""
    return "Active" if enabled else "Paused"


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------


class StrategyPerformance(BaseModel):
    total_pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0


class StrategyResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str  # Active | Paused | Backtesting | Draft
    enabled: bool = True
    config_yaml: str = ""
    performance: StrategyPerformance = Field(default_factory=StrategyPerformance)


class StrategyUpdateRequest(BaseModel):
    config_yaml: str


class ToggleResponse(BaseModel):
    id: str
    enabled: bool


# ---------------------------------------------------------------------------
# In-memory placeholder data (replaced by DB in production)
# ---------------------------------------------------------------------------

_STRATEGIES: dict[str, dict[str, Any]] = {
    "strat-1": {
        "id": "strat-1",
        "name": "LSTM Momentum",
        "description": "ML-based momentum strategy using LSTM predictions on 1h BTC/USDT",
        "status": "Active",
        "enabled": True,
        "config_yaml": "strategy:\n  type: lstm_momentum\n  timeframe: 1h\n",
        "performance": {
            "total_pnl": 1240.50,
            "win_rate": 68.7,
            "total_trades": 32,
            "sharpe_ratio": 1.84,
            "max_drawdown": 8.2,
        },
    },
    "strat-2": {
        "id": "strat-2",
        "name": "Mean Reversion RSI",
        "description": "RSI-based mean reversion with Bollinger Band confirmation",
        "status": "Active",
        "enabled": True,
        "config_yaml": "strategy:\n  type: mean_reversion_rsi\n  period: 14\n",
        "performance": {
            "total_pnl": 580.20,
            "win_rate": 61.1,
            "total_trades": 18,
            "sharpe_ratio": 1.22,
            "max_drawdown": 12.4,
        },
    },
    "strat-3": {
        "id": "strat-3",
        "name": "Breakout Scanner",
        "description": "Volume-weighted breakout detection across multiple timeframes",
        "status": "Paused",
        "enabled": False,
        "config_yaml": "strategy:\n  type: breakout_scanner\n  timeframes: [5m, 15m, 1h]\n",
        "performance": {
            "total_pnl": -120.00,
            "win_rate": 40.0,
            "total_trades": 5,
            "sharpe_ratio": 0.45,
            "max_drawdown": 15.8,
        },
    },
    "strat-4": {
        "id": "strat-4",
        "name": "XGBoost Ensemble",
        "description": "Ensemble of XGBoost models with feature importance weighting",
        "status": "Backtesting",
        "enabled": False,
        "config_yaml": "strategy:\n  type: xgboost_ensemble\n",
        "performance": {
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        },
    },
}


def _get_strategy(strategy_id: str) -> dict[str, Any]:
    if strategy_id not in _STRATEGIES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )
    return _STRATEGIES[strategy_id]


def _row_to_strategy(row: Any) -> dict[str, Any]:
    """Convert a DB row (from the strategies + aggregated trades query) to a dict."""
    name = row["name"]
    return {
        "id": row["id"],
        "name": name,
        "description": _STRATEGY_DESCRIPTIONS.get(name, _DEFAULT_DESCRIPTION),
        "status": _status_from_enabled(row["enabled"]),
        "enabled": row["enabled"],
        "config_yaml": "",
        "performance": {
            "total_pnl": round(float(row["total_pnl"]), 2),
            "win_rate": round(float(row["win_rate"]), 1),
            "total_trades": row["total_trades"],
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        },
    }


_STRATEGIES_QUERY = (
    "SELECT ts.strategy_id AS id, ts.strategy_id AS name, "
    "ts.exchange_id, TRUE AS enabled, "
    "COALESCE(SUM(t.pnl), 0) AS total_pnl, "
    "COUNT(t.id) AS total_trades, "
    "COALESCE("
    "  COUNT(CASE WHEN t.pnl > 0 THEN 1 END)::float "
    "  / NULLIF(COUNT(t.id), 0) * 100, 0"
    ") AS win_rate "
    "FROM trading_sessions ts "
    "LEFT JOIN trades t ON ts.strategy_id = t.strategy_id "
    "GROUP BY ts.strategy_id, ts.exchange_id"
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(request: Request) -> list[dict[str, Any]]:
    """List all strategy configs with status and performance."""
    pool = _pool_from_request(request)
    if pool is None:
        return list(_STRATEGIES.values())

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_STRATEGIES_QUERY)
        return [_row_to_strategy(row) for row in rows]
    except Exception:
        logger.exception("Failed to fetch strategies from DB")
        return list(_STRATEGIES.values())


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    """Get a single strategy detail."""
    pool = _pool_from_request(request)
    if pool is None:
        return _get_strategy(strategy_id)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _STRATEGIES_QUERY + " HAVING s.id = $1",
                strategy_id,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy {strategy_id} not found",
            )
        return _row_to_strategy(row)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to fetch strategy %s from DB", strategy_id)
        return _get_strategy(strategy_id)


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(strategy_id: str, body: StrategyUpdateRequest) -> dict[str, Any]:
    """Update strategy config (YAML content)."""
    strat = _get_strategy(strategy_id)
    strat["config_yaml"] = body.config_yaml
    return strat


@router.post("/{strategy_id}/toggle", response_model=ToggleResponse)
async def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Enable or disable a strategy (in-memory only)."""
    strat = _get_strategy(strategy_id)
    strat["enabled"] = not strat["enabled"]
    strat["status"] = _status_from_enabled(strat["enabled"])
    return {"id": strategy_id, "enabled": strat["enabled"]}


@router.get("/{strategy_id}/performance", response_model=StrategyPerformance)
async def get_strategy_performance(strategy_id: str) -> dict[str, Any]:
    """Get performance metrics for a strategy."""
    strat = _get_strategy(strategy_id)
    return strat["performance"]
