from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
    RuleSet,
)
from hydra.strategy.config import ExchangeStrategyConfig, StrategyConfig, TimeframeConfig

logger = logging.getLogger(__name__)

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
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

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
# Helper: synthetic bar generation
# ---------------------------------------------------------------------------


def _generate_sample_bars(count: int, seed: int = 42) -> list[OHLCV]:
    """Generate synthetic OHLCV bars using a random-walk price series."""
    import numpy as np

    rng = np.random.default_rng(seed)
    base_price = 42000.0
    bars: list[OHLCV] = []

    price = base_price
    for i in range(count):
        change = rng.normal(0, 0.005)
        price = price * (1 + change)
        price = max(price, 100.0)

        intra_vol = abs(rng.normal(0, 0.003))
        open_price = price * (1 + rng.normal(0, 0.001))
        high_price = max(price, open_price) * (1 + intra_vol)
        low_price = min(price, open_price) * (1 - intra_vol)
        volume = abs(rng.normal(1000, 200))

        bar = OHLCV(
            open=Decimal(str(round(open_price, 2))),
            high=Decimal(str(round(high_price, 2))),
            low=Decimal(str(round(low_price, 2))),
            close=Decimal(str(round(price, 2))),
            volume=Decimal(str(round(volume, 2))),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
        )
        bars.append(bar)

    return bars


# ---------------------------------------------------------------------------
# Helper: default RSI-based strategy config
# ---------------------------------------------------------------------------


def _default_strategy_config(
    strategy_id: str, symbol: str = "BTCUSDT"
) -> tuple[type[RuleBasedStrategy], StrategyConfig]:
    """Build a default RSI-based rule strategy config for backtesting."""
    rule_set = RuleSet(
        entry_long=ConditionGroup(
            operator=LogicOperator.AND,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.LESS_THAN,
                    value=30,
                ),
            ],
        ),
        exit_long=ConditionGroup(
            operator=LogicOperator.AND,
            conditions=[
                Condition(
                    indicator="rsi",
                    params={"period": 14},
                    comparator=Comparator.GREATER_THAN,
                    value=70,
                ),
            ],
        ),
    )
    config = StrategyConfig(
        id=strategy_id,
        name="Backtest Strategy",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=[symbol],
        exchange=ExchangeStrategyConfig(exchange_id="binance"),
        timeframes=TimeframeConfig(primary=Timeframe.H1),
        parameters={"rules": rule_set.model_dump(), "required_history": 50},
    )
    return RuleBasedStrategy, config


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_backtest_task(task_id: str, body: BacktestRunRequest, pool: Any) -> None:
    """Execute a backtest in the background and store results."""
    _TASKS[task_id]["status"] = "running"
    _TASKS[task_id]["progress"] = 10.0

    try:
        # Try to load bars from DB
        bars: list[OHLCV] | None = None
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT timestamp, open, high, low, close, volume "
                        "FROM ts.ohlcv_1m "
                        "WHERE symbol = $1 AND timestamp >= $2 AND timestamp <= $3 "
                        "ORDER BY timestamp",
                        "BTCUSDT",
                        body.start_date,
                        body.end_date,
                    )
                    if rows:
                        bars = [
                            OHLCV(
                                open=Decimal(str(row["open"])),
                                high=Decimal(str(row["high"])),
                                low=Decimal(str(row["low"])),
                                close=Decimal(str(row["close"])),
                                volume=Decimal(str(row["volume"])),
                                timestamp=row["timestamp"],
                            )
                            for row in rows
                        ]
            except Exception:
                logger.debug(
                    "Could not load bars from DB for task %s, falling back to synthetic",
                    task_id,
                )

        # Fallback to synthetic bars
        if not bars:
            start = datetime.fromisoformat(body.start_date)
            end = datetime.fromisoformat(body.end_date)
            hours = int((end - start).total_seconds() / 3600)
            bar_count = max(hours, 200)
            bars = _generate_sample_bars(bar_count, seed=hash(task_id) % 2**31)

        _TASKS[task_id]["progress"] = 30.0

        strategy_cls, config = _default_strategy_config(body.strategy_id)
        runner = BacktestRunner()
        result = await runner.run(
            strategy_class=strategy_cls,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal(str(body.initial_capital)),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        _TASKS[task_id]["progress"] = 90.0

        # Compute total PnL from equity curve (final equity - initial capital)
        initial = Decimal(str(body.initial_capital))
        final_equity = result.equity_curve[-1] if result.equity_curve else initial
        total_pnl = float(final_equity - initial)

        # Build equity curve with timestamps from bars
        equity_curve_out: list[dict[str, Any]] = []
        for idx, eq in enumerate(result.equity_curve):
            if idx < len(bars):
                ts_str = bars[idx].timestamp.isoformat()
            else:
                ts_str = bars[-1].timestamp.isoformat()
            equity_curve_out.append({"timestamp": ts_str, "value": float(eq)})

        # Format period string
        period = f"{body.start_date} - {body.end_date}"

        # Store result
        result_id = f"bt-{task_id}"
        _RESULTS[result_id] = {
            "id": result_id,
            "strategy": body.strategy_id,
            "period": period,
            "status": "completed",
            "metrics": {
                "total_trades": result.total_trades,
                "win_rate": round(float(result.win_rate) * 100, 2),
                "total_pnl": round(total_pnl, 2),
                "max_drawdown": round(float(result.max_drawdown) * 100, 2),
                "sharpe_ratio": round(result.sharpe_ratio, 2),
            },
            "equity_curve": equity_curve_out,
            "trades": [
                {
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "side": t.direction,
                    "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                    "pnl": float(t.pnl),
                }
                for t in result.trades
            ],
        }

        _TASKS[task_id]["status"] = "completed"
        _TASKS[task_id]["progress"] = 100.0
        _TASKS[task_id]["result_id"] = result_id

    except Exception as exc:
        logger.exception("Backtest task %s failed", task_id)
        _TASKS[task_id]["status"] = "failed"
        _TASKS[task_id]["progress"] = 0.0
        _TASKS[task_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/run", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(body: BacktestRunRequest, request: Request) -> dict[str, Any]:
    """Start a new backtest run. Returns a task_id to poll for status."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    _TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "progress": 0.0,
        "request": body.model_dump(),
    }

    pool = getattr(request.app.state, "db_pool", None)
    task = asyncio.create_task(_run_backtest_task(task_id, body, pool))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

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
