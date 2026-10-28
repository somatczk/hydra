"""Research jobs: optimisable strategy parameters, hyperopt and walk-forward runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from hydra.backtest import jobs
from hydra.backtest.hyperopt import ParamDef, ParameterSpace, unknown_param_names
from hydra.backtest.jobs import (
    HyperoptRunRequest,
    ParamDefRequest,
    WalkForwardRunRequest,
    walkforward_months,
    window_fits,
)
from hydra.core.types import Timeframe
from hydra.data.bars import ceil_to_tf, floor_to_tf, timeframe_seconds
from hydra.services import strategies as strategy_files
from hydra.services.backtest import (
    DataProvenanceModel,
    _load_backtest_strategy,
    _preflight,
)
from hydra.services.errors import ServiceError, db_errors, service_error
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
    return _extract_optimizable_params(rules_dict)


# ===========================================================================
# Hyperparameter optimisation
# ===========================================================================

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class HyperoptRunResponse(BaseModel):
    task_id: str
    status: str = "queued"


class JobProvenanceModel(DataProvenanceModel):
    """Provenance read from a job row: carries the code and config that ran it (D2 decision)."""

    hydra_version: str | None = None
    config_hash: str | None = None


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


def _check_param_names(space: ParameterSpace, config: StrategyConfig) -> None:
    """Reject names the strategy cannot read, before anything is queued."""
    unknown = unknown_param_names(space, config)
    if unknown:
        raise service_error(
            422,
            "invalid_param_space",
            f"param_space names the strategy never reads: {', '.join(unknown)}",
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
    _, config = _load_backtest_strategy(body.strategy_id)
    _check_param_names(space, config)
    pf = await _preflight(
        pool,
        body.strategy_id,
        body.timeframe,
        body.start_date,
        body.end_date,
        auto_fetch=auto_fetch,
    )
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
    with a sparse interior still defers — ``window_fits`` sizes from the bar count, and
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
    _, config = _load_backtest_strategy(body.strategy_id)
    _check_param_names(space, config)
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
        in_sample, out_of_sample = walkforward_months(first, last, body.fold_count, body.train_pct)
        if not window_fits(first, last, bar_count, in_sample, out_of_sample):
            raise service_error(
                422,
                "range_too_short",
                f"range too short for {body.fold_count} folds: stored bars span "
                f"{first.isoformat()} to {last.isoformat()}, one window needs "
                f"{in_sample + out_of_sample} months",
                "invalid",
                available_first_ts=first.isoformat(),
                available_last_ts=last.isoformat(),
            )
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
