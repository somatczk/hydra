"""The durable backtest job queue on real TimescaleDB.

Covers the P2-D2 acceptance: a job survives an engine restart, re-running a job is idempotent, an
abandoned running job is requeued (then poisoned after 3 losses), cancel and the job timeout work,
hyperopt and walk-forward results are durable, the strategy config snapshot wins over a later YAML
edit, and the worker waits for the schema instead of running migrations. With several workers, a
live peer's leased job is never stolen, a stale one is, and the expired worker is fenced out.

Needs HYDRA_DB_HOST/PORT/NAME/USER/PASSWORD (as exported by CI). The tests/infra.py fixtures
create and drop their own databases, so the target server's data is never touched.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI

from hydra.backtest import jobs, worker
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard.routes import backtest as backtest_routes
from hydra.dashboard.routes import strategies as strategies_routes
from hydra.dashboard.routes._db import install_error_handlers
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository

pytestmark = pytest.mark.integration

LEASE_S = 180.0
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

_NEVER_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": 1000000,
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

# the durable hyperopt test optimises two dimensions: the entry SMA period (the
# {indicator}_{param} auto key) and the exit SMA period (an explicit override)
_EXIT_SMA_RULES = {
    "entry_long": _SAW_RULES["entry_long"],
    "exit_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_below",
                "value": "sma:period=5",
                "value_ref_overrides": {"sma_period_exit": "period"},
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def pool(database_dsn: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, database_dsn)
    db_pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("TRUNCATE ts.ohlcv_1m")
            await conn.execute("TRUNCATE backtest_results CASCADE")
            await conn.execute("TRUNCATE backtest_jobs CASCADE")
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture(autouse=True)
def _heartbeat_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "worker.heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", str(path))
    return path


def _make_app(pool: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(backtest_routes.router)
    app.include_router(strategies_routes.router)
    install_error_handlers(app)
    app.state.db_pool = pool
    return app


@pytest.fixture()
async def client(pool: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_make_app(pool))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _client_for(pool: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A client on a fresh app over a fresh pool: what a restarted engine serves with."""
    transport = httpx.ASGITransport(app=_make_app(pool))
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


async def _seed(pool: Any, exchange: str, timeframe: Timeframe, bars: list[OHLCV]) -> None:
    repo = MarketDataRepository.from_pool(pool)
    await repo.store_bars([(exchange, "BTCUSDT", timeframe, b) for b in bars])


async def _seed_hourly(pool: Any) -> list[OHLCV]:
    binance = _saw(H1_START, H1_COUNT, timedelta(hours=1))
    await _seed(pool, "binance", Timeframe.H1, binance)
    return binance


def _write_strategy(
    directory: Path,
    strategy_id: str,
    rules: dict[str, Any] = _SAW_RULES,
    primary: str = "1h",
) -> None:
    data: dict[str, Any] = {
        "id": strategy_id,
        "name": f"Saw {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": primary},
        "parameters": {"rules": rules, "required_history": 10},
    }
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


async def _submit(client: httpx.AsyncClient, **body: Any) -> str:
    resp = await client.post("/api/backtest/run", json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()["task_id"]


async def _run_status(client: httpx.AsyncClient, pool: Any, task_id: str) -> dict[str, Any]:
    """Run an already-submitted job on the worker, then read its status."""
    assert await worker.run_once(pool) == task_id
    status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] == "completed", status
    return status


async def _run(client: httpx.AsyncClient, pool: Any, **body: Any) -> dict[str, Any]:
    task_id = await _submit(client, **body)
    assert await worker.run_once(pool) == task_id
    status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] == "completed", status
    return status


# ---------------------------------------------------------------------------
# Durable backtest jobs
# ---------------------------------------------------------------------------


async def test_job_survives_engine_restart(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path, database_dsn: str
) -> None:
    seed = await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    task_id = await _submit(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )

    restarted_pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=2)
    try:
        async for restarted in _client_for(restarted_pool):
            status = (await restarted.get(f"/api/backtest/status/{task_id}")).json()
            assert status["status"] == "queued"
            assert status["progress"] == 0.0
            assert await worker.run_once(restarted_pool) == task_id
    finally:
        await restarted_pool.close()

    status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] == "completed"
    assert status["progress"] == 100.0
    assert status["result_id"] == f"bt-{task_id}"

    detail = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()
    assert detail["provenance"]["bar_count"] == H1_COUNT == len(seed)
    async with pool.acquire() as conn:
        persisted = await conn.fetchval(
            "SELECT count(*) FROM backtest_result_trades WHERE backtest_id = $1",
            status["result_id"],
        )
        attempts = await conn.fetchval("SELECT attempts FROM backtest_jobs WHERE id = $1", task_id)
    assert persisted == detail["metrics"]["total_trades"]
    assert attempts == 1


async def test_rerun_is_idempotent(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    status = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    result_id = status["result_id"]
    task_id = status["task_id"]

    async with pool.acquire() as conn:
        first = dict(
            await conn.fetchrow(
                "SELECT total_trades, win_rate, total_pnl, sharpe_ratio, total_return "
                "FROM backtest_results WHERE id = $1",
                result_id,
            )
        )
        trades = await conn.fetchval(
            "SELECT count(*) FROM backtest_result_trades WHERE backtest_id = $1", result_id
        )
        await conn.execute(
            "UPDATE backtest_jobs SET status = 'queued', result = NULL, result_id = NULL, "
            "finished_at = NULL WHERE id = $1",
            task_id,
        )

    assert await worker.run_once(pool) == task_id

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 1
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM backtest_result_trades WHERE backtest_id = $1", result_id
            )
            == trades
        )
        second = dict(
            await conn.fetchrow(
                "SELECT total_trades, win_rate, total_pnl, sharpe_ratio, total_return "
                "FROM backtest_results WHERE id = $1",
                result_id,
            )
        )
    assert second == first


async def test_abandoned_running_job_is_requeued_then_poisoned(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    task_id = await _submit(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )

    claimed = await jobs.claim(pool, "w1")  # a worker claims it, then dies
    assert claimed is not None
    assert (claimed["id"], claimed["attempts"]) == (task_id, 1)

    await jobs.requeue_abandoned(pool, LEASE_S, worker_id="w1")  # its restart
    row = await jobs.get(pool, task_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("queued", 1)

    assert await worker.run_once(pool) == task_id
    row = await jobs.get(pool, task_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["attempts"] == 2

    poisoned_id = await _submit(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE backtest_jobs SET status = 'running', attempts = 3 WHERE id = $1", poisoned_id
        )
    await jobs.requeue_abandoned(pool, LEASE_S)
    row = await jobs.get(pool, poisoned_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "worker lost the job 3 times"


async def _backdate_heartbeat(pool: Any, job_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE backtest_jobs SET heartbeat_at = now() - make_interval(secs => $2) "
            "WHERE id = $1",
            job_id,
            LEASE_S + 1.0,
        )


async def test_two_workers_a_live_peer_keeps_its_job_and_a_stale_one_is_requeued(
    pool: Any,
) -> None:
    for _ in range(2):
        await jobs.enqueue(pool, "backtest", {}, {}, {})
    stale = await jobs.claim(pool, "w1")
    live = await jobs.claim(pool, "w2")
    assert stale is not None and live is not None
    stale_id, live_id = stale["id"], live["id"]

    await _backdate_heartbeat(pool, stale_id)  # w1 died; w2 keeps ticking
    assert not await jobs.tick(pool, live_id, 5.0, {}, "w2")
    await jobs.requeue_abandoned(pool, LEASE_S, worker_id="w3")  # w3's sweep

    rows = {r["id"]: r for r in [await jobs.get(pool, stale_id), await jobs.get(pool, live_id)]}
    assert (rows[stale_id]["status"], rows[stale_id]["attempts"]) == ("queued", 1)
    assert (rows[live_id]["status"], rows[live_id]["worker_id"]) == ("running", "w2")
    assert rows[live_id]["attempts"] == 1

    # w3 picks the stale job up; w1, if it was only partitioned, is fenced out of the row
    again = await jobs.claim(pool, "w3")
    assert again is not None
    assert (again["id"], again["attempts"]) == (stale_id, 2)
    with pytest.raises(jobs.LeaseLostError):
        await jobs.tick(pool, stale_id, 99.0, {"from": "w1"}, "w1")
    assert not await jobs.finish(pool, stale_id, "completed", worker_id="w1")
    await jobs.requeue_for_shutdown(pool, stale_id, "w1")
    row = await jobs.get(pool, stale_id)
    assert row is not None
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "w3", 2)
    assert row["progress"] == 0
    assert await jobs.finish(pool, stale_id, "completed", worker_id="w3")

    # a restarted worker reclaims its previous incarnation's row without waiting for the lease
    await jobs.requeue_abandoned(pool, LEASE_S, worker_id="w2")
    row = await jobs.get(pool, live_id)
    assert row is not None
    assert row["status"] == "queued"


async def test_a_worker_whose_lease_expired_abandons_the_running_job(
    pool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def _slow(pool: Any, fetch_engine: Any, job: Any, run: worker.JobRun) -> Any:
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(worker, "_HANDLERS", {"backtest": _slow})
    job_id = await jobs.enqueue(pool, "backtest", {}, {}, {})
    job = await jobs.claim(pool, "w1")
    assert job is not None
    run_task = asyncio.create_task(worker._run_claimed(pool, job, tick_s=0.05))
    await asyncio.wait_for(started.wait(), 5)

    # w1 stalled past its lease, so a peer's sweep requeued the job (the sweep itself is pinned
    # above; w1 still ticking here would renew a backdated heartbeat) and w2 claims it
    async with pool.acquire() as conn:
        await conn.execute("UPDATE backtest_jobs SET status = 'queued' WHERE id = $1", job_id)
    assert (await jobs.claim(pool, "w2")) is not None

    await asyncio.wait_for(run_task, 5)  # w1's next tick finds the lease lost
    row = await jobs.get(pool, job_id)
    assert row is not None
    assert (row["status"], row["worker_id"], row["attempts"]) == ("running", "w2", 2)
    assert row["finished_at"] is None


async def test_cancel_queued_job(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    task_id = await _submit(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )

    cancelled = await client.post(f"/api/backtest/jobs/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"task_id": task_id, "status": "cancelled"}

    assert await worker.run_once(pool) is None  # nothing left to claim
    again = await client.post(f"/api/backtest/jobs/{task_id}/cancel")
    assert again.status_code == 409


async def test_cancel_running_job(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed(pool, "binance", Timeframe.M1, _saw(H1_START, 20_000, timedelta(minutes=1)))
    _write_strategy(strategies_dir, "saw_1m", primary="1m")
    task_id = await _submit(
        client, strategy_id="saw_1m", start_date="2026-01-01", end_date="2026-01-31"
    )

    run_task = asyncio.create_task(worker.run_once(pool, tick_s=0.05))
    for _ in range(200):  # up to 10 s
        status = (await client.get(f"/api/backtest/status/{task_id}")).json()
        if status["progress"] > 0:
            break
        await asyncio.sleep(0.05)
    else:
        run_task.cancel()
        pytest.fail("job never reported progress")

    cancelled = await client.post(f"/api/backtest/jobs/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"task_id": task_id, "status": "cancelling"}

    assert await asyncio.wait_for(run_task, 2.0) == task_id
    row = await jobs.get(pool, task_id)
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0
    assert (await client.post(f"/api/backtest/jobs/{task_id}/cancel")).status_code == 409


async def test_timeout_fails_the_job(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed(pool, "binance", Timeframe.M1, _saw(H1_START, 20_000, timedelta(minutes=1)))
    _write_strategy(strategies_dir, "saw_1m", primary="1m")
    task_id = await _submit(
        client, strategy_id="saw_1m", start_date="2026-01-01", end_date="2026-01-31"
    )

    assert await worker.run_once(pool, timeout_s=0.01) == task_id

    row = await jobs.get(pool, task_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "timed out after 0.01s"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM backtest_results") == 0


# ---------------------------------------------------------------------------
# Durable hyperopt and walk-forward jobs
# ---------------------------------------------------------------------------


async def test_hyperopt_and_walkforward_are_durable(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path, database_dsn: str
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw", rules=_EXIT_SMA_RULES)

    resp = await client.post(
        "/api/backtest/hyperopt",
        json={
            "strategy_id": "saw",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "method": "grid",
            "max_trials": 4,
            "param_space": [
                {"name": "sma_period", "type": "int", "low": 4, "high": 5},
                {"name": "sma_period_exit", "type": "int", "low": 4, "high": 5},
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    ho_id = resp.json()["task_id"]
    assert await worker.run_once(pool) == ho_id

    await _seed(pool, "binance", Timeframe.D1, _saw(D1_START, D1_COUNT, timedelta(days=1)))
    _write_strategy(strategies_dir, "saw_d", primary="1d")
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
    wf_id = resp.json()["task_id"]
    assert await worker.run_once(pool) == wf_id
    before = (await client.get(f"/api/backtest/walkforward/{wf_id}/results")).json()
    assert len(before["folds"]) >= 2

    restarted_pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=2)
    try:
        async for restarted in _client_for(restarted_pool):
            progress = (await restarted.get(f"/api/backtest/hyperopt/{ho_id}")).json()
            assert progress["status"] == "completed"
            assert progress["completed_trials"] == progress["total_trials"] == 4
            assert progress["provenance"]["hydra_version"] == version("hydra")
            assert re.fullmatch(r"[0-9a-f]{12}", progress["provenance"]["config_hash"])
            results = (await restarted.get(f"/api/backtest/hyperopt/{ho_id}/results")).json()
            assert len(results["trials"]) == progress["completed_trials"]
            assert all("status" in t for t in results["trials"])
            ok_trials = [t for t in results["trials"] if t["status"] == "ok"]
            assert ok_trials
            assert results["best_metric"] == max(t["sharpe"] for t in ok_trials)
            assert results["seed"] == 42
            assert results["holdout"] is not None
            assert results["holdout"]["total_trades"] > 0
            assert results["dsr"] is not None
            assert results["provenance"]["hydra_version"] == version("hydra")
            history = (await restarted.get("/api/backtest/hyperopt/history")).json()
            assert ho_id in [h["task_id"] for h in history]

            after = (await restarted.get(f"/api/backtest/walkforward/{wf_id}/results")).json()
            assert after["folds"] == before["folds"]
            assert after["seed"] == 42
            assert all("selected_params" in f for f in after["folds"])
            assert after["provenance"]["hydra_version"] == version("hydra")
            assert re.fullmatch(r"[0-9a-f]{12}", after["provenance"]["config_hash"])
    finally:
        await restarted_pool.close()


async def test_backtest_uses_the_operators_global_risk_limits(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    """The worker passes the persisted global RiskConfig, like a paper session would (parity)."""
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")

    default = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    entries = [
        t
        for t in (await client.get(f"/api/backtest/results/{default['result_id']}")).json()[
            "transactions"
        ]
        if t["type"] == "entry"
    ]
    assert entries, "the sawtooth must trade with the default 10 % limits"
    assert all(t["quantity"] > 0.5 for t in entries)

    async with pool.acquire() as conn:
        await conn.execute("UPDATE risk_config SET max_position_pct = 0.01 WHERE scope = 'global'")
    tight = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    tight_detail = (await client.get(f"/api/backtest/results/{tight['result_id']}")).json()
    assert tight_detail["metrics"]["total_trades"] > 0
    for t in tight_detail["transactions"]:
        if t["type"] == "entry":
            # min(strategy 10 %, global 1 %): ~100 USDT entries instead of ~1000 USDT ones
            assert t["quantity"] < 0.15
    async with pool.acquire() as conn:
        await conn.execute("UPDATE risk_config SET max_position_pct = NULL WHERE scope = 'global'")


async def test_strategy_snapshot_wins_over_a_later_yaml_edit(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_strategy(strategies_dir, "saw")
    task_id = await _submit(
        client, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )

    # the YAML is edited between enqueue and run; the job still runs the snapshot
    _write_strategy(strategies_dir, "saw", rules=_NEVER_RULES)
    snap = await _run_status(client, pool, task_id)
    snapshot_detail = (await client.get(f"/api/backtest/results/{snap['result_id']}")).json()

    edited = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    edited_detail = (await client.get(f"/api/backtest/results/{edited['result_id']}")).json()
    assert edited_detail["metrics"]["total_trades"] == 0

    _write_strategy(strategies_dir, "saw")  # restore the original YAML
    fresh = await _run(
        client, pool, strategy_id="saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    fresh_detail = (await client.get(f"/api/backtest/results/{fresh['result_id']}")).json()
    assert snapshot_detail["metrics"] == fresh_detail["metrics"]
    assert snapshot_detail["trades"] == fresh_detail["trades"]
    assert snapshot_detail["equity_curve"] == fresh_detail["equity_curve"]


# ---------------------------------------------------------------------------
# The worker waits for the schema
# ---------------------------------------------------------------------------


async def test_worker_waits_for_schema(fresh_database: str) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(worker.wait_for_schema(fresh_database, poll_s=0.01), 0.3)

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    await asyncio.wait_for(worker.wait_for_schema(fresh_database, poll_s=0.01), 0.3)


# ---------------------------------------------------------------------------
# Perp backtests (P4-C): the futures 422 lifted, the job runs the perp executor
# ---------------------------------------------------------------------------


_PERP_SAW_RULES = {
    "entry_long": _SAW_RULES["entry_long"],
    "exit_long": _SAW_RULES["exit_long"],
    "entry_short": {
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
    "exit_short": {
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
}


def _write_perp_strategy(directory: Path, strategy_id: str, leverage: int = 2) -> None:
    data: dict[str, Any] = {
        "id": strategy_id,
        "name": f"Perp {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {
            "exchange_id": "binanceusdm",
            "market_type": "FUTURES",
            "leverage": leverage,
            "margin_mode": "isolated",
        },
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _PERP_SAW_RULES, "required_history": 10},
    }
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


async def _seed_perp_funding(pool: Any, count: int) -> int:
    """Settled rows every 8 hours from H1_START at +0.01 % (longs pay)."""
    rows = [
        (
            "binanceusdm",
            "BTCUSDT",
            Decimal("0.0001"),
            H1_START + timedelta(hours=8 * i),
        )
        for i in range(1, count)
    ]
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO ts.funding_rates (exchange, symbol, rate, next_funding_time,"
            " timestamp) VALUES ($1, $2, $3, NULL, $4)",
            rows,
        )
    return len(rows)


async def test_a_perp_backtest_job_runs_through_the_queue(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    seed = _saw(H1_START, H1_COUNT, timedelta(hours=1))
    await _seed(pool, "binanceusdm", Timeframe.H1, seed)
    funding_rows = await _seed_perp_funding(pool, H1_COUNT // 8)
    _write_perp_strategy(strategies_dir, "perp-saw")

    task_id = await _submit(
        client, strategy_id="perp-saw", start_date="2026-01-01", end_date="2026-01-31"
    )
    assert await worker.run_once(pool) == task_id  # fetch_engine=None: stored funding only
    status = (await client.get(f"/api/backtest/status/{task_id}")).json()
    assert status["status"] == "completed", status

    async with pool.acquire() as conn:
        detail = await conn.fetchrow("SELECT detail FROM backtest_jobs WHERE id = $1", task_id)
    job_detail = detail["detail"]
    if isinstance(job_detail, str):
        job_detail = json.loads(job_detail)
    assert job_detail["funding"] == {"state": "fetch_skipped", "rows_loaded": funding_rows}

    result = (await client.get(f"/api/backtest/results/{status['result_id']}")).json()
    assert result["provenance"]["exchange"] == "binanceusdm"
    directions = {t["side"] for t in result["trades"]}
    assert directions <= {"LONG", "SHORT"} and len(result["trades"]) > 0
    persisted = None
    async with pool.acquire() as conn:
        persisted = await conn.fetchval(
            "SELECT count(*) FROM backtest_result_trades WHERE backtest_id = $1",
            status["result_id"],
        )
    assert persisted == len(result["trades"])


async def test_a_spot_hyperopt_still_refuses_futures_strategies(
    client: httpx.AsyncClient, pool: Any, strategies_dir: Path
) -> None:
    await _seed_hourly(pool)
    _write_perp_strategy(strategies_dir, "perp-ho")
    resp = await client.post(
        "/api/backtest/hyperopt",
        json={
            "strategy_id": "perp-ho",
            "param_space": [
                {"name": "sma_period", "type": "int", "low": 2, "high": 6},
            ],
            "max_trials": 2,
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    )
    assert resp.status_code == 422
    assert "plain backtests only" in resp.text
