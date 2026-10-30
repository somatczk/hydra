"""Engine startup and shutdown.

Startup order: config, logging, credential key, DB pool, Alembic migrations, stored credentials,
system config, then the Redis ping when the opt-in event fan-out is on (``events.bus: redis``:
Redis becomes tier-1 and an unreachable server refuses startup, like the database), then one of
two modes behind the same routes: with ``trader.url`` set (the D5
process split) a :class:`~hydra.dashboard.trader_client.RemoteTrader` is bound on
``app.state.trader`` and no ``SessionManager`` is ever constructed in this process; unset, the
single-process engine starts here (``SessionManager.startup()``: risk state, recovery, display
rows) with its ``LocalTrader`` backend. Then the MCP mount (only when ``HYDRA_MCP_TOKEN`` is set:
no token, no ``/mcp`` surface) and the HealthMonitor (first readiness run; ``/livez`` turns
green — the engine-side full set, or the split set with the remote-trader check), then the
universe refresher (the daily top-100 ranking, only with a pool; a refresh failure logs a
warning and retries hourly, never blocking startup or readiness), then the data-cache
sweeper (the bounded LRU over auto-fetched bars, only with a pool; a failed sweep counts
one metric and yields to the next cycle, never blocking startup or a read), then the
funding-rate refresher (the binanceusdm top-up for the running perp sessions' symbols,
only with a pool; with no running perp sessions each cycle is a no-op, and a failed cycle
is a warning retried hourly, never blocking startup), then the risk relay (the ``/ws/risk``
producer and the Telegram alerter, only with a pool; it must page with no browser
connected, so it starts here rather than on the first connection, and a failed poll is
retried on its next tick, never blocking startup). Shutdown cancels the risk relay first,
then the funding
refresher, then the universe refresher, then the cache sweeper, then closes the MCP session
manager, unmounts ``/mcp`` and
resets ``app.state.mcp`` (the session manager is
single-use, so a re-entered lifespan remounts a fresh transport instead of finding the closed
mount shadowing it), then stops the monitor. Any startup error propagates, so uvicorn exits
non-zero instead of serving without a schema or a key. Without DATABASE_URL (dev only) there is
no pool, no migration and no key requirement. ``app.state.session_clients`` (a
``ClientFactory``), when set before startup, replaces the public exchange clients of every
session, and ``app.state.health_monitor_factory`` builds the ``HealthMonitor`` (the paper smoke
test injects its fakes and a monitor clock there), as does
``app.state.universe_refresher_factory`` for the universe refresher,
``app.state.data_cache_sweeper_factory`` for the cache sweeper,
``app.state.funding_refresher_factory`` for the funding-rate refresher and
``app.state.risk_relay_factory`` for the risk relay.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from hydra.core.config import ConfigError, HydraConfig, load_config
from hydra.core.event_bus import require_redis
from hydra.dashboard.deps import bind_config
from hydra.dashboard.routes import market_data
from hydra.execution.client_factory import ClientFactory

if TYPE_CHECKING:
    from hydra.dashboard.health import HealthMonitor

logger = logging.getLogger(__name__)

DB_CONNECT_BUDGET_S = 30.0
REDIS_CONNECT_BUDGET_S = 30.0
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
    app.state.trader = None
    app.state.health_monitor = None
    app.state.universe_refresher = None
    app.state.data_cache_sweeper = None
    app.state.funding_refresher = None
    app.state.risk_relay = None
    clients: ClientFactory | None = getattr(app.state, "session_clients", None)
    mcp_stack: AsyncExitStack | None = None
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
        await _require_redis(cfg)
        if cfg.trader.url is not None:
            # the split (D5): no SessionManager here — every engine op goes through RemoteTrader
            from hydra.dashboard.trader_client import RemoteTrader
            from hydra.services.trader import TraderBackend

            app.state.session_manager = None
            backend: TraderBackend = RemoteTrader(cfg)
            app.state.trader = backend
        else:
            await _init_session_manager(app, cfg, clients)
        if cfg.mcp_token is not None:
            from hydra.dashboard.mcp import mount_mcp

            app.state.mcp = mount_mcp(app, cfg)
            mcp_stack = AsyncExitStack()
            await mcp_stack.enter_async_context(app.state.mcp.session_manager.run())
        await _start_health_monitor(app)
        await _start_universe_refresher(app)
        await _start_data_cache_sweeper(app)
        await _start_funding_refresher(app)
        await _start_risk_relay(app)
        yield
    finally:
        await _stop_risk_relay(app)
        await _stop_funding_refresher(app)
        await _stop_universe_refresher(app)
        await _stop_data_cache_sweeper(app)
        if mcp_stack is not None:
            await mcp_stack.aclose()
        _unmount_mcp(app)
        await _stop_health_monitor(app)
        await _shutdown_session_manager(app)
        await _close_remote_trader(app)
        await _stop_trades_relay(app)
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


async def _require_redis(cfg: HydraConfig) -> None:
    """Ping Redis when the fan-out flag is on; an unreachable server refuses startup.

    The fan-out makes Redis tier-1 exactly when it is on: with the flag off this is a
    no-op and a Redis outage changes nothing (the health ping keeps its old meaning).
    """
    if cfg.events.bus != "redis":
        return
    secret = cfg.redis_url
    if secret is None:  # unreachable: the config cross-validation requires it
        raise ConfigError("events.bus 'redis' requires REDIS_URL")
    await require_redis(secret.get_secret_value(), budget_s=REDIS_CONNECT_BUDGET_S)


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
    from hydra.dashboard.routes.system import load_system_config

    app.state.system_config = await load_system_config(app.state.db_pool)


async def _init_session_manager(
    app: FastAPI, cfg: HydraConfig, clients: ClientFactory | None = None
) -> None:
    """RiskService, recovery and display rows (``SessionManager.startup``); errors propagate."""
    from hydra.execution.session_manager import SessionManager
    from hydra.services.trader import LocalTrader

    mgr = SessionManager(db_pool=app.state.db_pool, config=cfg, clients=clients)
    await mgr.startup()
    app.state.session_manager = mgr
    app.state.trader = LocalTrader(app.state.db_pool, mgr)


async def _close_remote_trader(app: FastAPI) -> None:
    """Close the split-mode backend's HTTP client (no-op in single-process mode)."""
    trader = getattr(app.state, "trader", None)
    aclose = getattr(trader, "aclose", None)
    if aclose is not None:
        await aclose()


async def _stop_trades_relay(app: FastAPI) -> None:
    """Stop the /ws/trades relay started by the first trades connection, if any."""
    relay = getattr(app.state, "trades_relay", None)
    if relay is not None:
        await relay.stop()


def _default_risk_relay(app: FastAPI) -> Any:
    from hydra.dashboard.risk_relay import RiskRelay

    return RiskRelay(app.state.db_pool)


async def _start_risk_relay(app: FastAPI) -> None:
    """Start the risk relay at startup; only with a pool, never a check.

    The relay is the ``/ws/risk`` producer and the Telegram alerter, so it runs
    whether or not a browser ever connects. A failed poll is caught inside the
    relay's loop (warning + retry next tick), so startup and readiness never
    depend on it. Tests install their own through ``app.state.risk_relay_factory``
    — the ``health_monitor_factory`` pattern.
    """
    if getattr(app.state, "db_pool", None) is None:
        return
    factory: Callable[[FastAPI], Any] = (
        getattr(app.state, "risk_relay_factory", None) or _default_risk_relay
    )
    relay = factory(app)
    app.state.risk_relay = relay
    await relay.ensure_started()


async def _stop_risk_relay(app: FastAPI) -> None:
    relay = getattr(app.state, "risk_relay", None)
    if relay is not None:
        await relay.stop()


async def _start_health_monitor(app: FastAPI) -> None:
    from hydra.dashboard.health import HealthMonitor as Monitor

    factory: Callable[[FastAPI], HealthMonitor] = (
        getattr(app.state, "health_monitor_factory", None) or Monitor
    )
    monitor = factory(app)
    app.state.health_monitor = monitor
    await monitor.start()


def _default_universe_refresher(app: FastAPI) -> Any:
    from hydra.services.universe import default_refresher

    return default_refresher(app.state.db_pool)


async def _start_universe_refresher(app: FastAPI) -> None:
    """Start the background universe refresher; only with a pool, never a check.

    A refresh failure is caught inside the refresher's loop (warning + hourly retry),
    so startup and readiness never depend on it. Tests and the smoke install their own
    through ``app.state.universe_refresher_factory`` — the ``health_monitor_factory``
    pattern — so no test ever needs Binance egress.
    """
    if getattr(app.state, "db_pool", None) is None:
        return
    factory: Callable[[FastAPI], Any] = (
        getattr(app.state, "universe_refresher_factory", None) or _default_universe_refresher
    )
    refresher = factory(app)
    app.state.universe_refresher = refresher
    await refresher.start()


def _default_data_cache_sweeper(app: FastAPI) -> Any:
    from hydra.data.sweeper import DataCacheSweeper

    return DataCacheSweeper(app.state.db_pool, app.state.config.data)


async def _start_data_cache_sweeper(app: FastAPI) -> None:
    """Start the background cache sweeper; only with a pool, never a check.

    A failed or hung sweep is caught inside the sweeper's loop (error counter +
    warning, retried next cycle), so startup and readiness never depend on it.
    Tests and the smoke install their own through
    ``app.state.data_cache_sweeper_factory`` — the ``health_monitor_factory`` pattern.
    """
    if getattr(app.state, "db_pool", None) is None:
        return
    factory: Callable[[FastAPI], Any] = (
        getattr(app.state, "data_cache_sweeper_factory", None) or _default_data_cache_sweeper
    )
    sweeper = factory(app)
    app.state.data_cache_sweeper = sweeper
    await sweeper.start()


def _default_funding_refresher(app: FastAPI) -> Any:
    from hydra.data.funding import FundingRefresher

    return FundingRefresher(app.state.db_pool)


async def _start_funding_refresher(app: FastAPI) -> None:
    """Start the background funding-rate refresher; only with a pool, never a check.

    Each cycle tops up the funding rows of the running perp sessions' symbols;
    with no running perp sessions (today: none exist) the cycle is a no-op. A
    failed cycle is caught inside the refresher's loop (warning + retry), so
    startup and readiness never depend on it. Tests install their own through
    ``app.state.funding_refresher_factory`` — the ``health_monitor_factory`` pattern.
    """
    if getattr(app.state, "db_pool", None) is None:
        return
    factory: Callable[[FastAPI], Any] = (
        getattr(app.state, "funding_refresher_factory", None) or _default_funding_refresher
    )
    refresher = factory(app)
    app.state.funding_refresher = refresher
    await refresher.start()


# ---------------------------------------------------------------------------
# Shutdown steps
# ---------------------------------------------------------------------------


def _unmount_mcp(app: FastAPI) -> None:
    """Detach the ``/mcp`` mount and reset ``app.state.mcp`` to the no-token value.

    The session manager is single-use, so leaving the mount behind would shadow the fresh
    one a re-entered lifespan attaches (the first, closed match wins in the route list).
    """
    from starlette.routing import Mount

    from hydra.dashboard.security import MCP_PATH_PREFIX

    app.router.routes[:] = [
        r for r in app.router.routes if not (isinstance(r, Mount) and r.path == MCP_PATH_PREFIX)
    ]
    app.state.mcp = None


async def _stop_health_monitor(app: FastAPI) -> None:
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is not None:
        await monitor.stop()


async def _stop_universe_refresher(app: FastAPI) -> None:
    refresher = getattr(app.state, "universe_refresher", None)
    if refresher is not None:
        await refresher.stop()


async def _stop_data_cache_sweeper(app: FastAPI) -> None:
    sweeper = getattr(app.state, "data_cache_sweeper", None)
    if sweeper is not None:
        await sweeper.stop()


async def _stop_funding_refresher(app: FastAPI) -> None:
    refresher = getattr(app.state, "funding_refresher", None)
    if refresher is not None:
        await refresher.stop()


async def _shutdown_session_manager(app: FastAPI) -> None:
    mgr = getattr(app.state, "session_manager", None)
    if mgr is not None:
        await mgr.graceful_shutdown()


async def _close_db_pool(app: FastAPI) -> None:
    pool = getattr(app.state, "db_pool", None)
    if pool is not None:
        await pool.close()
