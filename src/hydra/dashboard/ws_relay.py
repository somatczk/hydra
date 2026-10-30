"""The trades WebSocket relay: one R6 tick per new ``trades`` row, in both process modes.

The session-detail page uses ``/ws/trades?session_id=...`` only as a refetch trigger, and
:class:`hydra.execution.session_wiring` broadcasts its rich fill payload in the engine
process only — in the split layout (D5) that process is the trader, not the app the browser
is connected to. The relay closes the gap through the only durable shared state: it polls
``trades`` by its serial id every 2 s and broadcasts exactly what
``dashboard/src/lib/sessionWs.ts`` classifies as a fill (R6):

``{"event": "order_fill", "session_id": ..., "trade_id": ...}``

Status transitions cross the same gap through a running-set diff: each poll reads the ids
the database says are ``running``, and a session that left the set gets one
``session_status`` broadcast on its own ``trades:{session_id}`` channel — the same
vocabulary ``session_wiring.broadcast_status`` speaks, so the page's classifier refetches
unchanged. The serial-cursor late-commit window still needs the page's 15 s detail poll,
and the database stays the truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from hydra.core.websocket import manager

logger = logging.getLogger(__name__)

RELAY_POLL_S = 2.0
RELAY_BATCH = 500


class TradesRelay:
    """One background poll of ``trades`` per app, broadcasting the R6 tick per new row.

    The cursor starts at the current ``MAX(id)`` when the relay starts (the first
    ``/ws/trades`` connection does), so rows written before it are not re-announced; a
    failed poll keeps the cursor and the running-set snapshot, so a DB blip delays a
    tick, it never drops it.
    """

    def __init__(self, pool: Any, *, poll_s: float = RELAY_POLL_S) -> None:
        self._pool = pool
        self._poll_s = poll_s
        self._cursor: int | None = None
        self._running: set[str] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def ensure_started(self) -> None:
        """Start the poll task if it is not running; a restart re-anchors the cursor."""
        if self._task is None or self._task.done():
            self._cursor = None
            self._running = None
            self._task = asyncio.create_task(self._loop(), name="trades-relay")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Trades relay poll failed; retrying next tick")
            await asyncio.sleep(self._poll_s)

    async def _poll_once(self) -> None:
        if self._cursor is None:
            self._cursor = int(await self._pool.fetchval("SELECT COALESCE(MAX(id), 0) FROM trades"))
        rows = await self._pool.fetch(
            "SELECT id, session_id FROM trades WHERE id > $1 AND session_id IS NOT NULL "
            "ORDER BY id LIMIT $2",
            self._cursor,
            RELAY_BATCH,
        )
        for row in rows:
            self._cursor = int(row["id"])
            await manager.broadcast(
                f"trades:{row['session_id']}",
                {
                    "event": "order_fill",
                    "session_id": str(row["session_id"]),
                    "trade_id": int(row["id"]),
                },
            )
        await self._session_status_tick()

    async def _session_status_tick(self) -> None:
        """Diff the running set; a session that left it gets one status broadcast."""
        rows = await self._pool.fetch("SELECT id FROM trading_sessions WHERE status = 'running'")
        running = {str(row["id"]) for row in rows}
        if self._running is None:
            self._running = running  # the first poll anchors: stops before it are not replayed
            return
        gone = self._running - running
        self._running = running
        if not gone:
            return
        stopped = await self._pool.fetch(
            "SELECT id, status, stopped_at, error_message FROM trading_sessions WHERE id = ANY($1)",
            sorted(gone),
        )
        for row in stopped:
            await manager.broadcast(
                f"trades:{row['id']}",
                {
                    "event": "session_status",
                    "session_id": str(row["id"]),
                    "status": str(row["status"]),
                    "stopped_at": (
                        None if row["stopped_at"] is None else row["stopped_at"].isoformat()
                    ),
                    "error_message": row["error_message"],
                },
            )
