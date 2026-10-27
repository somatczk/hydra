"""The D5 kill-switch DB latch on real TimescaleDB: the write, and the trader's application.

``latch_kill_switch_db`` writes the same ``risk_config`` row the engine's RiskService
latches plus its audit event; the trader's cleanup-loop predicate
(``SessionManager._apply_db_kill_switch_latch``, R7) applies it exactly once — the
in-memory services latch during ``stop_all``, so a second tick never sweeps again.
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
from hydra.execution.session_manager import SessionManager
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


async def _manager(pool: asyncpg.Pool) -> SessionManager:
    mgr = SessionManager(db_pool=pool, config=HydraConfig(HYDRA_API_TOKEN=SecretStr("t" * 48)))
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
            "SELECT scope, kind, actor, details FROM risk_events WHERE kind = $1 ORDER BY id",
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
