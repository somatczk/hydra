"""Backtest runs and results: pre-flight checks, in-process tasks, result reads and sharing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from hydra.backtest.data import no_market_data
from hydra.backtest.jobs import (
    LIST_DETAILS_SQL,
    LIST_SUMMARIES_SQL,
    ONE_RESULT_SQL,
    RENAME_SQL,
    BacktestRunRequest,
    InvalidStrategyError,
    Preflight,
    detail_from_row,
    fail_task,
    fetch_fresh_bars,
    import_backtest_strategy,
    load_task_bars,
    persist_result,
    result_record,
    summary_from_row,
)
from hydra.backtest.runner import BacktestRunner
from hydra.core.types import MarketType, Timeframe
from hydra.data.storage import MarketDataRepository
from hydra.services import strategies as strategy_files
from hydra.services.errors import ServiceError, db_errors, require_pool, service_error
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BacktestRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class BacktestStatus(BaseModel):
    task_id: str
    status: str  # queued | running | completed | failed
    progress: float = 0.0  # 0-100
    error: str | None = None
    result_id: str | None = None
    fetch_error: str | None = None


class DataProvenanceModel(BaseModel):
    exchange: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    bar_count: int | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    gap_count: int | None = None
    requested_start: str | None = None
    requested_end: str | None = None
    data_source: str | None = None


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
    benchmark_return: float | None = None
    alpha: float | None = None
    beta: float | None = None
    max_consecutive_wins: int | None = None
    max_consecutive_losses: int | None = None
    expectancy: float | None = None
    total_return: float | None = None
    annualized_return: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    profit_factor: float | None = None
    max_drawdown_duration_s: int | None = None


class BacktestResultSummary(BaseModel):
    id: str
    strategy: str
    period: str
    status: str
    name: str = ""
    strategy_id: str | None = None
    initial_capital: float | None = None
    provenance: DataProvenanceModel | None = None
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)


class BacktestResultDetail(BaseModel):
    id: str
    strategy: str
    period: str
    status: str
    name: str = ""
    strategy_id: str | None = None
    initial_capital: float | None = None
    provenance: DataProvenanceModel | None = None
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
    primary_timeframe: str | None = None


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_TASKS: dict[str, dict[str, Any]] = {}
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
_RESULTS: dict[str, dict[str, Any]] = {}


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def populate_cache_from_db(pool: Any) -> None:
    """Load backtest results from DB into the in-memory cache on startup."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(LIST_DETAILS_SQL)
            trade_rows = await conn.fetch(
                "SELECT backtest_id, entry_time, exit_time, side, entry_price, exit_price, pnl "
                "FROM backtest_result_trades ORDER BY backtest_id, entry_time, id"
            )
    except Exception:
        logger.exception("Failed to populate backtest cache from DB")
        return
    trades_by_id: dict[str, list[Any]] = {}
    for t in trade_rows:
        trades_by_id.setdefault(t["backtest_id"], []).append(t)
    for row in rows:
        _RESULTS[row["id"]] = detail_from_row(row, trades_by_id.get(row["id"], []))


# ---------------------------------------------------------------------------
# Strategy loading and pre-flight checks
# ---------------------------------------------------------------------------


def _invalid_strategy(strategy_id: str, reason: str) -> ServiceError:
    return service_error(
        422,
        "invalid_strategy",
        f"Strategy {strategy_id}: {reason}",
        "invalid",
        strategy_id=strategy_id,
    )


def _load_backtest_strategy(strategy_id: str) -> tuple[type[BaseStrategy], StrategyConfig]:
    """Resolve a strategy YAML into its class and full config; 404 unknown, 422 unusable."""
    path = strategy_files.find_strategy_file(strategy_id)
    if path is None:
        raise service_error(
            404,
            "strategy_not_found",
            f"Strategy {strategy_id} not found",
            "not_found",
            strategy_id=strategy_id,
        )
    data = strategy_files.parse_strategy_file(path)
    if data is None:
        raise _invalid_strategy(strategy_id, "config file is not a YAML mapping")
    try:
        config = StrategyConfig(**data)
    except (ValidationError, TypeError) as exc:
        raise _invalid_strategy(strategy_id, f"invalid config: {exc}") from exc
    if not config.symbols:
        raise _invalid_strategy(strategy_id, "config lists no symbols")
    if config.exchange.market_type == MarketType.FUTURES:
        raise _invalid_strategy(
            strategy_id, "futures backtests are not supported until perpetuals support lands"
        )
    try:
        return import_backtest_strategy(config.strategy_class), config
    except InvalidStrategyError as exc:
        raise _invalid_strategy(strategy_id, str(exc)) from exc


async def _preflight(
    pool: Any,
    strategy_id: str,
    timeframe: Timeframe | None,
    start: datetime,
    end: datetime,
    *,
    require_data: bool = True,
) -> Preflight:
    """Validate a job before it is queued: 404 strategy, 422 config/data, 503 database."""
    strategy_cls, config = _load_backtest_strategy(strategy_id)
    tf = timeframe or config.timeframes.primary
    if tf != config.timeframes.primary:
        config = config.model_copy(
            update={"timeframes": config.timeframes.model_copy(update={"primary": tf})}
        )
    pool = require_pool(pool)
    exchange = config.exchange.exchange_id
    symbol = config.symbols[0]
    stored = None
    if require_data:
        repo = MarketDataRepository.from_pool(pool)
        async with db_errors("checking stored market data"):
            stored = await repo.get_bar_range(exchange, symbol, tf, start, end)
            if stored is None:
                missing = await no_market_data(repo, exchange, symbol, tf, start, end)
                raise ServiceError(422, missing.to_detail())
    return Preflight(strategy_cls, config, exchange, symbol, tf, pool, stored)


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_backtest_task(task_id: str, body: BacktestRunRequest, pf: Preflight) -> None:
    """Execute a backtest on stored bars and persist it; any failure is recorded on the task."""
    task = _TASKS[task_id]
    task["status"] = "running"
    task["progress"] = 10.0

    try:
        if body.fetch_fresh:
            await fetch_fresh_bars(task, pf, body)
        task["progress"] = 20.0

        loaded = await load_task_bars(task, pf, body.start_date, body.end_date)
        if loaded is None:
            return
        bars, provenance = loaded
        task["progress"] = 30.0

        def _report_progress(pct: float) -> None:
            task["progress"] = round(30.0 + pct * 60.0, 1)

        runner = BacktestRunner()
        initial = Decimal(str(body.initial_capital))

        def _run_in_thread() -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    runner.run(
                        strategy_class=pf.strategy_cls,
                        strategy_config=pf.config,
                        bars=bars,
                        initial_capital=initial,
                        symbol=pf.symbol,
                        timeframe=pf.timeframe,
                        on_progress=_report_progress,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)
        task["progress"] = 90.0

        result_id = f"bt-{task_id}"
        stored = result_record(result_id, body, pf, result, provenance)

        try:
            await persist_result(pf.pool, stored, result.trades, provenance)
        except Exception as exc:
            logger.exception("Backtest %s result not persisted", task_id)
            fail_task(task, f"result not persisted: {exc}")
            return

        _RESULTS[result_id] = stored
        task["status"] = "completed"
        task["progress"] = 100.0
        task["result_id"] = result_id

    except Exception as exc:
        logger.exception("Backtest task %s failed", task_id)
        fail_task(task, str(exc))


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def submit_backtest(pool: Any, body: BacktestRunRequest) -> dict[str, Any]:
    """Start a backtest on stored bars. Returns a task_id to poll for status."""
    pf = await _preflight(
        pool,
        body.strategy_id,
        body.timeframe,
        body.start_date,
        body.end_date,
        require_data=not body.fetch_fresh,
    )
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    _TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "progress": 0.0,
        "request": body.model_dump(mode="json"),
    }
    _spawn(_run_backtest_task(task_id, body, pf))
    return {"task_id": task_id, "status": "queued"}


def get_backtest_status(task_id: str) -> dict[str, Any]:
    """Check the progress of a running backtest."""
    if task_id not in _TASKS:
        raise service_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
    task = _TASKS[task_id]
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "error": task.get("error"),
        "result_id": task.get("result_id"),
        "fetch_error": task.get("fetch_error"),
    }


async def list_results(pool: Any) -> list[dict[str, Any]]:
    """List all persisted backtest result summaries."""
    pool = require_pool(pool)
    async with db_errors("listing backtest results"), pool.acquire() as conn:
        rows = await conn.fetch(LIST_SUMMARIES_SQL)
    return [summary_from_row(row) for row in rows]


async def get_result(pool: Any, result_id: str) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
    if result_id in _RESULTS:
        return _RESULTS[result_id]

    pool = require_pool(pool)
    async with db_errors("loading a backtest result"), pool.acquire() as conn:
        row = await conn.fetchrow(ONE_RESULT_SQL, result_id)
        trade_rows = (
            await conn.fetch(
                "SELECT entry_time, exit_time, side, entry_price, exit_price, pnl "
                "FROM backtest_result_trades WHERE backtest_id = $1 ORDER BY entry_time, id",
                result_id,
            )
            if row is not None
            else []
        )
    if row is None:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")
    return detail_from_row(row, trade_rows)


async def rename_result(pool: Any, result_id: str, name: str) -> dict[str, Any]:
    """Rename a backtest result in the database, then in the cache."""
    pool = require_pool(pool)
    async with db_errors("renaming a backtest result"), pool.acquire() as conn:
        row = await conn.fetchrow(RENAME_SQL, name, result_id)
    if row is None:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")
    if result_id in _RESULTS:
        _RESULTS[result_id]["name"] = name
    return summary_from_row(row)


async def delete_result(pool: Any, result_id: str) -> None:
    """Delete a backtest result from the database, then evict it from the cache."""
    pool = require_pool(pool)
    async with db_errors("deleting a backtest result"), pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM backtest_results WHERE id = $1 RETURNING id", result_id
        )
    if deleted is None:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")
    _RESULTS.pop(result_id, None)


async def verify_result(pool: Any, result_id: str) -> dict[str, Any]:
    """Verify a backtest result by recomputing metrics from trades."""
    # Reuse detail fetch logic
    detail = await get_result(pool, result_id)

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


def list_backtest_strategies() -> list[dict[str, Any]]:
    """List strategies available for backtesting, with their primary timeframe."""
    strategies: list[dict[str, Any]] = []
    for path, data in strategy_files.iter_strategy_yamls():
        timeframes = data.get("timeframes")
        primary = timeframes.get("primary") if isinstance(timeframes, dict) else None
        strategies.append(
            {
                "id": data.get("id", path.stem),
                "name": data.get("name", path.stem),
                "primary_timeframe": str(primary) if primary is not None else None,
            }
        )
    return strategies


# ---------------------------------------------------------------------------
# Backtest result sharing
# ---------------------------------------------------------------------------

_SHARED_TOKENS: dict[str, str] = {}  # token → result_id


async def share_result(pool: Any, result_id: str) -> dict[str, str]:
    """Generate a public sharing token for a backtest result."""
    pool = require_pool(pool)
    async with db_errors("checking the backtest result"), pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM backtest_results WHERE id = $1", result_id)
    if not exists:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")

    token = uuid.uuid4().hex[:12]
    _SHARED_TOKENS[token] = result_id
    return {"token": token, "share_url": f"/api/backtest/shared/{token}"}


async def get_shared_result(pool: Any, token: str) -> dict[str, Any]:
    """Public read-only access to a shared backtest result (no auth required)."""
    result_id = _SHARED_TOKENS.get(token)
    if result_id is None:
        raise ServiceError(404, "Shared link not found or expired")
    return await get_result(pool, result_id)
