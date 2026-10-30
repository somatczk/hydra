"""The risk relay against real TimescaleDB (P7-D): the cursor, the heartbeat, the latch.

The relay's own SQL is the seam here: the serial cursor advances over ``risk_events``,
the heartbeat reads the seeded ``paper`` risk_state row and the global kill-switch
latch, and every new row is handed to the Telegram sink (the real async client against
a local server — never api.telegram.org).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from hydra.core.websocket import manager
from hydra.dashboard.risk_relay import RiskRelay
from hydra.data.migrations.runner import upgrade_to_head
from hydra.ops.notifications import TelegramNotifier
from tests.unit.test_ops.test_notifications import _start

BOT_ID = "123456789"
BOT_BODY = "AAEhBOweik6ad9r_QXMENQjcrGbqCr4K"
TOKEN = f"{BOT_ID}:{BOT_BODY}"
CHAT = "-1002146360582"


class Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


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
async def relay(pool: asyncpg.Pool) -> AsyncIterator[RiskRelay]:
    relay = RiskRelay(pool, poll_s=0.05)
    yield relay
    await relay.stop()


@pytest.fixture()
async def ws() -> AsyncIterator[Socket]:
    socket = Socket()
    await manager.connect("risk", socket)
    try:
        yield socket
    finally:
        manager.disconnect("risk", socket)


async def _insert_event(pool: asyncpg.Pool, kind: str, tier: int | None = 3) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "INSERT INTO risk_events (scope, kind, tier, actor, reason, details) "
                "VALUES ('paper', $1, $2, 'risk_engine', 'drawdown crossed the tier "
                "threshold', '{}'::jsonb) RETURNING id",
                kind,
                tier,
            )
        )


async def _set_state(
    pool: asyncpg.Pool,
    *,
    equity: Decimal | None = Decimal("9512.40"),
    day_start: Decimal | None = Decimal("9632.40"),
    drawdown: Decimal = Decimal("0.1050"),
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE risk_state SET equity = $1, peak_portfolio_value = $2, "
            "day_start_equity = $3, current_drawdown = $4, consecutive_losses = 2, "
            "cb_tier = 3, updated_at = now() WHERE scope = 'paper'",
            equity,
            equity,
            day_start,
            drawdown,
        )


async def _wait(ws: Socket, n: int, event: str, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while len([m for m in ws.messages if m["event"] == event]) < n:
            await asyncio.sleep(0.02)


async def test_one_tick_per_row_and_the_initial_heartbeat(
    relay: RiskRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await _insert_event(pool, "kill_switch_on", tier=None)  # predates the anchor
    await relay.ensure_started()
    await asyncio.sleep(0.3)
    assert [m for m in ws.messages if m["event"] == "risk_event"] == []
    assert len([m for m in ws.messages if m["event"] == "risk_state"]) == 1

    first = await _insert_event(pool, "cb_escalate")
    second = await _insert_event(pool, "cb_deescalate", tier=1)
    await _wait(ws, 2, "risk_event")
    assert [m for m in ws.messages if m["event"] == "risk_event"] == [
        {
            "event": "risk_event",
            "id": first,
            "scope": "paper",
            "kind": "cb_escalate",
            "tier": 3,
            "actor": "risk_engine",
            "reason": "drawdown crossed the tier threshold",
        },
        {
            "event": "risk_event",
            "id": second,
            "scope": "paper",
            "kind": "cb_deescalate",
            "tier": 1,
            "actor": "risk_engine",
            "reason": "drawdown crossed the tier threshold",
        },
    ]
    assert relay._cursor == second


async def test_the_heartbeat_reads_the_real_state_and_kill_switch(
    relay: RiskRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await relay.ensure_started()
    await asyncio.sleep(0.3)
    (initial,) = [m for m in ws.messages if m["event"] == "risk_state"]
    assert initial["equity"] is None  # the seeded row has no established numbers
    assert initial["kill_switch_active"] is False

    await _set_state(pool)
    await _wait(ws, 2, "risk_state")
    beats = [m for m in ws.messages if m["event"] == "risk_state"]
    assert beats[1]["equity"] == 9512.4
    assert beats[1]["peak_equity"] == 9512.4
    assert beats[1]["day_start_equity"] == 9632.4
    assert beats[1]["daily_loss"] == 120.0
    assert beats[1]["current_drawdown"] == 10.5
    assert beats[1]["consecutive_losses"] == 2
    assert beats[1]["cb_tier"] == 3

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE risk_config SET kill_switch_active = TRUE, updated_at = now() "
            "WHERE scope = 'global'"
        )
    await _wait(ws, 3, "risk_state")
    beats = [m for m in ws.messages if m["event"] == "risk_state"]
    assert beats[-1]["kill_switch_active"] is True
    assert beats[-1]["equity"] == 9512.4  # the unchanged fields ride along


async def test_every_row_reaches_the_telegram_sink(pool: asyncpg.Pool, ws: Socket) -> None:
    server = await _start([200, 200])

    async def factory(_pool: Any) -> TelegramNotifier:
        return TelegramNotifier(TOKEN, CHAT, base_url=server.url)

    relay = RiskRelay(pool, poll_s=0.05, notifier_factory=factory)
    try:
        await relay.ensure_started()
        await asyncio.sleep(0.2)
        await _insert_event(pool, "cb_escalate")
        await _insert_event(pool, "cb_escalate")
        await _wait(ws, 2, "risk_event")
        await asyncio.sleep(0.1)  # the fire-and-forget sends land
    finally:
        await relay.stop()
        await server.close()

    assert [hit.path for hit in server.hits] == [f"/hook/bot{TOKEN}/sendMessage"] * 2
    for hit in server.hits:
        assert hit.body["chat_id"] == CHAT
        assert hit.body["text"].startswith("Hydra risk event (tier 3): cb_escalate")


async def test_the_cursor_and_baseline_survive_a_db_blip(
    relay: RiskRelay, pool: asyncpg.Pool, ws: Socket
) -> None:
    await relay.ensure_started()
    await asyncio.sleep(0.3)

    # terminate every other backend (the relay's pooled connections), not our own
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    await _insert_event(pool, "cb_escalate")
    await _wait(ws, 1, "risk_event")
    assert len([m for m in ws.messages if m["event"] == "risk_event"]) == 1
    assert relay.running
