"""The bybit spot protective battery: the pair ladder, the sibling-cancel on BOTH paths,
the rungs, the mode-independent guarantees, against the wire-contract fake.

Everything here runs against ``tests/fixtures/bybit_trading.py`` — the fake that owns the
bybit wire contract (written from the installed ccxt 4.5.88 source, pinned by
``test_real_ccxt_surface.py``) until the on-box testnet battery runs. The numbers are
asserted exactly: the SL trigger tick-rounded UP (it fires no later than asked), the TP
tick-rounded DOWN (marketable), the quantity floored to the lot step.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt
import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.types import MarketType, OrderType
from hydra.execution.errors import SubmitUnknownError
from hydra.execution.exchange_client import ExchangeClient
from hydra.execution.exchange_stops import StopBreachedError, StopUnplaceableError
from hydra.execution.live_executor import LiveOrderPoller, LiveTradingExecutor
from hydra.execution.live_orders import LiveOrderStatus
from hydra.execution.order_manager import OrderManager
from hydra.execution.protective_orders import ProtectionSpec
from hydra.execution.reconcile import reconcile_live_session
from hydra.execution.venue_stops import (
    arm_protective_stop,
    fetch_bybit_leg,
    normalize_bybit_leg,
    verify_bybit_pair,
)
from hydra.risk.market_rules import MarketRules
from tests.fixtures.bybit_trading import FakeBybitTradingClient

D = Decimal

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)
# bybit's registry trailing kind is NONE: the rules carry no trailing bounds at all
BYBIT_RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)

ENTRY = D("100000.005")
SL_ASKED = D("95000")
TP_ASKED = D("103000.00515")


def _client(fake: FakeBybitTradingClient) -> ExchangeClient:
    """The REAL ExchangeClient over the fake exchange: the bybit branches (the tpsl
    namespace fallback, the orderLinkId reads, the acknowledged param) run for real."""
    client = ExchangeClient("bybit", {}, market_type=MarketType.SPOT)
    client._exchange = fake
    return client


def _executor(client: ExchangeClient) -> LiveTradingExecutor:
    ex = LiveTradingExecutor(
        client,
        exchange_id="bybit",
        initial_balances={"USDT": D("10000")},
        db_pool=None,
        strategy_id="strat",
        session_id="sess",
        market_rules={"BTCUSDT": BYBIT_RULES},
        poll_interval=5.0,
        resolve_grace=0.0,
    )
    assert ex._pair_venue is True  # the registry's protective kind dispatch
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


async def _buy_filled(ex: LiveTradingExecutor, qty: str = "0.01000") -> None:
    """A market buy whose fill the poller applied: the position is in the ledger."""
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D(qty), params={"tag": "entry"})
    poller, _ = _poller(ex)
    await poller.sweep()


def _poller(ex: LiveTradingExecutor) -> tuple[LiveOrderPoller, OrderManager]:
    om = OrderManager(ex, InMemoryEventBus(), session_id=ex._session_id)
    return (
        LiveOrderPoller(
            ex, om, interval=0.05, resolve_grace=ex.resolve_grace, not_found_window=0.0
        ),
        om,
    )


async def _place_pair(ex: LiveTradingExecutor) -> dict[str, Any]:
    """The bybit protective pair: SL first, TP second, over the held long."""
    return await ex.create_order(
        "BTCUSDT",
        "SELL",
        OrderType.OCO.value,
        D("0.00999"),
        price=TP_ASKED,
        stop_price=SL_ASKED,
        params={"tag": "protective"},
    )


class _CancelCounting:
    """Wraps the fake so tests can assert EXACTLY ONE cancel reached the exchange."""

    def __init__(self, fake: FakeBybitTradingClient) -> None:
        self._fake = fake
        self.calls: list[tuple[str | None, str, dict[str, Any]]] = []

    async def cancel_order(
        self, order_id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((order_id, symbol, dict(params or {})))
        return await self._fake.cancel_order(order_id, symbol, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fake, name)


# ---------------------------------------------------------------------------
# The ladder: geometry, order, rungs, timeouts, the trailing fallback
# ---------------------------------------------------------------------------


async def test_the_pair_places_the_sl_leg_first_with_exact_wire_numbers() -> None:
    fake = FakeBybitTradingClient()
    created: list[dict[str, Any]] = []
    inner = fake.create_order

    async def hook(*args: Any, **kwargs: Any) -> dict[str, Any]:
        created.append(dict(kwargs["params"]))
        return await inner(*args, **kwargs)

    fake.create_order = hook
    placement = await arm_protective_stop(
        "bybit",
        _client(fake),
        RULES,
        "BTCUSDT",
        D("0.00999"),
        ProtectionSpec(stop_loss=SL_ASKED, take_profit=TP_ASKED),
        D("100000"),
        ENTRY,
        group_cid="hyd-group",
        pair_leg_cids=("hyd-sl", "hyd-tp"),
        fallback_cids=("hyd-fb0", "hyd-fb1"),
    )

    # the SL leg goes FIRST (protection before profit); each leg its own client id
    assert created[0] == {
        "clientOrderId": "hyd-sl",
        "stopLossPrice": "95000.00",  # tick-rounded UP: it fires no later than asked
    }
    assert created[1] == {
        "clientOrderId": "hyd-tp",
        "takeProfitPrice": "103000.00",  # tick-rounded DOWN: marketable
    }
    assert (
        placement.kind,
        placement.order_id,
        placement.quantity,
    ) == ("pair", "hyd-group", D("0.00999"))
    assert placement.stop_price == D("95000.00")
    assert placement.take_profit == D("103000.00")
    assert len(placement.leg_ids) == 2
    # both legs rest in the conditional namespace, the stop descending, the TP ascending
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    assert [leg["triggerDirection"] for leg in legs] == [2, 1]
    assert legs[0]["clientOrderId"] == "hyd-sl"


async def test_the_trailing_spec_goes_straight_to_the_fixed_stop() -> None:
    """The registry carries no percent trail for bybit: a trailing spec degrades to the
    fixed stop — no trailing param ever reaches the wire, and none is invented."""
    fake = FakeBybitTradingClient()
    placement = await arm_protective_stop(
        "bybit",
        _client(fake),
        BYBIT_RULES,
        "BTCUSDT",
        D("0.00999"),
        ProtectionSpec(trailing_pct=D("0.02")),
        D("100000"),
        ENTRY,
        group_cid="hyd-group",
        pair_leg_cids=(),
        fallback_cids=("hyd-fb0", "hyd-fb1"),
    )
    assert placement.kind == "stop"
    assert placement.stop_price == D("95000.01")  # entry*0.95 tick-rounded UP
    assert placement.trailing_pct is None
    [leg] = fake._open_raw("BTCUSDT", "tpslOrder")
    assert leg["clientOrderId"] == "hyd-fb0"
    assert leg["triggerPrice"] == "95000.01"


async def test_a_rejected_sl_leg_falls_to_the_sl_only_rung_then_the_default() -> None:
    """The rungs: the pair's SL leg is definitively rejected (the fake consumes the
    scripted failure on that one call), the SL-only rung at the strategy's stop lands."""
    fake = FakeBybitTradingClient()
    fake.fail_create = "invalid_order"

    placement = await arm_protective_stop(
        "bybit",
        _client(fake),
        RULES,
        "BTCUSDT",
        D("0.00999"),
        ProtectionSpec(stop_loss=SL_ASKED, take_profit=TP_ASKED),
        D("100000"),
        ENTRY,
        group_cid="hyd-group",
        pair_leg_cids=("hyd-sl", "hyd-tp"),
        fallback_cids=("hyd-fb0", "hyd-fb1"),
    )
    assert placement.kind == "stop"
    assert placement.stop_price == D("95000.00")
    [leg] = fake._open_raw("BTCUSDT", "tpslOrder")
    assert leg["clientOrderId"] == "hyd-fb0"


async def test_a_rejected_tp_leg_unwinds_the_resting_sl_and_falls_to_the_rung() -> None:
    """The TP leg rejected: the landed SL leg is cancelled (no stray stop survives to
    fire at the old entry's price), then the SL-only rung re-places protection."""
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    calls: list[dict[str, Any]] = []
    inner = fake.create_order

    async def hook(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs["params"]))
        if len(calls) == 2:
            raise ccxt.InvalidOrder("bybit {10001}s the order qty is invalid")
        return await inner(*args, **kwargs)

    fake.create_order = hook
    placement = await arm_protective_stop(
        "bybit",
        _client(counting),
        RULES,
        "BTCUSDT",
        D("0.00999"),
        ProtectionSpec(stop_loss=SL_ASKED, take_profit=TP_ASKED),
        D("100000"),
        ENTRY,
        group_cid="hyd-group",
        pair_leg_cids=("hyd-sl", "hyd-tp"),
        fallback_cids=("hyd-fb0", "hyd-fb1"),
    )
    assert placement.kind == "stop"
    # the landed SL leg was unwound by its client id, before the rung re-placed it
    [(order_id, _symbol, params)] = counting.calls
    assert order_id is None
    assert params == {"orderLinkId": "hyd-sl", "orderFilter": "tpslOrder"}
    [leg] = fake._open_raw("BTCUSDT", "tpslOrder")
    assert leg["clientOrderId"] == "hyd-fb0"


async def test_every_rung_definitively_rejected_is_stop_unplaceable() -> None:
    fake = FakeBybitTradingClient()
    calls: list[str] = []

    async def always_rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("x")
        raise ccxt.InvalidOrder("bybit {10001}s the order qty is invalid")

    fake.create_order = always_rejected
    with pytest.raises(StopUnplaceableError, match="every protective form rejected"):
        await arm_protective_stop(
            "bybit",
            _client(fake),
            RULES,
            "BTCUSDT",
            D("0.00999"),
            ProtectionSpec(stop_loss=SL_ASKED, take_profit=TP_ASKED),
            D("100000"),
            ENTRY,
            group_cid="hyd-group",
            pair_leg_cids=("hyd-sl", "hyd-tp"),
            fallback_cids=("hyd-fb0", "hyd-fb1"),
        )
    assert len(calls) == 3  # the pair's SL leg, then the two single-stop rungs


async def test_a_timeout_propagates_and_never_falls_through_the_ladder() -> None:
    fake = FakeBybitTradingClient()
    fake.fail_create = "timeout_not_landed"

    with pytest.raises(ccxt.RequestTimeout):
        await arm_protective_stop(
            "bybit",
            _client(fake),
            RULES,
            "BTCUSDT",
            D("0.00999"),
            ProtectionSpec(stop_loss=SL_ASKED, take_profit=TP_ASKED),
            D("100000"),
            ENTRY,
            group_cid="hyd-group",
            pair_leg_cids=("hyd-sl", "hyd-tp"),
            fallback_cids=("hyd-fb0", "hyd-fb1"),
        )
    assert fake._open_raw("BTCUSDT", "tpslOrder") == []  # nothing landed; no rung ran


async def test_a_stop_above_the_last_price_is_breached() -> None:
    fake = FakeBybitTradingClient()
    with pytest.raises(StopBreachedError, match="close at market"):
        await arm_protective_stop(
            "bybit",
            _client(fake),
            RULES,
            "BTCUSDT",
            D("0.00999"),
            ProtectionSpec(stop_loss=D("101000")),
            D("100000"),
            ENTRY,
            group_cid="hyd-group",
            pair_leg_cids=(),
            fallback_cids=("hyd-fb0", "hyd-fb1"),
        )


async def test_a_take_profit_that_would_not_rest_drops_the_leg() -> None:
    fake = FakeBybitTradingClient()
    placement = await arm_protective_stop(
        "bybit",
        _client(fake),
        RULES,
        "BTCUSDT",
        D("0.00999"),
        ProtectionSpec(stop_loss=SL_ASKED, take_profit=D("99000")),
        D("100000"),
        ENTRY,
        group_cid="hyd-group",
        pair_leg_cids=("hyd-sl", "hyd-tp"),
        fallback_cids=("hyd-fb0", "hyd-fb1"),
    )
    assert placement.kind == "stop"  # the stop alone: no take-profit leg went out
    [leg] = fake._open_raw("BTCUSDT", "tpslOrder")
    assert leg["clientOrderId"].startswith("hyd-fb0")


# ---------------------------------------------------------------------------
# The executor over the pair: arming, both sibling-cancel paths, the sweep
# ---------------------------------------------------------------------------


async def test_every_entry_fill_arms_the_pair_and_no_oco_call_is_made() -> None:
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    ex = _executor(_client(counting))
    await _buy_filled(ex)

    resp = await _place_pair(ex)

    assert resp["oco_link"] == resp["client_order_id"]
    assert len(resp["legs"]) == 2
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    assert (row.status, row.order_type) == (LiveOrderStatus.NEW, "OCO")
    # the pair's row identity is hydra's own group id (bybit has no list id)
    assert (row.exchange_order_id, row.order_list_id) == (resp["id"], resp["id"])
    assert fake._open_raw("BTCUSDT", "tpslOrder") and not fake._open_raw("BTCUSDT", "Order")
    # the SL leg is recorded first: the protection before the profit
    sl_cid, tp_cid = row.leg_client_order_ids[1], row.leg_client_order_ids[0]
    assert sl_cid.startswith("hyd-") and tp_cid.startswith("hyd-")
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    assert [leg["triggerDirection"] for leg in legs] == [2, 1]
    assert counting.calls == []  # the pair: no cancel ever fired


async def test_the_fill_driven_path_cancels_the_sibling_exactly_once() -> None:
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    ex = _executor(_client(counting))
    await _buy_filled(ex)
    await _place_pair(ex)
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    tp_leg = next(leg for leg in legs if leg["triggerDirection"] == 1)
    sl_leg = next(leg for leg in legs if leg["triggerDirection"] == 2)

    await fake.fill_order(tp_leg["orderId"], price="103000")
    fills = await ex.collect_exchange_fills(None)

    assert len(fills) == 1 and fills[0].side == "SELL"
    assert fills[0].price == D("103000")
    row = next(
        r
        for r in await ex.live_orders.orders(ex._session_id)
        if r.order_type == OrderType.OCO.value
    )
    assert row.status is LiveOrderStatus.FILLED
    # EXACTLY ONE cancel reached the exchange: the sibling, by its client id
    [(order_id, symbol, params)] = counting.calls
    assert order_id is None and symbol == "BTCUSDT"
    assert params == {"orderLinkId": row.leg_client_order_ids[1], "orderFilter": "tpslOrder"}
    assert fake.orders[sl_leg["orderId"]]["status"] == "Cancelled"
    assert not await ex.get_positions("BTCUSDT")


async def test_the_reconcile_driven_path_cancels_the_sibling() -> None:
    """A leg filled while no sweep ran: ONE reconcile pass proves it from the leg reads,
    cancels the sibling and answers ok — the both-paths rule."""
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    client = _client(counting)
    ex = _executor(client)
    await _buy_filled(ex)
    resp = await _place_pair(ex)
    poller, _ = _poller(ex)
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    tp_leg = next(leg for leg in legs if leg["triggerDirection"] == 1)
    sl_leg = next(leg for leg in legs if leg["triggerDirection"] == 2)

    await fake.fill_order(tp_leg["orderId"], price="103000")
    assert fake.orders[sl_leg["orderId"]]["status"] == "New"  # still resting

    status = await reconcile_live_session(ex.live_orders, client, ex, poller=poller)

    assert status.state == "ok", status.detail
    assert fake.orders[sl_leg["orderId"]]["status"] == "Cancelled"
    row = await ex.live_orders.by_order_list_id(resp["id"], ex._session_id)
    assert row is not None and row.status is LiveOrderStatus.FILLED
    assert not await ex.get_positions("BTCUSDT")


async def test_a_healthy_resting_pair_reconciles_ok_never_untracked() -> None:
    """F1's red/green: a correctly RESTING pair is tracked through its legs' own client
    ids — a healthy live session reconciles ``ok``, never a mismatch that names the
    resting legs "untracked exchange orders (not managed)" and pages forever."""
    fake = FakeBybitTradingClient()
    client = _client(fake)
    ex = _executor(client)
    await _buy_filled(ex)
    await _place_pair(ex)
    poller, _ = _poller(ex)
    assert len(fake._open_raw("BTCUSDT", "tpslOrder")) == 2  # both legs rest

    status = await reconcile_live_session(ex.live_orders, client, ex, poller=poller)

    assert status.state == "ok", status.detail


async def test_a_landed_single_stop_timeout_resolves_by_its_leg_cid() -> None:
    """F2's red/green: a single-stop row (a stop-only or trailing spec — a NON-OCO
    pair-venue row) whose create landed but timed out resolves by its leg cid. The row
    must never take a lying REJECTED "not found on exchange" while the stop rests there."""
    fake = FakeBybitTradingClient()
    client = _client(fake)
    ex = _executor(client)
    await _buy_filled(ex)
    inner = fake.create_order

    async def land_then_timeout(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await inner(*args, **kwargs)
        raise ccxt.RequestTimeout("bybit POST /v5/order/create request timed out")

    fake.create_order = land_then_timeout
    with pytest.raises(SubmitUnknownError):
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            OrderType.STOP_MARKET.value,
            D("0.00999"),
            stop_price=SL_ASKED,
            params={"tag": "protective"},
        )
    [rung] = fake._open_raw("BTCUSDT", "tpslOrder")  # the stop LANDED and rests
    poller, _ = _poller(ex)
    for _ in range(3):  # the three sweeps that must not resolve to a rejection
        await poller.sweep()

    rows = [
        r
        for r in await ex.live_orders.orders(ex._session_id)
        if r.order_type == OrderType.STOP_MARKET.value
    ]
    assert [row.status for row in rows] == [LiveOrderStatus.NEW]
    assert rows[0].exchange_order_id == rung["orderId"]
    assert fake.orders[rung["orderId"]]["status"] == "New"  # it still protects


async def test_the_refresh_path_cancels_the_part_filled_sibling_exactly_once() -> None:
    """F5: the refresh half of the both-paths sibling-cancel, driven directly — a TP leg
    filled and a sibling that left the resting list part-filled while no sweep applied the
    fill. The leg reads (the F3 seam) prove the ending, the row takes FILLED, and EXACTLY
    ONE cancel reaches the exchange: the part-filled sibling, by its client id."""
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    client = _client(counting)
    ex = _executor(client)
    await _buy_filled(ex)
    await _place_pair(ex)
    poller, _ = _poller(ex)
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    tp_leg = next(leg for leg in legs if leg["triggerDirection"] == 1)
    sl_leg = next(leg for leg in legs if leg["triggerDirection"] == 2)

    await fake.fill_order(tp_leg["orderId"], price="103000")
    await fake.fill_order(sl_leg["orderId"], price="95000", quantity="0.004")
    assert fake._open_raw("BTCUSDT", "tpslOrder") == []  # neither rests anymore

    await poller._refresh_open_statuses()

    row = next(
        r
        for r in await ex.live_orders.orders(ex._session_id)
        if r.order_type == OrderType.OCO.value
    )
    assert row.status is LiveOrderStatus.FILLED
    assert row.filled_quantity == D("0.00999")  # the proven leg's fill, not the sibling's
    [(order_id, symbol, params)] = counting.calls
    assert order_id is None and symbol == "BTCUSDT"
    assert params == {"orderLinkId": sl_leg["clientOrderId"], "orderFilter": "tpslOrder"}
    assert fake.orders[sl_leg["orderId"]]["status"] == "Cancelled"
    assert fake.orders[tp_leg["orderId"]]["status"] == "Filled"  # never a second cancel


async def test_the_kill_switch_sweep_cancels_both_namespaces() -> None:
    fake = FakeBybitTradingClient()
    ex = _executor(_client(fake))
    await _buy_filled(ex)
    await _place_pair(ex)
    resting_limit = await ex.create_order(
        "BTCUSDT", "SELL", "LIMIT", D("0.0001"), price=D("120000")
    )

    await ex.cancel_all_orders("BTCUSDT")

    # bybit's conditional legs rest behind the tpslOrder filter: the sweep must hit
    # BOTH namespaces (the plain sweep alone would leave the pair resting)
    assert fake.cancel_all_calls == [
        ("BTCUSDT", "Order"),
        ("BTCUSDT", "tpslOrder"),
    ]
    assert fake._open_raw("BTCUSDT", "Order") == []
    assert fake._open_raw("BTCUSDT", "tpslOrder") == []
    row = await ex.live_orders.by_client_order_id(resting_limit["client_order_id"])
    assert row is not None


async def test_cancelling_the_pair_row_cancels_both_legs_by_their_client_ids() -> None:
    fake = FakeBybitTradingClient()
    counting = _CancelCounting(fake)
    ex = _executor(_client(counting))
    await _buy_filled(ex)
    resp = await _place_pair(ex)

    out = await ex.cancel_order(resp["id"], "BTCUSDT")

    assert out == {"id": resp["id"], "status": "CANCELLED"}
    assert fake._open_raw("BTCUSDT", "tpslOrder") == []
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None and row.status is LiveOrderStatus.CANCELED


async def test_an_unsettle_pair_row_resolves_by_its_legs_never_a_list_id() -> None:
    fake = FakeBybitTradingClient()
    ex = _executor(_client(fake))
    await _buy_filled(ex)
    resp = await _place_pair(ex)
    poller, _ = _poller(ex)
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    await ex.live_orders.apply_status(
        row.client_order_id, LiveOrderStatus.UNKNOWN, error="submit outcome unknown"
    )

    await poller.sweep()

    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None and row.status is LiveOrderStatus.NEW
    assert row.exchange_order_id  # a resting leg's exchange id, never a list id
    # a plain order whose bybit create answered the real stub (status None) resolves
    # through its own orderLinkId read
    bought = await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.001"), params={"tag": "x"})
    plain = await ex.live_orders.by_client_order_id(bought["client_order_id"])
    assert plain is not None and plain.status is LiveOrderStatus.UNKNOWN
    await ex.live_orders.apply_status(
        plain.client_order_id, LiveOrderStatus.UNKNOWN, error="submit outcome unknown"
    )
    await poller.sweep()
    plain = await ex.live_orders.by_client_order_id(bought["client_order_id"])
    assert plain is not None and plain.status is LiveOrderStatus.FILLED


# ---------------------------------------------------------------------------
# Normalize and verify: the A8-live read on the bybit wire
# ---------------------------------------------------------------------------


async def test_a_resting_pair_verifies_confirmed_by_its_leg_ids() -> None:
    fake = FakeBybitTradingClient()
    client = _client(fake)
    ex = _executor(client)
    await _buy_filled(ex)
    resp = await _place_pair(ex)
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None

    class _Group:
        order_id = resp["id"]
        leg_ids = (row.exchange_order_id, *row.leg_client_order_ids)

    state = await verify_bybit_pair(client, "BTCUSDT", _Group())
    assert state.state == "confirmed"
    assert state.kind == "oco"
    assert state.stop_price == 95000.00
    assert state.take_profit == 103000.00
    # with no expectation, any resting reduce-only leg of hydra's own counts
    bare = await verify_bybit_pair(client, "BTCUSDT", None)
    assert bare.state == "confirmed"


async def test_an_unreadable_exchange_answers_unknown_never_missing() -> None:
    fake = FakeBybitTradingClient()

    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise ccxt.NetworkError("dns")

    fake.fetch_open_orders = boom
    state = await verify_bybit_pair(_client(fake), "BTCUSDT", None)
    assert (state.state, state.error) == ("unknown", "dns")


async def test_a_stopped_pair_answers_missing() -> None:
    fake = FakeBybitTradingClient()
    ex = _executor(_client(fake))
    await _buy_filled(ex)

    state = await verify_bybit_pair(_client(fake), "BTCUSDT", None)
    assert state.state == "missing"


async def test_normalize_reads_the_stop_and_take_profit_triggers_exactly() -> None:
    fake = FakeBybitTradingClient()
    ex = _executor(_client(fake))
    await _buy_filled(ex)
    await _place_pair(ex)
    legs = sorted(
        (
            normalize_bybit_leg(o)
            for o in await fake.fetch_open_orders("BTCUSDT", params={"orderFilter": "tpslOrder"})
        ),
        key=lambda leg: leg["reason"],
    )
    stop, take = legs
    assert (stop["reason"], stop["type"]) == ("stop_loss", OrderType.STOP_MARKET)
    assert stop["stop_price"] == D("95000.00")
    assert (take["reason"], take["type"]) == ("take_profit", OrderType.TAKE_PROFIT_MARKET)
    # the perp convention: a take-profit carries its trigger in stop_price
    assert take["stop_price"] == D("103000.00")
    assert take["oco_id"] is None
    assert take["client_order_id"].startswith("hyd-")
    assert take["reduce_only"] is True


async def test_fetch_bybit_leg_reads_by_client_id_through_the_tpsl_namespace() -> None:
    fake = FakeBybitTradingClient()
    client = _client(fake)
    ex = _executor(client)
    await _buy_filled(ex)
    await _place_pair(ex)
    legs = fake._open_raw("BTCUSDT", "tpslOrder")
    sl_cid = next(leg["clientOrderId"] for leg in legs if leg["triggerDirection"] == 2)

    order = await fetch_bybit_leg(client, sl_cid, "BTCUSDT")
    assert order["id"] == next(leg["orderId"] for leg in legs if leg["clientOrderId"] == sl_cid)
    assert order["reduceOnly"] is True
    # a plain regular order reads through the Order namespace fallback
    await ex.create_order("BTCUSDT", "SELL", "LIMIT", D("0.0001"), price=D("120000"))
    plain = fake._open_raw("BTCUSDT", "Order")[0]
    read = await fetch_bybit_leg(client, plain["orderId"], "BTCUSDT")
    assert read["id"] == plain["orderId"]
