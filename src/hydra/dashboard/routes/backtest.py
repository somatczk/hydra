from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BacktestRunRequest(BaseModel):
    strategy_id: str
    start_date: str
    end_date: str
    initial_capital: float = 10000.0


class BacktestRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class BacktestStatus(BaseModel):
    task_id: str
    status: str  # queued | running | completed | failed
    progress: float = 0.0  # 0-100


class BacktestTradeRecord(BaseModel):
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    pnl: float


class BacktestMetrics(BaseModel):
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0


class BacktestResultSummary(BaseModel):
    id: str
    strategy: str
    period: str
    status: str
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)


class BacktestResultDetail(BaseModel):
    id: str
    strategy: str
    period: str
    status: str
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[BacktestTradeRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_TASKS: dict[str, dict[str, Any]] = {}

_RESULTS: dict[str, dict[str, Any]] = {
    "bt-1": {
        "id": "bt-1",
        "strategy": "LSTM Momentum",
        "period": "Jan 1 - Mar 1, 2026",
        "status": "completed",
        "metrics": {
            "total_trades": 142,
            "win_rate": 67.6,
            "total_pnl": 3420.50,
            "max_drawdown": 8.2,
            "sharpe_ratio": 1.84,
        },
        "equity_curve": [
            {"timestamp": "2026-01-01", "value": 10000.0},
            {"timestamp": "2026-02-01", "value": 11200.0},
            {"timestamp": "2026-03-01", "value": 13420.50},
        ],
        "trades": [
            {
                "entry_time": "2026-01-05T10:00:00Z",
                "exit_time": "2026-01-05T14:00:00Z",
                "side": "Long",
                "entry_price": 65000.0,
                "exit_price": 65400.0,
                "pnl": 60.0,
            },
        ],
    },
    "bt-2": {
        "id": "bt-2",
        "strategy": "Mean Reversion RSI",
        "period": "Jan 1 - Mar 1, 2026",
        "status": "completed",
        "metrics": {
            "total_trades": 98,
            "win_rate": 58.2,
            "total_pnl": 1180.0,
            "max_drawdown": 12.4,
            "sharpe_ratio": 1.22,
        },
        "equity_curve": [
            {"timestamp": "2026-01-01", "value": 10000.0},
            {"timestamp": "2026-02-01", "value": 10600.0},
            {"timestamp": "2026-03-01", "value": 11180.0},
        ],
        "trades": [],
    },
    "bt-3": {
        "id": "bt-3",
        "strategy": "Breakout Scanner",
        "period": "Feb 1 - Mar 1, 2026",
        "status": "completed",
        "metrics": {
            "total_trades": 45,
            "win_rate": 42.2,
            "total_pnl": -340.0,
            "max_drawdown": 15.8,
            "sharpe_ratio": 0.45,
        },
        "equity_curve": [
            {"timestamp": "2026-02-01", "value": 10000.0},
            {"timestamp": "2026-03-01", "value": 9660.0},
        ],
        "trades": [],
    },
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/run", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(body: BacktestRunRequest) -> dict[str, Any]:
    """Start a new backtest run. Returns a task_id to poll for status."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    _TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "progress": 0.0,
        "request": body.model_dump(),
    }
    return {"task_id": task_id, "status": "queued"}


@router.get("/status/{task_id}", response_model=BacktestStatus)
async def get_backtest_status(task_id: str) -> dict[str, Any]:
    """Check the progress of a running backtest."""
    if task_id not in _TASKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    task = _TASKS[task_id]
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
    }


@router.get("/results", response_model=list[BacktestResultSummary])
async def list_results() -> list[dict[str, Any]]:
    """List all past backtest result summaries."""
    return [
        {
            "id": r["id"],
            "strategy": r["strategy"],
            "period": r["period"],
            "status": r["status"],
            "metrics": r["metrics"],
        }
        for r in _RESULTS.values()
    ]


@router.get("/results/{result_id}", response_model=BacktestResultDetail)
async def get_result_detail(result_id: str) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
    if result_id not in _RESULTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Result {result_id} not found",
        )
    return _RESULTS[result_id]
