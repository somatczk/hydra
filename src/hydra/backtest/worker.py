"""The durable backtest worker: claims jobs from ``backtest_jobs`` and runs them to completion.

Entry point is ``python -m hydra.backtest.worker``; ``check`` (the Docker healthcheck) exits 0
only while the heartbeat file is fresh. Jobs run directly on the worker's event loop — the runner
yields every 100 bars and hyperopt yields per trial, so ``cancel()`` and the job timeout apply
natively. The worker never runs migrations (the engine owns them): it waits until the schema is
at the Alembic head, then requeues rows its own previous incarnation left running.

Any number of workers share the queue. A claimed job is leased to its worker: every tick renews
``heartbeat_at``, and an idle worker requeues (up to three attempts) any running row whose
heartbeat is older than ``backtest.job_lease_s`` — its worker died or lost the database. Every
tick and terminal write is fenced by ``worker_id``, so a worker whose lease expired abandons the
job without touching the row or the result its new owner writes. ``WORKER_ID`` must be unique
per live worker: the container hostname (its id) plus the pid.

A job never fails "data unavailable": the worker holds ONE on-demand fetch engine (and one
refcounted REST client pool) for its whole lifetime, and every job fetches exactly its missing
bars before the run — the fetch phase maps to progress 0→20 and the engine's progress dict rides
the job detail, flushed live by the 10 s tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hydra.backtest import jobs, perp_funding
from hydra.backtest.hyperopt import HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.jobs import (
    BacktestRunRequest,
    HyperoptRunRequest,
    JobOutcome,
    MlTrainRunRequest,
    WalkForwardRunRequest,
    import_backtest_strategy,
)
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import WalkForwardAnalyzer, fold_windows
from hydra.core.types import ExchangeId, MarketType, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.data.migrations.runner import current_revision, head_revision
from hydra.data.on_demand import OnDemandFetchService, ProgressCallback
from hydra.data.storage import MarketDataRepository
from hydra.execution.client_factory import RestHandle, RestPoolFactory, SharedRestClients
from hydra.ml.consumers import LiveFeaturesPredictor, attach_research_ml
from hydra.ml.dataset import DatasetBuilder
from hydra.ml.export import export_xgboost_to_onnx, validate_onnx_export
from hydra.ml.features import FeatureSpec
from hydra.ml.registry import ModelRegistration, ModelRegistry
from hydra.ml.serving import MlServingError
from hydra.ml.training import ModelTrainer
from hydra.risk.pretrade import StrategyLimits, effective_limits
from hydra.risk.state import PgRiskStateStore
from hydra.strategy.config import StrategyConfig
from hydra.strategy.ml_access import bound_providers

if TYPE_CHECKING:
    from hydra.core.config import HydraConfig

logger = logging.getLogger(__name__)

JOB_TIMEOUT_S = 6 * 3600
TICK_S = 10.0
IDLE_SLEEP_S = 2.0
HEARTBEAT_MAX_AGE_S = 90.0
SWEEP_S = 30.0
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
    lease_lost: bool = False


# ---------------------------------------------------------------------------
# The fetch engine: exactly one instance per worker process
# ---------------------------------------------------------------------------


class _WorkerFetchClients:
    """The worker-lifetime REST clients behind the on-demand fetch engine.

    One refcounted pool per worker process: the first fetching job constructs the
    exchange client — ``load_markets`` is paid once per worker, not per job — and
    shutdown releases the held handle, which closes the client.
    """

    def __init__(self, rest_factory: RestPoolFactory | None = None) -> None:
        if rest_factory is None:
            self._pool = SharedRestClients()
        else:
            self._pool = SharedRestClients(rest_factory)
        self._handles: dict[ExchangeId, RestHandle] = {}

    async def client(self, exchange_id: ExchangeId) -> Any:
        """The pooled client for one exchange, constructed on first use."""
        handle = self._handles.get(exchange_id)
        if handle is None:
            handle = await self._pool.handle(exchange_id)
            # Two concurrent cold fetches both acquire; the loser must not leak its reference.
            existing = self._handles.setdefault(exchange_id, handle)
            if existing is not handle:
                await handle.release()
                handle = existing
        return handle.client

    def fetch_engine(self, cfg: HydraConfig) -> OnDemandFetchService | None:
        """The one engine per worker process; ``None`` while the kill switch is off."""
        if not cfg.data.auto_fetch_enabled:
            return None
        return OnDemandFetchService.from_config(cfg.data, self.client)

    async def close(self) -> None:
        for handle in self._handles.values():
            await handle.release()
        self._handles.clear()


# ---------------------------------------------------------------------------
# The fetch phase: progress 0→20, the engine's dict live on the job detail
# ---------------------------------------------------------------------------


def _bars_owed(ranges: list[list[str]], timeframe: Timeframe) -> int:
    """Bars the engine is fetching over its grid-aligned ranges (inclusive bounds)."""
    step = timeframe_seconds(timeframe)
    return sum(
        int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // step)
        + 1
        for start, end in ranges
    )


def _fetch_progress(run: JobRun, timeframe: Timeframe) -> ProgressCallback:
    """Map the engine's progress stream onto the 0→20 band and ``detail['fetch']``."""

    def on_progress(progress: dict[str, Any]) -> None:
        run.detail["fetch"] = progress
        owed = _bars_owed(progress["ranges"], timeframe)
        if owed:
            run.progress = min(20.0, round(20.0 * progress["bars_fetched"] / owed, 1))

    return on_progress


def _fetch_failed(run: JobRun, **extra: Any) -> None:
    """Stamp the terminal failure on the fetch detail, keeping the last counters."""
    run.detail["fetch"] = {**(run.detail.get("fetch") or {}), "state": "failed", **extra}


async def _attach_ml(
    pool: Any, config: StrategyConfig, model_version_id: uuid.UUID | None
) -> tuple[dict[str, LiveFeaturesPredictor], list[dict[str, Any]]]:
    """Attach the models the config consumes for this job only; a refusal fails the job.

    Runs where the pool is, before any bar is fetched: an un-promoted name, an
    unknown or unconsumed pinned version or a missing artifact fails loudly with
    the model named — never a completed run whose every entry was vetoed
    ``error`` (the runner itself has no pool to attach with).
    """
    try:
        return await attach_research_ml(pool, _ml_models_dir(), config, model_version_id)
    except (MlServingError, ValueError) as exc:
        raise JobError(str(exc)) from exc


async def _ensure_bars(
    pool: Any,
    fetch_engine: OnDemandFetchService | None,
    config: StrategyConfig,
    start: datetime,
    end: datetime,
    run: JobRun,
    *,
    force: bool = False,
) -> None:
    """The fetch phase: make the range's bars available before the handlers load them.

    Every declared timeframe fetches its own stream (a confirmation stream with no
    stored bars would silently never veto). ``fetch_engine=None`` (the kill switch)
    skips the phase — today's behaviour. A
    fetch past patience fails the job retryable with the reason on the row; a range
    the exchange genuinely lacks re-raises the P0-B no-market-data vocabulary.
    """
    if fetch_engine is None:
        return
    try:
        await jobs.ensure_job_bars(
            pool,
            fetch_engine,
            config,
            start,
            end,
            force=force,
            progress_for=lambda tf: _fetch_progress(run, tf),
        )
    except DataFetchError as exc:
        _fetch_failed(run, reason=exc.reason)
        raise JobError(exc.message) from exc
    except NoMarketDataError:
        _fetch_failed(run)
        raise
    run.progress = 20.0


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


async def _funding_preflight(
    pool: Any,
    fetch_engine: OnDemandFetchService | None,
    config: StrategyConfig,
    body: BacktestRunRequest | HyperoptRunRequest | WalkForwardRunRequest,
    run: JobRun,
) -> dict[str, list[Any]] | None:
    """A FUTURES config's funding rows for the job window, recorded on the run; None for spot."""
    if config.exchange.market_type is not MarketType.FUTURES:
        return None
    provider = fetch_engine.client_provider() if fetch_engine is not None else None
    funding, funding_state = await perp_funding.prepare_backtest_funding(
        pool, provider, config.symbols[0], body.start_date, body.end_date
    )
    run.detail["funding"] = {
        "state": funding_state,
        "rows_loaded": sum(len(rows) for rows in funding.values()),
    }
    return funding


async def run_backtest_job(
    pool: Any, fetch_engine: OnDemandFetchService | None, job: Mapping[str, Any], run: JobRun
) -> JobOutcome:
    """Execute a backtest on stored bars and persist it as ``bt-{job_id}``.

    A FUTURES config runs the perp paper executor (P4-C): its funding preflight tops the
    stored ``ts.funding_rates`` rows up for the window through the fetch engine's pooled
    client (skipped while auto-fetch is off) and loads what is stored — the executor
    accrues only from those rows.

    A config that consumes a model (an enabled ``ml_overlay`` or the ML-native strategy)
    has it attached first (``_attach_ml``) and bound for this run only: Production, or
    the request's pinned ``model_version_id`` of any stage; the served versions ride the
    job's provenance.

    The strategy's stored override is min-rendered into the risk config (tighter-only),
    so a limited strategy backtests under its real caps, exactly as a live session of
    it would be checked.
    """
    body = BacktestRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)
    models, served = await _attach_ml(pool, config, body.model_version_id)

    await _ensure_bars(
        pool, fetch_engine, config, body.start_date, body.end_date, run, force=body.fetch_fresh
    )
    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    extras = await jobs.load_job_extra_bars(pool, config, body.start_date, body.end_date)
    run.detail["provenance"] = {
        **jobs.provenance_payload(provenance, config, extras),
        "ml_models": served,
    }
    run.progress = 30.0

    funding = await _funding_preflight(pool, fetch_engine, config, body, run)

    def _report(pct: float) -> None:
        run.progress = round(30.0 + pct * 60.0, 1)

    store = PgRiskStateStore(pool)
    risk_config = effective_limits(
        (await store.load_config()).config,
        (await store.load_strategy_overrides()).get(config.id, StrategyLimits()),
    )
    with bound_providers(models):
        result = await BacktestRunner().run(
            strategy_class=strategy_cls,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal(str(body.initial_capital)),
            symbol=config.symbols[0],
            timeframe=config.timeframes.primary,
            on_progress=_report,
            risk_config=risk_config,
            funding=funding,
            extra_bars=extras,
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


async def run_hyperopt_job(
    pool: Any, fetch_engine: OnDemandFetchService | None, job: Mapping[str, Any], run: JobRun
) -> JobOutcome:
    """Execute a hyperopt run on stored bars; the result payload is stored on the row.

    A FUTURES config gets the plain backtest's funding preflight: the stored
    ``ts.funding_rates`` rows are topped up for the window and loaded once, so every
    trial and the holdout accrue only from the rows inside their own bars. Every
    declared non-primary timeframe is loaded and replayed with the primary stream.
    """
    body = HyperoptRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)
    models, served = await _attach_ml(pool, config, body.model_version_id)

    await _ensure_bars(pool, fetch_engine, config, body.start_date, body.end_date, run)
    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    extras = await jobs.load_job_extra_bars(pool, config, body.start_date, body.end_date)
    provenance_dict = {
        **jobs.provenance_payload(provenance, config, extras),
        "ml_models": served,
    }
    run.detail["provenance"] = provenance_dict
    funding = await _funding_preflight(pool, fetch_engine, config, body, run)

    space = ParameterSpace(
        params=[
            ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
            for p in body.param_space
        ]
    )
    best_seen: float | None = None

    async def _on_trial(trial_number: int, completed: int, total: int, record: Any) -> None:
        nonlocal best_seen
        # only completed, trading trials count as the best so far
        if record.status == "ok":
            best_seen = record.sharpe if best_seen is None else max(best_seen, record.sharpe)
        run.detail["completed_trials"] = completed
        run.detail["total_trials"] = total
        run.detail["last_trial_sharpe"] = record.sharpe
        run.detail["current_trial_params"] = record.params
        run.detail["best_so_far"] = best_seen

    with bound_providers(models):
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
            funding=funding,
            extra_bars=extras,
        )
    payload = result.to_dict()
    payload["provenance"] = provenance_dict
    return JobOutcome(result=payload)


async def run_walkforward_job(
    pool: Any, fetch_engine: OnDemandFetchService | None, job: Mapping[str, Any], run: JobRun
) -> JobOutcome:
    """Execute a walk-forward analysis on stored bars; the folds are stored on the row.

    A FUTURES config gets the plain backtest's funding preflight: the stored
    ``ts.funding_rates`` rows are topped up for the window and loaded once — the
    full-range dict serves every sub-window, and the executor accrues only the rows
    inside each run's own bars. Every declared non-primary timeframe is loaded and
    replayed with the primary stream in every fold.
    """
    body = WalkForwardRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))
    strategy_cls = import_backtest_strategy(config.strategy_class)
    models, served = await _attach_ml(pool, config, body.model_version_id)

    await _ensure_bars(pool, fetch_engine, config, body.start_date, body.end_date, run)
    bars, provenance = await jobs.load_job_bars(pool, config, body.start_date, body.end_date)
    extras = await jobs.load_job_extra_bars(pool, config, body.start_date, body.end_date)
    run.detail["provenance"] = {
        **jobs.provenance_payload(provenance, config, extras),
        "ml_models": served,
    }
    funding = await _funding_preflight(pool, fetch_engine, config, body, run)

    windows = fold_windows(
        provenance.bar_count,
        (provenance.last_ts - provenance.first_ts).days,
        body.fold_count,
        body.train_pct,
    )
    if windows is None:
        raise JobError(f"range too short for {body.fold_count} folds")
    run.detail["in_sample_bars"], run.detail["out_of_sample_bars"] = windows

    space = ParameterSpace(
        params=[
            ParamDef(name=p.name, type=p.type, low=p.low, high=p.high, choices=p.choices)
            for p in body.param_space
        ]
    )
    analyzer = WalkForwardAnalyzer(initial_capital=Decimal(str(body.initial_capital)))
    with bound_providers(models):
        result = await analyzer.run_walkforward(
            bars=bars,
            strategy_class=strategy_cls,
            strategy_config=config,
            param_space=space,
            fold_count=body.fold_count,
            train_pct=body.train_pct,
            method=body.method,
            max_trials=body.max_trials,
            anchored=body.anchored,
            symbol=config.symbols[0],
            timeframe=config.timeframes.primary,
            funding=funding,
            extra_bars=extras,
        )
    run.detail["fold_count"] = len(result.folds)
    return JobOutcome(result=result.to_dict())


# ---------------------------------------------------------------------------
# The ml_train job: dataset -> trainer -> ONNX export -> registry (P5-B)
# ---------------------------------------------------------------------------


def _ml_models_dir() -> Path:
    """The mounted models directory, from the config — never from a job payload."""
    from hydra.core.config import load_config

    return Path(load_config().ml.models_dir)


def _ml_reads(spec: FeatureSpec) -> tuple[tuple[Timeframe, int], ...]:
    """The dataset's three (timeframe, window) reads, in builder order."""
    return (
        (Timeframe.H1, spec.window_1h),
        (Timeframe.H4, spec.window_4h),
        (Timeframe.D1, spec.window_1d),
    )


def _ml_fetch_progress(
    run: JobRun, index: int, total: int, timeframe: Timeframe
) -> ProgressCallback:
    """Map one read's engine progress onto its slice of the 0→20 fetch band."""
    span = 20.0 / total

    def on_progress(progress: dict[str, Any]) -> None:
        run.detail["fetch"] = {**progress, "timeframe": str(timeframe)}
        owed = _bars_owed(progress["ranges"], timeframe)
        if owed:
            run.progress = min(20.0, round(span * (index + progress["bars_fetched"] / owed), 1))

    return on_progress


async def _ensure_ml_bars(
    pool: Any,
    fetch_engine: OnDemandFetchService | None,
    exchange: ExchangeId,
    symbol: str,
    spec: FeatureSpec,
    run: JobRun,
) -> None:
    """Make each window's bars available before the dataset builder reads them.

    The same engine and budget as the backtest fetch phase; each read covers
    exactly its (validated, bounded) window, so the phase is capped by
    ``data.fetch_max_bars`` per read and the dataset by the window sums.
    """
    if fetch_engine is None:
        return
    repo = MarketDataRepository.from_pool(pool)
    reads = _ml_reads(spec)
    now = datetime.now(UTC)
    for index, (timeframe, window) in enumerate(reads):
        start = now - timedelta(seconds=timeframe_seconds(timeframe) * window)
        try:
            await fetch_engine.ensure_available(
                repo,
                exchange,
                symbol,
                timeframe,
                start,
                now,
                on_progress=_ml_fetch_progress(run, index, len(reads), timeframe),
            )
        except DataFetchError as exc:
            _fetch_failed(run, reason=exc.reason)
            raise JobError(exc.message) from exc
        except NoMarketDataError:
            _fetch_failed(run)
            raise
    run.progress = 20.0


def _onnx_metadata(onnx_path: Path) -> dict[str, str]:
    """The artifact's custom metadata, read back the way serving reads it."""
    import onnxruntime as ort

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    meta = session.get_modelmeta()
    return dict(meta.custom_metadata_map) if meta is not None else {}


async def run_ml_train_job(
    pool: Any, fetch_engine: OnDemandFetchService | None, job: Mapping[str, Any], run: JobRun
) -> JobOutcome:
    """Train, export and register one model version — the serving bridge.

    DatasetBuilder → ModelTrainer → ``export_xgboost_to_onnx`` (the ONNX carries
    the feature-spec metadata) → ``validate_onnx_export`` gates the artifact
    before anything is registered → ``ModelRegistry.register`` under the job's
    id. Bounded three ways: the request's window/horizon bounds, the engine's
    ``fetch_max_bars`` per read, and the same 6 h job timeout as every kind.
    The parity sample is the most recent fully-warmed window — the rows serving
    will actually see.

    The register happens before the terminal ``jobs.finish`` write, so a worker
    death in between is bridged idempotently: the re-run serves the committed
    version (0013 keeps it the only one), and a cancel landing after the
    register discards it with the job.
    """
    body = MlTrainRunRequest.model_validate(jobs.json_value(job["request"]))
    config = StrategyConfig.model_validate(jobs.json_value(job["strategy_config"]))

    # the requeue story: a job that already registered serves its committed version
    already = await ModelRegistry(pool).get_by_training_job(str(job["id"]))
    if already is not None:
        return JobOutcome(result=already.to_dict())

    spec = jobs.ml_feature_spec(body)
    exchange = config.exchange.exchange_id

    await _ensure_ml_bars(pool, fetch_engine, exchange, body.symbol, spec, run)

    repo = MarketDataRepository.from_pool(pool)
    matrix, provenance = await DatasetBuilder(repo).build(spec, exchange, body.symbol)
    run.detail["provenance"] = provenance.to_dict()
    run.progress = 40.0

    trained = await asyncio.to_thread(ModelTrainer().train_xgboost, matrix, None)
    run.detail["metrics"] = dict(trained.metrics)
    run.progress = 70.0

    # the artifact file is named for the job id: server-generated, never request data
    artifact = _ml_models_dir() / f"{job['id']}.onnx"
    try:
        await asyncio.to_thread(
            export_xgboost_to_onnx,
            trained.model,
            trained.feature_names,
            artifact,
            trained.feature_spec,
        )
        metadata = await asyncio.to_thread(_onnx_metadata, artifact)
        if metadata.get("feature_version") != str(spec.spec_version) or metadata.get(
            "feature_names"
        ) != json.dumps(trained.feature_names):
            msg = (
                f"the onnx artifact does not carry the trained feature spec "
                f"(feature_version={metadata.get('feature_version')!r})"
            )
            raise JobError(msg)
        parity = await asyncio.to_thread(
            validate_onnx_export, trained.model, artifact, matrix.features[-64:]
        )
    except JobError:
        raise
    except Exception as exc:
        raise JobError(f"onnx export failed validation: {exc!r}") from exc
    if not parity:
        msg = "the onnx export does not reproduce the trained model's predictions"
        raise JobError(msg)
    run.progress = 90.0

    version = await ModelRegistry(pool).register(
        ModelRegistration(
            name=body.name,
            model_type=trained.model_type,
            metrics=dict(trained.metrics),
            feature_spec=trained.feature_spec.to_dict(),
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            trained_at=trained.train_timestamp,
            created_by=f"job:{job['id']}",
            training_job_id=str(job["id"]),
        )
    )
    return JobOutcome(result=version.to_dict())


Handler = Callable[
    [Any, OnDemandFetchService | None, Mapping[str, Any], JobRun],
    Coroutine[Any, Any, JobOutcome],
]

_HANDLERS: dict[str, Handler] = {
    "backtest": run_backtest_job,
    "hyperopt": run_hyperopt_job,
    "walkforward": run_walkforward_job,
    "ml_train": run_ml_train_job,
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
    pool: Any,
    job_id: str,
    worker_id: str,
    run: JobRun,
    job_task: asyncio.Task[JobOutcome],
    tick_s: float,
) -> None:
    """Every tick: touch the heartbeat, renew the lease, honour a cancel request or a lost lease."""
    while not job_task.done():
        touch_heartbeat()
        try:
            if await jobs.tick(pool, job_id, run.progress, run.detail, worker_id):
                job_task.cancel()
                return
        except jobs.LeaseLostError:
            run.lease_lost = True
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
    pool: Any,
    job_id: str,
    worker_id: str,
    status: str,
    *,
    detail: dict[str, Any] | None,
    **outcome: Any,
) -> None:
    """Persist the terminal row state, retrying until the database takes it.

    The outcome exists only in this process, so a DB blip at the finish write is outlived here
    instead of failing the job; if it outlasts the lease, a peer requeues the job and the fenced
    write then lands on no row.
    """
    delay = 0.5
    while True:
        try:
            if not await jobs.finish(
                pool, job_id, status, detail=detail, worker_id=worker_id, **outcome
            ):
                logger.warning(
                    "Job %s is no longer leased to %s; dropped its outcome", job_id, worker_id
                )
            return
        except Exception:
            logger.warning("Finishing %s failed; retrying in %.1fs", job_id, delay, exc_info=True)
            await asyncio.sleep(delay)
        delay = min(delay * 2, 5.0)


async def _discard_partial_result(pool: Any, job: Mapping[str, Any], run: JobRun) -> None:
    """Drop a result a cancelled or timed-out run committed but never finished the job with.

    A cancel is honoured on the next tick, which can land after the result
    transaction has committed; the job row then says cancelled, so no
    half-claimed result may stay behind. An ml_train job's half-claimed result
    is its registered version: the registry is asked by ``training_job_id``, so
    the discard holds even when the cancel lands between the register commit
    and the handler's return (and a human promotion outlives the job).
    """
    if run.persisted_result_id is not None:
        try:
            await jobs.delete_result_rows(pool, run.persisted_result_id)
        except Exception:
            logger.warning(
                "Could not drop the partial result %s", run.persisted_result_id, exc_info=True
            )
    if job["kind"] == "ml_train":
        try:
            await ModelRegistry(pool).discard_training_job_version(str(job["id"]))
        except Exception:
            logger.warning(
                "Could not drop the model registered by job %s", job["id"], exc_info=True
            )


async def _run_claimed(
    pool: Any,
    job: Mapping[str, Any],
    *,
    timeout_s: float = JOB_TIMEOUT_S,
    tick_s: float = TICK_S,
    stop: asyncio.Event | None = None,
    fetch_engine: OnDemandFetchService | None = None,
) -> None:
    """Run one claimed job to a terminal row state (or requeue it when shutting down).

    Every row write is fenced by the claim's ``worker_id``; a lost lease abandons the job
    without a write or a discard — the result rows now belong to the job's new owner.
    """
    job_id = job["id"]
    owner = job["worker_id"]
    handler = _HANDLERS.get(job["kind"])
    if handler is None:
        await _finish_with_retry(
            pool, job_id, owner, "failed", detail=None, error=f"unknown job kind: {job['kind']}"
        )
        return

    run = JobRun(detail=jobs.json_value(job["detail"]) or {})
    job_task = asyncio.create_task(handler(pool, fetch_engine, job, run))
    tick_task = asyncio.create_task(_tick_loop(pool, job_id, owner, run, job_task, tick_s))
    stop_task = asyncio.create_task(_cancel_on_stop(stop, job_task)) if stop is not None else None
    try:
        async with asyncio.timeout(timeout_s):
            outcome = await job_task
    except TimeoutError:
        job_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job_task
        await _discard_partial_result(pool, job, run)
        await _finish_with_retry(
            pool, job_id, owner, "failed", detail=run.detail, error=f"timed out after {timeout_s}s"
        )
    except asyncio.CancelledError:
        if run.lease_lost:
            logger.warning("Job %s lease expired; abandoned to its new owner", job_id)
        elif stop is not None and stop.is_set():
            await jobs.requeue_for_shutdown(pool, job_id, owner)
        else:
            await _discard_partial_result(pool, job, run)
            await _finish_with_retry(pool, job_id, owner, "cancelled", detail=run.detail)
    except JobError as exc:
        await _finish_with_retry(pool, job_id, owner, "failed", detail=run.detail, error=str(exc))
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        await _discard_partial_result(pool, job, run)
        await _finish_with_retry(pool, job_id, owner, "failed", detail=run.detail, error=str(exc))
    else:
        await _finish_with_retry(
            pool,
            job_id,
            owner,
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
    fetch_engine: OnDemandFetchService | None = None,
) -> str | None:
    """Claim and run exactly one job; its id, or None when the queue is empty."""
    job = await jobs.claim(pool, worker_id)
    if job is None:
        return None
    await _run_claimed(pool, job, timeout_s=timeout_s, tick_s=tick_s, fetch_engine=fetch_engine)
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


async def _serve_forever(
    pool: Any, fetch_engine: OnDemandFetchService | None, lease_s: float
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    next_sweep = 0.0
    while not stop.is_set():
        if loop.time() >= next_sweep:
            # a busy worker cannot claim anyway, so sweeping between jobs is enough
            try:
                await jobs.requeue_abandoned(pool, lease_s)
            except Exception:
                logger.warning("Sweeping expired job leases failed; retrying", exc_info=True)
            next_sweep = loop.time() + SWEEP_S
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
            await _run_claimed(pool, job, stop=stop, fetch_engine=fetch_engine)
        except Exception:
            logger.exception(
                "Job %s could not reach a terminal state; it stays running until its lease "
                "expires and a worker requeues it",
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
    fetch_clients = _WorkerFetchClients()
    try:
        await wait_for_schema(dsn)
        lease_s = cfg.backtest.job_lease_s
        await jobs.requeue_abandoned(pool, lease_s, worker_id=WORKER_ID)
        fetch_engine = fetch_clients.fetch_engine(cfg)
        logger.info(
            "Backtest worker started (id %s, auto-fetch %s)",
            WORKER_ID,
            "on" if fetch_engine is not None else "off",
        )
        await _serve_forever(pool, fetch_engine, lease_s)
    finally:
        await pool.close()
        await fetch_clients.close()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "check":
        sys.exit(check())
    if command != "run":
        print("usage: python -m hydra.backtest.worker [run|check]", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main())
