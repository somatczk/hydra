"""The /ws/trades DB relay (D5, R6): the fill tick per new trades row, in both modes.

The relay's cursor, the running-set diff and the R6/session_status message shapes are
asserted against a pool that answers the relay's own queries; the real SQL (the
``WHERE id > cursor``, the NULL ``session_id`` filter and the ``ANY($1)`` status read)
is pinned in ``tests/integration/test_ws_relay_pg.py``, and the whole relay runs
against a real database inside the split e2e. The handler wiring (the relay starts from the
app's pool, in both process modes) is asserted through the real ``/ws/trades`` route.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hydra.core.websocket import manager
from hydra.dashboard.api import app
from hydra.dashboard.ws_relay import TradesRelay


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


class FakePool:
    """Answers the relay's queries from mutable trades and trading_sessions tables."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.fail = False
        self.fail_on: str | None = None
        self.closed = False
        self.cursors: list[int | None] = []

    async def close(self) -> None:
        self.closed = True

    async def fetchval(self, sql: str) -> Any:
        assert "MAX(id)" in sql
        return max((r["id"] for r in self.rows), default=0)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.fail or (self.fail_on is not None and self.fail_on in sql):
            raise RuntimeError("database down")
        if "FROM trading_sessions WHERE status = 'running'" in sql:
            return [{"id": r["id"]} for r in self.sessions if r["status"] == "running"]
        if "FROM trading_sessions" in sql:
            wanted = set(args[0])
            return [dict(r) for r in self.sessions if r["id"] in wanted]
        assert "FROM trades" in sql
        (cursor,) = args[:1]
        self.cursors.append(cursor)
        return [
            {"id": r["id"], "session_id": r["session_id"]}
            for r in sorted(self.rows, key=lambda r: r["id"])
            if r["id"] > (cursor or 0)
        ][:500]


@pytest.fixture()
def pool() -> FakePool:
    return FakePool()


@pytest.fixture()
async def relay(pool: FakePool) -> Iterator[TradesRelay]:
    relay = TradesRelay(pool, poll_s=0.01)
    yield relay
    await relay.stop()


@pytest.fixture()
async def ws() -> Iterator[FakeSocket]:
    socket = FakeSocket()
    await manager.connect("trades:s1", socket)
    try:
        yield socket
    finally:
        manager.disconnect("trades:s1", socket)


async def _drained(relay: TradesRelay) -> None:
    """Let at least one poll interval pass."""
    await asyncio.sleep(0.2)
    assert relay.running


async def test_one_tick_per_new_row_in_the_r6_shape(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.rows = [
        {"id": 1, "session_id": "s1"},
        {"id": 2, "session_id": "s1"},
        {"id": 3, "session_id": "s2"},
    ]
    await _drained(relay)
    assert ws.messages == [
        {"event": "order_fill", "session_id": "s1", "trade_id": 1},
        {"event": "order_fill", "session_id": "s1", "trade_id": 2},
    ]


async def test_the_cursor_never_replays_a_row(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.rows = [{"id": 7, "session_id": "s1"}]
    await _drained(relay)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": 7}]
    pool.rows = [{"id": 7, "session_id": "s1"}, {"id": 8, "session_id": "s1"}]
    await _drained(relay)
    assert ws.messages == [
        {"event": "order_fill", "session_id": "s1", "trade_id": 7},
        {"event": "order_fill", "session_id": "s1", "trade_id": 8},
    ]
    assert relay._cursor == 8


async def test_rows_before_the_relay_started_are_not_announced(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    pool.rows = [{"id": 5, "session_id": "s1"}]
    await relay.ensure_started()
    await _drained(relay)
    assert ws.messages == []
    pool.rows.append({"id": 6, "session_id": "s1"})
    await _drained(relay)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": 6}]


async def test_a_failed_poll_keeps_the_cursor_and_retries(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.fail = True
    pool.rows = [{"id": 1, "session_id": "s1"}]
    await _drained(relay)
    assert ws.messages == []
    pool.fail = False
    await _drained(relay)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": 1}]


async def test_stop_cancels_the_poll(relay: TradesRelay, pool: FakePool) -> None:
    await relay.ensure_started()
    assert relay.running
    await relay.stop()
    assert not relay.running
    await relay.stop()


async def test_a_restarted_relay_re_anchors_its_cursor(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    await relay.ensure_started()
    await _drained(relay)
    pool.rows = [{"id": 4, "session_id": "s1"}]
    await _drained(relay)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": 4}]
    await relay.stop()
    await relay.ensure_started()
    await _drained(relay)
    assert ws.messages == [{"event": "order_fill", "session_id": "s1", "trade_id": 4}]


# ---------------------------------------------------------------------------
# The session-status diff: a session leaving the running set ticks once
# ---------------------------------------------------------------------------

_STOPPED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _session(sid: str, status: str = "running", **over: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "status": status,
        "stopped_at": _STOPPED_AT if status != "running" else None,
        "error_message": None,
        **over,
    }


@pytest.fixture()
async def ws2() -> Iterator[FakeSocket]:
    socket = FakeSocket()
    await manager.connect("trades:s2", socket)
    try:
        yield socket
    finally:
        manager.disconnect("trades:s2", socket)


async def test_a_session_leaving_the_running_set_ticks_once(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket, ws2: FakeSocket
) -> None:
    pool.sessions = [_session("s1"), _session("s2")]
    await relay.ensure_started()
    await _drained(relay)
    pool.sessions = [_session("s1", "stopped"), _session("s2")]
    await _drained(relay)

    assert ws.messages == [
        {
            "event": "session_status",
            "session_id": "s1",
            "status": "stopped",
            "stopped_at": _STOPPED_AT.isoformat(),
            "error_message": None,
        }
    ]
    assert ws2.messages == []  # s2 still runs: no tick on its channel
    pool.sessions = [_session("s1", "stopped"), _session("s2")]
    await _drained(relay)
    assert len(ws.messages) == 1  # one tick for the transition, never per poll


async def test_the_error_message_is_carried_in_the_tick(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    pool.sessions = [_session("s1")]
    await relay.ensure_started()
    await _drained(relay)
    pool.sessions = [_session("s1", "error", error_message="ccxtpro: websocket closed")]
    await _drained(relay)

    assert ws.messages == [
        {
            "event": "session_status",
            "session_id": "s1",
            "status": "error",
            "stopped_at": _STOPPED_AT.isoformat(),
            "error_message": "ccxtpro: websocket closed",
        }
    ]


async def test_a_restarted_relay_re_baselines_the_running_set(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    pool.sessions = [_session("s1")]
    await relay.ensure_started()
    await _drained(relay)
    pool.sessions = [_session("s1", "stopped")]  # stopped while the relay was down
    await relay.stop()
    await relay.ensure_started()
    await _drained(relay)

    assert ws.messages == []  # the re-anchored snapshot absorbs the stop


async def test_a_failed_poll_keeps_the_running_set_snapshot(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    pool.sessions = [_session("s1")]
    await relay.ensure_started()
    await _drained(relay)
    pool.sessions = [_session("s1", "stopped")]
    pool.fail_on = "trading_sessions"
    await _drained(relay)
    assert ws.messages == []  # the diff could not run: the stop is not lost

    pool.fail_on = None
    await _drained(relay)
    assert len(ws.messages) == 1
    assert ws.messages[0]["event"] == "session_status"


async def test_a_failed_stopped_refetch_keeps_the_diff_and_the_tick_lands(
    relay: TradesRelay, pool: FakePool, ws: FakeSocket
) -> None:
    pool.sessions = [_session("s1")]
    await relay.ensure_started()
    await _drained(relay)
    pool.sessions = [_session("s1", "stopped")]
    pool.fail_on = "id = ANY"  # the running-set read ran; only the stopped re-fetch failed
    await _drained(relay)
    assert ws.messages == []  # the snapshot was not committed, so the diff is not lost

    pool.fail_on = None
    await _drained(relay)
    assert ws.messages == [
        {
            "event": "session_status",
            "session_id": "s1",
            "status": "stopped",
            "stopped_at": _STOPPED_AT.isoformat(),
            "error_message": None,
        }
    ]


class TestHandlerWiring:
    """The /ws/trades handler starts the app's relay from its pool, whatever the mode."""

    @pytest.fixture(autouse=True)
    def _no_relay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app.state, "trades_relay", None, raising=False)

    def test_a_connection_with_a_pool_starts_the_relay(self) -> None:
        pool = FakePool()
        with TestClient(app) as client:
            app.state.db_pool = pool  # set after startup; the lifespan closes it at exit
            with client.websocket_connect("/ws/trades?session_id=s1") as ws_client:
                ws_client.send_text("hello")
                assert json.loads(ws_client.receive_text())["channel"] == "trades:s1"
                relay = app.state.trades_relay
                assert relay is not None and relay.running
        assert app.state.trades_relay is not None and not app.state.trades_relay.running
        assert pool.closed

    def test_a_connection_without_a_pool_starts_nothing(self) -> None:
        with TestClient(app) as client:
            app.state.db_pool = None
            with client.websocket_connect("/ws/trades?session_id=s1") as ws_client:
                ws_client.send_text("hello")
                assert json.loads(ws_client.receive_text())["channel"] == "trades:s1"
                assert app.state.trades_relay is None
