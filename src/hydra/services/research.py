"""Research jobs: optimisable strategy parameters, hyperopt and walk-forward runs."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from hydra.backtest.hyperopt import HyperoptResult, HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.jobs import (
    HyperoptRunRequest,
    Preflight,
    WalkForwardRunRequest,
    fail_task,
    load_task_bars,
)
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import (
    WalkForwardAnalyzer,
    WalkForwardResult,
    window_bar_counts,
)
from hydra.services import strategies as strategy_files
from hydra.services.backtest import DataProvenanceModel, _preflight, _spawn
from hydra.services.errors import ServiceError, service_error

logger = logging.getLogger(__name__)


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
# In-memory state for hyperopt tasks
# ---------------------------------------------------------------------------

_HYPEROPT_TASKS: dict[str, dict[str, Any]] = {}
_HYPEROPT_RESULTS: dict[str, HyperoptResult] = {}


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


async def _run_hyperopt_task(task_id: str, body: HyperoptRunRequest, pf: Preflight) -> None:
    """Execute a hyperopt run on stored bars and keep the result in memory."""
    task = _HYPEROPT_TASKS[task_id]
    task["status"] = "running"

    try:
        loaded = await load_task_bars(task, pf, body.start_date, body.end_date)
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
# Hyperopt operations
# ---------------------------------------------------------------------------


def list_hyperopt_history() -> list[dict[str, Any]]:
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


async def submit_hyperopt(pool: Any, body: HyperoptRunRequest) -> dict[str, Any]:
    """Start a hyperparameter optimisation run on stored bars.

    Returns a ``task_id`` to poll for progress and results.
    """
    try:
        for p in body.param_space:
            ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
    except ValueError as exc:
        raise service_error(422, "invalid_param_space", str(exc), "invalid") from exc
    pf = await _preflight(pool, body.strategy_id, body.timeframe, body.start_date, body.end_date)
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
        raise service_error(
            404, "task_not_found", f"Hyperopt task {task_id} not found", "not_found"
        )
    return _HYPEROPT_TASKS[task_id]


def get_hyperopt_progress(task_id: str) -> dict[str, Any]:
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


def get_hyperopt_results(task_id: str) -> dict[str, Any]:
    """Return the full result of a completed hyperopt run."""
    t = _hyperopt_task(task_id)
    if t["status"] != "completed":
        raise ServiceError(409, f"Hyperopt task {task_id} is not completed (status: {t['status']})")
    result = _HYPEROPT_RESULTS.get(task_id)
    if result is None:
        raise service_error(
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


def _window_fits(
    first: datetime, last: datetime, bar_count: int, in_sample: int, out_of_sample: int
) -> bool:
    days = (last - first).days
    if days <= 0:
        return False
    is_bars, oos_bars = window_bar_counts(bar_count, days, in_sample, out_of_sample)
    return is_bars > 0 and oos_bars > 0 and is_bars + oos_bars <= bar_count


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_walkforward_task(task_id: str, body: WalkForwardRunRequest, pf: Preflight) -> None:
    """Execute a walk-forward analysis on stored bars."""
    task = _WALKFORWARD_TASKS[task_id]
    task["status"] = "running"

    try:
        loaded = await load_task_bars(task, pf, body.start_date, body.end_date)
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
            fail_task(task, f"range too short for {body.fold_count} folds")
            return

        _WALKFORWARD_RESULTS[task_id] = result
        task["status"] = "completed"
        task["fold_count"] = len(result.folds)

    except Exception as exc:
        logger.exception("Walk-forward task %s failed", task_id)
        fail_task(task, str(exc))


# ---------------------------------------------------------------------------
# Walk-forward operations
# ---------------------------------------------------------------------------


async def submit_walkforward(pool: Any, body: WalkForwardRunRequest) -> dict[str, Any]:
    """Start a walk-forward validation run on stored bars.

    Returns a ``task_id`` to poll for results.
    """
    pf = await _preflight(pool, body.strategy_id, body.timeframe, body.start_date, body.end_date)
    if pf.stored is not None:
        first, last, bar_count = pf.stored
        in_sample, out_of_sample = _walkforward_months(first, last, body.fold_count, body.train_pct)
        if not _window_fits(first, last, bar_count, in_sample, out_of_sample):
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
    task_id = f"wf-{uuid.uuid4().hex[:8]}"
    _WALKFORWARD_TASKS[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "fold_count": 0,
        "request": body.model_dump(mode="json"),
    }
    _spawn(_run_walkforward_task(task_id, body, pf))
    return {"task_id": task_id, "status": "queued"}


def get_walkforward_results(task_id: str) -> dict[str, Any]:
    """Return fold metrics and aggregated OOS equity for a completed walk-forward run."""
    if task_id not in _WALKFORWARD_TASKS:
        raise service_error(
            404, "task_not_found", f"Walk-forward task {task_id} not found", "not_found"
        )
    t = _WALKFORWARD_TASKS[task_id]
    if t["status"] != "completed":
        detail = f"Walk-forward task {task_id} is not completed (status: {t['status']})"
        if t.get("error"):
            detail = f"{detail}: {t['error']}"
        raise ServiceError(409, detail)
    result = _WALKFORWARD_RESULTS.get(task_id)
    if result is None:
        raise service_error(
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
