"""The /ws/trades DB relay (D5, R6): the fill tick per new trades row, in both modes.

The relay's cursor and the R6 message shape are asserted against a pool that answers the
relay's own queries; the real SQL (the ``WHERE id > cursor`` and the NULL ``session_id``
filter) is pinned in ``tests/integration/test_ws_relay_pg.py``, and the whole relay runs
against a real database inside the split e2e. The handler wiring (the relay starts from the
app's pool, in both process modes) is asserted through the real ``/ws/trades`` route.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
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
    """Answers the relay's two queries from a mutable trades table."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = False
        self.closed = False
        self.cursors: list[int | None] = []

    async def close(self) -> None:
        self.closed = True

    async def fetchval(self, sql: str) -> Any:
        assert "MAX(id)" in sql
        return max((r["id"] for r in self.rows), default=0)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("database down")
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
