"""The risk relay (/ws/risk + the Telegram alerter, P7-D): one tick per risk_events row.

The relay's cursor, the two-event vocabulary and the Telegram hand-off are asserted
against a pool that answers the relay's own queries; the real SQL (the serial cursor,
the risk_state heartbeat read and the risk_config kill-switch latch) is pinned in
``tests/integration/test_risk_relay_pg.py``. Telegram sends run the real async client
against a local server (the ``base_url`` seam) — never api.telegram.org — and the
fire-and-forget contract is proven by parking the real client on a server that does
not answer and watching the poll loop tick on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from hydra.core.encryption import encrypt
from hydra.core.websocket import manager
from hydra.dashboard.api import app
from hydra.dashboard.risk_relay import (
    RiskRelay,
    _stored_telegram_notifier,
    _telegram_text,
)
from hydra.ops.notifications import TelegramNotifier
from tests.unit.test_ops.test_notifications import _start

BOT_ID = "123456789"
BOT_BODY = "AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c"
TOKEN = f"{BOT_ID}:{BOT_BODY}"
CHAT = "-1002146360582"

UPDATED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
CB_SINCE = datetime(2026, 10, 30, 11, 0, tzinfo=UTC)
HALT_UNTIL = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)


def _event(eid: int, kind: str = "cb_escalate", **over: Any) -> dict[str, Any]:
    return {
        "id": eid,
        "scope": "paper",
        "kind": kind,
        "tier": 3,
        "actor": "risk_engine",
        "reason": "drawdown crossed the tier threshold",
        **over,
    }


def _state(**over: Any) -> dict[str, Any]:
    return {
        "equity": Decimal("9512.40"),
        "peak_portfolio_value": Decimal("10630.10"),
        "day_start_equity": Decimal("9632.40"),
        "current_drawdown": Decimal("0.1050"),
        "consecutive_losses": 2,
        "cb_tier": 3,
        "cb_tier_since": CB_SINCE,
        "daily_halt_until": HALT_UNTIL,
        "consecutive_pause_until": None,
        "updated_at": UPDATED_AT,
        **over,
    }


class FakePool:
    """Answers the relay's queries from mutable risk_events/state/config tables."""

    def __init__(self, *, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.state: dict[str, Any] | None = _state()
        self.kill_switch = False
        self.config: dict[str, dict[str, Any]] = {}
        self.fail = False
        self.fail_on: str | None = None
        self.state_reads = 0
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakePool]:
        yield self

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        if "MAX(id)" in sql:
            return max((r["id"] for r in self.events), default=0)
        if "kill_switch_active" in sql:
            return self.kill_switch
        if sql == "SELECT 1":
            return 1
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._check(sql)
        if "FROM risk_state" in sql:
            self.state_reads += 1
            return None if self.state is None else dict(self.state)
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM risk_events" in sql:
            (cursor,) = args[:1]
            return [
                {k: r[k] for k in ("id", "scope", "kind", "tier", "actor", "reason")}
                for r in sorted(self.events, key=lambda r: r["id"])
                if r["id"] > (cursor or 0)
            ][:500]
        if sql == "SELECT key, value FROM system_config":
            return [{"key": k, "value": v["value"]} for k, v in self.config.items()]
        raise AssertionError(f"unexpected fetch: {sql}")

    def _check(self, sql: str) -> None:
        if self.fail or (self.fail_on is not None and self.fail_on in sql):
            raise RuntimeError("database down")


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def pool() -> FakePool:
    return FakePool()


@pytest.fixture()
async def relay(pool: FakePool) -> AsyncIterator[RiskRelay]:
    relay = RiskRelay(pool, poll_s=0.01)
    yield relay
    await relay.stop()


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


@pytest.fixture()
async def ws() -> AsyncIterator[_Socket]:
    socket = _Socket()
    await manager.connect("risk", socket)
    try:
        yield socket
    finally:
        manager.disconnect("risk", socket)


def _events_of(socket: _Socket) -> list[dict[str, Any]]:
    return [m for m in socket.messages if m["event"] == "risk_event"]


def _beats_of(socket: _Socket) -> list[dict[str, Any]]:
    return [m for m in socket.messages if m["event"] == "risk_state"]


async def _drained(relay: RiskRelay) -> None:
    """Let at least one poll interval pass."""
    await asyncio.sleep(0.2)
    assert relay.running


# ---------------------------------------------------------------------------
# The risk_events cursor: one tick per row, in order, never replayed
# ---------------------------------------------------------------------------


async def test_one_tick_per_row_in_order(relay: RiskRelay, pool: FakePool, ws: _Socket) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.events = [
        _event(1, kind="kill_switch_on", tier=None, reason="manual stop"),
        _event(2),
        _event(3, kind="cb_deescalate", tier=1),
    ]
    await _drained(relay)
    assert _events_of(ws) == [
        {
            "event": "risk_event",
            "id": 1,
            "scope": "paper",
            "kind": "kill_switch_on",
            "tier": None,
            "actor": "risk_engine",
            "reason": "manual stop",
        },
        {
            "event": "risk_event",
            "id": 2,
            "scope": "paper",
            "kind": "cb_escalate",
            "tier": 3,
            "actor": "risk_engine",
            "reason": "drawdown crossed the tier threshold",
        },
        {
            "event": "risk_event",
            "id": 3,
            "scope": "paper",
            "kind": "cb_deescalate",
            "tier": 1,
            "actor": "risk_engine",
            "reason": "drawdown crossed the tier threshold",
        },
    ]


async def test_the_cursor_never_replays_a_row(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.events = [_event(7)]
    await _drained(relay)
    assert len(_events_of(ws)) == 1
    pool.events = [_event(7), _event(8)]
    await _drained(relay)
    assert [e["id"] for e in _events_of(ws)] == [7, 8]
    assert relay._cursor == 8


async def test_rows_before_the_relay_started_are_not_announced(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    pool.events = [_event(5)]
    await relay.ensure_started()
    await _drained(relay)
    assert _events_of(ws) == []
    pool.events.append(_event(6))
    await _drained(relay)
    assert [e["id"] for e in _events_of(ws)] == [6]


async def test_a_failed_poll_keeps_the_cursor_and_the_baseline(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    assert len(ws.messages) == 1  # the initial heartbeat
    pool.fail = True
    pool.events = [_event(1)]
    pool.state = _state(equity=Decimal("9000.00"))
    await _drained(relay)
    assert ws.messages == [ws.messages[0]]  # nothing: the poll failed

    pool.fail = False
    await _drained(relay)
    assert [e["id"] for e in _events_of(ws)] == [1]
    assert _beats_of(ws)[-1]["equity"] == 9000.0


async def test_a_restarted_relay_re_anchors(relay: RiskRelay, pool: FakePool, ws: _Socket) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.events = [_event(4)]
    await _drained(relay)
    assert [e["id"] for e in _events_of(ws)] == [4]
    await relay.stop()
    pool.events.append(_event(5))
    await relay.ensure_started()
    await _drained(relay)
    # the restart re-anchored the cursor at MAX(id) and re-broadcast the heartbeat
    assert [e["id"] for e in _events_of(ws)] == [4]
    assert len(_beats_of(ws)) == 2


async def test_stop_cancels_the_poll(relay: RiskRelay, pool: FakePool) -> None:
    await relay.ensure_started()
    assert relay.running
    await relay.stop()
    assert not relay.running
    await relay.stop()


# ---------------------------------------------------------------------------
# The heartbeat: RiskStatus-shaped, on change only, None stays null
# ---------------------------------------------------------------------------


async def test_the_heartbeat_broadcasts_on_change_only(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    first = _beats_of(ws)
    assert len(first) == 1
    await _drained(relay)
    await _drained(relay)
    assert _beats_of(ws) == first  # an unchanged row broadcasts nothing

    pool.state = _state(equity=Decimal("9400.10"), updated_at=UPDATED_AT + timedelta(minutes=1))
    await _drained(relay)
    beats = _beats_of(ws)
    assert len(beats) == 2
    assert beats[1]["equity"] == 9400.1
    assert beats[1]["daily_loss"] == 232.3  # 9632.40 - 9400.10: the RiskStatus shape


async def test_the_heartbeat_payload_is_riskstatus_shaped(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    await relay.ensure_started()
    await _drained(relay)

    (beat,) = _beats_of(ws)
    assert beat == {
        "event": "risk_state",
        "scope": "paper",
        "equity": 9512.4,
        "peak_equity": 10630.1,
        "day_start_equity": 9632.4,
        "daily_loss": 120.0,
        "current_drawdown": 10.5,  # the stored fraction 0.1050, served as percent
        "consecutive_losses": 2,
        "cb_tier": 3,
        "cb_tier_since": CB_SINCE.isoformat(),
        "daily_halt_until": HALT_UNTIL.isoformat(),
        "consecutive_pause_until": None,
        "kill_switch_active": False,
        "updated_at": UPDATED_AT.isoformat(),
    }


async def test_a_state_without_established_numbers_keeps_them_null(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    pool.state = _state(
        equity=None,
        peak_portfolio_value=None,
        day_start_equity=None,
        current_drawdown=Decimal("0"),
        consecutive_losses=0,
        cb_tier=0,
        cb_tier_since=None,
        daily_halt_until=None,
    )
    await relay.ensure_started()
    await _drained(relay)
    (beat,) = _beats_of(ws)
    assert beat["equity"] is None
    assert beat["peak_equity"] is None
    assert beat["day_start_equity"] is None
    assert beat["daily_loss"] is None  # never fabricated from a missing day start
    assert beat["current_drawdown"] == 0.0


async def test_a_missing_state_row_heartbeats_nothing(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    pool.state = None
    await relay.ensure_started()
    await _drained(relay)
    assert _beats_of(ws) == []


async def test_the_kill_switch_latch_change_broadcasts(
    relay: RiskRelay, pool: FakePool, ws: _Socket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.kill_switch = True
    pool.state = _state(updated_at=UPDATED_AT + timedelta(seconds=1))
    await _drained(relay)
    beats = _beats_of(ws)
    assert len(beats) == 2
    assert beats[0]["kill_switch_active"] is False
    assert beats[1]["kill_switch_active"] is True


# ---------------------------------------------------------------------------
# The Telegram sink: every row, off the loop, unconfigured sends nothing
# ---------------------------------------------------------------------------


async def test_the_telegram_send_per_row_with_the_exact_url_and_payload(
    pool: FakePool, ws: _Socket
) -> None:
    server = await _start([200, 200])
    relay = RiskRelay(pool, poll_s=0.01, notifier_factory=_local_notifier_factory(server))
    try:
        await relay.ensure_started()
        await _drained(relay)
        pool.events = [
            _event(1, kind="kill_switch_on", tier=None, reason="manual stop"),
            _event(2),
        ]
        await _drained(relay)
    finally:
        await relay.stop()
        await server.close()

    assert [hit.path for hit in server.hits] == [f"/hook/bot{TOKEN}/sendMessage"] * 2
    title, message = _telegram_text(
        _event(1, kind="kill_switch_on", tier=None, reason="manual stop")
    )
    assert server.hits[0].body == {"chat_id": CHAT, "text": f"{title}\n\n{message}"}
    assert server.hits[1].body["chat_id"] == CHAT


def _local_notifier_factory(server: Any) -> Callable[[Any], Awaitable[TelegramNotifier]]:
    async def factory(_pool: Any) -> TelegramNotifier:
        return TelegramNotifier(TOKEN, CHAT, base_url=server.url)

    return factory


async def test_stored_credentials_build_the_notifier_and_a_half_pair_sends_nothing(
    pool: FakePool,
) -> None:
    assert await _stored_telegram_notifier(pool) is None

    pool.config["telegram_bot_token"] = {"value": json.dumps(encrypt(TOKEN))}
    assert await _stored_telegram_notifier(pool) is None  # half a pair sends nothing
    pool.config["telegram_chat_id"] = {"value": json.dumps(encrypt(CHAT))}

    notifier = await _stored_telegram_notifier(pool)
    assert isinstance(notifier, TelegramNotifier)
    assert notifier._url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"


async def test_a_failed_send_never_stops_the_relay(pool: FakePool, ws: _Socket) -> None:
    server = await _start([500, 500, 500])
    relay = RiskRelay(pool, poll_s=0.01, notifier_factory=_local_notifier_factory(server))
    try:
        await relay.ensure_started()
        await _drained(relay)
        pool.events = [_event(1), _event(2)]
        await _drained(relay)
        assert relay.running
        pool.events = [_event(1), _event(2), _event(3)]
        await _drained(relay)
    finally:
        await relay.stop()
        await server.close()

    assert len(server.hits) == 3  # every row still attempted its send
    assert [e["id"] for e in _events_of(ws)] == [1, 2, 3]
    assert relay.running is False  # only the test's stop() ended it


async def test_the_send_runs_off_the_poll_loop(pool: FakePool, ws: _Socket) -> None:
    hold = asyncio.Event()
    server = await _start([200], hold=hold)
    relay = RiskRelay(pool, poll_s=0.01, notifier_factory=_local_notifier_factory(server))
    try:
        await relay.ensure_started()
        await _drained(relay)
        reads_before = pool.state_reads
        pool.events = [_event(1)]
        await _drained(relay)
        assert len(server.hits) == 1  # the real client is parked on the held request
        assert len(_events_of(ws)) == 1  # the broadcast was never delayed by it
        assert pool.state_reads - reads_before >= 3  # polls ran while the send hung

        hold.set()
        await _drained(relay)
    finally:
        hold.set()
        await relay.stop()
        await server.close()


class TestHandlerWiring:
    """The /ws/risk handler runs the app's relay (the lifespan owns it in production)."""

    @pytest.fixture(autouse=True)
    def _no_relay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app.state, "risk_relay", None, raising=False)

    def test_a_connection_with_a_pool_runs_the_relay(self) -> None:
        pool = FakePool()
        with TestClient(app) as client:
            app.state.db_pool = pool  # set after startup; the lifespan closes it at exit
            with client.websocket_connect("/ws/risk") as ws_client:
                ws_client.send_text("hello")
                assert json.loads(ws_client.receive_text())["channel"] == "risk"
                relay = app.state.risk_relay
                assert relay is not None and relay.running
        assert app.state.risk_relay is not None and not app.state.risk_relay.running
        assert pool.closed

    def test_a_connection_without_a_pool_starts_nothing(self) -> None:
        with TestClient(app) as client:
            app.state.db_pool = None
            with client.websocket_connect("/ws/risk") as ws_client:
                ws_client.send_text("hello")
                assert json.loads(ws_client.receive_text())["channel"] == "risk"
                assert app.state.risk_relay is None
