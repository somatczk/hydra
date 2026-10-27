"""The D5 health split: the API's monitor beside a remote trader vs the engine's full set.

The split-API set (db, migrations, trader) embeds the remote trader's own ``/readyz`` body
under ``detail`` — a real stub trader under real uvicorn answers it here, so the embedding,
the transport-failure 503 and the gauges are the real monitor behaviour. The engine-side set
keeps today's checks, with the engine reads routed through the ``TraderBackend`` (A6).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import REGISTRY
from pydantic import SecretStr

from hydra.core import metrics
from hydra.core.config import HydraConfig, TraderConfig
from hydra.dashboard.health import REQUIRED_CHECKS, SPLIT_CHECKS, HealthMonitor
from hydra.dashboard.routes import health as health_routes
from hydra.execution.session_manager import HealthSnapshot
from hydra.services.trader import LocalTrader
from tests.unit.test_dashboard.test_health_routes import FakePool

TOKEN = "t" * 48
TRADER_BODY: dict[str, Any] = {
    "status": "ok",
    "version": "test",
    "checks": {
        "db": {"ok": True, "required": True},
        "migrations": {"ok": True, "required": True},
        "sessions": {"ok": True, "required": True, "zombies": [], "sessions": []},
        "feeds": {"ok": True, "required": True, "streams": []},
        "risk": {"ok": True, "required": True},
    },
}


class StubTrader:
    """The stub's control surface: its URL and whether its ``/readyz`` answers ok."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200

    def fail(self) -> None:
        self.status_code = 503


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


@pytest.fixture()
async def stub_trader() -> AsyncIterator[StubTrader]:
    stub = StubTrader("http://127.0.0.1:1")
    app = FastAPI()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if stub.status_code == 200:
            return JSONResponse(TRADER_BODY)
        return JSONResponse({**TRADER_BODY, "status": "not_ready"}, status_code=503)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    stub.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        yield stub
    finally:
        server.should_exit = True
        await task


def _split_app(url: str) -> FastAPI:
    app = FastAPI()
    app.state.config = HydraConfig(HYDRA_API_TOKEN=SecretStr(TOKEN), trader=TraderConfig(url=url))
    app.state.db_pool = FakePool()
    return app


def _probe(monitor: HealthMonitor) -> FastAPI:
    app = FastAPI()
    app.include_router(health_routes.router)
    app.state.health_monitor = monitor
    return app


class TestCheckSets:
    def test_an_app_beside_a_remote_trader_runs_the_split_set(
        self, stub_trader: StubTrader
    ) -> None:
        assert HealthMonitor(_split_app(stub_trader.url))._checks == SPLIT_CHECKS

    def test_an_engine_app_keeps_the_full_set(self) -> None:
        app = FastAPI()
        app.state.config = HydraConfig(HYDRA_API_TOKEN=SecretStr(TOKEN))
        assert HealthMonitor(app)._checks == REQUIRED_CHECKS

    def test_an_app_without_a_config_keeps_the_full_set(self) -> None:
        assert HealthMonitor(FastAPI())._checks == REQUIRED_CHECKS

    def test_an_explicit_checks_override_wins(self) -> None:
        app = FastAPI()
        app.state.config = HydraConfig(HYDRA_API_TOKEN=SecretStr(TOKEN))
        assert HealthMonitor(app, checks=SPLIT_CHECKS)._checks == SPLIT_CHECKS


class TestTraderCheck:
    async def test_the_trader_check_embeds_the_trader_readyz_body(
        self, stub_trader: StubTrader
    ) -> None:
        monitor = HealthMonitor(_split_app(stub_trader.url))
        result = await monitor.run_once()
        assert result.ready is True
        assert result.checks["trader"] == {"ok": True, "required": True, "detail": TRADER_BODY}
        assert set(result.checks) == {"db", "migrations", "trader"}

    async def test_a_failing_trader_fails_readiness_with_its_body(
        self, stub_trader: StubTrader
    ) -> None:
        app = _split_app(stub_trader.url)
        monitor = HealthMonitor(app, interval_s=0.05)
        probe = _probe(monitor)
        assert (await monitor.run_once()).ready is True
        stub_trader.fail()
        await asyncio.sleep(0.2)  # the fresh window (2 intervals) passes; the probe re-checks
        transport = httpx.ASGITransport(app=probe)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        trader = body["checks"]["trader"]
        assert trader["ok"] is False
        assert trader["detail"]["status"] == "not_ready"
        assert trader["detail"]["checks"]["risk"]["ok"] is True

    async def test_an_unreachable_trader_is_the_error_string(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monitor = HealthMonitor(_split_app(f"http://127.0.0.1:{port}"))
        probe_app = _probe(monitor)
        transport = httpx.ASGITransport(app=probe_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["trader"]["ok"] is False
        assert body["checks"]["trader"]["required"] is True
        assert isinstance(body["checks"]["trader"]["error"], str)

    async def test_no_trader_url_configured_is_an_error(self) -> None:
        app = FastAPI()
        app.state.config = HydraConfig(HYDRA_API_TOKEN=SecretStr(TOKEN))
        monitor = HealthMonitor(app, checks=SPLIT_CHECKS)
        result = await monitor.run_once()
        assert result.ready is False
        assert result.checks["trader"] == {
            "ok": False,
            "required": True,
            "error": "trader url not configured",
        }


class TestSplitGauges:
    async def test_the_split_run_sets_only_its_own_ready_gauges(
        self, stub_trader: StubTrader
    ) -> None:
        result = await HealthMonitor(_split_app(stub_trader.url)).run_once()
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "db"}) == 1.0
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "migrations"}) == 1.0
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "trader"}) == 1.0
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "sessions"}) is None
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "feeds"}) is None
        assert REGISTRY.get_sample_value("hydra_ready", {"check": "risk"}) is None
        assert result.sessions_known is False
        assert REGISTRY.get_sample_value("hydra_session_desired") is None


class _FakeEngine:
    """The engine handle the backend carries: one snapshot, no sessions."""

    def __init__(self, snap: HealthSnapshot) -> None:
        self._snap = snap

    async def health_snapshot(self) -> HealthSnapshot:
        return self._snap


EMPTY_SNAPSHOT = HealthSnapshot(
    sessions=(), desired_from_db=True, kill_switch=False, risk_available=True
)


class TestEngineReadsThroughTheBackend:
    async def test_the_full_set_reads_the_engine_through_the_backend(self) -> None:
        app = FastAPI()
        app.state.db_pool = FakePool()
        app.state.trader = LocalTrader(app.state.db_pool, _FakeEngine(EMPTY_SNAPSHOT))
        result = await HealthMonitor(app).run_once()
        assert result.ready is True
        assert result.checks["sessions"]["ok"] is True
        assert result.sessions_known is True

    async def test_the_direct_session_manager_bind_still_works(self) -> None:
        app = FastAPI()
        app.state.db_pool = FakePool()
        app.state.session_manager = _FakeEngine(EMPTY_SNAPSHOT)
        result = await HealthMonitor(app).run_once()
        assert result.checks["sessions"]["ok"] is True

    async def test_no_engine_at_all_is_today_session_manager_not_started(self) -> None:
        app = FastAPI()
        app.state.db_pool = FakePool()
        result = await HealthMonitor(app).run_once()
        assert result.ready is False
        assert result.checks["sessions"]["error"] == "session manager not started"
