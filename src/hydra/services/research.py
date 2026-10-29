"""Research jobs: optimisable strategy parameters, hyperopt and walk-forward runs."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hydra.backtest import jobs
from hydra.backtest.hyperopt import ParamDef, ParameterSpace, unknown_param_names
from hydra.backtest.jobs import (
    HyperoptRunRequest,
    ParamDefRequest,
    WalkForwardRunRequest,
)
from hydra.backtest.walkforward import MIN_WINDOW_DAYS, fold_windows
from hydra.core.types import Timeframe
from hydra.data.bars import ceil_to_tf, floor_to_tf, timeframe_seconds
from hydra.services import strategies as strategy_files
from hydra.services.backtest import (
    JobProvenanceModel,
    _invalid_strategy,
    _load_backtest_strategy,
    _preflight,
    check_model_pin,
)
from hydra.services.errors import ServiceError, db_errors, service_error
from hydra.strategy.base import BaseStrategy
from hydra.strategy.builtin.rule_based import bindable_params
from hydra.strategy.condition_schema import RuleSet
from hydra.strategy.config import StrategyConfig

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
    from hydra.strategy.context import PARAM_LIMITS

    if param_name in PARAM_LIMITS:
        lo, hi = (float(v) for v in PARAM_LIMITS[param_name])
    else:
        lo = max(0.1 if param_type == "float" else 1, round(default * 0.25))
        hi = max(round(default * 4), round(default + 10))
    step = max(1 if param_type == "int" else 0.1, round((hi - lo) / 20, 2))
    return lo, hi, step


def _extract_optimizable_params(rules_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per name ``bindable_params`` lists, ranged from its first site's value."""
    params: list[dict[str, Any]] = []
    for name, sites in bindable_params(RuleSet.model_validate(rules_dict)).items():
        site = sites[0]
        default = float(site.default)
        lo: float
        hi: float
        if site.target == "value":
            ptype = _infer_type(site.default)
            lo = max(0, round(default * 0.25))
            hi = round(default * 3) if default > 0 else 100
            step = max(1.0, round((hi - lo) / 20, 2))
        else:
            ptype = "int" if isinstance(site.default, int) else "float"
            lo, hi, step = _suggest_range(site.key, default, ptype)
        params.append(
            {
                "name": name,
                "label": _humanize_label(name),
                "type": ptype,
                "default": default,
                "min": lo,
                "max": hi,
                "step": step,
            }
        )
    return params


def get_strategy_params(strategy_id: str) -> list[dict[str, Any]]:
    """Extract optimizable parameters from a strategy's condition tree."""
    path = strategy_files.find_strategy_file(strategy_id)
    data = strategy_files.parse_strategy_file(path) if path is not None else None
    if data is None:
        raise service_error(
            404, "strategy_not_found", f"Strategy {strategy_id} not found", "not_found"
        )
    parameters = data.get("parameters")
    rules_dict = parameters.get("rules") if isinstance(parameters, dict) else None
    if not rules_dict:
        return []
    try:
        return _extract_optimizable_params(rules_dict)
    except ValueError as exc:
        raise _invalid_strategy(strategy_id, str(exc)) from exc


# ===========================================================================
# Hyperparameter optimisation
# ===========================================================================

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


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
    # informational: a terminal row's status/error columns are authoritative, not fetch.state
    fetch: dict[str, Any] | None = None
    provenance: JobProvenanceModel | None = None


class TrialRecordResponse(BaseModel):
    trial_number: int
    params: dict[str, Any]
    sharpe: float
    total_return: float
    max_drawdown: float
    total_trades: int
    status: str = "ok"
    error: str | None = None


class HyperoptHoldoutResponse(BaseModel):
    """The best config re-run once on the holdout window the optimiser never saw."""

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
    n_trials: int = 0
    failed_trials: int = 0
    seed: int = 42
    train_start: datetime | None = None
    train_end: datetime | None = None
    holdout_start: datetime | None = None
    holdout_end: datetime | None = None
    holdout: HyperoptHoldoutResponse | None = None
    holdout_psr: float | None = None
    dsr: float | None = None
    pbo: float | None = None
    provenance: JobProvenanceModel | None = None


class HyperoptHistoryEntry(BaseModel):
    task_id: str
    status: str
    strategy_id: str
    completed_trials: int
    total_trials: int
    best_so_far: float | None = None
    created_at: str
    error: str | None = None
    provenance: JobProvenanceModel | None = None


# ---------------------------------------------------------------------------
# Hyperopt operations
# ---------------------------------------------------------------------------


def _validated_space(defs: list[ParamDefRequest]) -> ParameterSpace:
    """Structurally valid ``ParameterSpace`` or a 422 ``invalid_param_space``."""
    try:
        return ParameterSpace(
            params=[
                ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
                for p in defs
            ]
        )
    except ValueError as exc:
        raise service_error(422, "invalid_param_space", str(exc), "invalid") from exc


def _check_param_names(
    space: ParameterSpace, config: StrategyConfig, strategy_class: type[BaseStrategy]
) -> None:
    """Reject names the strategy cannot read, before anything is queued."""
    try:
        unknown = unknown_param_names(space, config, strategy_class)
    except ValueError as exc:
        raise _invalid_strategy(config.id, str(exc)) from exc
    if unknown:
        raise service_error(
            422,
            "invalid_param_space",
            f"param_space names the strategy never reads: {', '.join(unknown)} "
            "(strategy_params lists the names a rule-based strategy binds)",
            "invalid",
        )


async def submit_hyperopt(
    pool: Any, body: HyperoptRunRequest, *, auto_fetch: bool = False
) -> dict[str, Any]:
    """Queue a hyperparameter optimisation run on the worker.

    Returns a ``task_id`` to poll for progress and results. With ``auto_fetch``
    on, a range with no stored data is queued anyway — the worker fetches it.
    """
    space = _validated_space(body.param_space)
    strategy_class, config = _load_backtest_strategy(body.strategy_id)
    _check_param_names(space, config, strategy_class)
    pf = await _preflight(
        pool,
        body.strategy_id,
        body.timeframe,
        body.start_date,
        body.end_date,
        auto_fetch=auto_fetch,
    )
    await check_model_pin(pf.pool, pf.config, body.model_version_id)
    async with db_errors("queueing the job"):
        task_id = await jobs.enqueue(
            pool,
            "hyperopt",
            body.model_dump(mode="json"),
            pf.config.model_dump(mode="json"),
            {"completed_trials": 0, "total_trials": body.max_trials, "best_so_far": None},
        )
    return {"task_id": task_id, "status": "queued"}


async def _hyperopt_row(pool: Any, task_id: str) -> Any:
    row = await jobs.get(pool, task_id, "hyperopt")
    if row is None:
        raise service_error(
            404, "task_not_found", f"Hyperopt task {task_id} not found", "not_found"
        )
    return row


async def get_hyperopt_progress(pool: Any, task_id: str) -> dict[str, Any]:
    """Poll progress of a hyperopt job."""
    row = await _hyperopt_row(pool, task_id)
    request = jobs.json_value(row["request"]) or {}
    detail = jobs.json_value(row["detail"]) or {}
    return {
        "task_id": task_id,
        "status": row["status"],
        "strategy_id": request.get("strategy_id", ""),
        "created_at": row["created_at"].isoformat(),
        "completed_trials": detail.get("completed_trials", 0),
        "total_trials": detail.get("total_trials", request.get("max_trials", 0)),
        "best_so_far": detail.get("best_so_far"),
        "current_trial_params": detail.get("current_trial_params"),
        "last_trial_sharpe": detail.get("last_trial_sharpe"),
        "start_date": request.get("start_date", ""),
        "end_date": request.get("end_date", ""),
        "error": row["error"],
        "fetch": detail.get("fetch"),
        "provenance": detail.get("provenance"),
    }


async def get_hyperopt_results(pool: Any, task_id: str) -> dict[str, Any]:
    """Return the full result of a completed hyperopt run."""
    row = await _hyperopt_row(pool, task_id)
    if row["status"] != "completed":
        raise ServiceError(
            409, f"Hyperopt task {task_id} is not completed (status: {row['status']})"
        )
    result = jobs.json_value(row["result"])
    if result is None:
        raise service_error(
            404, "result_not_found", f"No result stored for hyperopt task {task_id}", "not_found"
        )
    return {"task_id": task_id, "status": row["status"], **result}


async def list_hyperopt_history(pool: Any) -> list[dict[str, Any]]:
    """Return a summary of all hyperopt runs (most recent first)."""
    entries = []
    for row in await jobs.list_jobs(pool, "hyperopt"):
        request = jobs.json_value(row["request"]) or {}
        detail = jobs.json_value(row["detail"]) or {}
        entries.append(
            {
                "task_id": row["id"],
                "status": row["status"],
                "strategy_id": request.get("strategy_id", ""),
                "completed_trials": detail.get("completed_trials", 0),
                "total_trials": detail.get("total_trials", request.get("max_trials", 0)),
                "best_so_far": detail.get("best_so_far"),
                "created_at": row["created_at"].isoformat(),
                "error": row["error"],
                "provenance": detail.get("provenance"),
            }
        )
    return entries


# ===========================================================================
# Walk-forward validation
# ===========================================================================

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


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
    selected_params: dict[str, Any] = Field(default_factory=dict)
    n_trials: int = 0
    is_start: datetime | None = None
    is_end: datetime | None = None
    oos_start: datetime | None = None
    oos_end: datetime | None = None


class WalkForwardResultResponse(BaseModel):
    task_id: str
    status: str
    folds: list[FoldMetricsResponse]
    aggregated_sharpe: float | None
    aggregated_return: float | None
    aggregated_drawdown: float | None
    out_of_sample_equity: list[float]
    oos_psr: float | None = None
    efficiency: float | None = None
    anchored: bool = False
    seed: int = 42
    provenance: JobProvenanceModel | None = None


# ---------------------------------------------------------------------------
# Walk-forward operations
# ---------------------------------------------------------------------------


def _span_covers(
    stored: tuple[datetime, datetime, int], body: WalkForwardRunRequest, timeframe: Timeframe
) -> bool:
    """True when no fetch could change the window sizing: the stored span covers the
    requested grid's ends AND its bar count saturates that span's grid. A covered span
    with a sparse interior still defers — ``fold_windows`` sizes from the bar count, and
    the worker fetches the gaps first, then sizes from the real bars."""
    first, last, bar_count = stored
    if first > ceil_to_tf(body.start_date, timeframe) or last < floor_to_tf(
        body.end_date, timeframe
    ):
        return False
    grid_bars = int((last - first).total_seconds() // timeframe_seconds(timeframe)) + 1
    return bar_count >= grid_bars


async def submit_walkforward(
    pool: Any, body: WalkForwardRunRequest, *, auto_fetch: bool = False
) -> dict[str, Any]:
    """Queue a walk-forward validation run on the worker.

    Returns a ``task_id`` to poll for results. The submit-time ``range_too_short``
    fast-fail fires only when the stored span already fully covers the requested
    range AND its bars saturate the span's grid (no fetch could change the window
    sizing); anything shorter or sparser defers to the worker's post-load decision
    on the really-loaded span.
    """
    space = _validated_space(body.param_space)
    strategy_class, config = _load_backtest_strategy(body.strategy_id)
    _check_param_names(space, config, strategy_class)
    pf = await _preflight(
        pool,
        body.strategy_id,
        body.timeframe,
        body.start_date,
        body.end_date,
        auto_fetch=auto_fetch,
        read_span=True,
    )
    if pf.stored is not None and _span_covers(pf.stored, body, pf.timeframe):
        first, last, bar_count = pf.stored
        k, ratio = body.fold_count, body.train_pct / (1 - body.train_pct)
        if fold_windows(bar_count, (last - first).days, k, body.train_pct) is None:
            needed = math.ceil(MIN_WINDOW_DAYS * (k + ratio) * max(1.0, 1 / ratio))
            raise service_error(
                422,
                "range_too_short",
                f"range too short for {k} folds: stored bars span {first.isoformat()} to "
                f"{last.isoformat()}; {k} out-of-sample windows and one in-sample window at "
                f"train_pct {body.train_pct} of at least {MIN_WINDOW_DAYS} days each need "
                f"about {needed} days",
                "invalid",
                available_first_ts=first.isoformat(),
                available_last_ts=last.isoformat(),
            )
    await check_model_pin(pf.pool, pf.config, body.model_version_id)
    async with db_errors("queueing the job"):
        task_id = await jobs.enqueue(
            pool,
            "walkforward",
            body.model_dump(mode="json"),
            pf.config.model_dump(mode="json"),
            {},
        )
    return {"task_id": task_id, "status": "queued"}


async def get_walkforward_results(pool: Any, task_id: str) -> dict[str, Any]:
    """Return fold metrics and aggregated OOS equity for a completed walk-forward run."""
    row = await jobs.get(pool, task_id, "walkforward")
    if row is None:
        raise service_error(
            404, "task_not_found", f"Walk-forward task {task_id} not found", "not_found"
        )
    if row["status"] != "completed":
        detail = f"Walk-forward task {task_id} is not completed (status: {row['status']})"
        if row["error"]:
            detail = f"{detail}: {row['error']}"
        raise ServiceError(409, detail)
    result = jobs.json_value(row["result"])
    if result is None:
        raise service_error(
            404,
            "result_not_found",
            f"No result stored for walk-forward task {task_id}",
            "not_found",
        )
    job_detail = jobs.json_value(row["detail"]) or {}
    return {
        "task_id": task_id,
        "status": row["status"],
        **result,
        "provenance": job_detail.get("provenance"),
    }
