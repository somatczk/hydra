"""/readyz and /livez on the real app and lifespan against a real TimescaleDB (romulus env)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from cryptography.fernet import Fernet
from prometheus_client.parser import text_string_to_metric_families

from hydra.core import metrics
from hydra.dashboard import api, lifespan
from hydra.data.migrations.runner import head_revision


@pytest.fixture()
def romulus_env(fresh_database: str, redis_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("HYDRA_API_TOKEN", secrets.token_urlsafe(32))
    return fresh_database


@pytest.fixture()
async def engine(romulus_env: str) -> AsyncIterator[httpx.AsyncClient]:
    metrics._reset_metrics()
    async with lifespan.run(api.app):
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            yield client
    metrics._reset_metrics()


def _sample(text: str, name: str, **labels: str) -> float | None:
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return None


async def test_ready_with_a_real_pool_and_not_ready_once_it_closes(
    engine: httpx.AsyncClient,
) -> None:
    resp = await engine.get("/readyz")
    assert resp.status_code == 200, resp.json()
    checks = resp.json()["checks"]
    assert checks["db"]["ok"] is True
    assert isinstance(checks["db"]["latency_ms"], float)
    assert checks["db"]["latency_ms"] < 2000
    assert checks["migrations"] == {
        "ok": True,
        "required": True,
        "current": head_revision(),
        "head": head_revision(),
    }
    assert checks["sessions"]["zombies"] == []
    assert checks["risk"] == {"ok": True, "required": True}

    await api.app.state.db_pool.close()
    await api.app.state.health_monitor.run_once()

    resp = await engine.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
    assert resp.json()["checks"]["db"]["ok"] is False
    assert resp.json()["checks"]["db"]["error"] == "InterfaceError"
    live = await engine.get("/livez")
    assert live.status_code == 200
    assert (await engine.get("/health")).json() == live.json()


async def test_running_row_without_a_task_is_a_zombie(
    engine: httpx.AsyncClient, romulus_env: str
) -> None:
    conn = await asyncpg.connect(romulus_env)
    try:
        await conn.execute(
            "INSERT INTO trading_sessions (id, strategy_id, trading_mode, status, exchange_id,"
            " symbols, timeframe) VALUES ('zombie-1', 'ghost', 'paper', 'running', 'binance',"
            " ARRAY['BTCUSDT'], '1m')"
        )
    finally:
        await conn.close()
    await api.app.state.health_monitor.run_once()

    resp = await engine.get("/readyz")

    assert resp.status_code == 503
    sessions = resp.json()["checks"]["sessions"]
    assert sessions["zombies"] == ["zombie-1"]
    assert sessions["sessions"] == [
        {
            "session_id": "zombie-1",
            "strategy_id": "ghost",
            "mode": "paper",
            "desired": True,
            "supervised": False,
        }
    ]
    text = (await engine.get("/metrics")).text
    labels = {"session_id": "zombie-1", "strategy_id": "ghost", "mode": "paper"}
    assert _sample(text, "hydra_session_desired", **labels) == 1.0
    assert _sample(text, "hydra_session_supervised", **labels) == 0.0
    assert _sample(text, "hydra_ready", check="sessions") == 0.0
    assert _sample(text, "hydra_ready", check="db") == 1.0
