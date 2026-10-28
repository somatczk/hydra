"""Migration 0009 on real TimescaleDB: the live ledger schema, the store round-trip,
the DB claim-apply-confirm guard, the concurrent apply_status invariant, the
cross-session contract (F1/F13) and the downgrade path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic import command

from hydra.data.migrations.runner import (
    alembic_config,
    current_revision,
    head_revision,
    upgrade_to_head,
)
from hydra.execution.live_orders import (
    APPLIED,
    REFUSED,
    LiveOrderRecord,
    LiveOrderStatus,
    PgLiveOrderStore,
)

pytestmark = pytest.mark.integration

D = Decimal
NOW = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.fixture()
async def pool(fresh_database: str):
    import asyncio

    import asyncpg

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield created
    finally:
        await created.close()


def test_0009_is_the_single_head() -> None:
    # 0010 (P3-C's credential-permission column) is the head; 0009 lands the live schema
    assert head_revision() == "0010_validated_permissions"


async def test_the_schema_lands_idempotently_and_seeds_the_live_row(
    fresh_database: str, pool: Any
) -> None:
    import asyncio

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    assert await current_revision(fresh_database) == "0010_validated_permissions"

    async with pool.acquire() as conn:
        live_scope = await conn.fetchval("SELECT count(*) FROM risk_state WHERE scope = 'live'")
        assert live_scope == 1
        paper_scope = await conn.fetchval("SELECT count(*) FROM risk_state WHERE scope = 'paper'")
        assert paper_scope == 1

    # a second upgrade changes nothing (idempotent, forward-only)
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    assert await current_revision(fresh_database) == "0010_validated_permissions"


async def test_the_downgrade_drops_the_live_schema_and_the_seed(fresh_database: str) -> None:
    import asyncio

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    await asyncio.to_thread(
        command.downgrade, alembic_config(fresh_database), "0008_session_created_by"
    )
    assert await current_revision(fresh_database) == "0008_session_created_by"

    import asyncpg

    conn = await asyncpg.connect(fresh_database)
    try:
        assert await conn.fetchval("SELECT to_regclass('public.live_orders')") is None
        assert await conn.fetchval("SELECT to_regclass('public.live_fills')") is None
        assert await conn.fetchval("SELECT count(*) FROM risk_state WHERE scope = 'live'") == 0
    finally:
        await conn.close()

    await asyncio.to_thread(upgrade_to_head, fresh_database)
    assert await current_revision(fresh_database) == "0010_validated_permissions"


def _order(cid: str, symbol: str = "BTCUSDT", **overrides: Any) -> LiveOrderRecord:
    fields: dict[str, Any] = {
        "client_order_id": cid,
        "session_id": "sess",
        "strategy_id": "strat",
        "exchange_id": "binance",
        "symbol": symbol,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": D("0.01"),
        "status": LiveOrderStatus.PENDING_SUBMIT,
        "submitted_at": NOW,
    }
    fields.update(overrides)
    return LiveOrderRecord(**fields)


async def test_the_store_round_trips_on_the_real_schema(pool: Any) -> None:
    store = PgLiveOrderStore(pool)
    await store.create(_order("hyd-rt-1"))
    await store.create(_order("hyd-rt-2", symbol="ETHUSDT", status=LiveOrderStatus.UNKNOWN))

    assert await store.mark_ack("hyd-rt-1", "202", LiveOrderStatus.NEW)
    assert not await store.mark_ack("hyd-rt-1", "203", LiveOrderStatus.NEW)  # idempotent
    row = await store.by_client_order_id("hyd-rt-1")
    assert row is not None
    assert (row.status, row.exchange_order_id) == (LiveOrderStatus.NEW, "202")

    assert (
        await store.apply_status(
            "hyd-rt-1",
            LiveOrderStatus.PARTIALLY_FILLED,
            filled_quantity=D("0.005"),
            average_fill_price=D("100000"),
        )
        == APPLIED
    )
    row = await store.by_client_order_id("hyd-rt-1")
    assert row is not None
    assert (row.filled_quantity, row.average_fill_price) == (D("0.005"), D("100000"))

    assert await store.resolve_unknown(
        "hyd-rt-2", "201", LiveOrderStatus.FILLED, filled_quantity=D("0.01")
    )
    row = await store.by_exchange_order_id("201", "sess")
    assert row is not None and row.status is LiveOrderStatus.FILLED
    assert await store.by_exchange_order_id("201", "other") is None  # F1: session-scoped

    assert [r.client_order_id for r in await store.open_orders("sess")] == ["hyd-rt-1"]
    assert await store.symbols("sess") == ["BTCUSDT", "ETHUSDT"]
    unsettled = await store.unsettled("sess", NOW + timedelta(seconds=1))
    assert unsettled == []


async def test_concurrent_apply_status_pairs_cannot_regress_a_terminal_row(pool: Any) -> None:
    """F7: one conditional UPDATE — a filled/canceled pair racing on one row can never
    leave the loser's status written over the winner's terminal one."""
    store = PgLiveOrderStore(pool)
    await store.create(_order("hyd-race"))
    assert await store.mark_ack("hyd-race", "401", LiveOrderStatus.NEW)

    outcomes = await asyncio.gather(
        store.apply_status("hyd-race", LiveOrderStatus.FILLED, filled_quantity=D("0.01")),
        store.apply_status("hyd-race", LiveOrderStatus.CANCELED),
    )
    assert all(outcome in (APPLIED, REFUSED, "unchanged") for outcome in outcomes)
    row = await store.by_client_order_id("hyd-race")
    assert row is not None
    assert row.status in (LiveOrderStatus.FILLED, LiveOrderStatus.CANCELED)
    assert row.terminal_at is not None
    # whichever won, the loser is now refused and cannot overwrite the terminal status
    loser = (
        LiveOrderStatus.CANCELED
        if row.status is LiveOrderStatus.FILLED
        else (LiveOrderStatus.FILLED)
    )
    assert await store.apply_status("hyd-race", loser) == REFUSED
    after = await store.by_client_order_id("hyd-race")
    assert after is not None and after.status is row.status


async def test_the_oco_discriminator_and_claim_columns_round_trip(pool: Any) -> None:
    """F12 + F2: order_list_id/leg_client_order_ids land, the order-id lookup never
    matches a list id, and live_fills rows claim unapplied then confirm."""
    store = PgLiveOrderStore(pool)
    await store.create(
        _order(
            "hyd-oco-rt",
            side="SELL",
            order_type="OCO",
            leg_client_order_ids=("hyd-above", "hyd-below"),
        )
    )
    assert await store.mark_ack("hyd-oco-rt", "777", LiveOrderStatus.NEW, order_list_id="777")
    row = await store.by_order_list_id("777", "sess")
    assert row is not None
    assert (row.order_list_id, row.leg_client_order_ids) == ("777", ("hyd-above", "hyd-below"))
    assert await store.by_exchange_order_id("777", "sess") is None  # F12

    from hydra.execution.live_orders import LiveFillRecord

    fill = LiveFillRecord(
        trade_id="900100",
        client_order_id="hyd-oco-rt",
        session_id="sess",
        exchange_id="binance",
        symbol="BTCUSDT",
        side="SELL",
        price=D("94000"),
        quantity=D("0.01"),
        fee=D("0.01"),
        fee_currency="USDT",
        liquidity="taker",
        timestamp=NOW,
    )
    assert await store.record_fill(fill)  # the claim
    assert [f.trade_id for f in await store.unapplied_fills("sess")] == ["900100"]
    assert await store.mark_fill_applied(fill)  # the confirm
    assert await store.unapplied_fills("sess") == []
    async with pool.acquire() as conn:
        stored = await conn.fetchval("SELECT applied FROM live_fills WHERE trade_id = '900100'")
    assert stored is True
    assert not await store.mark_fill_applied(fill)  # idempotent


async def test_live_fills_reject_a_duplicate_trade_id_at_the_db_level(pool: Any) -> None:
    from hydra.execution.live_orders import LiveFillRecord

    store = PgLiveOrderStore(pool)
    fill = LiveFillRecord(
        trade_id="900001",
        client_order_id="hyd-rt-1",
        session_id="sess",
        exchange_id="binance",
        symbol="BTCUSDT",
        side="BUY",
        price=D("100000"),
        quantity=D("0.01"),
        fee=D("0.00001"),
        fee_currency="BTC",
        liquidity="taker",
        timestamp=NOW,
    )
    assert await store.record_fill(fill)
    assert not await store.record_fill(fill)  # ON CONFLICT DO NOTHING: the replay is a no-op
    # a different symbol is a different trade under the same id
    assert await store.record_fill(
        LiveFillRecord(
            trade_id="900001",
            client_order_id="hyd-rt-2",
            session_id="sess",
            exchange_id="binance",
            symbol="ETHUSDT",
            side="BUY",
            price=D("1000"),
            quantity=D("0.01"),
            fee=D("0.00001"),
            fee_currency="ETH",
            liquidity="taker",
            timestamp=NOW,
        )
    )
    assert await store.last_trade_ts("sess", "BTCUSDT") == NOW
