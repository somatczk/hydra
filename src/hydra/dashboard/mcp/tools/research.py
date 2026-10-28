"""Research tools (backtest, hyperopt, walk-forward, strategy params, data coverage).

Twelve tools registered on the shared server instance, each a thin call into the
service layer (the same functions the REST routes call). Inputs mirror the service
request models' fields — dates as ISO strings, ``param_space`` in the
``ParamDefRequest`` wire shape — and the models are constructed inside the tool so
pydantic revalidates and no schema drift is possible. Every ``ServiceError``
surfaces as a ``ToolError`` carrying the full error envelope verbatim, so a 409
``restart_required`` keeps its ``changed_fields`` and a 422 keeps its
``no_market_data`` extras. The three submission tools write a ``hydra.audit`` line
as the MCP principal: they create job rows (S8).
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from hydra.backtest.jobs import (
    BacktestRunRequest,
    HyperoptRunRequest,
    ParamDefRequest,
    WalkForwardRunRequest,
)
from hydra.core.types import ExchangeId, Timeframe
from hydra.dashboard.mcp import server
from hydra.dashboard.mcp.tools import _deps
from hydra.dashboard.security import audit
from hydra.services import backtest as backtest_svc
from hydra.services import data as data_svc
from hydra.services import research as research_svc
from hydra.services.errors import ServiceError


@server.tool(name="backtest_run", structured_output=False)
async def backtest_run(
    strategy_id: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
    name: str = "",
    timeframe: Timeframe | None = None,
    fetch_fresh: bool = False,
) -> str:
    """Queue a backtest of a strategy on stored bars (paper execution, no live orders).

    Dates are ISO strings (a bare YYYY-MM-DD is the whole UTC day). Returns
    ``{"task_id", "status": "queued"}``; poll ``backtest_status`` with the task_id.
    Nothing is simulated without stored bars: a range with no data is a 422
    ``no_market_data`` error carrying the stored coverage.
    """
    try:
        body = BacktestRunRequest(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            name=name,
            timeframe=timeframe,
            fetch_fresh=fetch_fresh,
        )
        result = await backtest_svc.submit_backtest(_deps.db_pool(), body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "backtest_run",
        strategy_id=strategy_id,
        task_id=result["task_id"],
    )
    return _deps.json_text(result)


@server.tool(name="backtest_status", structured_output=False)
async def backtest_status(task_id: str) -> str:
    """Check a backtest job: status (queued|running|completed|failed|cancelled), progress, error."""
    try:
        return _deps.json_text(await backtest_svc.get_backtest_status(_deps.db_pool(), task_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="backtest_cancel", structured_output=False)
async def backtest_cancel(job_id: str) -> str:
    """Cancel a queued or running backtest, hyperopt or walk-forward job by its id."""
    try:
        return _deps.json_text(await backtest_svc.cancel_job(_deps.db_pool(), job_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="backtest_results", structured_output=False)
async def backtest_results() -> str:
    """List persisted backtest result summaries, newest first (at most 200)."""
    try:
        return _deps.json_text(await backtest_svc.list_results(_deps.db_pool()))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="backtest_result", structured_output=False)
async def backtest_result(result_id: str) -> str:
    """Read one backtest result in full: metrics, equity curve, trades, provenance."""
    try:
        return _deps.json_text(await backtest_svc.get_result(_deps.db_pool(), result_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="hyperopt_run", structured_output=False)
async def hyperopt_run(
    strategy_id: str,
    start_date: str,
    end_date: str,
    param_space: list[ParamDefRequest],
    method: Literal["bayesian", "grid"] = "bayesian",
    max_trials: int = 50,
    timeframe: Timeframe | None = None,
    initial_capital: float = 10000.0,
) -> str:
    """Queue a hyperparameter optimisation on stored bars.

    ``param_space`` items are ``{name, type: int|float|categorical, low, high, choices}``;
    ``name`` must be a parameter the strategy reads (see ``strategy_params``). Returns
    ``{"task_id", "status": "queued"}``; poll ``hyperopt_progress`` and read
    ``hyperopt_results`` once completed.
    """
    try:
        body = HyperoptRunRequest(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            param_space=param_space,
            method=method,
            max_trials=max_trials,
            timeframe=timeframe,
            initial_capital=initial_capital,
        )
        result = await research_svc.submit_hyperopt(_deps.db_pool(), body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "hyperopt_run",
        strategy_id=strategy_id,
        task_id=result["task_id"],
    )
    return _deps.json_text(result)


@server.tool(name="hyperopt_progress", structured_output=False)
async def hyperopt_progress(task_id: str) -> str:
    """Poll a hyperopt job: trials completed/total, best metric so far, error."""
    try:
        return _deps.json_text(await research_svc.get_hyperopt_progress(_deps.db_pool(), task_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="hyperopt_results", structured_output=False)
async def hyperopt_results(task_id: str) -> str:
    """Read the full result of a completed hyperopt run: best params, per-trial records,
    holdout metrics and train/holdout windows when present."""
    try:
        return _deps.json_text(await research_svc.get_hyperopt_results(_deps.db_pool(), task_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="walkforward_run", structured_output=False)
async def walkforward_run(
    strategy_id: str,
    start_date: str,
    end_date: str,
    param_space: list[ParamDefRequest],
    method: Literal["bayesian", "grid"] = "grid",
    max_trials: int = 20,
    anchored: bool = False,
    fold_count: int = 6,
    train_pct: float = 0.75,
    timeframe: Timeframe | None = None,
    initial_capital: float = 10000.0,
) -> str:
    """Queue a walk-forward validation on stored bars (in-sample optimisation,
    out-of-sample verification per fold).

    ``max_trials`` is per fold; the stored range must fit one in-sample plus
    out-of-sample window per fold or the call fails 422 ``range_too_short`` with the
    stored coverage. Returns ``{"task_id", "status": "queued"}``; read
    ``walkforward_results`` once completed.
    """
    try:
        body = WalkForwardRunRequest(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            param_space=param_space,
            method=method,
            max_trials=max_trials,
            anchored=anchored,
            fold_count=fold_count,
            train_pct=train_pct,
            timeframe=timeframe,
            initial_capital=initial_capital,
        )
        result = await research_svc.submit_walkforward(_deps.db_pool(), body)
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
    except ValidationError as exc:
        raise _deps.invalid_arguments(exc) from exc
    audit(
        _deps.MCP_PRINCIPAL,
        "walkforward_run",
        strategy_id=strategy_id,
        task_id=result["task_id"],
    )
    return _deps.json_text(result)


@server.tool(name="walkforward_results", structured_output=False)
async def walkforward_results(task_id: str) -> str:
    """Read a completed walk-forward run: per-fold IS/OOS windows and metrics,
    selected params, aggregated out-of-sample equity."""
    try:
        return _deps.json_text(await research_svc.get_walkforward_results(_deps.db_pool(), task_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="strategy_params", structured_output=False)
async def strategy_params(strategy_id: str) -> str:
    """List a strategy's optimisable parameters (name, label, type, default, min, max,
    step) — the names ``hyperopt_run``/``walkforward_run`` accept in ``param_space``."""
    try:
        return _deps.json_text(research_svc.get_strategy_params(strategy_id))
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc


@server.tool(name="data_coverage", structured_output=False)
async def data_coverage(exchange_id: ExchangeId, symbol: str, timeframe: Timeframe) -> str:
    """The stored bar coverage for one market: first/last bar timestamp (ISO, UTC) and
    bar count — what backtests can actually run on. 404 ``no_stored_data`` when
    nothing is stored for the market."""
    try:
        return _deps.json_text(
            await data_svc.bar_coverage(_deps.db_pool(), exchange_id, symbol, timeframe)
        )
    except ServiceError as exc:
        raise _deps.tool_error(exc) from exc
