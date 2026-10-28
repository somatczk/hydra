"""Backtests, hyperopt, walk-forward and preview against a real TimescaleDB.

Needs HYDRA_DB_HOST/PORT/NAME/USER/PASSWORD (as exported by CI). The tests/infra.py fixtures
create and drop their own databases, so the target server's data is never touched.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml
from alembic import command
from fastapi import FastAPI

from hydra.backtest import worker
from hydra.core.config import DataConfig, HydraConfig
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes import backtest, strategies
from hydra.dashboard.routes._db import install_error_handlers
from hydra.data.backfill import GapRange
from hydra.data.bars import floor_to_tf, timeframe_seconds
from hydra.data.errors import DataFetchError, NoMarketDataError
from hydra.data.migrations.runner import alembic_config, upgrade_to_head
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

_METRIC_COLUMNS = {
    "total_return",
    "annualized_return",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "max_drawdown_duration_s",
}
_NEW_COLUMNS = _METRIC_COLUMNS | {
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
    "stopped_reason",
    "benchmark_equity",
    "benchmark_return",
    "alpha",
    "beta",
    "max_consecutive_wins",
    "max_consecutive_losses",
    "expectancy",
}


# ---------------------------------------------------------------------------
# Database lifecycle (database_dsn and fresh_database come from tests/infra.py)
# ---------------------------------------------------------------------------


async def _migrate(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    await asyncio.to_thread(upgrade_to_head, dsn)


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
            await conn.execute("TRUNCATE backtest_jobs CASCADE")
        yield db_pool
    finally:
        await db_pool.close()


def _app(pool: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(backtest.router)
    app.include_router(strategies.router)
    install_error_handlers(app)
    app.state.db_pool = pool
    return app


def _client_on(pool: Any, **state: Any) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A client whose app binds *state* on ``app.state`` (config, fetch_engine).

    A lifespan-less app without a bound config reads like the kill switch, so every
    test that wants the auto-fetch surfaces binds its own config (or engine) here —
    no test ever reaches the network.
    """
    app = _app(pool)
    for key, value in state.items():
        setattr(app.state, key, value)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


_NO_FETCH_CONFIG = HydraConfig(data=DataConfig(auto_fetch_enabled=False))


@pytest.fixture()
async def client(pool: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with _client_on(pool) as c:
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


async def _drain(pool: Any, fetch_engine: Any = None) -> None:
    """Run every queued job on the worker (the engine no longer executes them).

    ``fetch_engine`` injects a stand-in engine at the worker seam — no test reaches
    the network.
    """
    while await worker.run_once(pool, fetch_engine=fetch_engine) is not None:
        pass


async def _run(client: httpx.AsyncClient, pool: Any, **body: Any) -> dict[str, Any]:
    resp = await client.post("/api/backtest/run", json=body)
    assert resp.status_code == 202, resp.text
    await _drain(pool)
    status = (await client.get(f"/api/backtest/status/{resp.json()['task_id']}")).json()
    assert status["status"] == "completed", status
    return status


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _z(ts: datetime) -> str:
    """The API serialises UTC datetimes with a Z suffix."""
    return ts.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Single backtest
# ---------------------------------------------------------------------------


async def test_backtest_runs_on_stored_bars_with_exact_provenance(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    status = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31", name="real"
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
        await _run(client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31")
    )["result_id"]
    before = (await client.get(f"/api/backtest/results/{result_id}")).json()
    assert before["trades"]
    assert before["benchmark_equity"]
    assert before["metrics"]["max_consecutive_wins"] is not None
    assert before["metrics"]["expectancy"] is not None
    assert set(before["metrics"]) >= _METRIC_COLUMNS
    assert before["metrics"]["total_return"] is not None
    assert before["metrics"]["max_drawdown_duration_s"] is not None

    # every read now comes from the database, so a second read is identical
    after = (await client.get(f"/api/backtest/results/{result_id}")).json()
    assert after == before

    listed = (await client.get("/api/backtest/results")).json()
    assert listed[0]["metrics"] == before["metrics"]

    renamed = await client.patch(f"/api/backtest/results/{result_id}", json={"name": "kept"})
    assert renamed.status_code == 200
    assert renamed.json()["provenance"] == before["provenance"]
    assert (await client.delete(f"/api/backtest/results/{result_id}")).status_code == 204
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0
        assert await conn.fetchval("SELECT count(*) FROM backtest_result_trades") == 0
    assert (await client.delete(f"/api/backtest/results/{result_id}")).status_code == 404


async def test_no_stored_bars_is_422_with_available_range(pool: Any, strategies_dir: Path) -> None:
    """The moved P0-B pin: under the kill switch (data.auto_fetch_enabled=false) the
    submit-time 422 is byte-identical to the pre-P6 behaviour."""
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    async with _client_on(pool, config=_NO_FETCH_CONFIG) as client:
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
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_jobs") == 0
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0


class _NeverEngine:
    """A stand-in at the app seam: any submit-time fetch attempt fails the test."""

    def __init__(self) -> None:
        self.calls = 0

    async def ensure_available(self, *args: Any, **kw: Any) -> None:  # pragma: no cover
        self.calls += 1
        raise AssertionError("the submit path must never fetch — the worker owns availability")


async def test_a_cold_submit_is_queued_when_auto_fetch_is_enabled(
    pool: Any, strategies_dir: Path
) -> None:
    """The enabled default: a zero-data range never 422s at submit; the job is queued
    and the worker owns availability (zero egress through the app — the engine at the
    app seam is never called, and the drain runs without one)."""
    _write_strategy(strategies_dir, "saw")
    never = _NeverEngine()

    async with _client_on(pool, config=HydraConfig(), fetch_engine=never) as client:
        resp = await client.post(
            "/api/backtest/run",
            json={"strategy_id": "saw", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        )
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task_id"]
        assert resp.json()["status"] == "queued"
    assert never.calls == 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, error FROM backtest_jobs WHERE id = $1", task_id)
    assert row["status"] == "queued"

    # the drain runs with no engine: the worker answers today's honest vocabulary
    await _drain(pool)
    async with _client_on(pool, config=HydraConfig()) as client:
        status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == (
        "No market data for binance BTCUSDT 1h between 2026-01-01T00:00:00+00:00 "
        "and 2026-01-31T23:59:59.999999+00:00; available: none stored"
    )
    assert status["fetch"] is None


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

    # kraken bars are found, but the paper execution core trades Binance SPOT only
    resp = await client.post(
        "/api/backtest/run",
        json={"strategy_id": "saw_kraken", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    )
    assert resp.status_code == 202, resp.text
    await _drain(pool)
    status = (await client.get(f"/api/backtest/status/{resp.json()['task_id']}")).json()
    assert status["status"] == "failed"
    assert "kraken SPOT is not supported" in status["error"]

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

    status = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-12"
    )
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
        client,
        pool,
        strategy_id="saw4h",
        start_date="2026-01-01",
        end_date="2026-01-31",
        timeframe="1h",
    )
    prov = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()["provenance"]
    assert (prov["timeframe"], prov["bar_count"]) == ("1h", H1_COUNT)


# ---------------------------------------------------------------------------
# Hyperopt and walk-forward
# ---------------------------------------------------------------------------


async def _hyperopt(client: httpx.AsyncClient, pool: Any) -> tuple[dict[str, Any], dict[str, Any]]:
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
    await _drain(pool)
    task_id = resp.json()["task_id"]
    progress = (await client.get(f"/api/backtest/hyperopt/{task_id}")).json()
    assert progress["status"] == "completed", progress
    results = (await client.get(f"/api/backtest/hyperopt/{task_id}/results")).json()
    return progress, results


async def test_hyperopt_grid_runs_on_stored_bars_reproducibly(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    progress, first = await _hyperopt(client, pool)
    _, second = await _hyperopt(client, pool)

    assert progress["provenance"]["bar_count"] == H1_COUNT
    assert progress["provenance"]["hydra_version"] == version("hydra")
    assert re.fullmatch(r"[0-9a-f]{12}", progress["provenance"]["config_hash"])
    assert progress["strategy_id"] == "saw"
    assert progress["start_date"] == "2026-01-01T00:00:00Z"
    assert first["provenance"]["exchange"] == "binance"
    assert first["provenance"]["hydra_version"] == version("hydra")
    assert [t["params"] for t in first["trials"]] == [{"sma_period": 4}, {"sma_period": 5}]
    assert all(t["status"] == "ok" for t in first["trials"])
    assert first["best_metric"] == second["best_metric"]
    assert [t["sharpe"] for t in first["trials"]] == [t["sharpe"] for t in second["trials"]]
    assert any(t["total_trades"] > 0 for t in first["trials"])

    # the validation numbers: same seed, same holdout, same DSR/PBO on the same bars
    assert first["seed"] == second["seed"] == 42
    assert first["n_trials"] == second["n_trials"] == 2
    assert first["failed_trials"] == 0
    assert first["holdout"] is not None
    assert first["holdout"]["total_trades"] > 0
    assert first["holdout"]["sharpe"] == second["holdout"]["sharpe"]
    assert first["holdout_psr"] == second["holdout_psr"]
    assert first["dsr"] == second["dsr"]
    assert first["pbo"] == second["pbo"]
    # the holdout is the final 20 % of the range: the optimiser never saw bars 240+
    split = int(H1_COUNT * 0.8)
    assert first["train_start"] == _z(seed[0].timestamp)
    assert first["train_end"] == _z(seed[split - 1].timestamp)
    assert first["holdout_start"] == _z(seed[split].timestamp)
    assert first["holdout_end"] == _z(seed[-1].timestamp)


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
            "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
        },
    )
    assert resp.status_code == 202, resp.text
    await _drain(pool)
    task_id = resp.json()["task_id"]
    result = (await client.get(f"/api/backtest/walkforward/{task_id}/results")).json()
    assert len(result["folds"]) >= 1
    assert result["provenance"]["bar_count"] == D1_COUNT
    assert result["provenance"]["first_ts"] == _iso(daily[0].timestamp)
    assert result["provenance"]["last_ts"] == _iso(daily[-1].timestamp)
    assert result["provenance"]["hydra_version"] == version("hydra")
    assert re.fullmatch(r"[0-9a-f]{12}", result["provenance"]["config_hash"])

    # the window sizing the worker used lives on the durable job row
    async with pool.acquire() as conn:
        raw_detail = await conn.fetchval("SELECT detail FROM backtest_jobs WHERE id = $1", task_id)
    assert raw_detail is not None
    detail = json.loads(raw_detail)
    assert (detail["in_sample_months"], detail["out_of_sample_months"]) == (5, 2)
    # 400 daily bars over 399 days: 150 IS + 60 OOS bars per window, stepping 60 bars.
    assert len(result["folds"]) == 4
    for fold in result["folds"]:
        # every fold re-optimised in-sample and froze one of the two configs
        assert fold["selected_params"] in ({"sma_period": 4}, {"sma_period": 5})
        assert fold["n_trials"] == 2
        assert fold["in_sample_trades"] > 0
        assert fold["out_of_sample_trades"] > 0
        # the frozen params only ever traded inside the fold's own OOS window
        assert fold["is_start"] < fold["is_end"] <= fold["oos_start"] < fold["oos_end"]
    assert isinstance(result["oos_psr"], float)
    assert result["anchored"] is False
    assert result["seed"] == 42
    assert result["efficiency"] is None or isinstance(result["efficiency"], float)

    short = await client.post(
        "/api/backtest/walkforward",
        json={
            "strategy_id": "saw_d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "fold_count": 2,
            "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
        },
    )
    assert short.status_code == 422
    assert short.json()["detail"]["code"] == "range_too_short"
    assert "range too short for 2 folds" in short.json()["detail"]["message"]


async def test_walkforward_fast_fail_fires_only_when_the_stored_span_covers_the_request(
    pool: Any, strategies_dir: Path
) -> None:
    """The fast-fail predicate, both ways under the enabled default: a stored span that
    already covers the request AND is dense (no fetch could change the window sizing)
    fails 422 range_too_short at submit; a span that does not defers to the worker's
    post-load decision (202)."""
    daily = _saw(D1_START, D1_COUNT, timedelta(days=1))
    await _seed(pool, "binance", Timeframe.D1, daily)
    _write_strategy(strategies_dir, "saw_d", timeframes={"primary": "1d"})
    body = {"strategy_id": "saw_d", "fold_count": 2, "train_pct": 0.75}

    async with _client_on(pool, config=HydraConfig()) as client:
        # 31 stored daily bars across January's 31 grid slots: covered AND dense —
        # no fetch could change the window sizing, so the submit may fast-fail
        covered = await client.post(
            "/api/backtest/walkforward",
            json={
                **body,
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
            },
        )
        assert covered.status_code == 422
        assert covered.json()["detail"]["code"] == "range_too_short"
        assert "range too short for 2 folds" in covered.json()["detail"]["message"]

        # the request reaches past the stored span: a fetch could extend it, so the
        # worker's post-load decision owns the answer
        beyond = await client.post(
            "/api/backtest/walkforward",
            json={
                **body,
                "start_date": "2024-01-01",
                "end_date": "2026-06-01",
                "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
            },
        )
        assert beyond.status_code == 202, beyond.text
        task_id = beyond.json()["task_id"]

    # no engine in the drain: the worker sizes the windows from the really-loaded span
    await _drain(pool)
    async with _client_on(pool, config=HydraConfig()) as client:
        result = (await client.get(f"/api/backtest/walkforward/{task_id}/results")).json()
    assert len(result["folds"]) == 4
    assert result["provenance"]["bar_count"] == D1_COUNT


class _FakeJobFetchEngine:
    """A stand-in engine for the worker's fetch phase: fills the range with dense bars."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ensure_available(
        self,
        repo: MarketDataRepository,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        force: bool = False,
        on_progress: Any = None,
    ) -> None:
        self.calls.append({"start": start, "end": end})
        step_s = timeframe_seconds(timeframe)
        first = floor_to_tf(start, timeframe)
        count = int((end - first).total_seconds() // step_s) + 1
        bars = _saw(first, count, timedelta(seconds=step_s))
        await repo.store_bars([(exchange, symbol, timeframe, bar) for bar in bars])


async def test_walkforward_sparse_interior_span_defers_to_the_worker(
    pool: Any, strategies_dir: Path
) -> None:
    """The sparse-interior trap (the review's A1): 10 stored daily bars whose ends cover
    the whole 2024 request. The ends alone must not fast-fail — a fetch could fill the
    interior and change the window sizing, so the submit defers (202) and the worker
    fetches, then sizes the windows from the really-loaded dense span."""
    sparse = [
        *_saw(D1_START, 9, timedelta(days=40)),
        _saw(D1_START + timedelta(days=365), 1, timedelta(days=1))[0],
    ]
    await _seed(pool, "binance", Timeframe.D1, sparse)
    _write_strategy(strategies_dir, "saw_d", timeframes={"primary": "1d"})
    engine = _FakeJobFetchEngine()

    async with _client_on(pool, config=HydraConfig()) as client:
        resp = await client.post(
            "/api/backtest/walkforward",
            json={
                "strategy_id": "saw_d",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "fold_count": 2,
                "train_pct": 0.75,
                "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
            },
        )
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task_id"]

    # the worker fetches the gaps, then sizes the windows from the real bars
    await _drain(pool, fetch_engine=engine)
    assert len(engine.calls) == 1

    async with pool.acquire() as conn:
        raw_detail = await conn.fetchval("SELECT detail FROM backtest_jobs WHERE id = $1", task_id)
    detail = json.loads(raw_detail)
    assert (detail["in_sample_months"], detail["out_of_sample_months"]) == (4, 1)

    async with _client_on(pool, config=HydraConfig()) as client:
        result = (await client.get(f"/api/backtest/walkforward/{task_id}/results")).json()
    assert result["provenance"]["bar_count"] == 366
    assert result["provenance"]["gap_count"] == 0
    # honest sizing from the dense 366-bar span: 120 IS + 30 OOS bars per window,
    # stepping 30 bars -> 8 folds
    assert len(result["folds"]) == 8
    for fold in result["folds"]:
        assert fold["in_sample_trades"] > 0
        assert fold["out_of_sample_trades"] > 0


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

    # the moved P0-B pin: under the kill switch a cold market keeps today's 422
    async with _client_on(pool, config=_NO_FETCH_CONFIG) as no_fetch:
        missing = await no_fetch.post("/api/strategies/preview", json={**body, "symbol": "ETHUSDT"})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "no_market_data"


class _FakePreviewEngine:
    """A stand-in engine at the app seam: "fetches" by storing bars, or raises."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

    async def ensure_available(
        self,
        repo: MarketDataRepository,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        force: bool = False,
        on_progress: Any = None,
    ) -> None:
        self.calls.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
            }
        )
        if self._error is not None:
            raise self._error
        count = int((end - start).total_seconds() // timeframe_seconds(timeframe))
        first = end - timedelta(seconds=count * timeframe_seconds(timeframe))
        bars = _flat(first, count, timedelta(seconds=timeframe_seconds(timeframe)), "1000")
        await repo.store_bars([(exchange, symbol, timeframe, bar) for bar in bars])


async def test_preview_fetches_a_cold_market_one_page(pool: Any, strategies_dir: Path) -> None:
    """No stored bars + the engine: the latest bars_count bars are fetched (one page)
    and the preview runs on the re-loaded stored rows — data_source stays 'db'."""
    _write_strategy(strategies_dir, "saw")
    engine = _FakePreviewEngine()
    body = {
        "rules": _SAW_RULES,
        "timeframe": "1h",
        "exchange_id": "binance",
        "symbol": "ETHUSDT",
        "bars_count": 100,
    }

    async with _client_on(pool, config=HydraConfig(), fetch_engine=engine) as client:
        resp = await client.post("/api/strategies/preview", json=body)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provenance"]["bar_count"] == 100
    assert data["provenance"]["data_source"] == "db"
    assert data["provenance"]["symbol"] == "ETHUSDT"
    assert data["provenance"]["gap_count"] == 0
    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert (call["exchange"], call["symbol"], call["timeframe"]) == ("binance", "ETHUSDT", "1h")
    assert call["end"] - call["start"] == timedelta(hours=100)


async def test_preview_fetch_past_patience_is_the_retryable_503(
    pool: Any, strategies_dir: Path
) -> None:
    """DataFetchError maps to the retryable 503 data_fetch_failed envelope."""
    _write_strategy(strategies_dir, "saw")
    engine = _FakePreviewEngine(
        error=DataFetchError(
            "rate_limited",
            "binance",
            "ETHUSDT",
            Timeframe.H1,
            weight_spent=2,
            elapsed_s=31.2,
            remaining_ranges=[
                GapRange(
                    start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 5, tzinfo=UTC)
                )
            ],
        )
    )
    body = {
        "rules": _SAW_RULES,
        "timeframe": "1h",
        "exchange_id": "binance",
        "symbol": "ETHUSDT",
        "bars_count": 100,
    }

    async with _client_on(pool, config=HydraConfig(), fetch_engine=engine) as client:
        resp = await client.post("/api/strategies/preview", json=body)

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "data_fetch_failed"
    assert detail["state"] == "fetch_failed"
    assert detail["reason"] == "rate_limited"
    assert detail["symbol"] == "ETHUSDT"


async def test_preview_when_the_exchange_has_none_keeps_the_422(
    pool: Any, strategies_dir: Path
) -> None:
    """A market the exchange genuinely has no bars for keeps today's 422 vocabulary."""
    _write_strategy(strategies_dir, "saw")
    engine = _FakePreviewEngine(
        error=NoMarketDataError("binance", "ETHUSDT", Timeframe.H1, None, None, None, None)
    )
    body = {
        "rules": _SAW_RULES,
        "timeframe": "1h",
        "exchange_id": "binance",
        "symbol": "ETHUSDT",
        "bars_count": 100,
    }

    async with _client_on(pool, config=HydraConfig(), fetch_engine=engine) as client:
        resp = await client.post("/api/strategies/preview", json=body)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "no_market_data"
    assert detail["state"] == "no_data"
    assert "No market data for binance ETHUSDT 1h" in detail["message"]


async def test_backtest_status_surfaces_the_fetch_detail_and_tolerates_stale_fetching(
    pool: Any,
) -> None:
    """BacktestStatus carries detail.fetch verbatim; on a terminal row the status and
    error columns stay authoritative even when the stale fetch state says 'fetching'
    (the B2 requeue-skip and non-typed-exception paths both leave it behind)."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_jobs (id, kind, request, strategy_config, detail, "
            "status, progress, error) VALUES "
            "($1, 'backtest', '{}'::jsonb, '{}'::jsonb, $2::jsonb, 'completed', 100, NULL), "
            "($3, 'backtest', '{}'::jsonb, '{}'::jsonb, $4::jsonb, 'failed', 12.0, $5)",
            "task-fetched",
            json.dumps({"fetch": {"state": "fetched", "bars_fetched": 60, "weight_spent": 4}}),
            "task-stale",
            json.dumps({"fetch": {"state": "fetching", "bars_fetched": 30, "weight_spent": 2}}),
            "on-demand fetch for binance BTCUSDT 1h failed (rate_limited)",
        )

    async with _client_on(pool, config=HydraConfig()) as client:
        fetched = (await client.get("/api/backtest/status/task-fetched")).json()
        stale = (await client.get("/api/backtest/status/task-stale")).json()

    assert fetched["status"] == "completed"
    assert fetched["fetch"] == {"state": "fetched", "bars_fetched": 60, "weight_spent": 4}
    # the terminal row renders honestly: the columns decide, the fetch state is
    # informational and never overrides them
    assert stale["status"] == "failed"
    assert stale["error"] == "on-demand fetch for binance BTCUSDT 1h failed (rate_limited)"
    assert stale["fetch"] == {"state": "fetching", "bars_fetched": 30, "weight_spent": 2}


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
    monkeypatch: pytest.MonkeyPatch, fresh_database: str
) -> None:
    dsn = fresh_database
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


async def test_metrics_migration_adds_columns_and_purges_old_results(
    monkeypatch: pytest.MonkeyPatch, fresh_database: str
) -> None:
    dsn = fresh_database
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), "0004_credentials_validated_at")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO backtest_results (id, strategy, period, data_source) "
            "VALUES ('bt-task-old', 'Saw', 'x', 'db')"
        )
        await conn.execute(
            "INSERT INTO backtest_result_trades "
            "(backtest_id, entry_time, exit_time, side, entry_price, exit_price, pnl) "
            "VALUES ('bt-task-old', now(), now(), 'LONG', 42000, 42100, 1)"
        )

        await _migrate(monkeypatch, dsn)

        columns = await _columns(conn)
        assert {name: columns[name] for name in _METRIC_COLUMNS} == dict.fromkeys(
            _METRIC_COLUMNS, "YES"
        )
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0
        assert await conn.fetchval("SELECT count(*) FROM backtest_result_trades") == 0

        await conn.execute(
            "INSERT INTO backtest_results (id, strategy, period, data_source, total_return) "
            "VALUES ('bt-task-new', 'Saw', 'x', 'db', 1.5)"
        )
        migration = importlib.import_module("hydra.data.migrations.versions.0005_backtest_metrics")
        for statement in migration._STATEMENTS:
            await conn.execute(statement)
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM backtest_results")]
        assert ids == ["bt-task-new"]
    finally:
        await conn.close()
