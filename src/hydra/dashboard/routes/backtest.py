from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Self

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from hydra.backtest.data import (
    DataProvenance,
    InvalidMarketDataError,
    NoMarketDataError,
    load_bars,
    no_market_data,
)
from hydra.backtest.hyperopt import HyperoptResult, HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.metrics import Trade
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import WalkForwardAnalyzer, WalkForwardResult
from hydra.core.types import ExchangeId, MarketType, Timeframe
from hydra.dashboard.routes import strategies as strategy_files
from hydra.dashboard.routes._db import api_error, db_errors_to_503, require_pool
from hydra.data.backfill import ExchangeBackfillService
from hydra.data.storage import MarketDataRepository
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ---------------------------------------------------------------------------
# Request date handling
# ---------------------------------------------------------------------------

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_bound(value: Any, *, end_of_day: bool) -> Any:
    """A bare ``YYYY-MM-DD`` is that whole UTC day: midnight for start, 23:59:59.999999 for end."""
    if isinstance(value, str) and _DATE_ONLY.match(value.strip()):
        day = date.fromisoformat(value.strip())
        return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _DateRange(BaseModel):
    start_date: datetime
    end_date: datetime

    @field_validator("start_date", mode="before")
    @classmethod
    def _start_bound(cls, value: Any) -> Any:
        return _day_bound(value, end_of_day=False)

    @field_validator("end_date", mode="before")
    @classmethod
    def _end_bound(cls, value: Any) -> Any:
        return _day_bound(value, end_of_day=True)

    @field_validator("start_date", "end_date", mode="after")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start_date >= self.end_date:
            msg = "start_date must be before end_date"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BacktestRunRequest(_DateRange):
    strategy_id: str
    initial_capital: float = Field(default=10000.0, gt=0)
    name: str = ""
    timeframe: Timeframe | None = None
    fetch_fresh: bool = False


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


def _fail(task: dict[str, Any], error: str) -> None:
    task["status"] = "failed"
    task["progress"] = 0.0
    task["error"] = error


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------

_LIST_RESULTS_SQL = (
    "SELECT id, strategy, period, status, total_trades, win_rate, total_pnl, max_drawdown, "
    "sharpe_ratio, equity_curve, name, transactions, strategy_id, initial_capital, "
    "data_source, data_exchange, data_symbol, data_timeframe, bar_count, first_ts, "
    "last_ts, gap_count, requested_start, requested_end "
    "FROM backtest_results ORDER BY created_at DESC"
)
_ONE_RESULT_SQL = (
    "SELECT id, strategy, period, status, total_trades, win_rate, total_pnl, max_drawdown, "
    "sharpe_ratio, equity_curve, name, transactions, strategy_id, initial_capital, "
    "data_source, data_exchange, data_symbol, data_timeframe, bar_count, first_ts, "
    "last_ts, gap_count, requested_start, requested_end "
    "FROM backtest_results WHERE id = $1"
)


async def _persist_result_to_db(
    pool: Any,
    result: dict[str, Any],
    trades: list[Trade],
    provenance: DataProvenance,
) -> None:
    """Write the result row and its trades in one transaction; raise on any failure."""
    metrics = result["metrics"]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO backtest_results
                   (id, strategy, period, status, total_trades, win_rate,
                    total_pnl, max_drawdown, sharpe_ratio, equity_curve, name,
                    transactions, strategy_id, initial_capital, data_source,
                    data_exchange, data_symbol, data_timeframe, bar_count,
                    first_ts, last_ts, gap_count, requested_start, requested_end)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11,
                       $12::jsonb, $13, $14, $15, $16, $17, $18, $19, $20, $21,
                       $22, $23, $24)
               ON CONFLICT (id) DO UPDATE SET
                   status = EXCLUDED.status,
                   total_trades = EXCLUDED.total_trades,
                   win_rate = EXCLUDED.win_rate,
                   total_pnl = EXCLUDED.total_pnl,
                   max_drawdown = EXCLUDED.max_drawdown,
                   sharpe_ratio = EXCLUDED.sharpe_ratio,
                   equity_curve = EXCLUDED.equity_curve,
                   name = EXCLUDED.name,
                   transactions = EXCLUDED.transactions,
                   strategy_id = EXCLUDED.strategy_id,
                   initial_capital = EXCLUDED.initial_capital,
                   data_source = EXCLUDED.data_source,
                   data_exchange = EXCLUDED.data_exchange,
                   data_symbol = EXCLUDED.data_symbol,
                   data_timeframe = EXCLUDED.data_timeframe,
                   bar_count = EXCLUDED.bar_count,
                   first_ts = EXCLUDED.first_ts,
                   last_ts = EXCLUDED.last_ts,
                   gap_count = EXCLUDED.gap_count,
                   requested_start = EXCLUDED.requested_start,
                   requested_end = EXCLUDED.requested_end""",
            result["id"],
            result["strategy"],
            result["period"],
            result["status"],
            metrics["total_trades"],
            metrics["win_rate"],
            metrics["total_pnl"],
            metrics["max_drawdown"],
            metrics["sharpe_ratio"],
            json.dumps(result["equity_curve"]),
            result["name"],
            json.dumps(result["transactions"]),
            result["strategy_id"],
            result["initial_capital"],
            provenance.data_source,
            provenance.exchange,
            provenance.symbol,
            str(provenance.timeframe),
            provenance.bar_count,
            provenance.first_ts,
            provenance.last_ts,
            provenance.gap_count,
            provenance.requested_start,
            provenance.requested_end,
        )
        await conn.execute(
            "DELETE FROM backtest_result_trades WHERE backtest_id = $1", result["id"]
        )
        if trades:
            await conn.executemany(
                """INSERT INTO backtest_result_trades
                       (backtest_id, entry_time, exit_time, side,
                        entry_price, exit_price, pnl)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                [
                    (
                        result["id"],
                        t.entry_time,
                        t.exit_time,
                        t.direction,
                        float(t.entry_price),
                        float(t.exit_price),
                        float(t.pnl),
                    )
                    for t in trades
                ],
            )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _provenance_from_row(row: Any) -> dict[str, Any] | None:
    if row["data_source"] is None:
        return None
    return {
        "exchange": row["data_exchange"],
        "symbol": row["data_symbol"],
        "timeframe": row["data_timeframe"],
        "bar_count": row["bar_count"],
        "first_ts": _iso(row["first_ts"]),
        "last_ts": _iso(row["last_ts"]),
        "gap_count": row["gap_count"],
        "requested_start": _iso(row["requested_start"]),
        "requested_end": _iso(row["requested_end"]),
        "data_source": row["data_source"],
    }


def _json_column(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _summary_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "strategy": row["strategy"],
        "period": row["period"],
        "status": row["status"],
        "name": row["name"],
        "strategy_id": row["strategy_id"],
        "initial_capital": row["initial_capital"],
        "provenance": _provenance_from_row(row),
        "metrics": {
            "total_trades": row["total_trades"],
            "win_rate": row["win_rate"],
            "total_pnl": row["total_pnl"],
            "max_drawdown": row["max_drawdown"],
            "sharpe_ratio": row["sharpe_ratio"],
        },
    }


def _trade_from_row(t: Any) -> dict[str, Any]:
    return {
        "entry_time": t["entry_time"].isoformat(),
        "exit_time": t["exit_time"].isoformat(),
        "side": t["side"],
        "entry_price": t["entry_price"],
        "exit_price": t["exit_price"],
        "pnl": t["pnl"],
    }


def _detail_from_row(row: Any, trade_rows: list[Any]) -> dict[str, Any]:
    return {
        **_summary_from_row(row),
        "equity_curve": _json_column(row["equity_curve"]),
        "benchmark_equity": [],
        "trades": [_trade_from_row(t) for t in trade_rows],
        "transactions": _json_column(row["transactions"]),
    }


async def populate_cache_from_db(pool: Any) -> None:
    """Load backtest results from DB into the in-memory cache on startup."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_LIST_RESULTS_SQL)
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
        _RESULTS[row["id"]] = _detail_from_row(row, trades_by_id.get(row["id"], []))


# ---------------------------------------------------------------------------
# Strategy loading and pre-flight checks
# ---------------------------------------------------------------------------

_BACKTESTABLE_PACKAGE = "hydra.strategy."


def _invalid_strategy(strategy_id: str, reason: str) -> HTTPException:
    return api_error(
        422,
        "invalid_strategy",
        f"Strategy {strategy_id}: {reason}",
        "invalid",
        strategy_id=strategy_id,
    )


def _import_strategy_class(strategy_id: str, dotted_path: str) -> type[BaseStrategy]:
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path.startswith(_BACKTESTABLE_PACKAGE):
        raise _invalid_strategy(
            strategy_id, f"strategy class {dotted_path} must live under {_BACKTESTABLE_PACKAGE}"
        )
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
    except (ImportError, AttributeError) as exc:
        raise _invalid_strategy(strategy_id, f"cannot import strategy class {dotted_path}") from exc
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        raise _invalid_strategy(
            strategy_id, f"strategy class {dotted_path} is not supported by the backtester"
        )
    return cls


def _load_backtest_strategy(strategy_id: str) -> tuple[type[BaseStrategy], StrategyConfig]:
    """Resolve a strategy YAML into its class and full config; 404 unknown, 422 unusable."""
    path = strategy_files.find_strategy_file(strategy_id)
    if path is None:
        raise api_error(
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
    return _import_strategy_class(strategy_id, config.strategy_class), config


@dataclass(frozen=True, slots=True)
class _Preflight:
    strategy_cls: type[BaseStrategy]
    config: StrategyConfig
    exchange: ExchangeId
    symbol: str
    timeframe: Timeframe
    pool: Any
    stored: tuple[datetime, datetime, int] | None


async def _preflight(
    request: Request,
    strategy_id: str,
    timeframe: Timeframe | None,
    start: datetime,
    end: datetime,
    *,
    require_data: bool = True,
) -> _Preflight:
    """Validate a job before it is queued: 404 strategy, 422 config/data, 503 database."""
    strategy_cls, config = _load_backtest_strategy(strategy_id)
    tf = timeframe or config.timeframes.primary
    if tf != config.timeframes.primary:
        config = config.model_copy(
            update={"timeframes": config.timeframes.model_copy(update={"primary": tf})}
        )
    pool = require_pool(request)
    exchange = config.exchange.exchange_id
    symbol = config.symbols[0]
    stored = None
    if require_data:
        repo = MarketDataRepository.from_pool(pool)
        async with db_errors_to_503("checking stored market data"):
            stored = await repo.get_bar_range(exchange, symbol, tf, start, end)
            if stored is None:
                missing = await no_market_data(repo, exchange, symbol, tf, start, end)
                raise HTTPException(status_code=422, detail=missing.to_detail())
    return _Preflight(strategy_cls, config, exchange, symbol, tf, pool, stored)


async def _load_task_bars(
    task: dict[str, Any], pf: _Preflight, start: datetime, end: datetime
) -> tuple[list[Any], DataProvenance] | None:
    """Load the job's bars from the DB; on missing/invalid data fail the task and return None."""
    repo = MarketDataRepository.from_pool(pf.pool)
    try:
        return await load_bars(repo, pf.exchange, pf.symbol, pf.timeframe, start, end)
    except (NoMarketDataError, InvalidMarketDataError) as exc:
        _fail(task, str(exc))
        return None


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _fetch_fresh_bars(task: dict[str, Any], pf: _Preflight, body: BacktestRunRequest) -> None:
    exchange_id = pf.exchange

    def _ccxt_factory() -> Any:
        import ccxt

        cls = getattr(ccxt, exchange_id)
        return cls({"enableRateLimit": True})

    try:
        backfill = ExchangeBackfillService(
            repository=MarketDataRepository.from_pool(pf.pool),
            exchange_factories={exchange_id: _ccxt_factory},
        )
        await backfill.bulk_download(
            exchange_id=exchange_id,
            symbol=pf.symbol,
            timeframe=pf.timeframe,
            start=body.start_date,
            end=body.end_date,
        )
        await backfill.close()
    except Exception as exc:
        logger.warning("Fresh data fetch failed, using stored bars", exc_info=True)
        task["fetch_error"] = str(exc)


def _timestamped(values: list[Decimal], bars: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, value in enumerate(values):
        bar = bars[idx] if idx < len(bars) else bars[-1]
        out.append({"timestamp": bar.timestamp.isoformat(), "value": float(value)})
    return out


async def _run_backtest_task(task_id: str, body: BacktestRunRequest, pf: _Preflight) -> None:
    """Execute a backtest on stored bars and persist it; any failure is recorded on the task."""
    task = _TASKS[task_id]
    task["status"] = "running"
    task["progress"] = 10.0

    try:
        if body.fetch_fresh:
            await _fetch_fresh_bars(task, pf, body)
        task["progress"] = 20.0

        loaded = await _load_task_bars(task, pf, body.start_date, body.end_date)
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

        final_equity = result.equity_curve[-1] if result.equity_curve else initial
        result_id = f"bt-{task_id}"
        stored: dict[str, Any] = {
            "id": result_id,
            "strategy": pf.config.name,
            "strategy_id": body.strategy_id,
            "initial_capital": body.initial_capital,
            "period": f"{provenance.first_ts.isoformat()} - {provenance.last_ts.isoformat()}",
            "status": "completed",
            "name": body.name,
            "provenance": provenance.to_dict(),
            "stopped_reason": result.stopped_reason,
            "metrics": {
                "total_trades": result.total_trades,
                "win_rate": round(float(result.win_rate) * 100, 2),
                "total_pnl": round(float(final_equity - initial), 2),
                "max_drawdown": round(float(result.max_drawdown) * 100, 2),
                "sharpe_ratio": round(result.sharpe_ratio, 2),
                "benchmark_return": round(float(result.benchmark_return) * 100, 2),
                "alpha": round(result.alpha, 4),
                "beta": round(result.beta, 4),
                "max_consecutive_wins": result.max_consecutive_wins,
                "max_consecutive_losses": result.max_consecutive_losses,
                "expectancy": round(float(result.expectancy), 2),
            },
            "equity_curve": _timestamped(result.equity_curve, bars),
            "benchmark_equity": _timestamped(result.benchmark_equity, bars),
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

        try:
            await _persist_result_to_db(pf.pool, stored, result.trades, provenance)
        except Exception as exc:
            logger.exception("Backtest %s result not persisted", task_id)
            _fail(task, f"result not persisted: {exc}")
            return

        _RESULTS[result_id] = stored
        task["status"] = "completed"
        task["progress"] = 100.0
        task["result_id"] = result_id

    except Exception as exc:
        logger.exception("Backtest task %s failed", task_id)
        _fail(task, str(exc))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/run", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(body: BacktestRunRequest, request: Request) -> dict[str, Any]:
    """Start a backtest on stored bars. Returns a task_id to poll for status."""
    pf = await _preflight(
        request,
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


@router.get("/status/{task_id}", response_model=BacktestStatus)
async def get_backtest_status(task_id: str) -> dict[str, Any]:
    """Check the progress of a running backtest."""
    if task_id not in _TASKS:
        raise api_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
    task = _TASKS[task_id]
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "error": task.get("error"),
        "result_id": task.get("result_id"),
        "fetch_error": task.get("fetch_error"),
    }


@router.get("/results", response_model=list[BacktestResultSummary])
async def list_results(request: Request) -> list[dict[str, Any]]:
    """List all persisted backtest result summaries."""
    pool = require_pool(request)
    async with db_errors_to_503("listing backtest results"), pool.acquire() as conn:
        rows = await conn.fetch(_LIST_RESULTS_SQL)
    return [_summary_from_row(row) for row in rows]


@router.get("/results/{result_id}", response_model=BacktestResultDetail)
async def get_result_detail(result_id: str, request: Request) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
    if result_id in _RESULTS:
        return _RESULTS[result_id]

    pool = require_pool(request)
    async with db_errors_to_503("loading a backtest result"), pool.acquire() as conn:
        row = await conn.fetchrow(_ONE_RESULT_SQL, result_id)
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
        raise api_error(404, "result_not_found", f"Result {result_id} not found", "not_found")
    return _detail_from_row(row, trade_rows)


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

    return _RESULTS[result_id]


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
async def list_backtest_strategies() -> list[dict[str, Any]]:
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
# Strategy optimizable parameter extraction
# ---------------------------------------------------------------------------


class StrategyParamDef(BaseModel):
    """A single optimizable parameter extracted from a strategy's conditions."""

    name: str
    label: str
    type: str  # "int" | "float"
    default: float
    min: float
    max: float
    step: float


def _humanize_label(name: str) -> str:
    """Convert ``sma_period`` to ``SMA Period``."""
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in name.split("_"))


def _infer_type(value: object) -> str:
    if isinstance(value, int):
        return "int"
    if isinstance(value, float) and value == int(value):
        return "int"
    return "float"


def _suggest_range(param_name: str, default: float, param_type: str) -> tuple[float, float, float]:
    """Return (min, max, step) using indicator registry constraints or heuristic."""
    from hydra.strategy.indicator_registry import _PARAM_CONSTRAINTS

    constraints = _PARAM_CONSTRAINTS.get(param_name)
    if constraints:
        lo = float(constraints["min"])  # type: ignore[arg-type]
        hi = float(constraints["max"])  # type: ignore[arg-type]
    else:
        lo = max(0.1 if param_type == "float" else 1, round(default * 0.25))
        hi = max(round(default * 4), round(default + 10))
    step = max(1 if param_type == "int" else 0.1, round((hi - lo) / 20, 2))
    return lo, hi, step


def _parse_value_ref(value_str: str) -> tuple[str, dict[str, str]]:
    """Parse ``'sma:period=50'`` into ``('sma', {'period': '50'})``."""
    parts = value_str.split(":", maxsplit=1)
    if len(parts) < 2:
        return parts[0], {}
    embedded: dict[str, str] = {}
    for kv in parts[1].split(","):
        k, _, v = kv.partition("=")
        embedded[k.strip()] = v.strip()
    return parts[0], embedded


def _extract_optimizable_params(rules_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk all conditions and extract optimizable params."""
    seen: dict[str, dict[str, Any]] = {}

    for group_name in ("entry_long", "exit_long", "entry_short", "exit_short"):
        group = rules_dict.get(group_name)
        if not group or not group.get("conditions"):
            continue
        for cond in group["conditions"]:
            indicator = cond.get("indicator", "")
            params = cond.get("params", {})
            value = cond.get("value")
            param_key = cond.get("param_key")
            value_param_key = cond.get("value_param_key")
            value_ref_overrides = cond.get("value_ref_overrides")

            # 1. param_key → indicator params
            if param_key and params:
                primary_key = next(iter(params))
                default = params[primary_key]
                if param_key not in seen:
                    ptype = _infer_type(default)
                    lo, hi, step = _suggest_range(primary_key, float(default), ptype)
                    seen[param_key] = {
                        "name": param_key,
                        "label": _humanize_label(param_key),
                        "type": ptype,
                        "default": float(default),
                        "min": lo,
                        "max": hi,
                        "step": step,
                    }

            # 2. value_param_key → numeric threshold
            if value_param_key and isinstance(value, (int, float)) and value_param_key not in seen:
                ptype = _infer_type(value)
                default_val = float(value)
                lo = max(0, round(default_val * 0.25))
                hi = round(default_val * 3) if default_val > 0 else 100
                step = max(1.0, round((hi - lo) / 20, 2))
                seen[value_param_key] = {
                    "name": value_param_key,
                    "label": _humanize_label(value_param_key),
                    "type": ptype,
                    "default": default_val,
                    "min": lo,
                    "max": hi,
                    "step": step,
                }

            # 3. value_ref_overrides → value string embedded params
            if value_ref_overrides and isinstance(value, str):
                _, embedded = _parse_value_ref(value)
                for top_key, embedded_key in value_ref_overrides.items():
                    if top_key not in seen and embedded_key in embedded:
                        raw = embedded[embedded_key]
                        try:
                            default_val = float(raw)
                        except ValueError:
                            continue
                        ptype = "int" if "." not in raw else "float"
                        lo, hi, step = _suggest_range(embedded_key, default_val, ptype)
                        seen[top_key] = {
                            "name": top_key,
                            "label": _humanize_label(top_key),
                            "type": ptype,
                            "default": default_val,
                            "min": lo,
                            "max": hi,
                            "step": step,
                        }

            # 4. Auto-extract fallback (no annotations)
            if not param_key and params:
                for pname, pval in params.items():
                    auto_key = f"{indicator}_{pname}"
                    if auto_key not in seen and isinstance(pval, (int, float)):
                        ptype = _infer_type(pval)
                        lo, hi, step = _suggest_range(pname, float(pval), ptype)
                        seen[auto_key] = {
                            "name": auto_key,
                            "label": _humanize_label(auto_key),
                            "type": ptype,
                            "default": float(pval),
                            "min": lo,
                            "max": hi,
                            "step": step,
                        }

            if not value_ref_overrides and isinstance(value, str) and ":" in value:
                ref_ind, embedded = _parse_value_ref(value)
                for ek, ev in embedded.items():
                    auto_key = f"{ref_ind}_{ek}"
                    if auto_key not in seen:
                        try:
                            default_val = float(ev)
                        except ValueError:
                            continue
                        ptype = "int" if "." not in ev else "float"
                        lo, hi, step = _suggest_range(ek, default_val, ptype)
                        seen[auto_key] = {
                            "name": auto_key,
                            "label": _humanize_label(auto_key),
                            "type": ptype,
                            "default": default_val,
                            "min": lo,
                            "max": hi,
                            "step": step,
                        }

    return list(seen.values())


@router.get(
    "/strategies/{strategy_id}/params",
    response_model=list[StrategyParamDef],
)
async def get_strategy_params(strategy_id: str) -> list[dict[str, Any]]:
    """Extract optimizable parameters from a strategy's condition tree."""
    path = strategy_files.find_strategy_file(strategy_id)
    data = strategy_files.parse_strategy_file(path) if path is not None else None
    if data is None:
        raise api_error(404, "strategy_not_found", f"Strategy {strategy_id} not found", "not_found")
    parameters = data.get("parameters")
    rules_dict = parameters.get("rules") if isinstance(parameters, dict) else None
    if not rules_dict:
        return []
    return _extract_optimizable_params(rules_dict)


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
    type: Literal["int", "float", "categorical"]
    low: float | None = None
    high: float | None = None
    choices: list[Any] = Field(default_factory=list)


class HyperoptRunRequest(_DateRange):
    strategy_id: str
    param_space: list[ParamDefRequest]
    method: Literal["bayesian", "grid"] = "bayesian"
    max_trials: int = Field(default=50, ge=1, le=500)
    timeframe: Timeframe | None = None
    initial_capital: float = Field(default=10000.0, gt=0)


class HyperoptRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class HyperoptProgressResponse(BaseModel):
    task_id: str
    status: str
    strategy_id: str = ""
    created_at: str = ""
    completed_trials: int
    total_trials: int
    best_so_far: float | None = None
    current_trial_params: dict[str, Any] | None = None
    last_trial_sharpe: float | None = None
    start_date: str = ""
    end_date: str = ""
    error: str | None = None
    provenance: DataProvenanceModel | None = None


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
    provenance: DataProvenanceModel | None = None


class HyperoptHistoryEntry(BaseModel):
    task_id: str
    status: str
    strategy_id: str
    completed_trials: int
    total_trials: int
    best_so_far: float | None = None
    created_at: str
    error: str | None = None
    provenance: DataProvenanceModel | None = None


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_hyperopt_task(task_id: str, body: HyperoptRunRequest, pf: _Preflight) -> None:
    """Execute a hyperopt run on stored bars and keep the result in memory."""
    task = _HYPEROPT_TASKS[task_id]
    task["status"] = "running"

    try:
        loaded = await _load_task_bars(task, pf, body.start_date, body.end_date)
        if loaded is None:
            return
        bars, provenance = loaded
        task["provenance"] = provenance.to_dict()

        space = ParameterSpace(
            params=[
                ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
                for p in body.param_space
            ]
        )
        best_sharpe_seen = float("-inf")

        async def _on_trial_complete(
            trial_num: int,
            completed: int,
            total: int,
            record: Any,
        ) -> None:
            nonlocal best_sharpe_seen
            task["completed_trials"] = completed
            task["total_trials"] = total
            task["last_trial_sharpe"] = record.sharpe
            task["current_trial_params"] = record.params
            best_sharpe_seen = max(best_sharpe_seen, record.sharpe)
            task["best_so_far"] = best_sharpe_seen

        def _run_in_thread() -> HyperoptResult:
            loop = asyncio.new_event_loop()
            try:
                runner = HyperoptRunner(BacktestRunner())
                return loop.run_until_complete(
                    runner.run(
                        strategy_class=pf.strategy_cls,
                        base_config=pf.config,
                        bars=bars,
                        initial_capital=Decimal(str(body.initial_capital)),
                        param_space=space,
                        method=body.method,
                        max_trials=body.max_trials,
                        symbol=pf.symbol,
                        timeframe=pf.timeframe,
                        on_trial_complete=_on_trial_complete,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)

        _HYPEROPT_RESULTS[task_id] = result
        task["status"] = "completed"
        task["completed_trials"] = result.completed_trials
        task["total_trials"] = result.total_trials
        task["best_so_far"] = result.best_metric

    except Exception as exc:
        logger.exception("Hyperopt task %s failed", task_id)
        task["status"] = "failed"
        task["error"] = str(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/hyperopt/history",
    response_model=list[HyperoptHistoryEntry],
)
async def list_hyperopt_history() -> list[dict[str, Any]]:
    """Return a summary of all hyperopt runs (most recent first)."""
    entries = [
        {
            "task_id": tid,
            "status": t["status"],
            "strategy_id": t.get("strategy_id", ""),
            "completed_trials": t.get("completed_trials", 0),
            "total_trials": t.get("total_trials", 0),
            "best_so_far": t.get("best_so_far"),
            "created_at": t.get("created_at", ""),
            "error": t.get("error"),
            "provenance": t.get("provenance"),
        }
        for tid, t in _HYPEROPT_TASKS.items()
    ]
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    return entries


@router.post(
    "/hyperopt",
    response_model=HyperoptRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_hyperopt(body: HyperoptRunRequest, request: Request) -> dict[str, Any]:
    """Start a hyperparameter optimisation run on stored bars.

    Returns a ``task_id`` to poll for progress and results.
    """
    try:
        for p in body.param_space:
            ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
    except ValueError as exc:
        raise api_error(422, "invalid_param_space", str(exc), "invalid") from exc
    pf = await _preflight(request, body.strategy_id, body.timeframe, body.start_date, body.end_date)
    task_id = f"ho-{uuid.uuid4().hex[:8]}"
    _HYPEROPT_TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "completed_trials": 0,
        "total_trials": body.max_trials,
        "best_so_far": None,
        "strategy_id": body.strategy_id,
        "created_at": datetime.now(UTC).isoformat(),
        "request": body.model_dump(mode="json"),
    }
    _spawn(_run_hyperopt_task(task_id, body, pf))
    return {"task_id": task_id, "status": "queued"}


def _hyperopt_task(task_id: str) -> dict[str, Any]:
    if task_id not in _HYPEROPT_TASKS:
        raise api_error(404, "task_not_found", f"Hyperopt task {task_id} not found", "not_found")
    return _HYPEROPT_TASKS[task_id]


@router.get(
    "/hyperopt/{task_id}",
    response_model=HyperoptProgressResponse,
)
async def get_hyperopt_progress(task_id: str) -> dict[str, Any]:
    """Poll progress of a running hyperopt task."""
    t = _hyperopt_task(task_id)
    req = t.get("request", {})
    return {
        "task_id": task_id,
        "status": t["status"],
        "strategy_id": t.get("strategy_id", ""),
        "created_at": t.get("created_at", ""),
        "completed_trials": t["completed_trials"],
        "total_trials": t["total_trials"],
        "best_so_far": t.get("best_so_far"),
        "current_trial_params": t.get("current_trial_params"),
        "last_trial_sharpe": t.get("last_trial_sharpe"),
        "start_date": req.get("start_date", ""),
        "end_date": req.get("end_date", ""),
        "error": t.get("error"),
        "provenance": t.get("provenance"),
    }


@router.get(
    "/hyperopt/{task_id}/results",
    response_model=HyperoptResultResponse,
)
async def get_hyperopt_results(task_id: str) -> dict[str, Any]:
    """Return the full result of a completed hyperopt run."""
    t = _hyperopt_task(task_id)
    if t["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Hyperopt task {task_id} is not completed (status: {t['status']})",
        )
    result = _HYPEROPT_RESULTS.get(task_id)
    if result is None:
        raise api_error(
            404, "result_not_found", f"No result stored for hyperopt task {task_id}", "not_found"
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
        "provenance": t.get("provenance"),
    }


# ===========================================================================
# Walk-forward validation
# ===========================================================================

# ---------------------------------------------------------------------------
# In-memory state for walk-forward tasks
# ---------------------------------------------------------------------------

_WALKFORWARD_TASKS: dict[str, dict[str, Any]] = {}
_WALKFORWARD_RESULTS: dict[str, WalkForwardResult] = {}

_DAYS_PER_MONTH = 30.44
_ANALYZER_DAYS_PER_MONTH = 30


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class WalkForwardRunRequest(_DateRange):
    strategy_id: str
    fold_count: int = Field(default=6, ge=2, le=24)
    train_pct: float = Field(default=0.75, gt=0.0, lt=1.0)
    timeframe: Timeframe | None = None
    initial_capital: float = Field(default=10000.0, gt=0)


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
    provenance: DataProvenanceModel | None = None


def _walkforward_months(
    first: datetime, last: datetime, fold_count: int, train_pct: float
) -> tuple[int, int]:
    """In-sample / out-of-sample months per window, derived from the real data span."""
    total_months = max(1, math.floor((last - first).days / _DAYS_PER_MONTH))
    in_sample = max(1, round(total_months * train_pct / fold_count))
    out_of_sample = max(1, round(total_months * (1 - train_pct) / fold_count))
    return in_sample, out_of_sample


def _window_fits(first: datetime, last: datetime, in_sample: int, out_of_sample: int) -> bool:
    return (in_sample + out_of_sample) * _ANALYZER_DAYS_PER_MONTH <= (last - first).days


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_walkforward_task(task_id: str, body: WalkForwardRunRequest, pf: _Preflight) -> None:
    """Execute a walk-forward analysis on stored bars."""
    task = _WALKFORWARD_TASKS[task_id]
    task["status"] = "running"

    try:
        loaded = await _load_task_bars(task, pf, body.start_date, body.end_date)
        if loaded is None:
            return
        bars, provenance = loaded
        task["provenance"] = provenance.to_dict()

        in_sample, out_of_sample = _walkforward_months(
            provenance.first_ts, provenance.last_ts, body.fold_count, body.train_pct
        )
        task["in_sample_months"] = in_sample
        task["out_of_sample_months"] = out_of_sample

        def _run_in_thread() -> WalkForwardResult:
            loop = asyncio.new_event_loop()
            try:
                analyzer = WalkForwardAnalyzer(initial_capital=Decimal(str(body.initial_capital)))
                return loop.run_until_complete(
                    analyzer.run_walkforward(
                        bars=bars,
                        strategy_class=pf.strategy_cls,
                        strategy_config=pf.config,
                        in_sample_months=in_sample,
                        out_of_sample_months=out_of_sample,
                        symbol=pf.symbol,
                        timeframe=pf.timeframe,
                    )
                )
            finally:
                loop.close()

        result = await asyncio.to_thread(_run_in_thread)
        if not result.folds:
            _fail(task, f"range too short for {body.fold_count} folds")
            return

        _WALKFORWARD_RESULTS[task_id] = result
        task["status"] = "completed"
        task["fold_count"] = len(result.folds)

    except Exception as exc:
        logger.exception("Walk-forward task %s failed", task_id)
        _fail(task, str(exc))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/walkforward",
    response_model=WalkForwardRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_walkforward(body: WalkForwardRunRequest, request: Request) -> dict[str, Any]:
    """Start a walk-forward validation run on stored bars.

    Returns a ``task_id`` to poll for results.
    """
    pf = await _preflight(request, body.strategy_id, body.timeframe, body.start_date, body.end_date)
    if pf.stored is not None:
        first, last, _ = pf.stored
        in_sample, out_of_sample = _walkforward_months(first, last, body.fold_count, body.train_pct)
        if not _window_fits(first, last, in_sample, out_of_sample):
            raise api_error(
                422,
                "range_too_short",
                f"range too short for {body.fold_count} folds: stored bars span "
                f"{first.isoformat()} to {last.isoformat()}, one window needs "
                f"{in_sample + out_of_sample} months",
                "invalid",
                available_first_ts=first.isoformat(),
                available_last_ts=last.isoformat(),
            )
    task_id = f"wf-{uuid.uuid4().hex[:8]}"
    _WALKFORWARD_TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "fold_count": 0,
        "request": body.model_dump(mode="json"),
    }
    _spawn(_run_walkforward_task(task_id, body, pf))
    return {"task_id": task_id, "status": "queued"}


@router.get(
    "/walkforward/{task_id}/results",
    response_model=WalkForwardResultResponse,
)
async def get_walkforward_results(task_id: str) -> dict[str, Any]:
    """Return fold metrics and aggregated OOS equity for a completed walk-forward run."""
    if task_id not in _WALKFORWARD_TASKS:
        raise api_error(
            404, "task_not_found", f"Walk-forward task {task_id} not found", "not_found"
        )
    t = _WALKFORWARD_TASKS[task_id]
    if t["status"] != "completed":
        detail = f"Walk-forward task {task_id} is not completed (status: {t['status']})"
        if t.get("error"):
            detail = f"{detail}: {t['error']}"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    result = _WALKFORWARD_RESULTS.get(task_id)
    if result is None:
        raise api_error(
            404,
            "result_not_found",
            f"No result stored for walk-forward task {task_id}",
            "not_found",
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
        "provenance": t.get("provenance"),
    }


# ---------------------------------------------------------------------------
# Backtest result sharing
# ---------------------------------------------------------------------------

_SHARED_TOKENS: dict[str, str] = {}  # token → result_id


@router.post("/results/{result_id}/share")
async def share_result(result_id: str, request: Request) -> dict[str, str]:
    """Generate a public sharing token for a backtest result."""
    # Verify result exists
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT 1 FROM backtest_results WHERE id = $1", result_id
                )
                if not exists:
                    raise HTTPException(status_code=404, detail="Result not found")
        except HTTPException:
            raise
        except Exception:
            pass

    token = uuid.uuid4().hex[:12]
    _SHARED_TOKENS[token] = result_id
    return {"token": token, "share_url": f"/api/backtest/shared/{token}"}


@router.get("/shared/{token}")
async def get_shared_result(token: str, request: Request) -> dict[str, Any]:
    """Public read-only access to a shared backtest result (no auth required)."""
    result_id = _SHARED_TOKENS.get(token)
    if result_id is None:
        raise HTTPException(status_code=404, detail="Shared link not found or expired")
    return await get_result_detail(result_id, request)
