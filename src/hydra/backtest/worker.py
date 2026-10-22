"""The durable backtest worker: claims jobs from ``backtest_jobs`` and runs them to completion.

Entry point is ``python -m hydra.backtest.worker``; ``check`` (the Docker healthcheck) exits 0
only while the heartbeat file is fresh. Jobs run directly on the worker's event loop — the runner
yields every 100 bars and hyperopt yields per trial, so ``cancel()`` and the job timeout apply
natively. The worker never runs migrations (the engine owns them): it waits until the schema is
at the Alembic head, then requeues rows a dead worker left running (up to three attempts).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from hydra.backtest import jobs
from hydra.backtest.hyperopt import HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.jobs import (
    BacktestRunRequest,
    HyperoptRunRequest,
    JobOutcome,
    WalkForwardRunRequest,
    import_backtest_strategy,
)
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import WalkForwardAnalyzer, WalkForwardResult
from hydra.data.migrations.runner import current_revision, head_revision
from hydra.risk.state import PgRiskStateStore
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)

JOB_TIMEOUT_S = 6 * 3600
TICK_S = 10.0
IDLE_SLEEP_S = 2.0
HEARTBEAT_MAX_AGE_S = 90.0
DB_CONNECT_BUDGET_S = 30.0
_DEFAULT_HEARTBEAT = Path(tempfile.gettempdir()) / "hydra-worker.heartbeat"
HEARTBEAT_PATH = str(_DEFAULT_HEARTBEAT)
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


class JobError(Exception):
    """The job cannot complete; the message is stored on the row."""


@dataclass
class JobRun:
    """The in-memory progress the tick task flushes to the row."""

    progress: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)
    persisted_result_id: str | None = None


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


async def run_backtest_job(pool: Any, job: Mapping[str, Any], run: JobRun) -> JobOutcome:
    """Execute a backtest on stored bars and persist it as ``bt-{job_id}``."""
    body = BacktestRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)

    if body.fetch_fresh:
        run.progress = 10.0
        fetch_error = await jobs.fetch_fresh_bars(pool, config, body.start_date, body.end_date)
        if fetch_error is not None:
            run.detail["fetch_error"] = fetch_error
    run.progress = 20.0

    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    run.detail["provenance"] = jobs.provenance_payload(provenance, config)
    run.progress = 30.0

    def _report(pct: float) -> None:
        run.progress = round(30.0 + pct * 60.0, 1)

    risk_config = (await PgRiskStateStore(pool).load_config()).config
    result = await BacktestRunner().run(
        strategy_class=strategy_cls,
        strategy_config=config,
        bars=bars,
        initial_capital=Decimal(str(body.initial_capital)),
        symbol=config.symbols[0],
        timeframe=config.timeframes.primary,
        on_progress=_report,
        risk_config=risk_config,
    )
    run.progress = 90.0

    result_id = f"bt-{job['id']}"
    stored = jobs.result_record(result_id, body, config, result, provenance)
    try:
        await jobs.persist_result(pool, stored, result.trades, provenance)
    except Exception as exc:
        raise JobError(f"result not persisted: {exc}") from exc
    run.persisted_result_id = result_id
    return JobOutcome(result_id=result_id)


async def run_hyperopt_job(pool: Any, job: Mapping[str, Any], run: JobRun) -> JobOutcome:
    """Execute a hyperopt run on stored bars; the result payload is stored on the row."""
    body = HyperoptRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)

    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    provenance_dict = jobs.provenance_payload(provenance, config)
    run.detail["provenance"] = provenance_dict

    space = ParameterSpace(
        params=[
            ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
            for p in body.param_space
        ]
    )
    best_seen = float("-inf")

    async def _on_trial(trial_number: int, completed: int, total: int, record: Any) -> None:
        nonlocal best_seen
        best_seen = max(best_seen, record.sharpe)
        run.detail["completed_trials"] = completed
        run.detail["total_trials"] = total
        run.detail["last_trial_sharpe"] = record.sharpe
        run.detail["current_trial_params"] = record.params
        run.detail["best_so_far"] = best_seen

    result = await HyperoptRunner(BacktestRunner()).run(
        strategy_class=strategy_cls,
        base_config=config,
        bars=bars,
        initial_capital=Decimal(str(body.initial_capital)),
        param_space=space,
        method=body.method,
        max_trials=body.max_trials,
        symbol=config.symbols[0],
        timeframe=config.timeframes.primary,
        on_trial_complete=_on_trial,
    )
    return JobOutcome(
        result={
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
            "provenance": provenance_dict,
        }
    )


def walkforward_payload(result: WalkForwardResult) -> dict[str, Any]:
    """The walk-forward result payload stored on the job row (the response body minus ids)."""
    folds = [
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
    return {
        "folds": folds,
        "aggregated_sharpe": agg.sharpe_ratio if agg is not None else None,
        "aggregated_return": float(agg.total_return) if agg is not None else None,
        "aggregated_drawdown": float(agg.max_drawdown) if agg is not None else None,
        "out_of_sample_equity": [float(eq) for eq in agg.equity_curve] if agg is not None else [],
    }


async def run_walkforward_job(pool: Any, job: Mapping[str, Any], run: JobRun) -> JobOutcome:
    """Execute a walk-forward analysis on stored bars; the folds are stored on the row."""
    body = WalkForwardRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)

    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    run.detail["provenance"] = jobs.provenance_payload(provenance, config)

    in_sample, out_of_sample = jobs.walkforward_months(
        provenance.first_ts, provenance.last_ts, body.fold_count, body.train_pct
    )
    run.detail["in_sample_months"] = in_sample
    run.detail["out_of_sample_months"] = out_of_sample

    analyzer = WalkForwardAnalyzer(initial_capital=Decimal(str(body.initial_capital)))
    result = await analyzer.run_walkforward(
        bars=bars,
        strategy_class=strategy_cls,
        strategy_config=config,
        in_sample_months=in_sample,
        out_of_sample_months=out_of_sample,
        symbol=config.symbols[0],
        timeframe=config.timeframes.primary,
    )
    if not result.folds:
        raise JobError(f"range too short for {body.fold_count} folds")
    run.detail["fold_count"] = len(result.folds)
    return JobOutcome(result=walkforward_payload(result))


Handler = Callable[[Any, Mapping[str, Any], JobRun], Coroutine[Any, Any, JobOutcome]]

_HANDLERS: dict[str, Handler] = {
    "backtest": run_backtest_job,
    "hyperopt": run_hyperopt_job,
    "walkforward": run_walkforward_job,
}


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def touch_heartbeat(path: str | None = None) -> None:
    """Refresh the heartbeat file the Docker healthcheck watches."""
    heartbeat = Path(path or HEARTBEAT_PATH)
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def check(path: str | None = None) -> int:
    """Exit code for the healthcheck: 0 only while the heartbeat file is fresh."""
    try:
        age = time.time() - Path(path or HEARTBEAT_PATH).stat().st_mtime
    except OSError:
        return 1
    return 0 if age <= HEARTBEAT_MAX_AGE_S else 1


# ---------------------------------------------------------------------------
# Claiming and running one job
# ---------------------------------------------------------------------------


async def _tick_loop(
    pool: Any, job_id: str, run: JobRun, job_task: asyncio.Task[JobOutcome], tick_s: float
) -> None:
    """Every tick: touch the heartbeat, flush progress to the row, honour a cancel request."""
    while not job_task.done():
        touch_heartbeat()
        try:
            if await jobs.tick(pool, job_id, run.progress, run.detail):
                job_task.cancel()
                return
        except Exception:
            # a DB blip must not kill the tick (and with it the heartbeat) or the job
            logger.warning("Progress flush for %s failed; retrying", job_id, exc_info=True)
        await asyncio.sleep(tick_s)


async def _cancel_on_stop(stop: asyncio.Event, job_task: asyncio.Task[JobOutcome]) -> None:
    await stop.wait()
    job_task.cancel()


async def _finish_with_retry(
    pool: Any, job_id: str, status: str, *, detail: dict[str, Any] | None, **outcome: Any
) -> None:
    """Persist the terminal row state, retrying until the database takes it.

    The outcome exists only in this process, so a DB blip at the finish write is outlived here
    instead of failing the job; if it lasts, the stale heartbeat lets Docker restart the worker
    and the requeue path takes over.
    """
    delay = 0.5
    while True:
        try:
            await jobs.finish(pool, job_id, status, detail=detail, **outcome)
            return
        except Exception:
            logger.warning("Finishing %s failed; retrying in %.1fs", job_id, delay, exc_info=True)
            await asyncio.sleep(delay)
        delay = min(delay * 2, 5.0)


async def _discard_partial_result(pool: Any, run: JobRun) -> None:
    """Drop a result a cancelled or timed-out run committed but never finished the job with.

    A cancel is honoured on the next tick, which can land after the result transaction has
    committed; the job row then says cancelled, so no half-claimed result may stay behind.
    """
    if run.persisted_result_id is None:
        return
    try:
        await jobs.delete_result_rows(pool, run.persisted_result_id)
    except Exception:
        logger.warning(
            "Could not drop the partial result %s", run.persisted_result_id, exc_info=True
        )


async def _run_claimed(
    pool: Any,
    job: Mapping[str, Any],
    *,
    timeout_s: float = JOB_TIMEOUT_S,
    tick_s: float = TICK_S,
    stop: asyncio.Event | None = None,
) -> None:
    """Run one claimed job to a terminal row state (or requeue it when shutting down)."""
    job_id = job["id"]
    handler = _HANDLERS.get(job["kind"])
    if handler is None:
        await _finish_with_retry(
            pool, job_id, "failed", detail=None, error=f"unknown job kind: {job['kind']}"
        )
        return

    run = JobRun(detail=jobs.json_value(job["detail"]) or {})
    job_task = asyncio.create_task(handler(pool, job, run))
    tick_task = asyncio.create_task(_tick_loop(pool, job_id, run, job_task, tick_s))
    stop_task = asyncio.create_task(_cancel_on_stop(stop, job_task)) if stop is not None else None
    try:
        async with asyncio.timeout(timeout_s):
            outcome = await job_task
    except TimeoutError:
        job_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job_task
        await _discard_partial_result(pool, run)
        await _finish_with_retry(
            pool, job_id, "failed", detail=run.detail, error=f"timed out after {timeout_s}s"
        )
    except asyncio.CancelledError:
        if stop is not None and stop.is_set():
            await jobs.requeue_for_shutdown(pool, job_id)
        else:
            await _discard_partial_result(pool, run)
            await _finish_with_retry(pool, job_id, "cancelled", detail=run.detail)
    except JobError as exc:
        await _finish_with_retry(pool, job_id, "failed", detail=run.detail, error=str(exc))
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        await _discard_partial_result(pool, run)
        await _finish_with_retry(pool, job_id, "failed", detail=run.detail, error=str(exc))
    else:
        await _finish_with_retry(
            pool,
            job_id,
            "completed",
            detail=run.detail,
            result=outcome.result,
            result_id=outcome.result_id,
        )
    finally:
        tick_task.cancel()
        # the tick may already have died on its own error; it must not fail the finished job
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tick_task
        if stop_task is not None:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task


async def run_once(
    pool: Any,
    *,
    timeout_s: float = JOB_TIMEOUT_S,
    tick_s: float = TICK_S,
    worker_id: str = WORKER_ID,
) -> str | None:
    """Claim and run exactly one job; its id, or None when the queue is empty."""
    job = await jobs.claim(pool, worker_id)
    if job is None:
        return None
    await _run_claimed(pool, job, timeout_s=timeout_s, tick_s=tick_s)
    return str(job["id"])


# ---------------------------------------------------------------------------
# Worker startup and main loop
# ---------------------------------------------------------------------------


async def wait_for_schema(dsn: str, *, poll_s: float = 2.0) -> None:
    """Block until the engine has migrated the schema to the single Alembic head."""
    head = head_revision()
    while True:
        # alive and waiting: a long migration wait must not fail the file-age healthcheck
        touch_heartbeat()
        try:
            current = await current_revision(dsn)
        except Exception:
            current = None
            logger.warning("Reading the schema revision failed; retrying", exc_info=True)
        if current == head:
            return
        logger.info("Waiting for migrations (current=%s, head=%s)", current, head)
        await asyncio.sleep(poll_s)


async def _create_pool(dsn: str) -> Any:
    import asyncpg

    loop = asyncio.get_running_loop()
    deadline = loop.time() + DB_CONNECT_BUDGET_S
    delay = 0.5
    while True:
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
            if loop.time() + delay > deadline:
                raise
            touch_heartbeat()
            logger.warning(
                "Database not reachable yet (%s); retrying in %.1fs", type(exc).__name__, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


async def _serve_forever(pool: Any) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        try:
            job = await jobs.claim(pool, WORKER_ID)
        except Exception:
            # an idle worker must survive one failed claim (e.g. a DB restart) and retry
            logger.warning("Claiming a job failed; retrying after the idle sleep", exc_info=True)
            job = None
        if job is None:
            touch_heartbeat()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), IDLE_SLEEP_S)
            continue
        try:
            await _run_claimed(pool, job, stop=stop)
        except Exception:
            logger.exception(
                "Job %s could not reach a terminal state; it stays running and is requeued "
                "on the next worker start",
                job["id"],
            )
    logger.info("Backtest worker shutting down")


async def main() -> None:
    from hydra.core.config import load_config
    from hydra.core.logging import setup_logging

    cfg = load_config()
    setup_logging(level=cfg.logging.level, log_format=cfg.logging.format)
    dsn = cfg.database_url.get_secret_value() if cfg.database_url is not None else None
    if dsn is None:
        logger.error("DATABASE_URL is not set: the backtest worker needs the database")
        raise SystemExit(2)

    pool = await _create_pool(dsn)
    try:
        await wait_for_schema(dsn)
        await jobs.requeue_abandoned(pool)
        logger.info("Backtest worker started")
        await _serve_forever(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "check":
        sys.exit(check())
    if command != "run":
        print("usage: python -m hydra.backtest.worker [run|check]", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main())
