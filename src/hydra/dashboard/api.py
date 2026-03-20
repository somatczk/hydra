"""Hydra Trading Platform -- FastAPI application.

Main entry point for the dashboard REST API and WebSocket endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from hydra.core.websocket import manager
from hydra.dashboard.routes import (
    backtest,
    models,
    portfolio,
    risk,
    strategies,
    system,
    trading,
)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Hydra Trading API",
    version="0.1.0",
    description="REST and WebSocket API for the Hydra Bitcoin auto-trading platform.",
)

# CORS middleware for the Next.js dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.system_config = None
app.state.session_manager = None

# Include all route modules
app.include_router(strategies.router)
app.include_router(portfolio.router)
app.include_router(backtest.router)
app.include_router(risk.router)
app.include_router(models.router)
app.include_router(system.router)
app.include_router(trading.router)


# ---------------------------------------------------------------------------
# Health / metrics
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe with DB pool status."""
    pool = getattr(app.state, "db_pool", None)
    db_ok = pool is not None and not pool._closed if pool else False
    return {"status": "ok" if db_ok else "degraded", "db": "connected" if db_ok else "disconnected"}


@app.get("/metrics", tags=["observability"])
async def metrics() -> Any:
    """Expose Prometheus metrics in OpenMetrics / text format.

    Uses lazy import to avoid pulling ``prometheus_client`` at module level.
    Returns a ``Response`` with the correct ``Content-Type`` header so that
    Prometheus can scrape the endpoint without issues.
    """
    try:
        import prometheus_client
        from starlette.responses import Response

        # Ensure metric collectors are registered before generating output
        from hydra.dashboard.metrics import _ensure_initialized

        _ensure_initialized()

        return Response(
            content=prometheus_client.generate_latest(),
            media_type=prometheus_client.CONTENT_TYPE_LATEST,
        )
    except ImportError:
        return {"error": "prometheus_client not installed"}


# ---------------------------------------------------------------------------
# WebSocket connection manager (imported from hydra.core to avoid circular deps)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------


async def _ws_handler(channel: str, websocket: WebSocket) -> None:
    """Generic WebSocket handler: accept, listen for messages, handle disconnect."""
    await manager.connect(channel, websocket)
    try:
        while True:
            # Keep the connection alive; optionally receive client messages
            await websocket.receive_text()
            # Echo back (or ignore) -- real implementation would process commands
            await websocket.send_text(json.dumps({"ack": True, "channel": channel}))
    except WebSocketDisconnect:
        manager.disconnect(channel, websocket)


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket) -> None:
    """Real-time price updates."""
    await _ws_handler("market", websocket)


@app.websocket("/ws/trades")
async def ws_trades(websocket: WebSocket) -> None:
    """Real-time trade notifications. Accepts ?session_id= query param."""
    session_id = websocket.query_params.get("session_id")
    channel = f"trades:{session_id}" if session_id else "trades"
    await manager.connect(channel, websocket)
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_text(json.dumps({"ack": True, "channel": channel}))
    except WebSocketDisconnect:
        manager.disconnect(channel, websocket)


@app.websocket("/ws/portfolio")
async def ws_portfolio(websocket: WebSocket) -> None:
    """Portfolio value updates."""
    await _ws_handler("portfolio", websocket)


@app.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket) -> None:
    """Strategy signal notifications."""
    await _ws_handler("signals", websocket)


@app.websocket("/ws/risk")
async def ws_risk(websocket: WebSocket) -> None:
    """Risk status changes."""
    await _ws_handler("risk", websocket)


# ---------------------------------------------------------------------------
# Startup: logging, DB pool, migrations
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _init_logging() -> None:
    """Configure structured logging."""
    import os

    from hydra.core.logging import setup_logging

    level = os.environ.get("LOG_LEVEL", "INFO")
    fmt = os.environ.get("LOG_FORMAT", "json")
    setup_logging(level=level, log_format=fmt)


@app.on_event("startup")
async def _init_db_pool() -> None:
    """Create shared asyncpg connection pool."""
    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        app.state.db_pool = None
        return
    try:
        import asyncpg

        app.state.db_pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("DB pool creation failed: %s", exc)
        app.state.db_pool = None


@app.on_event("startup")
async def _load_exchange_credentials() -> None:
    """Load encrypted exchange credentials from DB into app state on startup."""
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        app.state.exchange_credentials = {}
        return
    try:
        from hydra.core.encryption import decrypt

        creds: dict[str, Any] = {}
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM exchange_credentials")
            for row in rows:
                creds[row["exchange_id"]] = {
                    "api_key": decrypt(row["encrypted_key"]),
                    "api_secret": decrypt(row["encrypted_secret"]),
                    "passphrase": (
                        decrypt(row["encrypted_passphrase"])
                        if row["encrypted_passphrase"]
                        else None
                    ),
                }
        app.state.exchange_credentials = creds
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Failed to load exchange credentials from DB")
        app.state.exchange_credentials = {}


@app.on_event("startup")
async def _init_session_manager() -> None:
    """Initialize the trading SessionManager."""
    from hydra.execution.session_manager import SessionManager

    pool = getattr(app.state, "db_pool", None)
    mgr = SessionManager(db_pool=pool)
    if pool is not None:
        await mgr.load_recent_sessions()
        await mgr.recover_running_sessions()
    app.state.session_manager = mgr


@app.on_event("shutdown")
async def _shutdown_session_manager() -> None:
    """Stop all trading sessions on shutdown."""
    mgr = getattr(app.state, "session_manager", None)
    if mgr is not None:
        await mgr.graceful_shutdown()


@app.on_event("shutdown")
async def _close_db_pool() -> None:
    """Close the DB pool on shutdown."""
    pool = getattr(app.state, "db_pool", None)
    if pool is not None:
        await pool.close()


@app.on_event("startup")
async def _run_migrations() -> None:
    """Apply database migrations on startup if DATABASE_URL is set."""
    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return

    try:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            # Check if ts schema exists
            row = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'ts')"
            )
            if not row:
                # Run the initial migration SQL inline (same as 001_initial_schema.py)
                await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS ts")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ts.ohlcv_1m (
                        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
                        timeframe TEXT NOT NULL, timestamp TIMESTAMPTZ NOT NULL,
                        open NUMERIC NOT NULL, high NUMERIC NOT NULL,
                        low NUMERIC NOT NULL, close NUMERIC NOT NULL,
                        volume NUMERIC NOT NULL,
                        UNIQUE (exchange, symbol, timeframe, timestamp)
                    )
                """)
                await conn.execute(
                    "SELECT create_hypertable('ts.ohlcv_1m', 'timestamp', if_not_exists => TRUE)"
                )
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ts.trades (
                        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
                        trade_id TEXT NOT NULL, price NUMERIC NOT NULL,
                        quantity NUMERIC NOT NULL, side TEXT NOT NULL,
                        timestamp TIMESTAMPTZ NOT NULL
                    )
                """)
                await conn.execute(
                    "SELECT create_hypertable('ts.trades', 'timestamp', if_not_exists => TRUE)"
                )
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ts.funding_rates (
                        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
                        rate NUMERIC NOT NULL, next_funding_time TIMESTAMPTZ,
                        timestamp TIMESTAMPTZ NOT NULL
                    )
                """)
                await conn.execute(
                    "SELECT create_hypertable("
                    "'ts.funding_rates', 'timestamp', if_not_exists => TRUE)"
                )

            # Trading sessions & risk config (Phase 2)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trading_sessions (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    exchange_id TEXT NOT NULL DEFAULT 'binance',
                    symbols TEXT[] NOT NULL DEFAULT ARRAY['BTCUSDT'],
                    timeframe TEXT NOT NULL DEFAULT '1h',
                    paper_capital NUMERIC(24,8),
                    started_at TIMESTAMPTZ,
                    stopped_at TIMESTAMPTZ,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_config (
                    id SERIAL PRIMARY KEY,
                    scope TEXT NOT NULL UNIQUE,
                    max_position_pct NUMERIC(8,4) DEFAULT 0.10,
                    max_risk_per_trade NUMERIC(8,4) DEFAULT 0.02,
                    max_portfolio_heat NUMERIC(8,4) DEFAULT 0.06,
                    max_daily_loss_pct NUMERIC(8,4) DEFAULT 0.03,
                    max_drawdown_pct NUMERIC(8,4) DEFAULT 0.15,
                    max_consecutive_losses INTEGER DEFAULT 5,
                    max_concurrent_positions INTEGER DEFAULT 10,
                    kill_switch_active BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute(
                "ALTER TABLE risk_config "
                "ADD COLUMN IF NOT EXISTS max_portfolio_heat NUMERIC(8,4) DEFAULT 0.06"
            )
            await conn.execute(
                "ALTER TABLE risk_config "
                "ADD COLUMN IF NOT EXISTS max_consecutive_losses INTEGER DEFAULT 5"
            )
            await conn.execute(
                "INSERT INTO risk_config (scope) VALUES ('global') ON CONFLICT (scope) DO NOTHING"
            )
            # Production tables for trades, positions, balance snapshots
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity NUMERIC(24,8) NOT NULL,
                    price NUMERIC(24,8) NOT NULL,
                    fee NUMERIC(24,8) NOT NULL DEFAULT 0,
                    pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    strategy_id TEXT NOT NULL,
                    exchange_id TEXT NOT NULL DEFAULT 'binance',
                    source TEXT NOT NULL DEFAULT 'live',
                    session_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_trades_strategy ON trades(strategy_id)"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(timestamp)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    session_id TEXT,
                    exchange_id TEXT NOT NULL DEFAULT 'binance',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
                    avg_entry_price NUMERIC(24,8) NOT NULL DEFAULT 0,
                    unrealized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'live',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (session_id, symbol)
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS balance_snapshots (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    total_value NUMERIC(24,8) NOT NULL,
                    unrealized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    drawdown_pct NUMERIC(8,4) NOT NULL DEFAULT 0,
                    peak_value NUMERIC(24,8) NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'live'
                )
            """)
            await conn.execute(
                "ALTER TABLE balance_snapshots "
                "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live'"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_balance_snap_ts ON balance_snapshots(timestamp)"
            )

            # Exchange credentials (encrypted at rest)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS exchange_credentials (
                    exchange_id TEXT PRIMARY KEY,
                    encrypted_key TEXT NOT NULL,
                    encrypted_secret TEXT NOT NULL,
                    encrypted_passphrase TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)

            # Notification preferences
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_preferences (
                    id SERIAL PRIMARY KEY,
                    preferences JSONB NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)

            # Risk state tracking
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    id SERIAL PRIMARY KEY,
                    scope TEXT NOT NULL UNIQUE DEFAULT 'global',
                    daily_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
                    daily_pnl_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    consecutive_losses INTEGER NOT NULL DEFAULT 0,
                    last_loss_at TIMESTAMPTZ,
                    current_drawdown NUMERIC(8,4) NOT NULL DEFAULT 0,
                    peak_portfolio_value NUMERIC(24,8) NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute(
                "INSERT INTO risk_state (scope) VALUES ('global') ON CONFLICT (scope) DO NOTHING"
            )

            # Backtest results persistence (idempotent)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    period TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
                    total_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                    max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
                    sharpe_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
                    equity_curve JSONB NOT NULL DEFAULT '[]',
                    name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute(
                "ALTER TABLE backtest_results"
                " ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''"
            )
            await conn.execute(
                "ALTER TABLE backtest_results"
                " ADD COLUMN IF NOT EXISTS transactions JSONB NOT NULL DEFAULT '[]'"
            )
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_result_trades (
                    id SERIAL PRIMARY KEY,
                    backtest_id TEXT NOT NULL REFERENCES backtest_results(id)
                        ON DELETE CASCADE,
                    entry_time TIMESTAMPTZ NOT NULL,
                    exit_time TIMESTAMPTZ NOT NULL,
                    side TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    exit_price DOUBLE PRECISION NOT NULL,
                    pnl DOUBLE PRECISION NOT NULL
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_backtest_result_trades_backtest
                    ON backtest_result_trades(backtest_id)
            """)
        finally:
            await conn.close()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("Migration skipped: %s", exc)


@app.on_event("startup")
async def _populate_backtest_cache() -> None:
    """Warm the in-memory backtest cache from DB."""
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        return
    from hydra.dashboard.routes.backtest import populate_cache_from_db

    await populate_cache_from_db(pool)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
