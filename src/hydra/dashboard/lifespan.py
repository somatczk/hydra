"""Engine startup and shutdown.

Startup order: config, logging, credential key, DB pool, Alembic migrations, stored credentials,
system config, SessionManager, backtest cache. Any startup error propagates, so uvicorn exits
non-zero instead of serving without a schema or a key. Without DATABASE_URL (dev only) there is no
pool, no migration and no key requirement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from hydra.core.config import HydraConfig, load_config
from hydra.dashboard.deps import bind_config
from hydra.dashboard.routes import market_data

logger = logging.getLogger(__name__)

DB_CONNECT_BUDGET_S = 30.0
_RETRY_MAX_DELAY_S = 5.0


@asynccontextmanager
async def run(app: FastAPI) -> AsyncIterator[None]:
    """The FastAPI lifespan."""
    cfg = load_config()
    bind_config(app, cfg)
    app.state.db_pool = None
    app.state.exchange_credentials = {}
    app.state.exchange_credential_errors = {}
    app.state.system_config = None
    app.state.session_manager = None
    try:
        await _init_logging(cfg)
        dsn = cfg.database_url.get_secret_value() if cfg.database_url is not None else None
        if dsn is None:
            logger.warning("DATABASE_URL is not set: running without a database (dev only)")
        else:
            await _require_credential_key(cfg)
            app.state.db_pool = await _init_db_pool(dsn)
            await _run_migrations(dsn)
            await _load_exchange_credentials(app)
            await _load_system_config(app)
        await _init_session_manager(app, cfg)
        await _populate_backtest_cache(app)
        yield
    finally:
        await _shutdown_session_manager(app)
        await market_data.close_market_clients(app)
        await _close_db_pool(app)


# ---------------------------------------------------------------------------
# Startup steps
# ---------------------------------------------------------------------------


async def _init_logging(cfg: HydraConfig) -> None:
    from hydra.core.logging import setup_logging

    setup_logging(level=cfg.logging.level, log_format=cfg.logging.format)


async def _require_credential_key(cfg: HydraConfig) -> None:
    from hydra.core.encryption import require_credential_key

    key = cfg.credential_key.get_secret_value() if cfg.credential_key is not None else None
    require_credential_key(key)


async def _init_db_pool(dsn: str) -> Any:
    """Create the pool, retrying while the server is not reachable yet; then give up loudly."""
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
            logger.warning(
                "Database not reachable yet (%s); retrying in %.1fs", type(exc).__name__, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY_S)


async def _run_migrations(dsn: str) -> None:
    from hydra.data.migrations.runner import current_revision, upgrade_to_head

    await asyncio.to_thread(upgrade_to_head, dsn)
    logger.info("Database schema at revision %s", await current_revision(dsn))


async def _load_exchange_credentials(app: FastAPI) -> None:
    """Decrypt stored credentials; a row the key cannot decrypt is reported, not fatal."""
    from cryptography.fernet import InvalidToken

    from hydra.core.encryption import decrypt

    async with app.state.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT exchange_id, encrypted_key, encrypted_secret, encrypted_passphrase "
            "FROM exchange_credentials"
        )
    creds: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for row in rows:
        exchange_id = row["exchange_id"]
        try:
            creds[exchange_id] = {
                "api_key": decrypt(row["encrypted_key"]),
                "api_secret": decrypt(row["encrypted_secret"]),
                "passphrase": (
                    decrypt(row["encrypted_passphrase"]) if row["encrypted_passphrase"] else None
                ),
            }
        except InvalidToken:
            errors[exchange_id] = "undecryptable"
            logger.error(
                "Stored credentials for %s cannot be decrypted with HYDRA_CREDENTIAL_KEY; "
                "reconnect the exchange in Settings",
                exchange_id,
            )
    app.state.exchange_credentials = creds
    app.state.exchange_credential_errors = errors


async def _load_system_config(app: FastAPI) -> None:
    async with app.state.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM system_config")
    app.state.system_config = {row["key"]: json.loads(row["value"]) for row in rows}


async def _init_session_manager(app: FastAPI, cfg: HydraConfig) -> None:
    from hydra.execution.session_manager import SessionManager

    pool = app.state.db_pool
    mgr = SessionManager(db_pool=pool, config=cfg)
    if pool is not None:
        await mgr.load_recent_sessions()
        await mgr.recover_running_sessions()
        if mgr._init_task is not None:
            await mgr._init_task
    app.state.session_manager = mgr


async def _populate_backtest_cache(app: FastAPI) -> None:
    pool = app.state.db_pool
    if pool is None:
        return
    from hydra.dashboard.routes.backtest import populate_cache_from_db

    await populate_cache_from_db(pool)


# ---------------------------------------------------------------------------
# Shutdown steps
# ---------------------------------------------------------------------------


async def _shutdown_session_manager(app: FastAPI) -> None:
    mgr = getattr(app.state, "session_manager", None)
    if mgr is not None:
        await mgr.graceful_shutdown()


async def _close_db_pool(app: FastAPI) -> None:
    pool = getattr(app.state, "db_pool", None)
    if pool is not None:
        await pool.close()
