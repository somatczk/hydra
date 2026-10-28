"""Hydra Trading Platform -- FastAPI application.

Main entry point for the dashboard REST API and WebSocket endpoints.
"""

from __future__ import annotations

import json

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import hydra
from hydra.core.websocket import manager
from hydra.dashboard import lifespan
from hydra.dashboard.routes import (
    activity,
    backtest,
    health,
    live,
    market_data,
    metrics,
    models,
    portfolio,
    risk,
    signals,
    strategies,
    system,
    templates,
    trading,
)
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.security import install_security
from hydra.dashboard.ws_relay import TradesRelay

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    lifespan=lifespan.run,
    title="Hydra Trading API",
    version=hydra.__version__,
    description="REST and WebSocket API for the Hydra Bitcoin auto-trading platform.",
)

# Auth + CSRF, with CORS (api.cors_origins) outermost; see hydra.dashboard.security
install_security(app)
install_error_handlers(app)

app.state.config = None
app.state.db_pool = None
app.state.exchange_credentials = {}
app.state.exchange_credential_errors = {}
app.state.system_config = None
app.state.session_manager = None
app.state.trader = None
app.state.health_monitor = None

# Include all route modules
app.include_router(health.router)
app.include_router(strategies.router)
app.include_router(portfolio.router)
app.include_router(backtest.router)
app.include_router(risk.router)
app.include_router(models.router)
app.include_router(system.router)
app.include_router(trading.router)
app.include_router(signals.router)
app.include_router(live.router)
app.include_router(templates.router)
app.include_router(market_data.router)
app.include_router(activity.router)
app.include_router(metrics.router)


# ---------------------------------------------------------------------------
# WebSocket connection manager (imported from hydra.core to avoid circular deps)
# ---------------------------------------------------------------------------


def _ensure_trades_relay(app: FastAPI) -> TradesRelay | None:
    """The app's trades relay, built on first use from its pool (None without one)."""
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        return None
    relay = getattr(app.state, "trades_relay", None)
    if relay is None:
        relay = TradesRelay(pool)
        app.state.trades_relay = relay
    return relay


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
    """Real-time trade notifications. Accepts ?session_id= query param.

    The relay task (started here, one per app) polls ``trades`` and sends the R6 fill
    tick per new row, so fills cross the process boundary of the split layout through
    the database; in single-process mode the engine's own fill broadcast still arrives.
    """
    session_id = websocket.query_params.get("session_id")
    channel = f"trades:{session_id}" if session_id else "trades"
    relay = _ensure_trades_relay(websocket.app)
    if relay is not None:
        await relay.ensure_started()
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
# Entry point
# ---------------------------------------------------------------------------


def build_uvicorn_config() -> uvicorn.Config:
    """Server config; proxy_headers=False keeps the raw TCP peer that proxy auth trusts."""
    return uvicorn.Config(app, host="0.0.0.0", port=8000, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    import os

    from hydra.core.logging import setup_logging

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "json"))
    uvicorn.Server(build_uvicorn_config()).run()
