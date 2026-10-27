"""The live order state machine: monotonic transitions, idempotent guards, the replay lock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.execution.live_orders import (
    APPLIED,
    TERMINAL_STATUSES,
    UNCHANGED,
    UNSETTLED_STATUSES,
    InMemoryLiveOrderStore,
    LiveOrderRecord,
    LiveOrderStatus,
    map_ccxt_status,
    new_client_order_id,
)

D = Decimal
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _order(**overrides: object) -> LiveOrderRecord:
    fields: dict[str, object] = {
        "client_order_id": "hyd-abc",
        "session_id": "s1",
        "strategy_id": "strat",
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": D("0.01"),
        "status": LiveOrderStatus.PENDING_SUBMIT,
        "submitted_at": NOW,
    }
    fields.update(overrides)
    return LiveOrderRecord(**fields)  # type: ignore[arg-type]


def test_client_order_ids_carry_the_hydra_prefix() -> None:
    cid = new_client_order_id()
    assert cid.startswith("hyd-")
    assert len(cid) == 36  # Binance's newClientOrderId limit


@pytest.mark.parametrize(
    ("raw", "filled", "expected"),
    [
        ("NEW", None, LiveOrderStatus.NEW),
        ("open", "0", LiveOrderStatus.NEW),
        ("open", "0.005", LiveOrderStatus.PARTIALLY_FILLED),
        ("PARTIALLY_FILLED", None, LiveOrderStatus.PARTIALLY_FILLED),
        ("FILLED", None, LiveOrderStatus.FILLED),
        ("closed", None, LiveOrderStatus.FILLED),
        ("CANCELED", None, LiveOrderStatus.CANCELED),
        ("EXPIRED", None, LiveOrderStatus.EXPIRED),
        ("REJECTED", None, LiveOrderStatus.REJECTED),
        ("", None, LiveOrderStatus.UNKNOWN),
        ("SOMETHING_ELSE", None, LiveOrderStatus.UNKNOWN),
    ],
)
def test_map_ccxt_status_covers_the_vocabulary(
    raw: str, filled: str | None, expected: LiveOrderStatus
) -> None:
    assert map_ccxt_status(raw, filled) == expected


async def test_outbox_first_and_ack_flow() -> None:
    store = InMemoryLiveOrderStore()
    await store.create(_order())
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None and row.status is LiveOrderStatus.PENDING_SUBMIT

    assert await store.mark_ack("hyd-abc", "201", LiveOrderStatus.FILLED)
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None
    assert (row.status, row.exchange_order_id) == (LiveOrderStatus.FILLED, "201")

    # an ack racing a resolved row is a refused no-op (R16), never an error
    assert not await store.mark_ack("hyd-abc", "202", LiveOrderStatus.NEW)
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None and row.exchange_order_id == "201"


async def test_terminal_status_is_never_left() -> None:
    store = InMemoryLiveOrderStore()
    await store.create(_order())
    await store.mark_ack("hyd-abc", "201", LiveOrderStatus.FILLED)
    assert await store.apply_status("hyd-abc", LiveOrderStatus.CANCELED) == "refused"
    assert await store.apply_status("hyd-abc", LiveOrderStatus.NEW) == "refused"
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None and row.status is LiveOrderStatus.FILLED


async def test_apply_status_is_monotonic_in_filled_quantity() -> None:
    store = InMemoryLiveOrderStore()
    await store.create(_order())
    await store.mark_ack("hyd-abc", "201", LiveOrderStatus.NEW)
    assert (
        await store.apply_status(
            "hyd-abc", LiveOrderStatus.PARTIALLY_FILLED, filled_quantity=D("0.005")
        )
        == APPLIED
    )
    # a same-status re-apply with the same quantity is the no-op 'unchanged'
    assert (
        await store.apply_status(
            "hyd-abc", LiveOrderStatus.PARTIALLY_FILLED, filled_quantity=D("0.005")
        )
        == UNCHANGED
    )
    # filled_quantity never decreases
    assert (
        await store.apply_status(
            "hyd-abc", LiveOrderStatus.PARTIALLY_FILLED, filled_quantity=D("0.002")
        )
        == "refused"
    )
    # a higher quantity at the same status is applied
    assert (
        await store.apply_status(
            "hyd-abc", LiveOrderStatus.PARTIALLY_FILLED, filled_quantity=D("0.01")
        )
        == APPLIED
    )
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None and row.filled_quantity == D("0.01")
    assert row.terminal_at is None


async def test_mark_submit_failed_is_idempotent_from_submitting_only() -> None:
    store = InMemoryLiveOrderStore()
    await store.create(_order())
    assert await store.mark_submit_failed("hyd-abc", "-1013 min notional")
    row = await store.by_client_order_id("hyd-abc")
    assert row is not None and (row.status, row.error) == (
        LiveOrderStatus.REJECTED,
        "-1013 min notional",
    )
    assert not await store.mark_submit_failed("hyd-abc", "again")
    assert not await store.mark_ack("hyd-abc", "201", LiveOrderStatus.NEW)


async def test_resolve_unknown_sets_the_exchange_id_from_any_unsettled_state() -> None:
    store = InMemoryLiveOrderStore()
    for status in sorted(UNSETTLED_STATUSES):
        cid = f"hyd-{status.value}"
        await store.create(_order(client_order_id=cid, status=status))
        assert await store.resolve_unknown(cid, "300", LiveOrderStatus.NEW)
        row = await store.by_client_order_id(cid)
        assert row is not None and row.exchange_order_id == "300"
    # a resolved row is refused a second resolve (the poller race)
    assert not await store.resolve_unknown("hyd-unknown", "301", LiveOrderStatus.NEW)
    # and a NEW row cannot be "resolved" at all
    await store.create(_order(client_order_id="hyd-new", status=LiveOrderStatus.NEW))
    assert not await store.resolve_unknown("hyd-new", "302", LiveOrderStatus.NEW)


async def test_record_fill_dedupes_on_the_replay_key() -> None:
    store = InMemoryLiveOrderStore()
    fill = _fill()
    assert await store.record_fill(fill)
    assert not await store.record_fill(_fill())
    # a different symbol or exchange id is a different trade
    assert await store.record_fill(_fill(symbol="ETHUSDT", trade_id="77"))
    assert await store.last_trade_ts("s1", "BTCUSDT") == fill.timestamp
    assert await store.last_trade_ts("s1", "ETHUSDT") is not None
    assert await store.last_trade_ts("s1", "SOLUSDT") is None


def _fill(*, trade_id: str = "77", symbol: str = "BTCUSDT", client_order_id: str = "hyd-abc"):
    from hydra.execution.live_orders import LiveFillRecord

    return LiveFillRecord(
        trade_id=trade_id,
        client_order_id=client_order_id,
        session_id="s1",
        exchange_id="binance",
        symbol=symbol,
        side="BUY",
        price=D("100000"),
        quantity=D("0.005"),
        fee=D("0.000005"),
        fee_currency="BTC",
        liquidity="taker",
        timestamp=NOW + timedelta(seconds=5),
    )


async def test_queries_split_by_status_and_symbol() -> None:
    store = InMemoryLiveOrderStore()
    await store.create(_order(client_order_id="hyd-1"))
    await store.create(
        _order(
            client_order_id="hyd-2",
            symbol="ETHUSDT",
            status=LiveOrderStatus.NEW,
            exchange_order_id="201",
        )
    )
    await store.mark_submit_failed("hyd-1", "nope")
    open_rows = await store.open_orders("s1")
    assert [r.client_order_id for r in open_rows] == ["hyd-2"]
    assert await store.symbols("s1") == ["BTCUSDT", "ETHUSDT"]
    refreshable = await store.refreshable("s1")
    assert [r.client_order_id for r in refreshable] == ["hyd-2"]
    unsettled = await store.unsettled("s1", NOW + timedelta(seconds=1))
    assert unsettled == []
    await store.create(
        _order(client_order_id="hyd-3", status=LiveOrderStatus.UNKNOWN, submitted_at=NOW)
    )
    unsettled = await store.unsettled("s1", NOW + timedelta(seconds=1))
    assert [r.client_order_id for r in unsettled] == ["hyd-3"]
    by_exchange = await store.by_exchange_order_id("301")
    assert by_exchange is None
    assert (await store.by_exchange_order_id("201")) is not None
    assert (await store.by_client_order_id("hyd-1")) is not None


def test_the_status_vocabulary_is_the_pinned_seam() -> None:
    assert {s.value for s in LiveOrderStatus} == {
        "pending_submit",
        "submitting",
        "new",
        "partially_filled",
        "filled",
        "canceled",
        "expired",
        "rejected",
        "unknown",
    }
    assert (
        frozenset(
            {
                LiveOrderStatus.FILLED,
                LiveOrderStatus.CANCELED,
                LiveOrderStatus.EXPIRED,
                LiveOrderStatus.REJECTED,
            }
        )
        == TERMINAL_STATUSES
    )
