"""/livez, /readyz and /health over a real SessionManager (no DB: tests/integration covers it).

The session side is the real seam (``SessionManager.health_snapshot`` on running paper sessions
with fake ccxt clients); the DB pool is an in-memory trading_sessions table plus the readiness
queries. A zombie from a real DB row is in tests/integration/test_readiness.py.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.types import Timeframe
from hydra.dashboard.api import app as real_app
from hydra.dashboard.health import (
    CACHE_INTERVALS,
    FEED_MESSAGE_MAX_AGE_S,
    HealthMonitor,
    closed_bar_max_age,
)
from hydra.dashboard.routes import health as health_routes
from hydra.dashboard.routes import system as system_routes
from hydra.data.bars import last_closed_open
from hydra.data.migrations.runner import head_revision
from hydra.execution.client_factory import ClientFactory, SharedRestClients
from hydra.execution.session_manager import HealthSnapshot, SessionHealth, TradingSession
from tests.fixtures.binance_kline import FakeRestExchange, FakeWsExchange
from tests.unit.test_execution.session_harness import (
    Harness,
    markets_loader,
    settle,
    write_strategy,
)

M = timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


class FakePool:
    """A trading_sessions table in memory, the readiness queries and paper writes that land.

    ``closed`` makes every query raise, the way a closed asyncpg pool does; ``gate`` holds
    ``SELECT 1`` until it is set, and ``pings`` counts them.
    """

    def __init__(self) -> None:
        self.revision = head_revision()
        self.closed = False
        self.status: dict[str, tuple[str, str, str]] = {}
        self.pings = 0
        self.gate: asyncio.Event | None = None

    def _check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")

    async def fetchval(self, sql: str) -> Any:
        self._check()
        if sql != "SELECT 1":
            return self.revision
        self.pings += 1
        if self.gate is not None:
            await self.gate.wait()
        return 1

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check()
        if "status = 'running'" in sql:
            return [{"id": sid} for sid, row in self.status.items() if row[2] == "running"]
        ids = args[0] if args else []
        return [
            {"id": sid, "strategy_id": self.status[sid][0], "trading_mode": self.status[sid][1]}
            for sid in ids
            if sid in self.status
        ]

    async def execute(self, sql: str, *args: Any) -> str:
        self._check()
        if sql.lstrip().startswith("INSERT INTO trading_sessions"):
            self.status[str(args[0])] = (str(args[1]), str(args[2]), str(args[3]))
        return "INSERT 0 1"

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakePool]:
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


@dataclass
class Clock:
    now: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    mono: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono


@dataclass
class MinuteHarness(Harness):
    """Harness on 1m bars; REST also serves the next minutes in case the test crosses one."""

    end: datetime = field(default_factory=lambda: last_closed_open(datetime.now(UTC), Timeframe.M1))

    def clients(self) -> ClientFactory:
        rows = [
            [int((self.end - i * M).timestamp() * 1000), 100.0, 100.0, 100.0, 100.0, 10.0]
            for i in range(-5, self.history_bars)
        ][::-1]

        def ws_factory(exchange_id: str) -> Any:
            def make() -> FakeWsExchange:
                ex = FakeWsExchange()
                self.ws.append(ex)
                return ex

            return make

        return ClientFactory(
            ws_factory=ws_factory,
            rest_pool=SharedRestClients(rest_factory=lambda eid, creds: FakeRestExchange(rows)),
            markets_loader=markets_loader,
            repository_factory=lambda _pool: None,
            history_window=20,
        )


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[MinuteHarness]:
    harness = MinuteHarness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


@dataclass
class Probe:
    app: FastAPI
    monitor: HealthMonitor
    clock: Clock
    pool: FakePool
    client: httpx.AsyncClient


@pytest.fixture()
async def probe(h: MinuteHarness) -> AsyncIterator[Probe]:
    app = FastAPI()
    app.include_router(health_routes.router)
    clock = Clock()
    pool = FakePool()
    assert h.mgr is not None
    h.mgr._db_pool = pool
    app.state.db_pool = pool
    app.state.session_manager = h.mgr
    monitor = HealthMonitor(app, interval_s=15.0, clock=clock, monotonic=clock.monotonic)
    app.state.health_monitor = monitor
    await monitor.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        yield Probe(app, monitor, clock, pool, client)
    await monitor.stop()


async def _session(h: MinuteHarness, strategies_dir: Path, sid: str) -> TradingSession:
    write_strategy(strategies_dir, sid, timeframes={"primary": "1m"})
    session = await h.session(sid, "500")
    await settle()
    return session


def _gauge(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _feed_labels() -> dict[str, str]:
    return {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1m"}


class TestReadiness:
    async def test_supervised_session_with_fresh_feed_is_ready(
        self, h: MinuteHarness, probe: Probe, strategies_dir: Path
    ) -> None:
        session = await _session(h, strategies_dir, "ready-1m")
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=probe.clock.now)
        await probe.monitor.run_once()

        resp = await probe.client.get("/readyz")

        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["sessions"]["zombies"] == []
        assert body["checks"]["sessions"]["sessions"] == [
            {
                "session_id": session.session_id,
                "strategy_id": "ready-1m",
                "mode": "paper",
                "desired": True,
                "supervised": True,
            }
        ]
        [stream] = body["checks"]["feeds"]["streams"]
        assert (stream["session_id"], stream["live"], stream["ok"]) == (
            session.session_id,
            True,
            True,
        )
        assert stream["last_message_age_s"] == 0.0
        assert body["checks"]["migrations"] == {
            "ok": True,
            "required": True,
            "current": head_revision(),
            "head": head_revision(),
        }
        assert body["checks"]["kill_switch"] == {"ok": True, "required": False, "active": False}
        labels = {"session_id": session.session_id, "strategy_id": "ready-1m", "mode": "paper"}
        assert _gauge("hydra_session_desired", **labels) == 1.0
        assert _gauge("hydra_session_supervised", **labels) == 1.0
        for check in ("db", "migrations", "sessions", "feeds", "risk"):
            assert _gauge("hydra_ready", check=check) == 1.0, check

    async def test_running_session_without_a_live_task_is_a_zombie(
        self, h: MinuteHarness, probe: Probe, strategies_dir: Path
    ) -> None:
        session = await _session(h, strategies_dir, "zombie-1m")
        assert session._task is not None
        session._task.cancel()
        await settle()
        assert session.status == "running" and not session.task_alive
        await probe.monitor.run_once()

        resp = await probe.client.get("/readyz")

        assert resp.status_code == 503
        sessions = resp.json()["checks"]["sessions"]
        assert sessions["ok"] is False
        assert sessions["zombies"] == [session.session_id]
        labels = {"session_id": session.session_id, "strategy_id": "zombie-1m", "mode": "paper"}
        assert _gauge("hydra_session_desired", **labels) == 1.0
        assert _gauge("hydra_session_supervised", **labels) == 0.0
        assert _gauge("hydra_ready", check="sessions") == 0.0

    async def test_closed_bar_overdue_on_the_monitor_clock_fails_feeds(
        self, h: MinuteHarness, probe: Probe, strategies_dir: Path
    ) -> None:
        session = await _session(h, strategies_dir, "stale-1m")
        bar_ts = _gauge("hydra_feed_last_closed_bar_timestamp_seconds", **_feed_labels())
        assert bar_ts is not None and bar_ts >= h.end.timestamp()
        assert closed_bar_max_age(60.0) == 180.0

        probe.clock.now = bar_ts + 180.0
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=probe.clock.now)
        await probe.monitor.run_once()
        assert (await probe.client.get("/readyz")).status_code == 200

        probe.clock.now = bar_ts + 181.0
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=probe.clock.now)
        await probe.monitor.run_once()
        resp = await probe.client.get("/readyz")

        assert resp.status_code == 503
        [stream] = resp.json()["checks"]["feeds"]["streams"]
        assert stream["session_id"] == session.session_id
        assert stream["last_closed_bar_age_s"] == 181.0
        assert stream["stale_reasons"] == ["closed_bar_age"]
        assert _gauge("hydra_ready", check="feeds") == 0.0
        assert _gauge("hydra_ready", check="sessions") == 1.0

    async def test_feed_message_age_over_the_alert_threshold_fails_feeds(
        self, h: MinuteHarness, probe: Probe, strategies_dir: Path
    ) -> None:
        await _session(h, strategies_dir, "silent-1m")
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=probe.clock.now)
        probe.clock.now += FEED_MESSAGE_MAX_AGE_S + 1

        resp = await probe.client.get("/readyz")

        assert resp.status_code == 503
        [stream] = resp.json()["checks"]["feeds"]["streams"]
        assert "message_age" in stream["stale_reasons"]

    async def test_unreachable_db_and_stale_schema_are_not_ready(
        self, h: MinuteHarness, probe: Probe
    ) -> None:
        probe.pool.revision = "0001"
        await probe.monitor.run_once()
        resp = await probe.client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["checks"]["migrations"]["ok"] is False

        probe.pool.closed = True
        await probe.monitor.run_once()
        resp = await probe.client.get("/readyz")
        assert resp.status_code == 503
        db = resp.json()["checks"]["db"]
        assert db == {"ok": False, "required": True, "error": "RuntimeError"}
        assert _gauge("hydra_ready", check="db") == 0.0

    async def test_kill_switch_is_reported_but_not_required(
        self, h: MinuteHarness, probe: Probe
    ) -> None:
        assert h.mgr is not None
        await h.mgr.stop_all(actor="test", reason="readiness")
        await probe.monitor.run_once()

        resp = await probe.client.get("/readyz")

        assert resp.status_code == 200
        assert resp.json()["checks"]["kill_switch"] == {
            "ok": False,
            "required": False,
            "active": True,
        }

    async def test_probe_serves_a_fresh_result_and_never_writes_the_gauges(
        self, probe: Probe
    ) -> None:
        assert _gauge("hydra_ready", check="db") == 1.0
        probe.pool.closed = True

        cached = await probe.client.get("/readyz")
        assert cached.status_code == 200
        assert probe.pool.pings == 1

        probe.clock.now += CACHE_INTERVALS * probe.monitor.interval_s + 1
        resp = await probe.client.get("/readyz")

        assert resp.status_code == 503
        assert resp.json()["checks"]["db"]["error"] == "RuntimeError"
        assert probe.monitor.last is not None and probe.monitor.last.ready is False
        assert _gauge("hydra_ready", check="db") == 1.0
        await probe.monitor.run_once()
        assert _gauge("hydra_ready", check="db") == 0.0

    async def test_concurrent_probes_share_one_evaluation(self, probe: Probe) -> None:
        assert probe.pool.pings == 1
        probe.clock.now += CACHE_INTERVALS * probe.monitor.interval_s + 1
        probe.pool.gate = asyncio.Event()

        probes = [asyncio.create_task(probe.client.get("/readyz")) for _ in range(20)]
        while probe.pool.pings < 2:
            await asyncio.sleep(0)
        for _ in range(20):
            await asyncio.sleep(0)
        probe.pool.gate.set()
        responses = await asyncio.gather(*probes)

        assert [r.status_code for r in responses] == [200] * 20
        assert probe.pool.pings == 2

    async def test_stopped_session_series_are_removed(
        self, h: MinuteHarness, probe: Probe, strategies_dir: Path
    ) -> None:
        session = await _session(h, strategies_dir, "gone-1m")
        metrics.mark_feed_message("binance", "BTCUSDT", "1m", at=probe.clock.now)
        await probe.monitor.run_once()
        labels = {"session_id": session.session_id, "strategy_id": "gone-1m", "mode": "paper"}
        assert _gauge("hydra_session_desired", **labels) == 1.0

        assert h.mgr is not None
        await h.mgr.stop_session(session.session_id)
        await probe.monitor.run_once()

        assert _gauge("hydra_session_desired", **labels) is None
        assert _gauge("hydra_session_supervised", **labels) is None
        assert _gauge("hydra_health_monitor_last_run_timestamp_seconds") == probe.clock.now


class SnapshotManager:
    """``health_snapshot`` returns the next prepared snapshot (the #136 contract)."""

    def __init__(self, *snapshots: HealthSnapshot) -> None:
        self._snapshots = list(snapshots)

    async def health_snapshot(self) -> HealthSnapshot:
        return self._snapshots.pop(0)

    def get_session(self, session_id: str) -> None:
        return None


def _health(**fields: Any) -> SessionHealth:
    base: dict[str, Any] = {
        "session_id": "s-held",
        "strategy_id": "held",
        "mode": "paper",
        "status": "running",
        "desired": True,
        "supervised": True,
        "last_bar_at": None,
        "last_bar_age_s": None,
        "last_fill_at": None,
        "open_positions": 1,
        "flatten_incomplete": False,
        "entries_paused": None,
        "feed_live": True,
        "feed_stale_reasons": {},
        "persist_backlog": 0,
        "persist_dirty": False,
        "error_message": None,
    }
    return SessionHealth(**(base | fields))


def _snapshot(*sessions: SessionHealth) -> HealthSnapshot:
    return HealthSnapshot(
        sessions=tuple(sessions), desired_from_db=True, risk_available=True, kill_switch=False
    )


async def test_errored_session_holding_base_keeps_its_position_and_dirty_series() -> None:
    app = FastAPI()
    app.state.db_pool = FakePool()
    errored = {"status": "error", "desired": False, "supervised": False}
    app.state.session_manager = SnapshotManager(
        _snapshot(_health()),
        _snapshot(_health(**errored, persist_dirty=True)),
        _snapshot(_health(**errored, open_positions=0, flatten_incomplete=True)),
        _snapshot(_health(**errored, open_positions=0)),
    )
    monitor = HealthMonitor(app)
    metrics.set_session_position("s-held", "held", "binance", "BTCUSDT", 0.01)
    position = {"session_id": "s-held", "strategy_id": "held", "exchange_id": "binance"}
    labels = {"session_id": "s-held", "strategy_id": "held", "mode": "paper"}
    await monitor.run_once()
    metrics.set_paper_persist_dirty("s-held", True)

    result = await monitor.run_once()

    assert result.ready is True
    assert _gauge("hydra_paper_persist_dirty", session_id="s-held") == 1.0
    assert _gauge("hydra_position_size", symbol="BTCUSDT", **position) == 0.01
    assert (
        _gauge("hydra_session_desired", **labels),
        _gauge("hydra_session_supervised", **labels),
    ) == (0.0, 0.0)
    await monitor.run_once()
    assert _gauge("hydra_paper_persist_dirty", session_id="s-held") == 1.0

    await monitor.run_once()
    assert _gauge("hydra_paper_persist_dirty", session_id="s-held") is None
    assert _gauge("hydra_position_size", symbol="BTCUSDT", **position) is None
    assert _gauge("hydra_session_desired", **labels) is None


class TestReadinessApi:
    async def test_serves_the_last_result_without_running_a_check(self, probe: Probe) -> None:
        probe.app.include_router(system_routes.router)
        pings = probe.pool.pings
        assert (await probe.client.get("/api/system/readiness")).json()["stale"] is False
        probe.clock.now += CACHE_INTERVALS * probe.monitor.interval_s + 1

        resp = await probe.client.get("/api/system/readiness")

        assert resp.status_code == 200
        last = probe.monitor.last
        assert last is not None
        assert resp.json() == {
            **last.body(),
            "checked_at": datetime.fromtimestamp(last.checked_at, UTC).isoformat(),
            "stale": True,
        }
        assert probe.pool.pings == pings
        await probe.client.get("/readyz")
        assert probe.pool.pings == pings + 1

    async def test_no_result_yet_is_503(self, probe: Probe) -> None:
        probe.app.include_router(system_routes.router)
        probe.monitor.last = None
        resp = await probe.client.get("/api/system/readiness")
        assert (resp.status_code, resp.json()["detail"]["code"]) == (503, "readiness_unavailable")


class TestLiveness:
    async def test_livez_needs_a_recent_monitor_run(self, probe: Probe) -> None:
        resp = await probe.client.get("/livez")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": health_routes.hydra.__version__}

        probe.clock.mono += 3 * probe.monitor.interval_s + 1
        assert (await probe.client.get("/livez")).status_code == 503
        assert (await probe.client.get("/health")).status_code == 503

        await probe.monitor.run_once()
        assert (await probe.client.get("/livez")).status_code == 200

    async def test_livez_is_down_after_stop(self, probe: Probe) -> None:
        await probe.monitor.stop()
        resp = await probe.client.get("/livez")
        assert resp.status_code == 503
        assert resp.json()["status"] == "down"


class TestRealApp:
    def test_probes_are_503_before_the_lifespan_and_public(self) -> None:
        client = TestClient(real_app, headers={"Authorization": ""})
        for path in ("/livez", "/health", "/readyz"):
            assert client.get(path).status_code == 503, path

    def test_livez_after_startup_and_health_is_an_alias(self) -> None:
        with TestClient(real_app) as client:
            livez = client.get("/livez")
            health = client.get("/health")
            readyz = client.get("/readyz")
        assert livez.status_code == 200
        assert (health.status_code, health.json()) == (livez.status_code, livez.json())
        assert readyz.status_code == 503
        assert readyz.json()["checks"]["db"] == {
            "ok": False,
            "required": True,
            "error": "no database configured",
        }
        with TestClient(real_app) as client:
            pass
        assert client.get("/livez").status_code == 503

    def test_readiness_api_needs_credentials_and_mirrors_readyz(self) -> None:
        with TestClient(real_app) as client:
            anonymous = client.get("/api/system/readiness", headers={"Authorization": ""})
            readyz = client.get("/readyz").json()
            readiness = client.get("/api/system/readiness")
        assert anonymous.status_code == 401
        assert readiness.status_code == 200
        body = readiness.json()
        assert body.pop("checked_at")
        assert body.pop("stale") is False
        assert (body, body["status"]) == (readyz, "not_ready")
