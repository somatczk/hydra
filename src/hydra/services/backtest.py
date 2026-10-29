"""Backtest runs and results: pre-flight checks, durable job submission and result reads."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from hydra.backtest import jobs
from hydra.backtest.data import no_market_data
from hydra.backtest.jobs import (
    LIST_SUMMARIES_SQL,
    MAX_RESULT_SUMMARIES,
    ONE_RESULT_SQL,
    RENAME_SQL,
    TERMINAL_STATUSES,
    BacktestRunRequest,
    InvalidStrategyError,
    Preflight,
    detail_from_row,
    import_backtest_strategy,
    summary_from_row,
)
from hydra.core.types import MarketType, Timeframe
from hydra.data.storage import MarketDataRepository
from hydra.ml.consumers import consumed_model_names, pinned_version
from hydra.ml.serving import MlServingError
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
    status: str  # queued | running | completed | failed | cancelled
    progress: float = 0.0  # 0-100
    error: str | None = None
    result_id: str | None = None
    fetch_error: str | None = None
    # informational: a terminal row's status/error columns are authoritative, not fetch.state
    fetch: dict[str, Any] | None = None
    provenance: JobProvenanceModel | None = None


class CancelJobResponse(BaseModel):
    task_id: str
    status: str


class JobListEntry(BaseModel):
    task_id: str
    kind: jobs.JobKind
    status: str  # queued | running | cancelling | completed | failed | cancelled
    progress: float = 0.0
    created_at: str
    finished_at: str | None = None
    strategy_id: str = ""
    name: str = ""
    error: str | None = None
    result_id: str | None = None
    completed_trials: int | None = None
    total_trials: int | None = None
    # the worker's auto-fetch progress (state, ranges, bars_fetched, weight_spent, elapsed_ms)
    fetch: dict[str, Any] | None = None
    # the worker holding (or last holding) the job's lease
    worker_id: str | None = None


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


class MlModelProvenance(BaseModel):
    """One registry version a research job served, and whether the request pinned it."""

    id: str
    name: str
    version: int
    stage: str
    pinned: bool = False


class JobProvenanceModel(DataProvenanceModel):
    """Provenance read from a job row: carries the code and config that ran it (D2 decision)."""

    hydra_version: str | None = None
    config_hash: str | None = None
    ml_models: list[MlModelProvenance] | None = None


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
    model_names: list[str] = Field(default_factory=list)


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


def _load_backtest_strategy(
    strategy_id: str, *, allow_futures: bool = False
) -> tuple[type[BaseStrategy], StrategyConfig]:
    """Resolve a strategy YAML into its class and full config; 404 unknown, 422 unusable.

    A USD⌂-M perp strategy runs as a plain backtest (``allow_futures=True``, P4-C: the
    perp paper executor through the runner's parity path); hyperopt and walk-forward
    keep refusing futures until their worker paths carry the funding preflight too.
    """
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
    if config.exchange.market_type == MarketType.FUTURES and not allow_futures:
        raise _invalid_strategy(
            strategy_id,
            "futures strategies run as plain backtests only for now "
            "(perp hyperopt and walk-forward arrive after 2.8.0)",
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
    auto_fetch: bool = False,
    read_span: bool = False,
    allow_futures: bool = False,
) -> Preflight:
    """Validate a job before it is queued: 404 strategy, 422 config/data, 503 database.

    The data leg is config-gated: with ``auto_fetch`` on (``data.auto_fetch_enabled``)
    a missing range never 422s — the job is queued and the worker owns availability;
    off, the exact P0-B 422 ``no_market_data`` with the available range. ``read_span``
    keeps the stored-span read for callers that size from it (the walk-forward
    fast-fail) even while fetching. ``allow_futures`` opens the USD⌂-M perp path
    (plain backtests only).
    """
    strategy_cls, config = _load_backtest_strategy(strategy_id, allow_futures=allow_futures)
    tf = timeframe or config.timeframes.primary
    if tf != config.timeframes.primary:
        timeframes = config.timeframes.model_copy(update={"primary": tf})
        try:  # revalidated: a schedule must still match a bar of the requested timeframe
            config = StrategyConfig.model_validate(
                {**config.model_dump(), "timeframes": timeframes}
            )
        except ValidationError as exc:
            raise _invalid_strategy(strategy_id, f"invalid config on {tf}: {exc}") from exc
    pool = require_pool(pool)
    exchange = config.exchange.exchange_id
    symbol = config.symbols[0]
    stored = None
    if require_data or read_span:
        repo = MarketDataRepository.from_pool(pool)
        async with db_errors("checking stored market data"):
            stored = await repo.get_bar_range(exchange, symbol, tf, start, end)
            if stored is None and require_data and not auto_fetch:
                missing = await no_market_data(repo, exchange, symbol, tf, start, end)
                raise ServiceError(422, missing.to_detail())
    return Preflight(strategy_cls, config, exchange, symbol, tf, pool, stored)


async def check_model_pin(
    pool: Any, config: StrategyConfig, model_version_id: uuid.UUID | None
) -> None:
    """A research job's pinned model version must exist and be one the strategy consumes."""
    if model_version_id is None:
        return
    try:
        async with db_errors("reading the model registry"):
            await pinned_version(pool, config, model_version_id)
    except MlServingError as exc:
        raise service_error(404, "model_not_found", str(exc), "not_found") from exc
    except ValueError as exc:
        raise service_error(422, "model_not_consumed", str(exc), "invalid") from exc


# ---------------------------------------------------------------------------
# Job submission and status
# ---------------------------------------------------------------------------


async def submit_backtest(
    pool: Any, body: BacktestRunRequest, *, auto_fetch: bool = False
) -> dict[str, Any]:
    """Queue a backtest on the worker. Returns a task_id to poll for status.

    With ``auto_fetch`` on, a range with no stored data is queued anyway — the
    worker fetches its missing bars before the run.
    """
    pf = await _preflight(
        pool,
        body.strategy_id,
        body.timeframe,
        body.start_date,
        body.end_date,
        require_data=not body.fetch_fresh,
        auto_fetch=auto_fetch,
        allow_futures=True,
    )
    await check_model_pin(pf.pool, pf.config, body.model_version_id)
    async with db_errors("queueing the job"):
        task_id = await jobs.enqueue(
            pool,
            "backtest",
            body.model_dump(mode="json"),
            pf.config.model_dump(mode="json"),
            {},
        )
    return {"task_id": task_id, "status": "queued"}


async def get_backtest_status(pool: Any, task_id: str) -> dict[str, Any]:
    """Check the progress of a queued, running or finished backtest job."""
    pool = require_pool(pool)
    async with db_errors("reading a backtest job"), pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM backtest_jobs WHERE id = $1 AND kind = 'backtest'", task_id
        )
    if row is None:
        raise service_error(404, "task_not_found", f"Task {task_id} not found", "not_found")
    detail = jobs.json_value(row["detail"]) or {}
    return {
        "task_id": task_id,
        "status": row["status"],
        "progress": row["progress"],
        "error": row["error"],
        "result_id": row["result_id"],
        "fetch_error": detail.get("fetch_error"),
        "fetch": detail.get("fetch"),
        "provenance": detail.get("provenance"),
    }


async def cancel_job(pool: Any, job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job; 404 unknown, 409 already terminal."""
    pool = require_pool(pool)
    async with db_errors("cancelling a job"):
        row = await jobs.get(pool, job_id)
        if row is None:
            raise service_error(404, "task_not_found", f"Task {job_id} not found", "not_found")
        if row["status"] in TERMINAL_STATUSES:
            raise ServiceError(409, f"Task {job_id} is already {row['status']}")
        row = await jobs.request_cancel(pool, job_id)
    if row is None:  # a concurrent cancel won the race between our two reads
        raise ServiceError(409, f"Task {job_id} is already finished or cancelling")
    status = "cancelled" if row["status"] == "cancelled" else "cancelling"
    return {"task_id": job_id, "status": status}


_ALL_KINDS: tuple[jobs.JobKind, ...] = ("backtest", "hyperopt", "walkforward")


async def list_jobs(
    pool: Any, kind: jobs.JobKind | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Durable job rows of every kind, newest first; only the extracted fields leave the row.

    Every per-kind read carries the same SQL LIMIT: a kind's share of the global
    newest ``limit`` rows fits within that kind's own newest ``limit``, so the
    cross-kind merge below still yields the unbounded read's top ``limit``.
    """
    pool = require_pool(pool)
    async with db_errors("listing jobs"):
        rows: list[Any] = []
        for one in (kind,) if kind else _ALL_KINDS:
            rows.extend(await jobs.list_jobs(pool, one, limit))
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    entries: list[dict[str, Any]] = []
    for row in rows[:limit]:
        request = jobs.json_value(row["request"]) or {}
        detail = jobs.json_value(row["detail"]) or {}
        entry = {
            "task_id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "progress": row["progress"],
            "created_at": row["created_at"].isoformat(),
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "strategy_id": str(request.get("strategy_id", "")),
            "name": str(request.get("name", "")),
            "error": row["error"],
            "result_id": row["result_id"],
            "fetch": detail.get("fetch"),
            "worker_id": row["worker_id"],
        }
        if row["kind"] == "hyperopt":
            entry["completed_trials"] = detail.get("completed_trials", 0)
            entry["total_trials"] = detail.get("total_trials", request.get("max_trials", 0))
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Result reads
# ---------------------------------------------------------------------------


async def list_results(pool: Any, limit: int = MAX_RESULT_SUMMARIES) -> list[dict[str, Any]]:
    """Persisted result summaries, newest first; the SQL read stops at ``limit`` rows."""
    pool = require_pool(pool)
    async with db_errors("listing backtest results"), pool.acquire() as conn:
        rows = await conn.fetch(LIST_SUMMARIES_SQL, limit)
    return [summary_from_row(row) for row in rows]


async def get_result(pool: Any, result_id: str) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
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
    """Rename a backtest result in the database."""
    pool = require_pool(pool)
    async with db_errors("renaming a backtest result"), pool.acquire() as conn:
        row = await conn.fetchrow(RENAME_SQL, name, result_id)
    if row is None:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")
    return summary_from_row(row)


async def delete_result(pool: Any, result_id: str) -> None:
    """Delete a backtest result and its trades from the database."""
    pool = require_pool(pool)
    async with db_errors("deleting a backtest result"), pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM backtest_results WHERE id = $1 RETURNING id", result_id
        )
    if deleted is None:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")


async def verify_result(pool: Any, result_id: str) -> dict[str, Any]:
    """Verify a backtest result by recomputing metrics from trades."""
    detail = await get_result(pool, result_id)

    trades = detail.get("trades", [])
    metrics = detail.get("metrics", {})
    reported_trade_count = metrics.get("total_trades", 0)
    reported_win_rate = metrics.get("win_rate", 0.0)
    reported_total_pnl = metrics.get("total_pnl", 0.0)

    computed_trade_count = len(trades)
    computed_total_pnl = round(sum(t["pnl"] for t in trades), 2)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    computed_win_rate = (
        round(wins / computed_trade_count * 100, 2) if computed_trade_count > 0 else 0.0
    )

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
    """List strategies available for backtesting: the primary timeframe and the registry
    names a run serves (what a ``model_version_id`` pin may name)."""
    strategies: list[dict[str, Any]] = []
    for path, data in strategy_files.iter_strategy_yamls():
        timeframes = data.get("timeframes")
        primary = timeframes.get("primary") if isinstance(timeframes, dict) else None
        try:
            model_names = consumed_model_names(StrategyConfig(**data))
        except (TypeError, ValueError):  # an unloadable config refuses at submit anyway
            model_names = []
        strategies.append(
            {
                "id": data.get("id", path.stem),
                "name": data.get("name", path.stem),
                "primary_timeframe": str(primary) if primary is not None else None,
                "model_names": model_names,
            }
        )
    return strategies


# ---------------------------------------------------------------------------
# Backtest result sharing
# ---------------------------------------------------------------------------

_SHARED_TOKENS: dict[str, str] = {}  # token → result_id


async def share_result(pool: Any, result_id: str) -> dict[str, str]:
    """Mint an in-memory sharing token for a backtest result (it does not bypass auth)."""
    pool = require_pool(pool)
    async with db_errors("checking the backtest result"), pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM backtest_results WHERE id = $1", result_id)
    if not exists:
        raise service_error(404, "result_not_found", f"Result {result_id} not found", "not_found")

    token = uuid.uuid4().hex[:12]
    _SHARED_TOKENS[token] = result_id
    return {"token": token, "share_url": f"/api/backtest/shared/{token}"}


async def get_shared_result(pool: Any, token: str) -> dict[str, Any]:
    """Read-only access to a shared backtest result: behind API auth like every route, the
    in-memory token only names the result (a restart ends every token)."""
    result_id = _SHARED_TOKENS.get(token)
    if result_id is None:
        raise ServiceError(404, "Shared link not found or expired")
    return await get_result(pool, result_id)
