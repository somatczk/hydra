"""RemoteTrader over the real ASGI pair with the trader's server (D5).

No envelope parsing is mocked: the RemoteTrader calls the trader server built by
``hydra.trader.server.build_app`` through httpx's ASGI transport, so the wire models, the
envelope rehydration (verbatim, ``changed_fields`` included) and the route coverage are the
real round-trip. Transport errors run against a really-closed local port; the timeout
mapping is pinned with a transport that raises httpx's own timeout error (ASGI has no
network timeouts to hit).
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from hydra.core.config import HydraConfig, TraderConfig
from hydra.dashboard.trader_client import RemoteTrader
from hydra.execution.session_manager import HealthSnapshot, SessionHealth
from hydra.services.errors import ServiceError, error_detail
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest
from hydra.services.trader import TraderBackend
from tests.unit.test_dashboard.test_trader_server import RecordingBackend, _app
from tests.unit.test_services.test_trader_backend import _ops

TOKEN = "t" * 48

SNAPSHOT = HealthSnapshot(
    sessions=(
        SessionHealth(
            session_id="s1",
            strategy_id="sma5",
            mode="paper",
            status="running",
            desired=True,
            supervised=True,
            last_bar_at=datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
            last_bar_age_s=12.0,
            last_fill_at=None,
            open_positions=1,
            flatten_incomplete=False,
            entries_paused=None,
            feed_live=True,
            feed_stale_reasons={"BTCUSDT": []},
            persist_backlog=0,
            persist_dirty=False,
            error_message=None,
            protection_failures=("BTCUSDT: protective update failed (boom)",),
        ),
    ),
    desired_from_db=True,
    kill_switch=False,
    risk_available=True,
)


def _cfg(url: str | None = "http://trader", timeout_s: float = 10.0) -> HydraConfig:
    return HydraConfig(
        HYDRA_API_TOKEN=SecretStr(TOKEN),
        trader=TraderConfig(url=url, timeout_s=timeout_s),
    )


@pytest.fixture()
def backend() -> RecordingBackend:
    return RecordingBackend(
        answers={
            "kill_switch_active": {"active": True},
            "reset_paper_account": None,
            "reload_paper_capital": None,
            "health_snapshot": SNAPSHOT,
        }
    )


@pytest.fixture()
async def remote(backend: RecordingBackend) -> AsyncIterator[RemoteTrader]:
    app = _app(backend)
    client = RemoteTrader(_cfg(), transport=httpx.ASGITransport(app=app))
    try:
        yield client
    finally:
        await client.aclose()


async def _drive_every_op(remote: RemoteTrader, backend: RecordingBackend) -> None:
    body = StartSessionRequest(strategy_id="sma5", trading_mode="paper", paper_capital="1000")
    assert await remote.start_session("user:ada", body) == {"ok": True, "op": "start_session"}
    assert await remote.stop_session("s1") == {"ok": True, "op": "stop_session"}
    assert await remote.list_sessions() == [{"ok": True, "op": "list_sessions"}]
    assert await remote.session_detail("s1") == {"ok": True, "op": "session_detail"}
    order = QuickTradeRequest(session_id="s1", side="buy", quantity="0.5")
    assert await remote.quick_trade(order) == {"ok": True, "op": "quick_trade"}
    assert await remote.webhook_signal(
        symbol="BTCUSDT", side="buy", action="entry", quantity=Decimal("0.25"), strategy_id="sma5"
    ) == {"ok": True, "op": "webhook_signal"}
    assert await remote.reload_strategy("sma5") == {"ok": True, "op": "reload_strategy"}
    assert await remote.activate_kill_switch("user:ada", "boom") == {
        "ok": True,
        "op": "activate_kill_switch",
    }
    assert await remote.release_kill_switch("user:ada", "calm") == {
        "ok": True,
        "op": "release_kill_switch",
    }
    assert await remote.kill_switch_active() is True
    assert await remote.risk_status() == {"ok": True, "op": "risk_status"}
    assert await remote.var_estimate() == {"ok": True, "op": "var_estimate"}
    assert await remote.circuit_breakers() == [{"ok": True, "op": "circuit_breakers"}]
    assert await remote.reset_breaker(2, "user:ada", "reset", 2) == [
        {"ok": True, "op": "reset_breaker"}
    ]
    assert await remote.reset_paper_account("user:ada", "reset") is None
    assert await remote.live_status() == {"ok": True, "op": "live_status"}
    assert await remote.live_status(source="live") == {"ok": True, "op": "live_status"}
    assert await remote.positions() == [{"ok": True, "op": "positions"}]
    assert await remote.reload_paper_capital("user:ada") is None
    assert await remote.health_snapshot() == SNAPSHOT


class TestRemoteTrader:
    async def test_every_op_round_trips_through_the_server(
        self, remote: RemoteTrader, backend: RecordingBackend
    ) -> None:
        await _drive_every_op(remote, backend)
        assert set(backend.names()) == set(_ops(TraderBackend))
        assert len(backend.calls) == 20  # 19 ops; live_status driven twice (paper + live)

    async def test_start_session_parses_the_body_back_to_the_model(
        self, remote: RemoteTrader, backend: RecordingBackend
    ) -> None:
        await remote.start_session(
            "user:ada",
            StartSessionRequest(strategy_id="sma5", paper_capital="1000.5"),
        )
        [(_, args, _)] = backend.calls
        assert args[0] == "user:ada"
        assert args[1] == StartSessionRequest(strategy_id="sma5", paper_capital="1000.5")
        assert isinstance(args[1].paper_capital, Decimal)

    async def test_health_snapshot_round_trips_through_the_wire(self, remote: RemoteTrader) -> None:
        assert await remote.health_snapshot() == SNAPSHOT

    async def test_a_service_error_rehydrates_verbatim(self, backend: RecordingBackend) -> None:
        backend.error = ServiceError(
            409,
            error_detail(
                "restart_required", "symbols changed", "invalid", changed_fields=["symbols"]
            ),
        )
        app = _app(backend)
        async with RemoteTrader(_cfg(), transport=httpx.ASGITransport(app=app)) as remote:
            with pytest.raises(ServiceError) as exc_info:
                await remote.reload_strategy("sma5")
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {
            "code": "restart_required",
            "message": "symbols changed",
            "state": "invalid",
            "changed_fields": ["symbols"],
        }

    async def test_a_non_envelope_error_is_honest_about_the_status(
        self, backend: RecordingBackend
    ) -> None:
        class PlainText(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(500, text="Internal Server Error")

        async with RemoteTrader(_cfg(), transport=PlainText()) as remote:
            with pytest.raises(ServiceError) as exc_info:
                await remote.list_sessions()
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["code"] == "engine_error"
        assert exc_info.value.detail["status_code"] == 500

    async def test_a_transport_error_is_engine_unreachable(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        async with RemoteTrader(_cfg(url=f"http://127.0.0.1:{port}")) as remote:
            with pytest.raises(ServiceError) as exc_info:
                await remote.list_sessions()
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "engine_unreachable"
        assert exc_info.value.detail["state"] == "unavailable"

    async def test_a_timeout_is_engine_unreachable(self) -> None:
        class Slow(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("read timed out")

        async with RemoteTrader(_cfg(timeout_s=0.05), transport=Slow()) as remote:
            with pytest.raises(ServiceError) as exc_info:
                await remote.list_sessions()
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "engine_unreachable"

    async def test_aclose_closes_the_client(self, remote: RemoteTrader) -> None:
        client = remote._client
        assert not client.is_closed
        await remote.aclose()
        assert client.is_closed
