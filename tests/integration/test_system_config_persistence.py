"""System config, exchange key state and portfolio reads on real TimescaleDB.

The routes run in-process over httpx's ASGI transport, on the same event loop as the asyncpg pool.
The only fake is the ccxt ``binance`` class (no exchange is called).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import ccxt.async_support
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from hydra.core.config import load_config
from hydra.core.encryption import decrypt
from hydra.dashboard import deps, lifespan
from hydra.dashboard.routes import portfolio, system
from hydra.dashboard.routes._db import install_error_handlers
from hydra.data.migrations.runner import upgrade_to_head
from hydra.risk.state import PgRiskStateStore
from hydra.services.universe import refresh_universe
from tests.fixtures.universe import offline_compute

HOOK = "hook-" + Fernet.generate_key().decode()[:16]
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(deps, "_bound", load_config())


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


def _app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(system.router)
    app.include_router(portfolio.router)
    app.state.db_pool = pool
    app.state.system_config = None
    app.state.session_manager = None
    app.state.exchange_credentials = {}
    app.state.exchange_credential_errors = {}
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_schema_and_seeded_rows(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        keys = [r["key"] for r in await conn.fetch("SELECT key FROM system_config ORDER BY key")]
        column = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'exchange_credentials' AND column_name = 'validated_at'"
        )
    assert keys == [
        "default_pair",
        "default_timeframe",
        "max_concurrent_strategies",
        "paper_capital",
        "trading_mode",
    ]
    assert column == "timestamp with time zone"
    async with _client(_app(pool)) as client:
        data = (await client.get("/api/system/config")).json()
    assert data["paper_capital"] == 10000.0
    assert data["trading_mode"] == "paper"


async def test_config_survives_a_restart_with_secrets_encrypted(
    pool: asyncpg.Pool, fresh_database: str
) -> None:
    # the running engine's steady state: the universe refresher has landed its ranking
    await refresh_universe(pool, offline_compute)
    async with _client(_app(pool)) as client:
        resp = await client.put(
            "/api/system/config",
            json={"paper_capital": 25000, "default_pair": "ETH/USDT", "webhook_secret": HOOK},
        )
    assert resp.status_code == 200, resp.text

    async with pool.acquire() as conn:
        rows = {
            r["key"]: (json.loads(r["value"]), r["updated_by"])
            for r in await conn.fetch("SELECT key, value, updated_by FROM system_config")
        }
    assert rows["paper_capital"] == (25000, "anonymous")
    raw_secret = rows["webhook_secret"][0]
    assert raw_secret != HOOK
    assert decrypt(raw_secret) == HOOK

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    restarted = _app(pool)
    await lifespan._load_system_config(restarted)
    assert restarted.state.system_config["webhook_secret"] == HOOK
    async with _client(restarted) as client:
        data = (await client.get("/api/system/config")).json()
    assert data["paper_capital"] == 25000.0
    assert data["default_pair"] == "ETH/USDT"
    assert data["webhook_secret_set"] is True
    assert HOOK not in json.dumps(data)

    loaded = await PgRiskStateStore(pool).load_config()
    assert loaded.paper_capital == Decimal("25000")


async def test_missing_paper_capital_row_is_503_everywhere(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM system_config WHERE key = 'paper_capital'")
    async with _client(_app(pool)) as client:
        config = await client.get("/api/system/config")
        summary = await client.get("/api/portfolio/summary?source=paper")
    assert config.status_code == 503
    assert config.json()["detail"]["code"] == "system_config_missing"
    assert summary.status_code == 503
    assert summary.json()["detail"]["code"] == "system_config_missing"
    assert "10000" not in summary.text


class _FakeBinance:
    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    async def fetch_balance(self) -> dict[str, Any]:
        return {"free": {"USDT": 1.0}}

    async def close(self) -> None:
        return None


async def test_connect_stores_validated_at(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ccxt.async_support, "binance", _FakeBinance)
    async with _client(_app(pool)) as client:
        resp = await client.post(
            "/api/system/exchanges/binance/connect",
            json={"api_key": "k-" + HOOK, "api_secret": "s-" + HOOK},
        )
        assert resp.status_code == 200, resp.text
        exchanges = (await client.get("/api/system/exchanges")).json()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_key, validated_at, validated_environment FROM exchange_credentials "
            "WHERE exchange_id = $1",
            "binance",
        )
    assert row is not None
    assert decrypt(row["encrypted_key"]) == "k-" + HOOK
    assert row["validated_at"] is not None
    assert row["validated_environment"] == "testnet"
    assert exchanges[0]["connected"] is True
    assert exchanges[0]["environment"] == "testnet"
    assert exchanges[0]["last_sync"] == row["validated_at"].isoformat()


async def test_portfolio_reads_on_the_real_schema(pool: asyncpg.Pool) -> None:
    today = datetime.now(UTC)
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO trading_sessions (id, strategy_id, trading_mode, status, timeframe, "
            "paper_capital, stopped_at) VALUES ($1, $2, 'paper', $3, '1h', $4, $5)",
            [
                ("big", "s1", "running", 9000, None),
                ("small", "s2", "stopped", 1000, T0 + timedelta(minutes=90)),
            ],
        )
        await conn.executemany(
            "INSERT INTO balance_snapshots (timestamp, total_value, source, session_id) "
            "VALUES ($1, $2, 'paper', $3)",
            [
                (T0, 9100, "big"),
                (T0 + timedelta(hours=1), 1050, "small"),
                (T0 + timedelta(hours=2), 8600, "big"),
                (T0 + timedelta(hours=2, minutes=30), 8650, "big"),
                (T0 + timedelta(hours=2, minutes=40), 123, None),
            ],
        )
        await conn.execute(
            "INSERT INTO risk_events (ts, scope, kind, actor, details) "
            "VALUES ($1, 'paper', 'paper_reset', 'user:soma', $2::jsonb)",
            T0 + timedelta(hours=3),
            '{"old_capital": "10000", "new_capital": "20000", "old_carried": "50"}',
        )
        await conn.executemany(
            "INSERT INTO trades (symbol, side, quantity, price, fee, pnl, timestamp, strategy_id, "
            "source, session_id) VALUES ('BTCUSDT', 'SELL', 0.01, 60000, $1, $2, $3, 's', "
            "'paper', $4)",
            [
                (20, -100, today, "small"),
                (0, 50, today, "big"),
                (0, 500, today - timedelta(days=2), "big"),
            ],
        )
        await conn.execute(
            "INSERT INTO positions (strategy_id, session_id, exchange_id, symbol, direction, "
            "quantity, avg_entry_price, unrealized_pnl, source) "
            "VALUES ('s1', 'big', 'binance', 'BTCUSDT', 'LONG', 0.1, 60000, 5, 'paper')"
        )
        await conn.executemany(
            "INSERT INTO ts.ohlcv_1m (exchange, symbol, timeframe, timestamp, open, high, low, "
            "close, volume) VALUES ($1, 'BTCUSDT', $2, $3, 1, 1, 1, $4, 1)",
            [
                ("binance", "1h", T0, 61000),
                ("bybit", "1h", T0 + timedelta(minutes=5), 99999),
                ("binance", "1m", T0 + timedelta(minutes=6), 1),
            ],
        )

    async with _client(_app(pool)) as client:
        summary = (await client.get("/api/portfolio/summary?source=paper")).json()
        curve = (await client.get("/api/portfolio/equity-curve?source=paper")).json()
        positions = (await client.get("/api/portfolio/positions?source=paper")).json()
        trades = await client.get("/api/portfolio/trades?from_date=2026-01-01&to_date=2099-01-01")
        bad = await client.get("/api/portfolio/trades?from_date=notadate")
        daily = (await client.get("/api/portfolio/daily-pnl?source=paper")).json()
        attribution = (await client.get("/api/portfolio/attribution?source=paper")).json()
        live = await client.get("/api/portfolio/live-metrics?source=paper")

    # hourly last values; the reset at T0+3h moves capital to 20000 and drops the stopped sleeve
    assert [p["value"] for p in curve] == [10100.0, 10150.0, 9700.0, 19650.0]
    assert summary["max_drawdown_pct"] == round(450 / 10150 * 100, 1)
    assert summary["total_value"] == 19650.0
    assert summary["daily_pnl"] == -70.0
    assert summary["realized_pnl"] == 450.0
    assert summary["unrealized_pnl"] == 5.0
    assert daily == [
        {"date": (today - timedelta(days=2)).strftime("%Y-%m-%d"), "pnl": 500.0},
        {"date": today.strftime("%Y-%m-%d"), "pnl": -70.0},
    ]
    assert [a["pnl"] for a in attribution] == [430.0]
    assert live.status_code == 200, live.text
    assert positions[0]["current_price"] == 61000.0
    assert positions[0]["unrealized_pnl"] == 100.0
    assert trades.status_code == 200
    assert trades.json()["total"] == 3
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "validation_error"
