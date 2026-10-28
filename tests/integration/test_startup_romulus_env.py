"""Real lifespan, romulus engine env, real database: migrations first, then DB values are used."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import asyncpg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from hydra.core.encryption import encrypt
from hydra.dashboard import lifespan
from hydra.data.migrations.runner import current_revision, head_revision, upgrade_to_head
from tests.fixtures.universe import offline_refresher_factory


@pytest.fixture()
def key_b() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture()
def romulus_env(
    fresh_database: str, redis_url: str, key_b: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The env romulus gives the engine (stacks/hydra/compose.yml) plus the credential key."""
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR", "HYDRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", key_b)
    monkeypatch.setenv("HYDRA_API__CORS_ORIGINS", "https://trading.example.com")
    monkeypatch.setenv("TZ", "Europe/London")
    return fresh_database


async def _execute(dsn: str, sql: str, *args: object) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _fetchval(dsn: str, sql: str) -> object:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


async def test_fresh_database_boots_from_seeded_rows(
    romulus_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    app = FastAPI()
    app.state.universe_refresher_factory = offline_refresher_factory
    async with lifespan.run(app):
        mgr = app.state.session_manager
        assert app.state.db_pool is not None
        assert mgr.risk.paper_capital == Decimal("10000")
        assert mgr.risk.config.max_position_pct == Decimal("0.1000")
        assert app.state.system_config["paper_capital"] == 10000
        assert app.state.config.api.cors_origins == ["https://trading.example.com"]

    assert await current_revision(romulus_env) == head_revision()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    events = [(entry["logger"], entry["event"]) for entry in lines]
    assert all(entry["level"] in {"debug", "info", "warning"} for entry in lines), lines
    assert ("hydra.dashboard.lifespan", f"Database schema at revision {head_revision()}") in events
    migrated = events.index(
        ("hydra.dashboard.lifespan", f"Database schema at revision {head_revision()}")
    )
    risk_loaded = next(
        i
        for i, (name, event) in enumerate(events)
        if name == "hydra.execution.session_manager" and event.startswith("Risk service loaded")
    )
    assert migrated < risk_loaded


async def test_db_values_win_over_defaults_and_survive_restart(
    romulus_env: str, key_b: str
) -> None:
    await asyncio.to_thread(upgrade_to_head, romulus_env)
    await _execute(
        romulus_env, "UPDATE system_config SET value = '2500' WHERE key = 'paper_capital'"
    )
    await _execute(romulus_env, "UPDATE risk_config SET max_position_pct = 0.0700")
    await _execute(
        romulus_env,
        "INSERT INTO exchange_credentials (exchange_id, encrypted_key, encrypted_secret) "
        "VALUES ($1, $2, $3), ($4, $5, $6)",
        "binance",
        encrypt("live-key"),
        encrypt("live-secret"),
        "bybit",
        Fernet(Fernet.generate_key()).encrypt(b"lost").decode(),
        Fernet(Fernet.generate_key()).encrypt(b"lost").decode(),
    )

    for _boot in range(2):
        app = FastAPI()
        app.state.universe_refresher_factory = offline_refresher_factory
        async with lifespan.run(app):
            mgr = app.state.session_manager
            assert mgr.risk.paper_capital == Decimal("2500")
            assert mgr.risk.config.max_position_pct == Decimal("0.0700")
            assert app.state.system_config["paper_capital"] == 2500
            assert app.state.exchange_credentials == {
                "binance": {"api_key": "live-key", "api_secret": "live-secret", "passphrase": None}
            }
            assert app.state.exchange_credential_errors == {"bybit": "undecryptable"}

    assert await _fetchval(romulus_env, "SELECT count(*) FROM alembic_version") == 1
    raw = await _fetchval(
        romulus_env, "SELECT encrypted_key FROM exchange_credentials WHERE exchange_id = 'binance'"
    )
    assert raw != "live-key"
    assert key_b not in str(raw)
