"""The trader process startup, after the backtest worker's pattern (D5).

``main()`` loads the config, validates the credential key, creates the pool with a retry
budget, waits for the schema at the Alembic head (the API process owns migrations, like the
engine the worker waits for), starts the engine (``SessionManager.startup()``: risk state,
recovery, display rows), then serves the internal API on :8000 with the full-check
HealthMonitor until SIGTERM — and shuts the engine down gracefully. The heartbeat file
(``python -m hydra.trader check``) is the Docker healthcheck's freshness anchor: the schema
wait keeps it alive, and the serving loop refreshes it every 10 s, so a stalled process goes
unhealthy within ``HEARTBEAT_MAX_AGE_S``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from hydra.data.migrations.runner import current_revision, head_revision

logger = logging.getLogger(__name__)

DB_CONNECT_BUDGET_S = 30.0
HEARTBEAT_MAX_AGE_S = 90.0
HEARTBEAT_TOUCH_S = 10.0
SCHEMA_POLL_S = 2.0
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
        await _serve(cfg, pool, mgr)
    finally:
        await pool.close()
