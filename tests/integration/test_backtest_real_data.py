"""Backtests, hyperopt, walk-forward and preview against a real TimescaleDB.

Needs HYDRA_DB_HOST/PORT/NAME/USER/PASSWORD (as exported by CI). Each module run creates
and drops its own database, so the target server's data is never touched.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI

from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard import api
from hydra.dashboard.routes import backtest, strategies
from hydra.dashboard.routes._db import install_error_handlers
from hydra.data.storage import MarketDataRepository

pytestmark = pytest.mark.integration

H1_START = datetime(2026, 1, 1, tzinfo=UTC)
H1_COUNT = 300
D1_START = datetime(2024, 1, 1, tzinfo=UTC)
D1_COUNT = 400

_SAW_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": "sma:period=5",
            }
        ],
    },
    "exit_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_below",
                "value": "sma:period=5",
            }
        ],
    },
}

_NEW_COLUMNS = {
    "strategy_id",
    "initial_capital",
    "data_source",
    "data_exchange",
    "data_symbol",
    "data_timeframe",
    "bar_count",
    "first_ts",
    "last_ts",
    "gap_count",
    "requested_start",
    "requested_end",
}


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------


def _dsn(database: str) -> str:
    user = quote(os.environ.get("HYDRA_DB_USER", "hydra"), safe="")
    password = quote(os.environ.get("HYDRA_DB_PASSWORD", ""), safe="")
    host = os.environ["HYDRA_DB_HOST"]
    port = os.environ.get("HYDRA_DB_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(_dsn(os.environ.get("HYDRA_DB_NAME", "postgres")), timeout=10)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _create_database() -> str:
    """Create an empty database on the configured server; fail (not skip) if unreachable."""
    if not os.environ.get("HYDRA_DB_HOST"):
        pytest.skip("HYDRA_DB_HOST not set; integration database not configured")
    name = f"hydra_bt_it_{uuid.uuid4().hex[:10]}"
    try:
        await _admin(f'CREATE DATABASE "{name}"')
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.fail(f"HYDRA_DB_* is set but the database server is unreachable: {exc!r}")
    return name


async def _drop_database(name: str) -> None:
    await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@asynccontextmanager
async def _fresh_database() -> AsyncIterator[str]:
    name = await _create_database()
    try:
        yield _dsn(name)
    finally:
        await _drop_database(name)


@pytest.fixture(scope="module")
def database_dsn() -> Iterator[str]:
    name = asyncio.run(_create_database())
    try:
        yield _dsn(name)
    finally:
        asyncio.run(_drop_database(name))


async def _migrate(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    monkeypatch.setenv("DATABASE_URL", dsn)
    await api._run_migrations()


async def _columns(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'backtest_results'"
    )
    return {r["column_name"]: r["is_nullable"] for r in rows}


@pytest.fixture()
async def pool(database_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    await _migrate(monkeypatch, database_dsn)
    db_pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
    try:
        async with db_pool.acquire() as conn:
            assert set(await _columns(conn)) >= _NEW_COLUMNS, "migration did not apply"
            await conn.execute("TRUNCATE ts.ohlcv_1m")
            await conn.execute("TRUNCATE backtest_results CASCADE")
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture(autouse=True)
def _clean_route_state() -> Iterator[None]:
    stores: list[dict[str, Any]] = [
        backtest._TASKS,
        backtest._RESULTS,
        backtest._HYPEROPT_TASKS,
        backtest._HYPEROPT_RESULTS,
        backtest._WALKFORWARD_TASKS,
        backtest._WALKFORWARD_RESULTS,
    ]
    for s in stores:
        s.clear()
    yield
    for s in stores:
        s.clear()


@pytest.fixture()
async def client(pool: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(backtest.router)
    app.include_router(strategies.router)
    install_error_handlers(app)
    app.state.db_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _saw(start: datetime, count: int, step: timedelta) -> list[OHLCV]:
    """Deterministic sawtooth: close climbs 1000 -> 1095 over 20 bars, then resets."""
    bars = []
    for i in range(count):
        price = Decimal(1000 + (i % 20) * 5)
        bars.append(
            OHLCV(
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(10),
                timestamp=start + i * step,
            )
        )
    return bars


def _flat(start: datetime, count: int, step: timedelta, price: str) -> list[OHLCV]:
    p = Decimal(price)
    return [
        OHLCV(open=p, high=p, low=p, close=p, volume=Decimal(1), timestamp=start + i * step)
        for i in range(count)
    ]


async def _seed(pool: Any, exchange: str, timeframe: Timeframe, bars: list[OHLCV]) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await repo.store_bars([(exchange, "BTCUSDT", timeframe, b) for b in bars])


async def _seed_hourly(pool: Any) -> list[OHLCV]:
    binance = _saw(H1_START, H1_COUNT, timedelta(hours=1))
    await _seed(pool, "binance", Timeframe.H1, binance)
    await _seed(pool, "kraken", Timeframe.H1, _flat(H1_START, H1_COUNT, timedelta(hours=1), "5"))
    return binance


def _write_strategy(directory: Path, strategy_id: str, **overrides: Any) -> None:
    data: dict[str, Any] = {
        "id": strategy_id,
        "name": f"Saw {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _SAW_RULES, "required_history": 10},
    }
    data.update(overrides)
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


async def _drain() -> None:
    await asyncio.gather(*list(backtest._BACKGROUND_TASKS))


async def _run(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    resp = await client.post("/api/backtest/run", json=body)
    assert resp.status_code == 202, resp.text
    await _drain()
    status = (await client.get(f"/api/backtest/status/{resp.json()['task_id']}")).json()
    assert status["status"] == "completed", status
    return status


def _iso(ts: datetime) -> str:
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Single backtest
# ---------------------------------------------------------------------------


async def test_backtest_runs_on_stored_bars_with_exact_provenance(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    status = await _run(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31", name="real"
    )
    result_id = status["result_id"]
    detail = (await client.get(f"/api/backtest/results/{result_id}")).json()

    assert detail["provenance"] == {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "bar_count": H1_COUNT,
        "first_ts": _iso(seed[0].timestamp),
        "last_ts": _iso(seed[-1].timestamp),
        "gap_count": 0,
        "requested_start": "2026-01-01T00:00:00+00:00",
        "requested_end": "2026-01-31T23:59:59.999999+00:00",
        "data_source": "db",
    }
    assert detail["strategy_id"] == "saw"
    assert detail["initial_capital"] == 10000.0
    assert detail["period"] == f"{_iso(seed[0].timestamp)} - {_iso(seed[-1].timestamp)}"

    total_trades = detail["metrics"]["total_trades"]
    assert total_trades > 0
    for trade in detail["trades"]:
        # The sawtooth trades 1000-1095 plus slippage; kraken is 5.0, the old random walk ~42000.
        assert 900 < trade["entry_price"] < 1200
        assert 900 < trade["exit_price"] < 1200

    async with pool.acquire() as conn:
        persisted = await conn.fetchval(
            "SELECT count(*) FROM backtest_result_trades WHERE backtest_id = $1", result_id
        )
        row = await conn.fetchrow(
            "SELECT data_source, data_exchange, bar_count, first_ts, last_ts, total_trades "
            "FROM backtest_results WHERE id = $1",
            result_id,
        )
    assert persisted == total_trades == row["total_trades"]
    assert row["data_source"] == "db"
    assert row["data_exchange"] == "binance"
    assert row["bar_count"] == H1_COUNT
    assert (row["first_ts"], row["last_ts"]) == (seed[0].timestamp, seed[-1].timestamp)

    listed = (await client.get("/api/backtest/results")).json()
    assert [r["id"] for r in listed] == [result_id]
    assert listed[0]["provenance"]["bar_count"] == H1_COUNT


async def test_results_survive_a_restart(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    result_id = (
        await _run(client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31")
    )["result_id"]
    before = (await client.get(f"/api/backtest/results/{result_id}")).json()

    backtest._RESULTS.clear()
    await backtest.populate_cache_from_db(pool)

    after = (await client.get(f"/api/backtest/results/{result_id}")).json()
    assert len(after["trades"]) == len(before["trades"]) == before["metrics"]["total_trades"]
    assert [t["entry_time"] for t in after["trades"]] == [t["entry_time"] for t in before["trades"]]
    assert [t["pnl"] for t in after["trades"]] == [t["pnl"] for t in before["trades"]]
    assert after["provenance"] == before["provenance"]
    assert set(backtest._RESULTS) == {result_id}


async def test_no_stored_bars_is_422_with_available_range(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    resp = await client.post(
        "/api/backtest/run",
        json={"strategy_id": "saw", "start_date": "2020-01-01", "end_date": "2020-02-01"},
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "no_market_data"
    assert detail["state"] == "no_data"
    assert detail["available_first_ts"] == _iso(seed[0].timestamp)
    assert detail["available_last_ts"] == _iso(seed[-1].timestamp)
    assert "available: 2026-01-01T00:00:00+00:00 to 2026-01-13T11:00:00+00:00" in detail["message"]
    assert backtest._TASKS == {}
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0


async def test_bars_are_filtered_by_exchange(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(
        strategies_dir, "saw_kraken", exchange={"exchange_id": "kraken", "market_type": "SPOT"}
    )
    _write_strategy(
        strategies_dir, "saw_okx", exchange={"exchange_id": "okx", "market_type": "SPOT"}
    )

    status = await _run(
        client, strategy_id="saw_kraken", start_date="2026-01-01", end_date="2026-01-31"
    )
    detail = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()
    assert detail["provenance"]["exchange"] == "kraken"
    assert detail["provenance"]["bar_count"] == H1_COUNT
    assert all(t["entry_price"] < 10 for t in detail["trades"])

    resp = await client.post(
        "/api/backtest/run",
        json={"strategy_id": "saw_okx", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["available_first_ts"] is None
    assert "available: none stored" in resp.json()["detail"]["message"]


async def test_bare_end_date_includes_that_whole_day(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    status = await _run(client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-12")
    prov = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()["provenance"]
    assert prov["bar_count"] == 12 * 24
    assert prov["last_ts"] == "2026-01-12T23:00:00+00:00"


async def test_request_timeframe_overrides_primary_and_is_recorded(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw4h", timeframes={"primary": "4h"})

    default = await client.post(
        "/api/backtest/run",
        json={"strategy_id": "saw4h", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    )
    assert default.status_code == 422
    assert "binance BTCUSDT 4h" in default.json()["detail"]["message"]

    status = await _run(
        client, strategy_id="saw4h", start_date="2026-01-01", end_date="2026-01-31", timeframe="1h"
    )
    prov = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()["provenance"]
    assert (prov["timeframe"], prov["bar_count"]) == ("1h", H1_COUNT)


# ---------------------------------------------------------------------------
# Hyperopt and walk-forward
# ---------------------------------------------------------------------------


async def _hyperopt(client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    resp = await client.post(
        "/api/backtest/hyperopt",
        json={
            "strategy_id": "saw",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "method": "grid",
            "max_trials": 2,
            "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
        },
    )
    assert resp.status_code == 202, resp.text
    await _drain()
    task_id = resp.json()["task_id"]
    progress = (await client.get(f"/api/backtest/hyperopt/{task_id}")).json()
    assert progress["status"] == "completed", progress
    results = (await client.get(f"/api/backtest/hyperopt/{task_id}/results")).json()
    return progress, results


async def test_hyperopt_grid_runs_on_stored_bars_reproducibly(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    progress, first = await _hyperopt(client)
    _, second = await _hyperopt(client)

    assert progress["provenance"]["bar_count"] == H1_COUNT
    assert progress["strategy_id"] == "saw"
    assert progress["start_date"] == "2026-01-01T00:00:00Z"
    assert first["provenance"]["exchange"] == "binance"
    assert [t["params"] for t in first["trials"]] == [{"sma_period": 4}, {"sma_period": 5}]
    assert first["best_metric"] == second["best_metric"]
    assert [t["sharpe"] for t in first["trials"]] == [t["sharpe"] for t in second["trials"]]
    assert any(t["total_trades"] > 0 for t in first["trials"])


async def test_walkforward_uses_the_real_span(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    daily = _saw(D1_START, D1_COUNT, timedelta(days=1))
    await _seed(pool, "binance", Timeframe.D1, daily)
    _write_strategy(strategies_dir, "saw_d", timeframes={"primary": "1d"})

    resp = await client.post(
        "/api/backtest/walkforward",
        json={
            "strategy_id": "saw_d",
            "start_date": "2024-01-01",
            "end_date": "2025-12-31",
            "fold_count": 2,
        },
    )
    assert resp.status_code == 202, resp.text
    await _drain()
    task_id = resp.json()["task_id"]
    result = (await client.get(f"/api/backtest/walkforward/{task_id}/results")).json()
    assert len(result["folds"]) >= 1
    assert result["provenance"]["bar_count"] == D1_COUNT
    assert result["provenance"]["first_ts"] == _iso(daily[0].timestamp)
    assert result["provenance"]["last_ts"] == _iso(daily[-1].timestamp)
    task = backtest._WALKFORWARD_TASKS[task_id]
    assert (task["in_sample_months"], task["out_of_sample_months"]) == (5, 2)

    short = await client.post(
        "/api/backtest/walkforward",
        json={
            "strategy_id": "saw_d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "fold_count": 2,
        },
    )
    assert short.status_code == 422
    assert short.json()["detail"]["code"] == "range_too_short"
    assert "range too short for 2 folds" in short.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Builder preview
# ---------------------------------------------------------------------------


async def test_preview_uses_the_latest_stored_bars(client: httpx.AsyncClient, pool: Any) -> None:
    seed = await _seed_hourly(pool)
    body = {
        "rules": _SAW_RULES,
        "timeframe": "1h",
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "bars_count": 100,
    }

    resp = await client.post("/api/strategies/preview", json=body)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    window_start = seed[-100].timestamp
    assert data["provenance"]["bar_count"] == 100
    assert data["provenance"]["first_ts"] == _iso(window_start)
    assert data["provenance"]["last_ts"] == _iso(seed[-1].timestamp)
    assert data["signals"], "sawtooth rules should signal on 100 bars"
    for signal in data["signals"]:
        assert datetime.fromisoformat(signal["timestamp"]) >= window_start
        assert 900 < signal["price"] < 1200

    missing = await client.post("/api/strategies/preview", json={**body, "symbol": "ETHUSDT"})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "no_market_data"


# ---------------------------------------------------------------------------
# Migration: provenance columns, purge of rows without provenance, idempotent
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = [
    """
    CREATE TABLE backtest_results (
        id TEXT PRIMARY KEY,
        strategy TEXT NOT NULL,
        period TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed',
        total_trades INTEGER NOT NULL DEFAULT 0,
        win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
        total_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
        max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
        sharpe_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
        equity_curve JSONB NOT NULL DEFAULT '[]',
        name TEXT NOT NULL DEFAULT '',
        transactions JSONB NOT NULL DEFAULT '[]',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE backtest_result_trades (
        id SERIAL PRIMARY KEY,
        backtest_id TEXT NOT NULL REFERENCES backtest_results(id) ON DELETE CASCADE,
        entry_time TIMESTAMPTZ NOT NULL,
        exit_time TIMESTAMPTZ NOT NULL,
        side TEXT NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL,
        exit_price DOUBLE PRECISION NOT NULL,
        pnl DOUBLE PRECISION NOT NULL
    )
    """,
]


async def test_migration_adds_provenance_and_purges_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fresh_database() as dsn:
        conn = await asyncpg.connect(dsn)
        try:
            for ddl in _LEGACY_SCHEMA:
                await conn.execute(ddl)
            for rid, strategy in (("bt-1", "LSTM Momentum"), ("bt-task-0legacy", "Old run")):
                await conn.execute(
                    "INSERT INTO backtest_results (id, strategy, period) VALUES ($1, $2, 'x')",
                    rid,
                    strategy,
                )
            await conn.execute(
                "INSERT INTO backtest_result_trades "
                "(backtest_id, entry_time, exit_time, side, entry_price, exit_price, pnl) "
                "VALUES ('bt-task-0legacy', now(), now(), 'LONG', 42000, 42100, 1)"
            )

            await _migrate(monkeypatch, dsn)

            columns = await _columns(conn)
            assert set(columns) >= _NEW_COLUMNS
            assert columns["data_source"] == "NO"
            assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0
            assert await conn.fetchval("SELECT count(*) FROM backtest_result_trades") == 0

            await conn.execute(
                "INSERT INTO backtest_results (id, strategy, period, data_source) "
                "VALUES ('bt-task-real', 'Saw', 'x', 'db')"
            )
            await _migrate(monkeypatch, dsn)

            ids = [r["id"] for r in await conn.fetch("SELECT id FROM backtest_results")]
            assert ids == ["bt-task-real"]
        finally:
            await conn.close()
