"""Approval lifecycle on real TimescaleDB: rows written, read back, actor filter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from hydra.data.migrations.runner import upgrade_to_head
from hydra.risk.state import RiskEventRecord, fetch_risk_events
from hydra.services.approvals import APPROVAL_ACTOR, ApprovalStore, write_approval_event

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[asyncpg.Pool]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


def _store(pool: asyncpg.Pool, clock: FakeClock) -> ApprovalStore:
    """The store wired the way the dashboard wires it: writes go to risk_events best-effort."""

    async def writer(event: RiskEventRecord) -> None:
        await write_approval_event(pool, event)

    return ApprovalStore(event_writer=writer, clock=clock)


async def test_gated_flow_writes_rows_and_the_actor_filter_reads_them_back(
    pool: asyncpg.Pool,
) -> None:
    store = _store(pool, FakeClock())
    calls: list[tuple[Any, dict[str, Any], str]] = []

    async def release(deps: Any, params: dict[str, Any], actor: str) -> Any:
        calls.append((deps, dict(params), actor))
        return {"released": True}

    store.register("kill_switch_release", release)
    approval = await store.create(
        "kill_switch_release", "claude wants to resume trading", {"reason": "calm"}
    )

    rows = await fetch_risk_events(pool, 10)
    assert [r.kind for r in rows] == ["approval_requested"]
    requested = rows[0]
    assert requested.actor == APPROVAL_ACTOR
    assert requested.scope == "paper"
    assert requested.reason == "claude wants to resume trading"
    assert requested.details["approval_id"] == approval.id
    assert requested.details["action"] == "kill_switch_release"

    outcome = await store.approve(approval.id, approval.code, approved_by="user:soma", deps="deps")
    assert outcome.executed is True
    assert outcome.result == {"released": True}
    assert calls == [("deps", {"reason": "calm"}, APPROVAL_ACTOR)]

    rows = await fetch_risk_events(pool, 10)  # newest first
    assert [r.kind for r in rows] == ["approval_approved", "approval_requested"]
    assert rows[0].actor == "user:soma"
    assert rows[0].details["approval_id"] == approval.id

    service_rows = await fetch_risk_events(pool, 10, actor=APPROVAL_ACTOR)
    assert [r.kind for r in service_rows] == ["approval_requested"]
    user_rows = await fetch_risk_events(pool, 10, actor="user:soma")
    assert [r.kind for r in user_rows] == ["approval_approved"]


async def test_deny_dedup_and_expiry_rows(pool: asyncpg.Pool) -> None:
    clock = FakeClock()
    store = _store(pool, clock)

    first = await store.create("kill_switch_release", "claude asks", {"reason": "one"})
    again = await store.create("kill_switch_release", "claude asks again", {"reason": "two"})
    assert again is first  # dedup: one approval, one row

    await store.deny(first.id, decided_by="user:soma", reason="not now")
    fresh = await store.create("kill_switch_release", "claude asks", {"reason": "three"})
    clock.advance(minutes=10)  # exactly the TTL
    assert await store.list_pending() == []

    rows = await fetch_risk_events(pool, 20)  # newest first
    assert [r.kind for r in rows] == [
        "approval_expired",
        "approval_requested",
        "approval_denied",
        "approval_requested",
    ]
    denial = rows[2]
    assert (denial.actor, denial.details["reason"]) == ("user:soma", "not now")
    assert fresh.id in {r.details["approval_id"] for r in rows}
