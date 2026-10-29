"""The trader process startup, after the backtest worker's pattern (D5).

``main()`` loads the config, validates the credential key, creates the pool with a retry
budget, waits for the schema at the Alembic head (the API process owns migrations, like the
engine the worker waits for), starts the engine (``SessionManager.startup()``: risk state,
recovery, display rows), measures every Production model's feature drift once
(``measure_startup_drift``, P5-D — startup-only, never a periodic task), then serves the
internal API on :8000 with the full-check HealthMonitor until SIGTERM — and shuts the engine
down gracefully. The heartbeat file (``python -m hydra.trader check``) is the Docker
healthcheck's freshness anchor: the schema wait keeps it alive, and the serving loop
refreshes it every 10 s, so a stalled process goes unhealthy within ``HEARTBEAT_MAX_AGE_S``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.data.migrations.runner import current_revision, head_revision

logger = logging.getLogger(__name__)

DB_CONNECT_BUDGET_S = 30.0
HEARTBEAT_MAX_AGE_S = 90.0
HEARTBEAT_TOUCH_S = 10.0
SCHEMA_POLL_S = 2.0
DRIFT_MIN_DISTINCT_VALUES = 300
"""A feature must carry at least this many distinct values in the current window
before a decile-PSI against its stored reference is honest: below it the chi-square
noise floor (~9/n) sits at the alert threshold, so sparsely-sampled features (the
1d block joins ~83 daily values into a 2000-hour matrix, the cyclical temporal
encodings hold 7 and 24) would read their own sampling noise as drift."""
_RETRY_MAX_DELAY_S = 5.0
HEARTBEAT_PATH = str(Path(tempfile.gettempdir()) / "hydra-trader.heartbeat")


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


async def wait_for_schema(dsn: str, *, poll_s: float = SCHEMA_POLL_S) -> None:
    """Block until the API process has migrated the schema to the single Alembic head."""
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
            return await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
            if loop.time() + delay > deadline:
                raise
            touch_heartbeat()
            logger.warning(
                "Database not reachable yet (%s); retrying in %.1fs", type(exc).__name__, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY_S)


async def _heartbeat_loop() -> None:
    while True:
        touch_heartbeat()
        await asyncio.sleep(HEARTBEAT_TOUCH_S)


def _drift_status(max_psi: float, any_all_nan: bool) -> str:
    """The drift band, from retraining.py's shared thresholds: low, moderate or high."""
    from hydra.ml.retraining import PSI_ALERT_THRESHOLD, PSI_RETRAIN_THRESHOLD

    if max_psi > PSI_RETRAIN_THRESHOLD:
        return "high"
    if max_psi > PSI_ALERT_THRESHOLD or any_all_nan:
        return "moderate"
    return "low"


async def measure_startup_drift(pool: Any) -> None:
    """Measure every Production model's feature drift once, at trader startup (P5-D).

    For each Production version: resolve the market it was trained on through its
    training job (the request's symbol, the strategy config's exchange), rebuild
    the recent feature window through the DatasetBuilder (the frozen spec off
    the registry row, at the spec's own window sizes — the same shape the
    training reference was computed over, so start-anchored features like the
    OBV rate of change stay comparable), score the quantile-PSI against the
    stored reference deciles, write the drift jsonb onto the row and set the
    drift gauges — the only producer, here in the trader, never inside
    ``hydra.ml`` (§8). Startup-only by the user's cut: no periodic re-run, and
    the alert the gauges feed pages the operator to retrain manually — nothing
    here retrains, promotes or demotes anything.

    A feature is only scored when its stored deciles are real bins and its
    current window carries enough distinct values (see
    ``DRIFT_MIN_DISTINCT_VALUES``); the jsonb carries both counts. A model that
    cannot be measured keeps its drift ``unknown``, loudly: an unreadable
    spec, a spec trained without reference quantiles, a version with no
    training-job provenance (uploads), a symbol with no stored bars, or no
    feature left that can be scored each skip with a log line. One model's
    failure never stops the others, and the whole task never raises: the
    trader serves even when drift cannot be read.
    """
    from hydra.ml.registry import ModelRegistry

    try:
        versions = await ModelRegistry(pool).list_production()
    except Exception:
        logger.exception("ml drift: listing Production models failed; nothing measured")
        return
    if not versions:
        logger.info("ml drift: no Production models to measure")
        return

    for version in versions:
        try:
            await _measure_one_model(pool, version)
        except Exception:
            logger.exception(
                "ml drift: measuring model %s v%s failed; its drift stays unknown",
                version.name,
                version.version,
            )


def _scored_features(
    window: Any, feature_names: tuple[str, ...], reference_quantiles: tuple[Any, ...]
) -> tuple[list[Any], int]:
    """The per-feature PSI scores, plus how many features could not be scored.

    A feature is skipped when its stored deciles are degenerate (duplicate
    edges — the 7-valued weekday encodings can never spread over ten uniform
    bins) or when its current window holds fewer distinct values than
    ``DRIFT_MIN_DISTINCT_VALUES`` (at that effective sample size the
    chi-square noise floor sits at the alert threshold, so the 1d block's
    joined daily values would read noise as drift). A feature with no stored
    quantiles at all scores zero; the NaN rate is taken for every feature,
    scored or not.
    """
    import numpy as np

    from hydra.ml.retraining import FeaturePsi, feature_psi

    scores: list[Any] = []
    unscored = 0
    for i in range(len(feature_names)):
        column = window[:, i]
        quantiles = reference_quantiles[i]
        nan_rate = float(np.isnan(column).mean()) if len(column) else 0.0
        distinct = len(set(column[~np.isnan(column)].tolist())) if len(column) else 0
        degenerate = quantiles is not None and len(set(quantiles)) < len(quantiles)
        if quantiles is None or degenerate or distinct < DRIFT_MIN_DISTINCT_VALUES:
            unscored += 1
            scores.append(FeaturePsi(psi=0.0, nan_rate=nan_rate))
        else:
            scores.append(feature_psi(column, quantiles))
    return scores, unscored


async def _measure_one_model(pool: Any, version: Any) -> None:
    """Measure one Production version and write its drift snapshot onto its row."""
    from hydra.backtest import jobs
    from hydra.core.metrics import set_ml_drift
    from hydra.data.errors import NoMarketDataError
    from hydra.data.storage import MarketDataRepository
    from hydra.ml.dataset import DatasetBuilder
    from hydra.ml.features import FeatureSpec
    from hydra.ml.registry import ModelRegistry

    if version.training_job_id is None:
        logger.info(
            "ml drift: model %s v%s has no training job, so no market to measure against",
            version.name,
            version.version,
        )
        return
    job = await jobs.get(pool, version.training_job_id, kind="ml_train")
    if job is None:
        logger.info(
            "ml drift: model %s v%s names training job %s that no longer exists",
            version.name,
            version.version,
            version.training_job_id,
        )
        return
    request = jobs.json_value(job["request"]) or {}
    strategy_config = jobs.json_value(job["strategy_config"]) or {}
    exchange = (strategy_config.get("exchange") or {}).get("exchange_id")
    symbol = request.get("symbol")
    if not symbol or not exchange:
        logger.info(
            "ml drift: model %s v%s training job %s carries no symbol/exchange to measure",
            version.name,
            version.version,
            version.training_job_id,
        )
        return
    try:
        spec = FeatureSpec.from_dict(version.feature_spec)
    except ValueError:
        logger.info(
            "ml drift: model %s v%s stores no readable feature spec; drift stays unknown",
            version.name,
            version.version,
        )
        return
    if spec.reference_quantiles is None:
        logger.info(
            "ml drift: model %s v%s was registered without reference quantiles; "
            "drift stays unknown",
            version.name,
            version.version,
        )
        return

    builder = DatasetBuilder(MarketDataRepository.from_pool(pool))
    try:
        matrix, _provenance = await builder.build(spec, str(exchange), str(symbol))
    except NoMarketDataError:
        logger.warning(
            "ml drift: no stored bars for %s %s; model %s v%s drift stays unknown",
            exchange,
            symbol,
            version.name,
            version.version,
        )
        return

    scores, unscored = _scored_features(
        matrix.features, spec.feature_names, spec.reference_quantiles
    )
    if unscored == len(scores):
        logger.info(
            "ml drift: model %s v%s has no feature decile bins can score; drift stays unknown",
            version.name,
            version.version,
        )
        return
    max_psi = max((score.psi for score in scores), default=0.0)
    max_nan_rate = max((score.nan_rate for score in scores), default=0.0)
    any_all_nan = any(score.nan_rate >= 1.0 for score in scores)
    snapshot = {
        "max_psi": max_psi,
        "status": _drift_status(max_psi, any_all_nan),
        "max_nan_rate": max_nan_rate,
        "scored_features": len(scores) - unscored,
        "unscored_features": unscored,
        "computed_at": datetime.now(UTC).isoformat(),
        "window": {
            "rows": len(matrix.features),
            "first_ts": matrix.timestamps[0].isoformat(),
            "last_ts": matrix.timestamps[-1].isoformat(),
            "exchange": str(exchange),
            "symbol": str(symbol),
        },
    }
    await ModelRegistry(pool).record_drift(version.name, version.version, snapshot)
    set_ml_drift(version.name, max_psi, max_nan_rate)
    logger.info(
        "ml drift measured: model=%s version=%s max_psi=%.4f nan_rate=%.4f status=%s "
        "scored=%d unscored=%d rows=%d",
        version.name,
        version.version,
        max_psi,
        max_nan_rate,
        snapshot["status"],
        snapshot["scored_features"],
        unscored,
        len(matrix.features),
    )


async def _serve(cfg: Any, pool: Any, mgr: Any) -> None:
    """Serve the internal API with the full-check monitor until SIGTERM (uvicorn traps it)."""
    import uvicorn

    from hydra.dashboard.health import HealthMonitor
    from hydra.trader.server import build_app

    app = build_app(cfg, pool, mgr)
    monitor = HealthMonitor(app)
    app.state.health_monitor = monitor
    await monitor.start()
    heartbeat = asyncio.create_task(_heartbeat_loop(), name="trader-heartbeat")
    try:
        await uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=8000, log_config=None, proxy_headers=False)
        ).serve()
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await monitor.stop()
        await mgr.graceful_shutdown()
        logger.info("Trader process shut down")


async def main() -> None:
    from hydra.core.config import load_config
    from hydra.core.encryption import require_credential_key
    from hydra.core.logging import setup_logging
    from hydra.execution.session_manager import SessionManager

    cfg = load_config()
    setup_logging(level=cfg.logging.level, log_format=cfg.logging.format)
    dsn = cfg.database_url.get_secret_value() if cfg.database_url is not None else None
    if dsn is None:
        logger.error("DATABASE_URL is not set: the trader needs the database")
        raise SystemExit(2)
    require_credential_key(
        cfg.credential_key.get_secret_value() if cfg.credential_key is not None else None
    )
    pool = await _create_pool(dsn)
    try:
        await wait_for_schema(dsn)
        mgr = SessionManager(db_pool=pool, config=cfg)
        await mgr.startup()
        logger.info("Trader engine started")
        await measure_startup_drift(pool)
        await _serve(cfg, pool, mgr)
    finally:
        await pool.close()
