from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes.strategy_builder import (
    _CONFIG_DIR as _STRATEGY_CONFIG_DIR,
)
from hydra.dashboard.routes.strategy_builder import (
    _find_strategy_file,
    _parse_strategy_yaml,
)
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
    name: str = ""


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


class BacktestTransaction(BaseModel):
    trade_id: int
    type: str  # "entry" or "exit"
    time: str
    side: str
    price: float
    quantity: float
    fee: float
    pnl: float | None = None


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
    name: str = ""
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)


class BacktestResultDetail(BaseModel):
    id: str
    strategy: str
    period: str
    status: str
    name: str = ""
    stopped_reason: str | None = None
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[BacktestTradeRecord] = Field(default_factory=list)
    transactions: list[BacktestTransaction] = Field(default_factory=list)


class BacktestRenameRequest(BaseModel):
    name: str


class BacktestVerification(BaseModel):
    trade_count_match: bool
    win_rate_match: bool
    total_pnl_match: bool
    computed_trade_count: int
    computed_win_rate: float
    computed_total_pnl: float
    reported_trade_count: int
    reported_win_rate: float
    reported_total_pnl: float
    all_passed: bool


class BacktestStrategyOption(BaseModel):
    id: str
    name: str


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
        "name": "LSTM Momentum Q1",
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
        "name": "Mean Reversion Q1",
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
        "name": "Breakout Feb",
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
# DB persistence helpers
# ---------------------------------------------------------------------------


async def _persist_result_to_db(pool: Any, result: dict[str, Any]) -> None:
    """Persist a single backtest result (and its trades) to TimescaleDB."""
    try:
        async with pool.acquire() as conn:
            metrics = result.get("metrics", {})
            await conn.execute(
                """INSERT INTO backtest_results
                       (id, strategy, period, status, total_trades, win_rate,
                        total_pnl, max_drawdown, sharpe_ratio, equity_curve, name,
                        transactions)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11,
                           $12::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                       status = EXCLUDED.status,
                       total_trades = EXCLUDED.total_trades,
                       win_rate = EXCLUDED.win_rate,
                       total_pnl = EXCLUDED.total_pnl,
                       max_drawdown = EXCLUDED.max_drawdown,
                       sharpe_ratio = EXCLUDED.sharpe_ratio,
                       equity_curve = EXCLUDED.equity_curve,
                       name = EXCLUDED.name,
                       transactions = EXCLUDED.transactions""",
                result["id"],
                result["strategy"],
                result["period"],
                result["status"],
                metrics.get("total_trades", 0),
                metrics.get("win_rate", 0.0),
                metrics.get("total_pnl", 0.0),
                metrics.get("max_drawdown", 0.0),
                metrics.get("sharpe_ratio", 0.0),
                json.dumps(result.get("equity_curve", [])),
                result.get("name", ""),
                json.dumps(result.get("transactions", [])),
            )
            trades = result.get("trades", [])
            if trades:
                await conn.execute(
                    "DELETE FROM backtest_result_trades WHERE backtest_id = $1",
                    result["id"],
                )
                await conn.executemany(
                    """INSERT INTO backtest_result_trades
                           (backtest_id, entry_time, exit_time, side,
                            entry_price, exit_price, pnl)
                       VALUES ($1, $2::timestamptz, $3::timestamptz, $4, $5, $6, $7)""",
                    [
                        (
                            result["id"],
                            t["entry_time"],
                            t["exit_time"],
                            t["side"],
                            t["entry_price"],
                            t["exit_price"],
                            t["pnl"],
                        )
                        for t in trades
                    ],
                )
    except Exception:
        logger.debug("Failed to persist backtest %s to DB", result["id"], exc_info=True)


async def populate_cache_from_db(pool: Any) -> None:
    """Load backtest results from DB into the in-memory cache on startup."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, strategy, period, status, total_trades, win_rate, "
                "total_pnl, max_drawdown, sharpe_ratio, equity_curve, name, "
                "transactions "
                "FROM backtest_results ORDER BY created_at DESC"
            )
            for row in rows:
                result_id = row["id"]
                trade_rows = await conn.fetch(
                    "SELECT entry_time, exit_time, side, entry_price, exit_price, pnl "
                    "FROM backtest_result_trades WHERE backtest_id = $1 "
                    "ORDER BY entry_time",
                    result_id,
                )
                equity_curve = row["equity_curve"]
                if isinstance(equity_curve, str):
                    equity_curve = json.loads(equity_curve)
                transactions = row["transactions"]
                if isinstance(transactions, str):
                    transactions = json.loads(transactions)
                _RESULTS[result_id] = {
                    "id": result_id,
                    "strategy": row["strategy"],
                    "period": row["period"],
                    "status": row["status"],
                    "name": row["name"],
                    "metrics": {
                        "total_trades": row["total_trades"],
                        "win_rate": row["win_rate"],
                        "total_pnl": row["total_pnl"],
                        "max_drawdown": row["max_drawdown"],
                        "sharpe_ratio": row["sharpe_ratio"],
                    },
                    "equity_curve": equity_curve,
                    "trades": [
                        {
                            "entry_time": t["entry_time"].isoformat(),
                            "exit_time": t["exit_time"].isoformat(),
                            "side": t["side"],
                            "entry_price": t["entry_price"],
                            "exit_price": t["exit_price"],
                            "pnl": t["pnl"],
                        }
                        for t in trade_rows
                    ],
                    "transactions": transactions,
                }
            # Seed defaults into DB if table is empty
            if not rows:
                for result in list(_RESULTS.values()):
                    await _persist_result_to_db(pool, result)
    except Exception:
        logger.debug("Failed to populate backtest cache from DB", exc_info=True)


# ---------------------------------------------------------------------------
# Helper: synthetic bar generation
# ---------------------------------------------------------------------------


def _generate_sample_bars(count: int, seed: int = 42, start: datetime | None = None) -> list[OHLCV]:
    """Generate synthetic OHLCV bars using a random-walk price series."""
    import numpy as np

    rng = np.random.default_rng(seed)
    base_price = 42000.0
    bars: list[OHLCV] = []
    bar_start = start or datetime(2024, 1, 1, tzinfo=UTC)

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
            timestamp=bar_start + timedelta(hours=i),
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


def _load_strategy_config(
    strategy_id: str, symbol: str = "BTCUSDT"
) -> tuple[type[RuleBasedStrategy], StrategyConfig]:
    """Load strategy config from YAML if available, else fall back to default RSI."""
    path = _find_strategy_file(strategy_id)
    if path is not None:
        data = _parse_strategy_yaml(path)
        if data is not None:
            params = data.get("parameters", {})
            exchange = data.get("exchange", {})
            symbols = data.get("symbols", [symbol])
            timeframes_data = data.get("timeframes", {})
            primary_tf = timeframes_data.get("primary", "1h")

            config = StrategyConfig(
                id=data.get("id", strategy_id),
                name=data.get("name", "Backtest Strategy"),
                strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
                symbols=symbols,
                exchange=ExchangeStrategyConfig(
                    exchange_id=exchange.get("exchange_id", "binance"),
                ),
                timeframes=TimeframeConfig(primary=Timeframe(primary_tf)),
                parameters=params,
            )
            return RuleBasedStrategy, config

    return _default_strategy_config(strategy_id, symbol)


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
            bars = _generate_sample_bars(bar_count, seed=hash(task_id) % 2**31, start=start)

        _TASKS[task_id]["progress"] = 30.0

        strategy_cls, config = _load_strategy_config(body.strategy_id)
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
            "strategy": config.name,
            "period": period,
            "status": "completed",
            "name": body.name,
            "stopped_reason": result.stopped_reason,
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
            "transactions": result.transactions,
        }

        # Persist to DB
        if pool is not None:
            await _persist_result_to_db(pool, _RESULTS[result_id])

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
async def list_results(request: Request) -> list[dict[str, Any]]:
    """List all past backtest result summaries."""
    pool = getattr(request.app.state, "db_pool", None)
    results: dict[str, dict[str, Any]] = {}

    # Try DB first
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, strategy, period, status, total_trades, win_rate, "
                    "total_pnl, max_drawdown, sharpe_ratio, name "
                    "FROM backtest_results ORDER BY created_at DESC"
                )
                for row in rows:
                    results[row["id"]] = {
                        "id": row["id"],
                        "strategy": row["strategy"],
                        "period": row["period"],
                        "status": row["status"],
                        "name": row["name"],
                        "metrics": {
                            "total_trades": row["total_trades"],
                            "win_rate": row["win_rate"],
                            "total_pnl": row["total_pnl"],
                            "max_drawdown": row["max_drawdown"],
                            "sharpe_ratio": row["sharpe_ratio"],
                        },
                    }
        except Exception:
            logger.debug("Failed to load backtest results from DB", exc_info=True)

    # Merge in-memory results (DB takes precedence)
    for rid, r in _RESULTS.items():
        if rid not in results:
            results[rid] = {
                "id": r["id"],
                "strategy": r["strategy"],
                "period": r["period"],
                "status": r["status"],
                "name": r.get("name", ""),
                "metrics": r["metrics"],
            }

    return list(results.values())


@router.get("/results/{result_id}", response_model=BacktestResultDetail)
async def get_result_detail(result_id: str, request: Request) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
    # Prefer in-memory cache (most complete/fresh data, includes transactions)
    if result_id in _RESULTS:
        r = _RESULTS[result_id]
        return {
            "id": r["id"],
            "strategy": r["strategy"],
            "period": r["period"],
            "status": r["status"],
            "name": r.get("name", ""),
            "stopped_reason": r.get("stopped_reason"),
            "metrics": r["metrics"],
            "equity_curve": r.get("equity_curve", []),
            "trades": r.get("trades", []),
            "transactions": r.get("transactions", []),
        }

    # Fall back to DB
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, strategy, period, status, total_trades, win_rate, "
                    "total_pnl, max_drawdown, sharpe_ratio, equity_curve, name, "
                    "transactions "
                    "FROM backtest_results WHERE id = $1",
                    result_id,
                )
                if row:
                    trade_rows = await conn.fetch(
                        "SELECT entry_time, exit_time, side, entry_price, "
                        "exit_price, pnl FROM backtest_result_trades "
                        "WHERE backtest_id = $1 ORDER BY entry_time",
                        result_id,
                    )
                    equity_curve = row["equity_curve"]
                    if isinstance(equity_curve, str):
                        equity_curve = json.loads(equity_curve)
                    transactions = row["transactions"]
                    if isinstance(transactions, str):
                        transactions = json.loads(transactions)
                    return {
                        "id": row["id"],
                        "strategy": row["strategy"],
                        "period": row["period"],
                        "status": row["status"],
                        "name": row["name"],
                        "metrics": {
                            "total_trades": row["total_trades"],
                            "win_rate": row["win_rate"],
                            "total_pnl": row["total_pnl"],
                            "max_drawdown": row["max_drawdown"],
                            "sharpe_ratio": row["sharpe_ratio"],
                        },
                        "equity_curve": equity_curve,
                        "trades": [
                            {
                                "entry_time": t["entry_time"].isoformat(),
                                "exit_time": t["exit_time"].isoformat(),
                                "side": t["side"],
                                "entry_price": t["entry_price"],
                                "exit_price": t["exit_price"],
                                "pnl": t["pnl"],
                            }
                            for t in trade_rows
                        ],
                        "transactions": transactions,
                    }
        except Exception:
            logger.debug("Failed to load backtest %s from DB", result_id, exc_info=True)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Result {result_id} not found",
    )


@router.patch("/results/{result_id}", response_model=BacktestResultSummary)
async def rename_result(
    result_id: str, body: BacktestRenameRequest, request: Request
) -> dict[str, Any]:
    """Rename a backtest result."""
    if result_id not in _RESULTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Result {result_id} not found",
        )
    _RESULTS[result_id]["name"] = body.name

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE backtest_results SET name = $1 WHERE id = $2",
                    body.name,
                    result_id,
                )
        except Exception:
            logger.debug("Failed to update name in DB for %s", result_id, exc_info=True)

    r = _RESULTS[result_id]
    return {
        "id": r["id"],
        "strategy": r["strategy"],
        "period": r["period"],
        "status": r["status"],
        "name": r.get("name", ""),
        "metrics": r["metrics"],
    }


@router.delete("/results/{result_id}", status_code=204)
async def delete_result(result_id: str, request: Request) -> None:
    """Delete a backtest result."""
    if result_id not in _RESULTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Result {result_id} not found",
        )
    _RESULTS.pop(result_id, None)

    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM backtest_results WHERE id = $1", result_id)
        except Exception:
            logger.debug("Failed to delete backtest %s from DB", result_id, exc_info=True)


@router.get("/results/{result_id}/verify", response_model=BacktestVerification)
async def verify_result(result_id: str, request: Request) -> dict[str, Any]:
    """Verify a backtest result by recomputing metrics from trades."""
    # Reuse detail fetch logic
    detail = await get_result_detail(result_id, request)

    trades = detail.get("trades", []) if isinstance(detail, dict) else detail.trades
    metrics = detail.get("metrics", {}) if isinstance(detail, dict) else detail.metrics

    if isinstance(metrics, dict):
        reported_trade_count = metrics.get("total_trades", 0)
        reported_win_rate = metrics.get("win_rate", 0.0)
        reported_total_pnl = metrics.get("total_pnl", 0.0)
    else:
        reported_trade_count = metrics.total_trades
        reported_win_rate = metrics.win_rate
        reported_total_pnl = metrics.total_pnl

    # Compute from trades
    computed_trade_count = len(trades)
    computed_total_pnl = round(sum(t["pnl"] if isinstance(t, dict) else t.pnl for t in trades), 2)
    wins = sum(1 for t in trades if (t["pnl"] if isinstance(t, dict) else t.pnl) > 0)
    computed_win_rate = (
        round(wins / computed_trade_count * 100, 2) if computed_trade_count > 0 else 0.0
    )

    # Compare with tolerance
    trade_count_match = computed_trade_count == reported_trade_count
    pnl_match = abs(computed_total_pnl - reported_total_pnl) < 0.02
    win_rate_match = abs(computed_win_rate - reported_win_rate) < 0.2

    return {
        "trade_count_match": trade_count_match,
        "win_rate_match": win_rate_match,
        "total_pnl_match": pnl_match,
        "computed_trade_count": computed_trade_count,
        "computed_win_rate": computed_win_rate,
        "computed_total_pnl": computed_total_pnl,
        "reported_trade_count": reported_trade_count,
        "reported_win_rate": reported_win_rate,
        "reported_total_pnl": reported_total_pnl,
        "all_passed": trade_count_match and pnl_match and win_rate_match,
    }


@router.get("/strategies", response_model=list[BacktestStrategyOption])
async def list_backtest_strategies() -> list[dict[str, str]]:
    """List strategies available for backtesting."""
    strategies: list[dict[str, str]] = []
    if _STRATEGY_CONFIG_DIR.is_dir():
        for path in sorted(_STRATEGY_CONFIG_DIR.glob("*.yaml")):
            data = _parse_strategy_yaml(path)
            if data is not None:
                strategies.append(
                    {
                        "id": data.get("id", path.stem),
                        "name": data.get("name", path.stem),
                    }
                )
    return strategies
