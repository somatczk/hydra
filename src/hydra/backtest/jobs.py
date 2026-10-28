"""Backtest job building blocks: request models, strategy import, bar loading, result rows and
the durable job queue (``backtest_jobs``) that the worker claims from.

Lives in hydra.backtest so the routes' services (enqueue/read) and the worker (claim/run) share it
without either importing the other's layer.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from importlib.metadata import version
from typing import Any, Literal, NamedTuple, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from hydra.backtest.data import (
    DataProvenance,
    load_bars,
)
from hydra.backtest.metrics import BacktestResult, Trade
from hydra.backtest.walkforward import window_bar_counts
from hydra.core.types import OHLCV, ExchangeId, Timeframe
from hydra.data.backfill import ExchangeBackfillService
from hydra.data.storage import MarketDataRepository
from hydra.execution.client_factory import rest_exchange_factory
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)


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


class WalkForwardRunRequest(_DateRange):
    strategy_id: str
    param_space: list[ParamDefRequest] = Field(min_length=1)
    method: Literal["bayesian", "grid"] = "grid"
    max_trials: int = Field(default=20, ge=1, le=200)  # per fold
    anchored: bool = False
    fold_count: int = Field(default=6, ge=2, le=24)
    train_pct: float = Field(default=0.75, gt=0.0, lt=1.0)
    timeframe: Timeframe | None = None
    initial_capital: float = Field(default=10000.0, gt=0)


# ---------------------------------------------------------------------------
# Strategy import
# ---------------------------------------------------------------------------

_BACKTESTABLE_PACKAGE = "hydra.strategy."


class InvalidStrategyError(ValueError):
    """The strategy config or class cannot be backtested."""


def import_backtest_strategy(dotted_path: str) -> type[BaseStrategy]:
    """Import a strategy class, only from under hydra.strategy and only a BaseStrategy."""
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path.startswith(_BACKTESTABLE_PACKAGE):
        msg = f"strategy class {dotted_path} must live under {_BACKTESTABLE_PACKAGE}"
        raise InvalidStrategyError(msg)
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
    except (ImportError, AttributeError) as exc:
        msg = f"cannot import strategy class {dotted_path}"
        raise InvalidStrategyError(msg) from exc
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        msg = f"strategy class {dotted_path} is not supported by the backtester"
        raise InvalidStrategyError(msg)
    return cls


@dataclass(frozen=True, slots=True)
class Preflight:
    strategy_cls: type[BaseStrategy]
    config: StrategyConfig
    exchange: ExchangeId
    symbol: str
    timeframe: Timeframe
    pool: Any
    stored: tuple[datetime, datetime, int] | None


# ---------------------------------------------------------------------------
# Config identity on the persisted provenance
# ---------------------------------------------------------------------------


def config_hash(config: StrategyConfig) -> str:
    """First 12 hex chars of sha256 over the config; ``enabled`` and ``name`` are noise."""
    payload = json.dumps(
        config.model_dump(mode="json", exclude={"enabled", "name"}), sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def provenance_payload(provenance: DataProvenance, config: StrategyConfig) -> dict[str, Any]:
    """The provenance dict persisted on the job row, including the code and config it ran."""
    payload: dict[str, Any] = provenance.to_dict()
    payload["hydra_version"] = version("hydra")
    payload["config_hash"] = config_hash(config)
    return payload


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------


async def fetch_fresh_bars(
    pool: Any, config: StrategyConfig, start: datetime, end: datetime
) -> str | None:
    """Backfill the range over the exchange's public async REST API; the error when it failed."""
    exchange_id = config.exchange.exchange_id

    def _rest_factory() -> Any:
        return rest_exchange_factory(exchange_id)

    backfill = ExchangeBackfillService(
        repository=MarketDataRepository.from_pool(pool),
        exchange_factories={exchange_id: _rest_factory},
    )
    try:
        await backfill.bulk_download(
            exchange_id=exchange_id,
            symbol=config.symbols[0],
            timeframe=config.timeframes.primary,
            start=start,
            end=end,
        )
    except Exception as exc:
        logger.warning("Fresh data fetch failed, using stored bars", exc_info=True)
        return str(exc)
    finally:
        await backfill.close()
    return None


async def load_job_bars(
    pool: Any, config: StrategyConfig, start: datetime, end: datetime
) -> tuple[list[OHLCV], DataProvenance]:
    """Load the job's bars from the DB; raises when the stored data is missing or invalid."""
    repo = MarketDataRepository.from_pool(pool)
    return await load_bars(
        repo,
        config.exchange.exchange_id,
        config.symbols[0],
        config.timeframes.primary,
        start,
        end,
    )


def _timestamped(values: list[Decimal], timestamps: list[datetime]) -> list[dict[str, Any]]:
    return [
        {"timestamp": ts.isoformat(), "value": float(v)}
        for v, ts in zip(values, timestamps, strict=False)
    ]


def _round_or_none(value: float | Decimal | None) -> float | None:
    return None if value is None else round(float(value), 2)


def result_record(
    result_id: str,
    body: BacktestRunRequest,
    config: StrategyConfig,
    result: BacktestResult,
    provenance: DataProvenance,
) -> dict[str, Any]:
    """The stored result: what is persisted and what the result detail returns."""
    initial = Decimal(str(body.initial_capital))
    final_equity = result.equity_curve[-1] if result.equity_curve else initial
    return {
        "id": result_id,
        "strategy": config.name,
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
            "total_return": round(float(result.total_return) * 100, 2),
            "annualized_return": _round_or_none(
                None if result.annualized_return is None else result.annualized_return * 100
            ),
            "sortino_ratio": _round_or_none(result.sortino_ratio),
            "calmar_ratio": _round_or_none(result.calmar_ratio),
            "profit_factor": _round_or_none(result.profit_factor),
            "max_drawdown_duration_s": int(result.max_drawdown_duration.total_seconds()),
        },
        "equity_curve": _timestamped(result.equity_curve, result.timestamps),
        "benchmark_equity": _timestamped(result.benchmark_equity, result.timestamps),
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


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------

_EXTENDED_METRICS = (
    "benchmark_return",
    "alpha",
    "beta",
    "max_consecutive_wins",
    "max_consecutive_losses",
    "expectancy",
    "total_return",
    "annualized_return",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "max_drawdown_duration_s",
)
MAX_RESULT_SUMMARIES = 200
LIST_SUMMARIES_SQL = (
    "SELECT "
    "id, strategy, period, status, total_trades, win_rate, total_pnl, max_drawdown, "
    "sharpe_ratio, benchmark_return, alpha, beta, max_consecutive_wins, "
    "max_consecutive_losses, expectancy, total_return, annualized_return, sortino_ratio, "
    "calmar_ratio, profit_factor, max_drawdown_duration_s, name, strategy_id, initial_capital, "
    "data_source, data_exchange, data_symbol, data_timeframe, bar_count, first_ts, "
    "last_ts, gap_count, requested_start, requested_end "
    "FROM backtest_results ORDER BY created_at DESC LIMIT $1"
)
ONE_RESULT_SQL = (
    "SELECT "
    "id, strategy, period, status, total_trades, win_rate, total_pnl, max_drawdown, "
    "sharpe_ratio, benchmark_return, alpha, beta, max_consecutive_wins, "
    "max_consecutive_losses, expectancy, total_return, annualized_return, sortino_ratio, "
    "calmar_ratio, profit_factor, max_drawdown_duration_s, name, strategy_id, initial_capital, "
    "data_source, data_exchange, data_symbol, data_timeframe, bar_count, first_ts, "
    "last_ts, gap_count, requested_start, requested_end, "
    "stopped_reason, equity_curve, benchmark_equity, transactions "
    "FROM backtest_results WHERE id = $1"
)
RENAME_SQL = (
    "UPDATE backtest_results SET name = $1 WHERE id = $2 RETURNING "
    "id, strategy, period, status, total_trades, win_rate, total_pnl, max_drawdown, "
    "sharpe_ratio, benchmark_return, alpha, beta, max_consecutive_wins, "
    "max_consecutive_losses, expectancy, total_return, annualized_return, sortino_ratio, "
    "calmar_ratio, profit_factor, max_drawdown_duration_s, name, strategy_id, initial_capital, "
    "data_source, data_exchange, data_symbol, data_timeframe, bar_count, first_ts, "
    "last_ts, gap_count, requested_start, requested_end"
)


async def persist_result(
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
                    first_ts, last_ts, gap_count, requested_start, requested_end,
                    stopped_reason, benchmark_equity, benchmark_return, alpha, beta,
                    max_consecutive_wins, max_consecutive_losses, expectancy,
                    total_return, annualized_return, sortino_ratio, calmar_ratio,
                    profit_factor, max_drawdown_duration_s)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11,
                       $12::jsonb, $13, $14, $15, $16, $17, $18, $19, $20, $21,
                       $22, $23, $24, $25, $26::jsonb, $27, $28, $29, $30, $31, $32,
                       $33, $34, $35, $36, $37, $38)
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
                   requested_end = EXCLUDED.requested_end,
                   stopped_reason = EXCLUDED.stopped_reason,
                   benchmark_equity = EXCLUDED.benchmark_equity,
                   benchmark_return = EXCLUDED.benchmark_return,
                   alpha = EXCLUDED.alpha,
                   beta = EXCLUDED.beta,
                   max_consecutive_wins = EXCLUDED.max_consecutive_wins,
                   max_consecutive_losses = EXCLUDED.max_consecutive_losses,
                   expectancy = EXCLUDED.expectancy,
                   total_return = EXCLUDED.total_return,
                   annualized_return = EXCLUDED.annualized_return,
                   sortino_ratio = EXCLUDED.sortino_ratio,
                   calmar_ratio = EXCLUDED.calmar_ratio,
                   profit_factor = EXCLUDED.profit_factor,
                   max_drawdown_duration_s = EXCLUDED.max_drawdown_duration_s""",
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
            result["stopped_reason"],
            json.dumps(result["benchmark_equity"]),
            *(metrics[name] for name in _EXTENDED_METRICS),
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


def json_value(value: Any) -> Any:
    """asyncpg returns jsonb columns as JSON text; dicts and records pass through."""
    return json.loads(value) if isinstance(value, str) else value


def summary_from_row(row: Any) -> dict[str, Any]:
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
            **{name: row[name] for name in _EXTENDED_METRICS},
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


def detail_from_row(row: Any, trade_rows: list[Any]) -> dict[str, Any]:
    return {
        **summary_from_row(row),
        "stopped_reason": row["stopped_reason"],
        "equity_curve": json_value(row["equity_curve"]),
        "benchmark_equity": json_value(row["benchmark_equity"]),
        "trades": [_trade_from_row(t) for t in trade_rows],
        "transactions": json_value(row["transactions"]),
    }


# ---------------------------------------------------------------------------
# The durable job queue
# ---------------------------------------------------------------------------

JobKind = Literal["backtest", "hyperopt", "walkforward"]
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

_ID_PREFIX: dict[JobKind, str] = {"backtest": "task", "hyperopt": "ho", "walkforward": "wf"}


class JobOutcome(NamedTuple):
    """What a finished job leaves on its row."""

    result_id: str | None = None
    result: dict[str, Any] | None = None


async def enqueue(
    pool: Any,
    kind: JobKind,
    request: dict[str, Any],
    strategy_config: dict[str, Any],
    detail: dict[str, Any],
) -> str:
    """Insert a queued job; the id prefix (task-/ho-/wf-) identifies the kind."""
    job_id = f"{_ID_PREFIX[kind]}-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_jobs (id, kind, request, strategy_config, detail) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)",
            job_id,
            kind,
            json.dumps(request),
            json.dumps(strategy_config),
            json.dumps(detail),
        )
    return job_id


async def get(pool: Any, job_id: str, kind: JobKind | None = None) -> Any:
    """One job row, or None; ``kind`` narrows the lookup when given."""
    async with pool.acquire() as conn:
        if kind is None:
            return await conn.fetchrow("SELECT * FROM backtest_jobs WHERE id = $1", job_id)
        return await conn.fetchrow(
            "SELECT * FROM backtest_jobs WHERE id = $1 AND kind = $2", job_id, kind
        )


async def list_jobs(pool: Any, kind: JobKind, limit: int | None = None) -> list[Any]:
    """Rows of one kind, newest first; the SQL read stops at ``limit`` rows when given."""
    async with pool.acquire() as conn:
        if limit is None:
            rows = await conn.fetch(
                "SELECT * FROM backtest_jobs WHERE kind = $1 ORDER BY created_at DESC", kind
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM backtest_jobs WHERE kind = $1 ORDER BY created_at DESC LIMIT $2",
                kind,
                limit,
            )
    return list(rows)


async def request_cancel(pool: Any, job_id: str) -> Any:
    """Cancel a job: queued goes straight to cancelled; running asks the worker to stop.

    None when the job is unknown, already terminal, or already asked to cancel.
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "UPDATE backtest_jobs SET "
            "status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END, "
            "cancel_requested = TRUE, "
            "finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END "
            "WHERE id = $1 AND (status = 'queued' "
            "OR (status = 'running' AND NOT cancel_requested)) "
            "RETURNING *",
            job_id,
        )


async def claim(pool: Any, worker_id: str) -> Any:
    """Claim the oldest queued job transactionally (FOR UPDATE SKIP LOCKED), or None."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "UPDATE backtest_jobs SET status = 'running', attempts = attempts + 1, "
            "started_at = now(), heartbeat_at = now(), worker_id = $1 "
            "WHERE id = (SELECT id FROM backtest_jobs WHERE status = 'queued' "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING *",
            worker_id,
        )


async def tick(pool: Any, job_id: str, progress: float, detail: dict[str, Any]) -> bool:
    """Flush progress to the row; True when a cancel was requested."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE backtest_jobs SET progress = $2, detail = $3::jsonb, heartbeat_at = now() "
            "WHERE id = $1 AND status = 'running' RETURNING cancel_requested",
            job_id,
            progress,
            json.dumps(detail),
        )
    return bool(row["cancel_requested"]) if row is not None else False


async def finish(
    pool: Any,
    job_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    result_id: str | None = None,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write the job's terminal state, flushing the handler's final ``detail``.

    The 10 s tick is not guaranteed to have run since the last mutation (short jobs finish
    inside one tick), so the terminal write persists the detail itself; ``detail=None`` keeps
    whatever the last tick flushed.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE backtest_jobs SET status = $2, "
            "progress = CASE WHEN $2 = 'completed' THEN 100 ELSE progress END, "
            "detail = COALESCE($6::jsonb, detail), "
            "result = $3::jsonb, result_id = $4, error = $5, cancel_requested = FALSE, "
            "heartbeat_at = now(), finished_at = now() WHERE id = $1",
            job_id,
            status,
            json.dumps(result) if result is not None else None,
            result_id,
            error,
            json.dumps(detail) if detail is not None else None,
        )


async def delete_result_rows(pool: Any, result_id: str) -> None:
    """Drop a job's result rows: a cancelled or timed-out run leaves no result behind."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM backtest_result_trades WHERE backtest_id = $1", result_id)
        await conn.execute("DELETE FROM backtest_results WHERE id = $1", result_id)


async def requeue_abandoned(pool: Any, max_attempts: int = 3) -> None:
    """A worker restart requeues rows left running, up to ``max_attempts``; then they are poison."""
    # shortcut: single worker, so startup requeues all running rows; lease by
    # heartbeat_at before running more than one worker
    async with pool.acquire() as conn, conn.transaction():
        requeued = await conn.fetch(
            "UPDATE backtest_jobs SET status = 'queued' "
            "WHERE status = 'running' AND attempts < $1 RETURNING id",
            max_attempts,
        )
        poisoned = await conn.fetch(
            "UPDATE backtest_jobs SET status = 'failed', finished_at = now(), error = $2 "
            "WHERE status = 'running' AND attempts >= $1 RETURNING id",
            max_attempts,
            f"worker lost the job {max_attempts} times",
        )
    if requeued:
        logger.info("Requeued %d job(s) abandoned mid-run", len(requeued))
    if poisoned:
        logger.warning("Failed %d job(s) the worker lost %d times", len(poisoned), max_attempts)


async def requeue_for_shutdown(pool: Any, job_id: str) -> None:
    """A deploy requeues the running job without counting the interrupted attempt."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE backtest_jobs SET status = 'queued', attempts = GREATEST(attempts - 1, 0), "
            "cancel_requested = FALSE WHERE id = $1 AND status = 'running'",
            job_id,
        )


# ---------------------------------------------------------------------------
# Walk-forward window sizing from the real span
# ---------------------------------------------------------------------------

_DAYS_PER_MONTH = 30.44


def walkforward_months(
    first: datetime, last: datetime, fold_count: int, train_pct: float
) -> tuple[int, int]:
    """In-sample / out-of-sample months per window, derived from the real data span."""
    total_months = max(1, math.floor((last - first).days / _DAYS_PER_MONTH))
    in_sample = max(1, round(total_months * train_pct / fold_count))
    out_of_sample = max(1, round(total_months * (1 - train_pct) / fold_count))
    return in_sample, out_of_sample


def window_fits(
    first: datetime, last: datetime, bar_count: int, in_sample: int, out_of_sample: int
) -> bool:
    days = (last - first).days
    if days <= 0:
        return False
    is_bars, oos_bars = window_bar_counts(bar_count, days, in_sample, out_of_sample)
    return is_bars > 0 and oos_bars > 0 and is_bars + oos_bars <= bar_count
