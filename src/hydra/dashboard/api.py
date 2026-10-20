"""Hydra Trading Platform -- FastAPI application.

Main entry point for the dashboard REST API and WebSocket endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import hydra
from hydra.core.websocket import manager
from hydra.dashboard import lifespan
from hydra.dashboard.routes import (
    backtest,
    market_data,
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

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    lifespan=lifespan.run,
    title="Hydra Trading API",
    version=hydra.__version__,
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

install_error_handlers(app)

app.state.config = None
app.state.db_pool = None
app.state.exchange_credentials = {}
app.state.exchange_credential_errors = {}
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
app.include_router(signals.router)
app.include_router(templates.router)
app.include_router(market_data.router)


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
        from hydra.core.metrics import _ensure_initialized

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    import uvicorn

    from hydra.core.logging import setup_logging

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FORMAT", "json"))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)
