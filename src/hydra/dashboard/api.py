"""Hydra Trading Platform -- FastAPI application.

Main entry point for the dashboard REST API and WebSocket endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from hydra.dashboard.routes import (
    backtest,
    builder,
    models,
    portfolio,
    risk,
    strategies,
    system,
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

# Include all route modules
app.include_router(strategies.router)
app.include_router(portfolio.router)
app.include_router(backtest.router)
app.include_router(risk.router)
app.include_router(models.router)
app.include_router(system.router)
app.include_router(builder.router)


# ---------------------------------------------------------------------------
# Health / metrics
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Simple liveness probe."""
    return {"status": "ok"}


@app.get("/metrics", tags=["observability"])
async def metrics() -> str:
    """Expose Prometheus metrics in text format.

    Uses lazy import to avoid pulling prometheus_client at module level.
    """
    try:
        import prometheus_client

        return prometheus_client.generate_latest().decode("utf-8")
    except ImportError:
        return "# prometheus_client not installed\n"


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Tracks active WebSocket connections per channel and broadcasts messages."""

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, channel: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(channel, []).append(websocket)

    def disconnect(self, channel: str, websocket: WebSocket) -> None:
        conns = self._connections.get(channel, [])
        if websocket in conns:
            conns.remove(websocket)

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self._connections.get(channel, []):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)

    @property
    def channels(self) -> dict[str, int]:
        return {ch: len(conns) for ch, conns in self._connections.items() if conns}


manager = ConnectionManager()


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
    """Trade notifications."""
    await _ws_handler("trades", websocket)


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
