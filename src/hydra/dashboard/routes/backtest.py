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

from hydra.backtest.hyperopt import HyperoptResult, HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import WalkForwardAnalyzer, WalkForwardResult
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes.strategies import (
    _CONFIG_DIR as _STRATEGY_CONFIG_DIR,
)
from hydra.dashboard.routes.strategies import (
    _find_strategy_file,
    _parse_any_strategy_yaml,
    _parse_strategy_yaml,
)
from hydra.data.backfill import ExchangeBackfillService
from hydra.data.storage import MarketDataRepository
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
    timeframe: str = "1h"
    fetch_fresh: bool = False


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
    benchmark_return: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    expectancy: float = 0.0


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
    benchmark_equity: list[dict[str, Any]] = Field(default_factory=list)
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


_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def _generate_sample_bars(
    count: int,
    seed: int = 42,
    start: datetime | None = None,
    timeframe: str = "1h",
) -> list[OHLCV]:
    """Generate synthetic OHLCV bars using a random-walk price series."""
    import numpy as np

    rng = np.random.default_rng(seed)
    base_price = 42000.0
    bars: list[OHLCV] = []
    bar_start = start or datetime(2024, 1, 1, tzinfo=UTC)
    interval_minutes = _TIMEFRAME_MINUTES.get(timeframe, 60)

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
            timestamp=bar_start + timedelta(minutes=i * interval_minutes),
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
        # Resolve timeframe early (needed by fresh-fetch and DB query)
        try:
            tf = Timeframe(body.timeframe)
        except ValueError:
            tf = Timeframe.H1
        tf_minutes = _TIMEFRAME_MINUTES.get(body.timeframe, 60)

        # Load strategy config → derive symbol
        strategy_cls, config = _load_strategy_config(body.strategy_id)
        symbol = config.symbols[0] if config.symbols else "BTCUSDT"

        start_dt = datetime.fromisoformat(body.start_date)
        end_dt = datetime.fromisoformat(body.end_date)

        # Optionally fetch fresh exchange data before querying DB
        if body.fetch_fresh and pool is not None:
            try:
                repo = MarketDataRepository.from_pool(pool)
                exchange_id = config.exchange.exchange_id

                def _ccxt_factory() -> Any:
                    import ccxt

                    cls = getattr(ccxt, exchange_id)
                    return cls({"enableRateLimit": True})

                backfill = ExchangeBackfillService(
                    repository=repo,
                    exchange_factories={exchange_id: _ccxt_factory},
                )
                await backfill.bulk_download(
                    exchange_id=exchange_id,
                    symbol=symbol,
                    timeframe=tf,
                    start=start_dt,
                    end=end_dt,
                )
                await backfill.close()
            except Exception:
                logger.warning("Fresh data fetch failed, proceeding with cached", exc_info=True)
        _TASKS[task_id]["progress"] = 20.0

        # Try to load bars from DB
        bars: list[OHLCV] | None = None
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT timestamp, open, high, low, close, volume "
                        "FROM ts.ohlcv_1m "
                        "WHERE symbol = $1 AND timeframe = $2 "
                        "AND timestamp >= $3 AND timestamp <= $4 "
                        "ORDER BY timestamp",
                        symbol,
                        str(tf),
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
            total_minutes = int((end_dt - start_dt).total_seconds() / 60)
            bar_count = max(total_minutes // tf_minutes, 200)
            bars = _generate_sample_bars(
                bar_count,
                seed=hash(task_id) % 2**31,
                start=start_dt,
                timeframe=body.timeframe,
            )

        _TASKS[task_id]["progress"] = 30.0

        def _report_progress(pct: float) -> None:
            # Map runner progress (0-1) into the 30-90 range
            _TASKS[task_id]["progress"] = round(30.0 + pct * 60.0, 1)

        runner = BacktestRunner()

        # Run backtest in a thread with its own event loop so the main
        # FastAPI loop stays responsive for other API requests.
        def _run_in_thread() -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    runner.run(
                        strategy_class=strategy_cls,
                        strategy_config=config,
                        bars=bars,
                        initial_capital=Decimal(str(body.initial_capital)),
                        symbol=symbol,
                        timeframe=tf,
                        on_progress=_report_progress,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)

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

        # Build benchmark equity curve with timestamps from bars
        benchmark_equity_out: list[dict[str, Any]] = []
        for idx, eq in enumerate(result.benchmark_equity):
            if idx < len(bars):
                ts_str = bars[idx].timestamp.isoformat()
            else:
                ts_str = bars[-1].timestamp.isoformat()
            benchmark_equity_out.append({"timestamp": ts_str, "value": float(eq)})

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
                "benchmark_return": round(float(result.benchmark_return) * 100, 2),
                "alpha": round(result.alpha, 4),
                "beta": round(result.beta, 4),
                "max_consecutive_wins": result.max_consecutive_wins,
                "max_consecutive_losses": result.max_consecutive_losses,
                "expectancy": round(float(result.expectancy), 2),
            },
            "equity_curve": equity_curve_out,
            "benchmark_equity": benchmark_equity_out,
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
            "benchmark_equity": r.get("benchmark_equity", []),
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
                        "benchmark_equity": [],
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
            data = _parse_any_strategy_yaml(path)
            if data is not None:
                strategies.append(
                    {
                        "id": data.get("id", path.stem),
                        "name": data.get("name", path.stem),
                    }
                )
    return strategies


# ===========================================================================
# Hyperparameter optimisation
# ===========================================================================

# ---------------------------------------------------------------------------
# In-memory state for hyperopt tasks
# ---------------------------------------------------------------------------

_HYPEROPT_TASKS: dict[str, dict[str, Any]] = {}
_HYPEROPT_RESULTS: dict[str, HyperoptResult] = {}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ParamDefRequest(BaseModel):
    """Wire representation of a single parameter definition."""

    name: str
    type: str  # "int" | "float" | "categorical"
    low: float | None = None
    high: float | None = None
    choices: list[Any] = Field(default_factory=list)


class HyperoptRunRequest(BaseModel):
    strategy_id: str
    param_space: list[ParamDefRequest]
    method: str = "bayesian"  # "bayesian" | "grid"
    max_trials: int = Field(default=50, ge=1, le=500)
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    initial_capital: float = 10000.0


class HyperoptRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class HyperoptProgressResponse(BaseModel):
    task_id: str
    status: str
    completed_trials: int
    total_trials: int
    best_so_far: float | None = None


class TrialRecordResponse(BaseModel):
    trial_number: int
    params: dict[str, Any]
    sharpe: float
    total_return: float
    max_drawdown: float
    total_trades: int


class HyperoptResultResponse(BaseModel):
    task_id: str
    status: str
    best_params: dict[str, Any]
    best_metric: float
    trials: list[TrialRecordResponse]
    total_trials: int
    completed_trials: int


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_hyperopt_task(task_id: str, body: HyperoptRunRequest) -> None:
    """Execute a hyperopt run in the background and store results."""
    _HYPEROPT_TASKS[task_id]["status"] = "running"

    try:
        # Resolve timeframe
        try:
            tf = Timeframe(body.timeframe)
        except ValueError:
            tf = Timeframe.H1

        # Load strategy
        strategy_cls, config = _load_strategy_config(body.strategy_id, body.symbol)

        # Generate synthetic bars for the optimisation (1 year of hourly data)
        bars = _generate_sample_bars(
            count=8760,
            seed=hash(task_id) % 2**31,
            timeframe=body.timeframe,
        )

        # Build parameter space
        param_defs = [
            ParamDef(
                name=p.name,
                type=p.type,  # type: ignore[arg-type]
                low=p.low,
                high=p.high,
                choices=p.choices,
            )
            for p in body.param_space
        ]
        space = ParameterSpace(params=param_defs)

        # Progress callback — update task state after each trial
        async def _on_trial_complete(trial_num: int, completed: int, total: int) -> None:
            _HYPEROPT_TASKS[task_id]["completed_trials"] = completed
            _HYPEROPT_TASKS[task_id]["total_trials"] = total
            # Track best Sharpe seen so far
            results_so_far = _HYPEROPT_RESULTS.get(task_id)
            if results_so_far is not None and results_so_far.trials:
                best = max(r.sharpe for r in results_so_far.trials[:completed])
                _HYPEROPT_TASKS[task_id]["best_so_far"] = best

        # Run in a thread so the backtest coroutines can use their own loop
        def _run_in_thread() -> HyperoptResult:
            loop = asyncio.new_event_loop()
            try:
                runner = HyperoptRunner(BacktestRunner())
                # Use a simple wrapper because the thread loop can't share the
                # main-loop's callbacks; we collect progress after the fact.
                return loop.run_until_complete(
                    runner.run(
                        strategy_class=strategy_cls,
                        base_config=config,
                        bars=bars,
                        initial_capital=Decimal(str(body.initial_capital)),
                        param_space=space,
                        method=body.method,  # type: ignore[arg-type]
                        max_trials=body.max_trials,
                        symbol=body.symbol,
                        timeframe=tf,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)

        _HYPEROPT_RESULTS[task_id] = result
        _HYPEROPT_TASKS[task_id]["status"] = "completed"
        _HYPEROPT_TASKS[task_id]["completed_trials"] = result.completed_trials
        _HYPEROPT_TASKS[task_id]["total_trials"] = result.total_trials
        _HYPEROPT_TASKS[task_id]["best_so_far"] = result.best_metric

    except Exception as exc:
        logger.exception("Hyperopt task %s failed", task_id)
        _HYPEROPT_TASKS[task_id]["status"] = "failed"
        _HYPEROPT_TASKS[task_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/hyperopt",
    response_model=HyperoptRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_hyperopt(body: HyperoptRunRequest, request: Request) -> dict[str, Any]:
    """Start a hyperparameter optimisation run.

    Returns a ``task_id`` to poll for progress and results.
    """
    task_id = f"ho-{uuid.uuid4().hex[:8]}"
    _HYPEROPT_TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "completed_trials": 0,
        "total_trials": body.max_trials,
        "best_so_far": None,
        "request": body.model_dump(),
    }

    task = asyncio.create_task(_run_hyperopt_task(task_id, body))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {"task_id": task_id, "status": "queued"}


@router.get(
    "/hyperopt/{task_id}",
    response_model=HyperoptProgressResponse,
)
async def get_hyperopt_progress(task_id: str) -> dict[str, Any]:
    """Poll progress of a running hyperopt task."""
    if task_id not in _HYPEROPT_TASKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hyperopt task {task_id} not found",
        )
    t = _HYPEROPT_TASKS[task_id]
    return {
        "task_id": task_id,
        "status": t["status"],
        "completed_trials": t["completed_trials"],
        "total_trials": t["total_trials"],
        "best_so_far": t.get("best_so_far"),
    }


@router.get(
    "/hyperopt/{task_id}/results",
    response_model=HyperoptResultResponse,
)
async def get_hyperopt_results(task_id: str) -> dict[str, Any]:
    """Return the full result of a completed hyperopt run."""
    if task_id not in _HYPEROPT_TASKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hyperopt task {task_id} not found",
        )
    t = _HYPEROPT_TASKS[task_id]
    if t["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Hyperopt task {task_id} is not completed (status: {t['status']})",
        )
    result = _HYPEROPT_RESULTS.get(task_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No result stored for hyperopt task {task_id}",
        )
    return {
        "task_id": task_id,
        "status": t["status"],
        "best_params": result.best_params,
        "best_metric": result.best_metric,
        "trials": [
            {
                "trial_number": r.trial_number,
                "params": r.params,
                "sharpe": r.sharpe,
                "total_return": r.total_return,
                "max_drawdown": r.max_drawdown,
                "total_trades": r.total_trades,
            }
            for r in result.trials
        ],
        "total_trials": result.total_trials,
        "completed_trials": result.completed_trials,
    }


# ===========================================================================
# Walk-forward validation
# ===========================================================================

# ---------------------------------------------------------------------------
# In-memory state for walk-forward tasks
# ---------------------------------------------------------------------------

_WALKFORWARD_TASKS: dict[str, dict[str, Any]] = {}
_WALKFORWARD_RESULTS: dict[str, WalkForwardResult] = {}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class WalkForwardRunRequest(BaseModel):
    strategy_id: str
    fold_count: int = Field(default=6, ge=2, le=24)
    train_pct: float = Field(default=0.75, gt=0.0, lt=1.0)
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    initial_capital: float = 10000.0


class WalkForwardRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class FoldMetricsResponse(BaseModel):
    fold_index: int
    in_sample_sharpe: float
    in_sample_return: float
    in_sample_drawdown: float
    in_sample_trades: int
    out_of_sample_sharpe: float
    out_of_sample_return: float
    out_of_sample_drawdown: float
    out_of_sample_trades: int


class WalkForwardResultResponse(BaseModel):
    task_id: str
    status: str
    folds: list[FoldMetricsResponse]
    aggregated_sharpe: float | None
    aggregated_return: float | None
    aggregated_drawdown: float | None
    out_of_sample_equity: list[float]


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_walkforward_task(task_id: str, body: WalkForwardRunRequest) -> None:
    """Execute a walk-forward analysis in the background."""
    _WALKFORWARD_TASKS[task_id]["status"] = "running"

    try:
        try:
            tf = Timeframe(body.timeframe)
        except ValueError:
            tf = Timeframe.H1

        # Derive in-sample / out-of-sample month split from fold_count and train_pct.
        # Use 24 months of total data as a sensible default window.
        total_months = 24
        in_sample_months = max(1, round(total_months * body.train_pct / body.fold_count))
        out_of_sample_months = max(1, round(total_months * (1 - body.train_pct) / body.fold_count))

        strategy_cls, config = _load_strategy_config(body.strategy_id, body.symbol)

        # 2 years of bars at the requested timeframe
        tf_minutes = _TIMEFRAME_MINUTES.get(body.timeframe, 60)
        bar_count = (2 * 365 * 24 * 60) // tf_minutes
        bars = _generate_sample_bars(
            count=bar_count,
            seed=hash(task_id) % 2**31,
            timeframe=body.timeframe,
        )

        def _run_in_thread() -> WalkForwardResult:
            loop = asyncio.new_event_loop()
            try:
                analyzer = WalkForwardAnalyzer(initial_capital=Decimal(str(body.initial_capital)))
                return loop.run_until_complete(
                    analyzer.run_walkforward(
                        bars=bars,
                        strategy_class=strategy_cls,
                        strategy_config=config,
                        in_sample_months=in_sample_months,
                        out_of_sample_months=out_of_sample_months,
                        symbol=body.symbol,
                        timeframe=tf,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)

        _WALKFORWARD_RESULTS[task_id] = result
        _WALKFORWARD_TASKS[task_id]["status"] = "completed"
        _WALKFORWARD_TASKS[task_id]["fold_count"] = len(result.folds)

    except Exception as exc:
        logger.exception("Walk-forward task %s failed", task_id)
        _WALKFORWARD_TASKS[task_id]["status"] = "failed"
        _WALKFORWARD_TASKS[task_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/walkforward",
    response_model=WalkForwardRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_walkforward(body: WalkForwardRunRequest, request: Request) -> dict[str, Any]:
    """Start a walk-forward validation run.

    Returns a ``task_id`` to poll for results.
    """
    task_id = f"wf-{uuid.uuid4().hex[:8]}"
    _WALKFORWARD_TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "fold_count": 0,
        "request": body.model_dump(),
    }

    task = asyncio.create_task(_run_walkforward_task(task_id, body))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {"task_id": task_id, "status": "queued"}


@router.get(
    "/walkforward/{task_id}/results",
    response_model=WalkForwardResultResponse,
)
async def get_walkforward_results(task_id: str) -> dict[str, Any]:
    """Return fold metrics and aggregated OOS equity for a completed walk-forward run."""
    if task_id not in _WALKFORWARD_TASKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Walk-forward task {task_id} not found",
        )
    t = _WALKFORWARD_TASKS[task_id]
    if t["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Walk-forward task {task_id} is not completed (status: {t['status']})",
        )
    result = _WALKFORWARD_RESULTS.get(task_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No result stored for walk-forward task {task_id}",
        )

    folds_out: list[dict[str, Any]] = [
        {
            "fold_index": fold.fold_index,
            "in_sample_sharpe": fold.in_sample_metrics.sharpe_ratio,
            "in_sample_return": float(fold.in_sample_metrics.total_return),
            "in_sample_drawdown": float(fold.in_sample_metrics.max_drawdown),
            "in_sample_trades": fold.in_sample_metrics.total_trades,
            "out_of_sample_sharpe": fold.out_of_sample_metrics.sharpe_ratio,
            "out_of_sample_return": float(fold.out_of_sample_metrics.total_return),
            "out_of_sample_drawdown": float(fold.out_of_sample_metrics.max_drawdown),
            "out_of_sample_trades": fold.out_of_sample_metrics.total_trades,
        }
        for fold in result.folds
    ]

    agg = result.aggregated_oos_metrics
    oos_equity: list[float] = []
    if agg is not None:
        oos_equity = [float(eq) for eq in agg.equity_curve]

    return {
        "task_id": task_id,
        "status": t["status"],
        "folds": folds_out,
        "aggregated_sharpe": agg.sharpe_ratio if agg is not None else None,
        "aggregated_return": float(agg.total_return) if agg is not None else None,
        "aggregated_drawdown": float(agg.max_drawdown) if agg is not None else None,
        "out_of_sample_equity": oos_equity,
    }
