from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request, status

from hydra.backtest.jobs import BacktestRunRequest, HyperoptRunRequest, WalkForwardRunRequest
from hydra.dashboard.routes._db import (
    ServiceErrorRoute,
    request_auto_fetch,
    request_pool,
)
from hydra.services import backtest as svc
from hydra.services import research
from hydra.services.backtest import (
    BacktestRenameRequest,
    BacktestResultDetail,
    BacktestResultSummary,
    BacktestRunResponse,
    BacktestStatus,
    BacktestStrategyOption,
    BacktestVerification,
    CancelJobResponse,
    JobListEntry,
)
from hydra.services.research import (
    HyperoptHistoryEntry,
    HyperoptProgressResponse,
    HyperoptResultResponse,
    HyperoptRunResponse,
    StrategyParamDef,
    WalkForwardResultResponse,
    WalkForwardRunResponse,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"], route_class=ServiceErrorRoute)


@router.post("/run", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(body: BacktestRunRequest, request: Request) -> dict[str, Any]:
    """Start a backtest on stored bars. Returns a task_id to poll for status.

    A range with no stored data is auto-fetched by the worker before the run
    (``data.auto_fetch_enabled``).
    """
    return await svc.submit_backtest(
        request_pool(request), body, auto_fetch=request_auto_fetch(request)
    )


@router.get("/status/{task_id}", response_model=BacktestStatus)
async def get_backtest_status(task_id: str, request: Request) -> dict[str, Any]:
    """Check the progress of a queued, running or finished backtest job."""
    return await svc.get_backtest_status(request_pool(request), task_id)


@router.post("/jobs/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    """Cancel a queued or running backtest, hyperopt or walk-forward job."""
    return await svc.cancel_job(request_pool(request), job_id)


@router.get("/jobs", response_model=list[JobListEntry])
async def list_jobs(
    request: Request,
    kind: Literal["backtest", "hyperopt", "walkforward"] | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List durable backtest, hyperopt and walk-forward jobs, newest first."""
    return await svc.list_jobs(request_pool(request), kind, limit)


@router.get("/results", response_model=list[BacktestResultSummary])
async def list_results(request: Request) -> list[dict[str, Any]]:
    """List persisted backtest result summaries, newest first (at most 200)."""
    return await svc.list_results(request_pool(request))


@router.get("/results/{result_id}", response_model=BacktestResultDetail)
async def get_result_detail(result_id: str, request: Request) -> dict[str, Any]:
    """Get detailed backtest result including equity curve and trades."""
    return await svc.get_result(request_pool(request), result_id)


@router.patch("/results/{result_id}", response_model=BacktestResultSummary)
async def rename_result(
    result_id: str, body: BacktestRenameRequest, request: Request
) -> dict[str, Any]:
    """Rename a backtest result in the database."""
    return await svc.rename_result(request_pool(request), result_id, body.name)


@router.delete("/results/{result_id}", status_code=204)
async def delete_result(result_id: str, request: Request) -> None:
    """Delete a backtest result and its trades from the database."""
    await svc.delete_result(request_pool(request), result_id)


@router.get("/results/{result_id}/verify", response_model=BacktestVerification)
async def verify_result(result_id: str, request: Request) -> dict[str, Any]:
    """Verify a backtest result by recomputing metrics from trades."""
    return await svc.verify_result(request_pool(request), result_id)


@router.get("/strategies", response_model=list[BacktestStrategyOption])
async def list_backtest_strategies() -> list[dict[str, Any]]:
    """List strategies available for backtesting, with their primary timeframe."""
    return svc.list_backtest_strategies()


@router.get(
    "/strategies/{strategy_id}/params",
    response_model=list[StrategyParamDef],
)
async def get_strategy_params(strategy_id: str) -> list[dict[str, Any]]:
    """Extract optimizable parameters from a strategy's condition tree."""
    return research.get_strategy_params(strategy_id)


@router.get(
    "/hyperopt/history",
    response_model=list[HyperoptHistoryEntry],
)
async def list_hyperopt_history(request: Request) -> list[dict[str, Any]]:
    """Return a summary of all hyperopt runs (most recent first)."""
    return await research.list_hyperopt_history(request_pool(request))


@router.post(
    "/hyperopt",
    response_model=HyperoptRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_hyperopt(body: HyperoptRunRequest, request: Request) -> dict[str, Any]:
    """Start a hyperparameter optimisation run on stored bars.

    Returns a ``task_id`` to poll for progress and results.
    """
    return await research.submit_hyperopt(
        request_pool(request), body, auto_fetch=request_auto_fetch(request)
    )


@router.get(
    "/hyperopt/{task_id}",
    response_model=HyperoptProgressResponse,
)
async def get_hyperopt_progress(task_id: str, request: Request) -> dict[str, Any]:
    """Poll progress of a running hyperopt job."""
    return await research.get_hyperopt_progress(request_pool(request), task_id)


@router.get(
    "/hyperopt/{task_id}/results",
    response_model=HyperoptResultResponse,
)
async def get_hyperopt_results(task_id: str, request: Request) -> dict[str, Any]:
    """Return the full result of a completed hyperopt run."""
    return await research.get_hyperopt_results(request_pool(request), task_id)


@router.post(
    "/walkforward",
    response_model=WalkForwardRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_walkforward(body: WalkForwardRunRequest, request: Request) -> dict[str, Any]:
    """Start a walk-forward validation run on stored bars.

    Returns a ``task_id`` to poll for results.
    """
    return await research.submit_walkforward(
        request_pool(request), body, auto_fetch=request_auto_fetch(request)
    )


@router.get(
    "/walkforward/{task_id}/results",
    response_model=WalkForwardResultResponse,
)
async def get_walkforward_results(task_id: str, request: Request) -> dict[str, Any]:
    """Return fold metrics and aggregated OOS equity for a completed walk-forward run."""
    return await research.get_walkforward_results(request_pool(request), task_id)


@router.post("/results/{result_id}/share")
async def share_result(result_id: str, request: Request) -> dict[str, str]:
    """Generate a public sharing token for a backtest result."""
    return await svc.share_result(request_pool(request), result_id)


@router.get("/shared/{token}")
async def get_shared_result(token: str, request: Request) -> dict[str, Any]:
    """Public read-only access to a shared backtest result (no auth required)."""
    return await svc.get_shared_result(request_pool(request), token)
