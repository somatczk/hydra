"""The /ws/trades DB relay against a real ``trades`` table (D5, R6).

The relay's own SQL is the seam here: the cursor advances by serial id, rows with no
session are skipped, and exactly one tick goes out per new row in the pinned shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from hydra.core.websocket import manager
from hydra.dashboard.ws_relay import TradesRelay
from hydra.data.migrations.runner import upgrade_to_head


class Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


async def _insert(pool: asyncpg.Pool, *, session_id: str | None, symbol: str = "BTCUSDT") -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "INSERT INTO trades (symbol, side, quantity, price, strategy_id, source,"
                " session_id) VALUES ($1, 'BUY', 1, 1, 'sma5', 'paper', $2) RETURNING id",
                symbol,
                session_id,
            )
        )


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture()
async def relay(pool: asyncpg.Pool) -> AsyncIterator[TradesRelay]:
    relay = TradesRelay(pool, poll_s=0.05)
    yield relay
    await relay.stop()


@pytest.fixture()
async def ws() -> AsyncIterator[Socket]:
    socket = Socket()
    await manager.connect("trades:s1", socket)
    try:
        yield socket
    finally:
        manager.disconnect("trades:s1", socket)


async def _wait(msgs: Socket, n: int, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while len(msgs.messages) < n:
            await asyncio.sleep(0.02)


async def test_one_tick_per_new_row_and_none_for_rows_without_a_session(
    relay: TradesRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    orphan = await _insert(pool, session_id=None)
    await relay.ensure_started()
    await asyncio.sleep(0.2)
    assert ws.messages == []

    first = await _insert(pool, session_id="s1")
    other = await _insert(pool, session_id="s2")
    await _wait(ws, 1)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": first}]
    assert relay._cursor == other  # the s2 row advanced the cursor without a broadcast
    assert relay._cursor > orphan

    second = await _insert(pool, session_id="s1")
    await _wait(ws, 2)
    assert ws.messages == [
        {"event": "order_fill", "session_id": "s1", "trade_id": first},
        {"event": "order_fill", "session_id": "s1", "trade_id": second},
    ]


async def test_rows_before_the_relay_started_are_not_replayed(
    relay: TradesRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await _insert(pool, session_id="s1")
    await _insert(pool, session_id="s1")
    await relay.ensure_started()
    await asyncio.sleep(0.3)
    assert ws.messages == []
    third = await _insert(pool, session_id="s1")
    await _wait(ws, 1)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": third}]


# ---------------------------------------------------------------------------
# The session-status diff on the real trading_sessions table
# ---------------------------------------------------------------------------


async def _start_session(pool: asyncpg.Pool, session_id: str, status: str = "running") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO trading_sessions (id, strategy_id, trading_mode, status) "
            "VALUES ($1, 'sma5', 'paper', $2)",
            session_id,
            status,
        )


async def _stop_session(
    pool: asyncpg.Pool, session_id: str, status: str, error_message: str | None
) -> str:
    async with pool.acquire() as conn:
        stopped_at = await conn.fetchval(
            "UPDATE trading_sessions SET status = $2, stopped_at = now(), "
            "error_message = $3 WHERE id = $1 RETURNING stopped_at",
            session_id,
            status,
            error_message,
        )
    assert stopped_at is not None
    return stopped_at.isoformat()


async def test_a_session_leaving_the_running_set_ticks_once(
    relay: TradesRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await _start_session(pool, "s1")
    await _start_session(pool, "s2")
    await relay.ensure_started()
    await asyncio.sleep(0.3)

    await _stop_session(pool, "s1", "stopped", None)
    await _wait(ws, 1)
    (tick,) = ws.messages
    assert tick["event"] == "session_status"
    assert tick["session_id"] == "s1"
    assert tick["status"] == "stopped"
    assert tick["stopped_at"] is not None
    assert tick["error_message"] is None
    await asyncio.sleep(0.3)
    assert len(ws.messages) == 1  # one tick per transition, never per poll

    await _stop_session(pool, "s1", "error", "ccxtpro: websocket closed")
    await asyncio.sleep(0.3)
    assert len(ws.messages) == 1  # already out of the running set: no re-tick


async def test_an_errored_session_carries_its_error_message(
    relay: TradesRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await _start_session(pool, "s1")
    await relay.ensure_started()
    await asyncio.sleep(0.3)

    await _stop_session(pool, "s1", "error", "ccxtpro: websocket closed")
    await _wait(ws, 1)
    assert ws.messages[0]["status"] == "error"
    assert ws.messages[0]["error_message"] == "ccxtpro: websocket closed"
