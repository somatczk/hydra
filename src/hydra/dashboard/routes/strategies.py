from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StrategyResponse])
async def list_strategies() -> list[dict[str, Any]]:
    """List all strategy configs with status and performance."""
    return list(_STRATEGIES.values())


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(strategy_id: str) -> dict[str, Any]:
    """Get a single strategy detail."""
    return _get_strategy(strategy_id)


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(strategy_id: str, body: StrategyUpdateRequest) -> dict[str, Any]:
    """Update strategy config (YAML content)."""
    strat = _get_strategy(strategy_id)
    strat["config_yaml"] = body.config_yaml
    return strat


@router.post("/{strategy_id}/toggle", response_model=ToggleResponse)
async def toggle_strategy(strategy_id: str) -> dict[str, Any]:
    """Enable or disable a strategy."""
    strat = _get_strategy(strategy_id)
    strat["enabled"] = not strat["enabled"]
    strat["status"] = "Active" if strat["enabled"] else "Paused"
    return {"id": strategy_id, "enabled": strat["enabled"]}


@router.get("/{strategy_id}/performance", response_model=StrategyPerformance)
async def get_strategy_performance(strategy_id: str) -> dict[str, Any]:
    """Get performance metrics for a strategy."""
    strat = _get_strategy(strategy_id)
    return strat["performance"]
