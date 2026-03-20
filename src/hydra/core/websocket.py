"""WebSocket connection manager for broadcasting messages to connected clients.

Extracted to ``hydra.core`` so that both the dashboard layer and the execution
layer can import it without creating circular dependencies.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.websockets import WebSocket


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
