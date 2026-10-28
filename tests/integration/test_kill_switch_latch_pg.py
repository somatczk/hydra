"""The D5 kill-switch DB latch on real TimescaleDB: the write, and the trader's application.

``latch_kill_switch_db`` writes the same ``risk_config`` row the engine's RiskService
latches plus its audit event; the trader's cleanup-loop predicate
(``SessionManager._apply_db_kill_switch_latch``, R7) applies it exactly once — the
in-memory services latch during ``stop_all``, so a second tick never sweeps again. A
failed application surfaces as a ``kill_switch_latch_failed`` row (R30/A2), and the
real loop tick — not a direct call — applies a latch the API wrote while the trader
was unreachable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from pydantic import SecretStr

from hydra.core.config import HydraConfig
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution.flatten import FlattenResult
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.services import sessions as sessions_svc
from hydra.services.errors import ServiceError
from hydra.services.sessions import latch_kill_switch_db


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


async def _manager(pool: asyncpg.Pool, **kwargs: Any) -> SessionManager:
    mgr = SessionManager(
        db_pool=pool, config=HydraConfig(HYDRA_API_TOKEN=SecretStr("t" * 48)), **kwargs
    )
    await mgr.startup()
    return mgr


async def _latched(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval("SELECT kill_switch_active FROM risk_config WHERE scope = 'global'")
        )


async def _latch_events(pool: asyncpg.Pool, kind: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT scope, kind, actor, reason, details FROM risk_events WHERE kind = $1 "
            "ORDER BY id",
            kind,
        )


async def test_latch_kill_switch_db_writes_the_row_and_the_audit_event(
    pool: asyncpg.Pool,
) -> None:
    assert not await _latched(pool)
    await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
    assert await _latched(pool)
    [row] = await _latch_events(pool, "kill_switch_on")
    assert (row["scope"], row["actor"]) == ("global", "user:ada")
    details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"])
    assert details == {"persisted": True, "source": "db_latch"}


async def test_latch_without_a_risk_config_row_is_a_503(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM risk_config WHERE scope = 'global'")
    with pytest.raises(ServiceError) as exc_info:
        await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "risk_config_missing"
    assert not await _latched(pool)


async def test_latch_kill_switch_db_needs_the_pool() -> None:
    with pytest.raises(ServiceError) as exc_info:
        await latch_kill_switch_db(None, "user:ada", "trader unreachable")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "database_unavailable"


class TestTraderAppliesTheLatch:
    async def test_the_latch_applies_stop_all_once(self, pool: asyncpg.Pool) -> None:
        mgr = await _manager(pool)
        try:
            assert not mgr.kill_switch_active()
            # the switch is OFF in memory and in the DB: nothing to apply
            await mgr._apply_db_kill_switch_latch()
            assert not mgr.kill_switch_active()
            assert await _latch_events(pool, "kill_switch_on") == []

            # the API latched it in the database while the trader was unreachable
            await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
            await mgr._apply_db_kill_switch_latch()
            assert mgr.kill_switch_active()
            rows = await _latch_events(pool, "kill_switch_on")
            assert [r["scope"] for r in rows] == ["global", "paper", "live"]
            assert rows[1]["actor"] == "db-latch"
            assert rows[2]["actor"] == "db-latch"

            # R7: one application, then the in-memory latch keeps later ticks no-ops
            await mgr._apply_db_kill_switch_latch()
            await mgr._apply_db_kill_switch_latch()
            assert len(await _latch_events(pool, "kill_switch_on")) == 3
        finally:
            await mgr.graceful_shutdown()

    async def test_a_db_error_does_not_kill_the_tick(self, pool: asyncpg.Pool) -> None:
        class _BrokenPool:
            def acquire(self) -> Any:
                raise ConnectionError("database down")

        mgr = await _manager(pool)
        try:
            assert not mgr.kill_switch_active()
            mgr._db_pool = _BrokenPool()
            await mgr._apply_db_kill_switch_latch()
            assert not mgr.kill_switch_active()
            mgr._db_pool = pool
            await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
            await mgr._apply_db_kill_switch_latch()
            assert mgr.kill_switch_active()
        finally:
            mgr._db_pool = pool
            await mgr.graceful_shutdown()


class _FakeExecutor:
    """The duck-typed live surface the sweep probes (R4a)."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancelled.append(symbol)

    async def get_positions(self, symbol: str | None = None) -> list[Any]:
        return []


class _FlakyRuntime:
    """A live-session flatten that fails on its first call (the mid-sweep failure)."""

    def __init__(self, session: Any) -> None:
        self.closing = False
        self.session = session
        self.attempts = 0

    async def flatten(self) -> FlattenResult:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("flatten exploded mid-sweep")
        self.session.positions_at_stop = 0
        self.session.flatten_incomplete = False
        return FlattenResult(0)

    async def shutdown(self) -> None:
        return None

    async def broadcast_status(self) -> None:
        return None


def _flaky_live_session(mgr: SessionManager) -> _FlakyRuntime:
    session = TradingSession(
        session_id="latch-stuck",
        strategy_id="strat-latch",
        trading_mode="live",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
    )
    session._executor = _FakeExecutor()
    runtime = _FlakyRuntime(session)
    session._runtime = runtime
    mgr._sessions[session.session_id] = session
    return runtime


class TestLatchApplicationFailure:
    """R30/A2: a failed application surfaces; the flip side of the sweep decides the retry."""

    async def test_a_mid_sweep_failure_surfaces_and_a_refired_stop_all_completes_it(
        self, pool: asyncpg.Pool
    ) -> None:
        mgr = await _manager(pool)
        try:
            runtime = _flaky_live_session(mgr)
            session = mgr._sessions["latch-stuck"]
            await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
            await mgr._apply_db_kill_switch_latch()

            # the failure surfaced: the operator's signal is the risk_events row
            [row] = await _latch_events(pool, "kill_switch_latch_failed")
            assert row["scope"] == "global"
            assert "flatten exploded mid-sweep" in row["reason"]

            # stop_all latched memory BEFORE the sweep, so the sweep is partial but
            # consistent: ON, and the in-memory gate keeps later ticks from re-sweeping
            assert mgr.kill_switch_active() is True
            await mgr._apply_db_kill_switch_latch()
            assert runtime.attempts == 1

            # the operator re-fires through the API seam (no gate on the memory switch)
            body = await sessions_svc.activate_kill_switch(mgr, "operator", "re-fired stop")
            assert runtime.attempts == 2
            assert session.status == "stopped"
            assert (session.positions_at_stop, body["positions_remaining"]) == (0, 0)
        finally:
            await mgr.graceful_shutdown()

    async def test_a_failure_before_the_memory_flip_retries_next_tick(
        self, pool: asyncpg.Pool
    ) -> None:
        # no risk services: nothing latches memory, so the in-memory gate stays open
        mgr = SessionManager(db_pool=pool, config=HydraConfig(HYDRA_API_TOKEN=SecretStr("t" * 48)))
        try:
            runtime = _flaky_live_session(mgr)
            await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
            await mgr._apply_db_kill_switch_latch()
            assert runtime.attempts == 1
            assert mgr.kill_switch_active() is False

            # memory OFF: the next tick retries the application
            await mgr._apply_db_kill_switch_latch()
            assert runtime.attempts == 2
            assert mgr.kill_switch_active() is False
            rows = await _latch_events(pool, "kill_switch_latch_failed")
            assert len(rows) == 2
        finally:
            await mgr.graceful_shutdown()

    async def test_the_real_loop_tick_applies_the_latch(self, pool: asyncpg.Pool) -> None:
        mgr = await _manager(pool, cleanup_tick_s=0.05)
        try:
            await latch_kill_switch_db(pool, "user:ada", "trader unreachable")
            # the real _stale_order_cleanup_loop task applies it — no direct _apply call
            for _ in range(200):
                if mgr.kill_switch_active():
                    break
                await asyncio.sleep(0.05)
            assert mgr.kill_switch_active() is True
            # the flag flips on the first service's memory latch, before either scope's
            # audit row is written (each activation latches, persists, then audits): poll
            rows = await _latch_events(pool, "kill_switch_on")
            for _ in range(200):
                if len(rows) >= 3:
                    break
                await asyncio.sleep(0.05)
                rows = await _latch_events(pool, "kill_switch_on")
            assert [r["actor"] for r in rows] == ["user:ada", "db-latch", "db-latch"]
            assert [r["scope"] for r in rows] == ["global", "paper", "live"]

            # R7: still one application after more real ticks
            for _ in range(5):
                await asyncio.sleep(0.05)
            assert len(await _latch_events(pool, "kill_switch_on")) == 3
        finally:
            await mgr.graceful_shutdown()
