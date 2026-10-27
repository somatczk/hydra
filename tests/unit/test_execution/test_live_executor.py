"""The live executor against the fake exchange: outbox-first, no simulation, real fills.

Every number below is asserted exactly at the ledger seam: the wallet-truth model (a base
commission nets the credited base and stays inside the entry basis; a quote commission is
charged to the quote; BNB converts at the fill price), the weighted average on a two-part
fill, and the replay lock (the same myTrades page applied once).

The review round's money invariants (per R28): F1 — two sessions on one account book only
their own fills; F2 — claim-apply-confirm retries a transiently failed apply; F5 — LIMIT
sells floor to the lot step; F8 — a single -2013 never terminalizes and the myTrades
cross-check resolves a filled submit; F9 — an OCO resolves through its list, never the
order endpoint's word for a list id; F11 — cancelling a pending submit answers ambiguous.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.types import OHLCV
from hydra.execution.errors import ExchangeRateLimitedError, SubmitUnknownError
from hydra.execution.live_executor import (
    _NOT_FOUND_REQUIRED,
    LiveOrderPoller,
    LiveTradingExecutor,
)
from hydra.execution.live_orders import InMemoryLiveOrderStore, LiveOrderStatus
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import InsufficientBalanceError, OrderRejectedError
from hydra.risk.market_rules import MarketRules
from tests.fixtures.binance_trading import FakeBinanceTradingClient

D = Decimal
H = timedelta(hours=1)
BASE_TS = datetime(2026, 10, 27, tzinfo=UTC)

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
    trailing_delta_bips=(D("10"), D("2000")),
)


def _executor(
    fake: FakeBinanceTradingClient,
    *,
    initial: str = "10000",
    session: str = "sess",
) -> LiveTradingExecutor:
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": D(initial)},
        db_pool=None,
        strategy_id="strat",
        session_id=session,
        market_rules={"BTCUSDT": RULES},
        poll_interval=5.0,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


def _poller(ex: LiveTradingExecutor) -> tuple[LiveOrderPoller, OrderManager]:
    om = OrderManager(ex, InMemoryEventBus(), session_id=ex._session_id)
    return (
        LiveOrderPoller(
            ex, om, interval=0.05, resolve_grace=ex.resolve_grace, not_found_window=0.0
        ),
        om,
    )


def _bar(close: str, ts: datetime) -> OHLCV:
    c = D(close)
    return OHLCV(open=c, high=c, low=c, close=c, volume=D("1"), timestamp=ts)


async def _buy_filled(ex: LiveTradingExecutor, qty: str = "0.01000") -> None:
    """A market buy whose fill the poller applied: the position is in the ledger."""
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D(qty), params={"tag": "entry"})
    poller, _ = _poller(ex)
    await poller.sweep()


# ---------------------------------------------------------------------------
# (1)-(2) outbox-first, ack, no ledger mutation
# ---------------------------------------------------------------------------


async def test_the_outbox_row_exists_before_the_exchange_call() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    seen: list[str] = []
    original = fake.create_order

    async def hook(*args: Any, **kwargs: Any) -> dict[str, Any]:
        rows = await ex.live_orders.open_orders(ex._session_id)
        seen.extend(r.status.value for r in rows)
        return await original(*args, **kwargs)

    fake.create_order = hook
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    assert seen == ["pending_submit"]


async def test_the_ack_sets_the_exchange_id_and_never_touches_the_ledger() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    before = ex.get_account_snapshot()

    resp = await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})

    assert resp["status"] == "PENDING" and resp["filled"] is False
    assert resp["client_order_id"].startswith("hyd-")
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    assert (row.status, row.exchange_order_id) == (LiveOrderStatus.FILLED, resp["id"])
    after = ex.get_account_snapshot()
    assert (after.quote_free, after.base_totals) == (before.quote_free, before.base_totals)
    # the exchange really filled it; the ledger waits for the poller
    assert fake.orders[int(resp["id"])]["status"] == "FILLED"


# ---------------------------------------------------------------------------
# (3) the submit error taxonomy
# ---------------------------------------------------------------------------


async def test_insufficient_funds_maps_to_the_paper_error_and_rejects_the_row() -> None:
    fake = FakeBinanceTradingClient(balances={"USDT": D("500")})
    ex = _executor(fake)
    with pytest.raises(InsufficientBalanceError):
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    rows = await ex.live_orders.orders(ex._session_id)
    assert len(rows) == 1 and rows[0].status is LiveOrderStatus.REJECTED


async def test_local_market_rules_pre_check_rejects_before_any_call() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    with pytest.raises(OrderRejectedError, match="below the minimum"):
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.00001"))
    assert await ex.live_orders.orders(ex._session_id) == []
    assert fake.orders == {}


async def test_rate_limit_maps_to_the_typed_error_and_rejects_the_row() -> None:
    fake = FakeBinanceTradingClient()
    fake.fail_create = "rate_limit"
    ex = _executor(fake)
    with pytest.raises(ExchangeRateLimitedError):
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))
    rows = await ex.live_orders.orders(ex._session_id)
    assert len(rows) == 1 and rows[0].status is LiveOrderStatus.REJECTED


# ---------------------------------------------------------------------------
# (4) the critical mid-submit cases
# ---------------------------------------------------------------------------


async def test_timeout_after_land_stays_unknown_until_the_poller_resolves_it() -> None:
    fake = FakeBinanceTradingClient()
    fake.fail_create = "timeout_after_land"
    ex = _executor(fake)

    with pytest.raises(SubmitUnknownError) as raised:
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    cid = raised.value.client_order_id

    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None and row.status is LiveOrderStatus.UNKNOWN
    assert ex.get_account_snapshot().base_totals == {}

    poller, _om = _poller(ex)
    await poller.sweep()

    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None
    assert (row.status, row.exchange_order_id) == (LiveOrderStatus.FILLED, "100")
    assert len(fake.orders) == 1  # exactly one order exists; nothing resubmitted
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.00999")}


async def test_timeout_not_landed_resolves_to_rejected_after_the_grace_window() -> None:
    fake = FakeBinanceTradingClient()
    fake.fail_create = "timeout_not_landed"
    ex = _executor(fake)

    with pytest.raises(SubmitUnknownError) as raised:
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))
    cid = raised.value.client_order_id

    poller, _ = _poller(ex)
    await poller.sweep()
    # F8: a single -2013 never terminalizes — the row is still unresolved
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None and row.status is LiveOrderStatus.UNKNOWN

    for _ in range(_NOT_FOUND_REQUIRED):
        await poller.sweep()
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None and row.status is LiveOrderStatus.REJECTED
    assert "not found on exchange" in (row.error or "")
    assert fake.orders == {}
    assert ex.get_account_snapshot().base_totals == {}


async def test_a_fresh_unknown_row_waits_for_the_grace_window() -> None:
    fake = FakeBinanceTradingClient()
    fake.fail_create = "timeout_not_landed"
    ex = _executor(fake, session="grace")
    ex.resolve_grace = 600.0
    with pytest.raises(SubmitUnknownError):
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))
    poller, _ = _poller(ex)
    await poller.sweep()  # inside the grace window: no resolution attempt
    rows = await ex.live_orders.unsettled(ex._session_id, datetime.now(UTC))
    assert len(rows) == 1 and rows[0].status is LiveOrderStatus.UNKNOWN
    assert fake.orders == {}


async def test_a_late_landing_order_resolves_and_books_after_not_found_answers() -> None:
    """F8's red case: the create lands LATE (exchange incident) after -2013s began; the
    row must never have been rejected, so the fill still books."""
    fake = FakeBinanceTradingClient()
    fake.fail_create = "timeout_not_landed"
    ex = _executor(fake)
    with pytest.raises(SubmitUnknownError) as raised:
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    cid = raised.value.client_order_id
    poller, _om = _poller(ex)

    await poller.sweep()  # one -2013: still unknown, never rejected
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None and row.status is LiveOrderStatus.UNKNOWN

    # Binance finishes processing the create now: the order lands and fills
    raw = fake._new_order("BTCUSDT", "BUY", "MARKET", D("0.01"), None, None, cid)
    fake._apply_fill(raw, fake.last_prices["BTCUSDT"], D("0.01"))

    await poller.sweep()
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None
    assert (row.status, row.exchange_order_id) == (LiveOrderStatus.FILLED, str(raw["orderId"]))
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.00999")}
    assert len(ex.get_fills()) == 1


async def test_the_trades_cross_check_resolves_a_filled_submit_the_order_lookup_misses() -> None:
    """F8: at rejection eligibility, a trade for the client id resolves as filled even
    when the client-id order lookup keeps answering -2013."""
    import ccxt

    fake = FakeBinanceTradingClient()
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0")
    ex = _executor(fake)
    poller, _om = _poller(ex)

    fake.fail_create = "timeout_not_landed"
    with pytest.raises(SubmitUnknownError) as raised:
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    cid = raised.value.client_order_id

    # the create actually landed and filled while the answer was lost
    raw = fake._new_order("BTCUSDT", "BUY", "MARKET", D("0.01"), None, None, cid)
    fake._apply_fill(raw, fake.last_prices["BTCUSDT"], D("0.01"))
    fake.trades[-1]["time"] = int(datetime.now(UTC).timestamp() * 1000)

    # the client-id lookup answers -2013; the by-order-id read the cross-check uses works
    real_fetch = fake.fetch_order

    async def flaky_by_client(
        order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if params and params.get("clientOrderId"):
            raise ccxt.OrderNotFound("binance {-2013} Order does not exist")
        return await real_fetch(order_id, symbol, params)

    fake.fetch_order = flaky_by_client
    for _ in range(_NOT_FOUND_REQUIRED):
        await poller.sweep()
    fake.fetch_order = real_fetch

    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None
    assert row.status is LiveOrderStatus.FILLED
    assert row.exchange_order_id == str(raw["orderId"])
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.01")}
    assert len(ex.get_fills()) == 1


# ---------------------------------------------------------------------------
# (5) fills at the real price and commission
# ---------------------------------------------------------------------------


async def test_real_prices_and_commissions_are_booked_exactly() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, initial="2000")

    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01000"), params={"tag": "entry"})
    poller, _om = _poller(ex)
    await poller.sweep()
    snap = ex.get_account_snapshot()
    # commission 0.00001 BTC was taken in base: the quote debit is exactly the notional
    assert snap.base_totals == {"BTC": D("0.00999")}
    assert snap.quote_free == D("1000")  # the 2000 seed minus the exact 1000.00 debit
    position = (await ex.get_positions("BTCUSDT"))[0]
    assert position.avg_entry_price == D("1000.00") / D("0.00999")

    # the sell fills at 101000 with an explicit 0.010 USDT commission
    fake.set_mark("BTCUSDT", "101000")
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0.01")
    await ex.create_order(
        "BTCUSDT", "SELL", "MARKET", D("0.00999"), params={"tag": "exit", "reduce_only": True}
    )
    await poller.sweep()

    snap = ex.get_account_snapshot()
    assert snap.quote_free == D("2008.98")  # 1000 seed-1000 debit + 1008.98 net proceeds
    assert snap.realized_net == D("8.98")
    assert snap.fees_paid == D("1.01")  # 1.00 quote value of the base fee + 0.01
    assert not any(q > 0 for q in snap.base_totals.values())
    fills = ex.get_fills()
    assert [(f.side, f.reason, f.net_pnl) for f in fills] == [
        ("BUY", "entry", None),
        ("SELL", "exit", D("8.98")),
    ]


async def test_bnb_commission_converts_at_the_fill_price() -> None:
    fake = FakeBinanceTradingClient()
    fake.commission_asset = "BNB"
    fake.balances["BNB"] = D("10")
    ex = _executor(fake, initial="2000")
    fake.commission_amount = D("0.0005")  # converts to 50 USDT at the 100000 fill price

    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    poller, _ = _poller(ex)
    await poller.sweep()

    snap = ex.get_account_snapshot()
    # the documented approximation: the BNB fee converts at the fill price into the quote
    assert snap.quote_free == D("2000") - D("1000") - D("50")
    assert snap.base_totals == {"BTC": D("0.01")}
    assert snap.fees_paid == D("50")
    assert (await ex.get_positions("BTCUSDT"))[0].avg_entry_price == D("100000")


# ---------------------------------------------------------------------------
# (6) partial fills accumulate to the weighted average
# ---------------------------------------------------------------------------


async def test_partial_fills_accumulate_to_the_weighted_average_entry() -> None:
    fake = FakeBinanceTradingClient()
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0")
    ex = _executor(fake, initial="2000")

    resp = await ex.create_order(
        "BTCUSDT", "BUY", "LIMIT", D("0.010"), price=D("100000"), params={"tag": "entry"}
    )
    order_id = int(resp["id"])
    fake.set_mark("BTCUSDT", "100000")
    await fake.fill_order(order_id, price="100000", quantity="0.005")
    fake.set_mark("BTCUSDT", "101000")
    await fake.fill_order(order_id, price="101000", quantity="0.005")

    poller, _ = _poller(ex)
    await poller.sweep()

    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    assert (row.status, row.filled_quantity) == (LiveOrderStatus.FILLED, D("0.010"))
    position = (await ex.get_positions("BTCUSDT"))[0]
    assert position.avg_entry_price == D("100500.00")
    assert position.quantity == D("0.010")


# ---------------------------------------------------------------------------
# (7) replay and the known-orders-only filter
# ---------------------------------------------------------------------------


async def test_poll_replay_applies_nothing_twice() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    poller, _om = _poller(ex)
    await poller.sweep()
    after_first = ex.get_account_snapshot()

    await poller.sweep()
    await poller.sweep()

    again = ex.get_account_snapshot()
    assert (again.quote_free, again.base_totals) == (
        after_first.quote_free,
        after_first.base_totals,
    )
    assert len(ex.get_fills()) == 1


async def test_the_poller_never_takes_the_users_manual_order() -> None:
    """R5: a trade whose order id is not a known session order never enters the ledger."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake, initial="1000")

    user = await fake.create_order(
        "BTCUSDT", "BUY", "market", D("0.005"), params={"newClientOrderId": "user-own-1"}
    )
    assert user["id"] != ""
    poller, _ = _poller(ex)
    await poller.sweep()

    assert ex.get_account_snapshot().base_totals == {}
    assert ex.get_fills() == []
    assert await ex.live_orders.orders(ex._session_id) == []


# ---------------------------------------------------------------------------
# R17: closed bars do the bookkeeping, never the simulation
# ---------------------------------------------------------------------------


async def test_closed_bar_updates_marks_and_peak_without_simulating() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)

    await ex.on_closed_bar("BTCUSDT", _bar("102000", BASE_TS))
    snap = ex.get_account_snapshot()
    assert snap.peak_equity == D("10018.98")  # 9000 quote + 0.00999 BTC at 102000
    assert snap.drawdown == D("0")

    await ex.on_closed_bar("BTCUSDT", _bar("90000", BASE_TS + H))
    snap = ex.get_account_snapshot()
    assert snap.peak_equity == D("10018.98")
    assert snap.drawdown == (D("10018.98") - D("9899.10")) / D("10018.98")
    # balances only ever move through real fills
    assert snap.quote_free == D("9000")
    assert (await ex.on_closed_bar("BTCUSDT", _bar("90000", BASE_TS + 2 * H))) == []


# ---------------------------------------------------------------------------
# the protective ladder, cancel, flatten and the sweep
# ---------------------------------------------------------------------------


async def test_protective_stop_markets_delegate_to_the_exchange_ladder() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)

    resp = await ex.create_order(
        "BTCUSDT",
        "SELL",
        "STOP_MARKET",
        D("0.00999"),
        stop_price=D("95000"),
        params={"tag": "protective"},
    )
    assert resp["id"].isdigit()
    assert [leg["id"] for leg in resp["legs"]] != []
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    assert (row.status, row.tag, row.stop_price) == (LiveOrderStatus.NEW, "protective", D("95000"))
    raw = fake.orders[int(resp["id"])]
    assert raw["type"] == "STOP_LOSS" and raw["stopPrice"] == "95000.00"


async def test_cancel_is_real_and_a_terminal_row_is_a_no_op() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    resp = await ex.create_order(
        "BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000")
    )

    out = await ex.cancel_order(resp["id"], "BTCUSDT")
    assert out == {"id": resp["id"], "status": "CANCELLED"}
    assert fake.orders[int(resp["id"])]["status"] == "CANCELED"
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None and row.status is LiveOrderStatus.CANCELED
    # already terminal: a second cancel answers success without an exchange call
    assert await ex.cancel_order(resp["id"], "BTCUSDT") == {"id": resp["id"], "status": "CANCELLED"}


async def test_cancel_all_orders_delegates_to_the_keyed_client() -> None:
    """R4: the kill-switch sweep's duck-typed gate lands on the client call."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    await ex.create_order("BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000"))

    await ex.cancel_all_orders("BTCUSDT")

    assert fake.cancel_all_calls == ["BTCUSDT"]
    assert fake.open_orders_raw() == []
    assert all(o["status"] in ("CANCELED", "FILLED") for o in fake.orders.values())


async def test_fetch_open_orders_serves_the_tracked_rows_in_the_paper_shape() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    resp = await ex.create_order(
        "BTCUSDT", "SELL", "OCO", D("0.00999"), price=D("103000"), stop_price=D("95000")
    )

    rows = await ex.fetch_open_orders("BTCUSDT")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == resp["id"]
    assert (row["side"], row["type"], row["reason"]) == ("SELL", "OCO", "stop_loss")
    assert row["oco_id"] == resp["id"]
    assert row["price"] == 103000.0
    assert row["stop_price"] == 95000.0
    assert row["client_order_id"].startswith("hyd-")
    assert row["status"] == "new"


async def test_flatten_sells_for_real_and_drains_the_landed_fills() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    await ex.create_order("BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000"))

    fills = await ex.flatten(reason="kill switch")

    assert len(fills) == 1 and fills[0].side == "SELL"
    snap = ex.get_account_snapshot()
    assert not any(q > 0 for q in snap.base_totals.values())
    sells = [o for o in fake.orders.values() if o["side"] == "SELL"]
    assert {o["type"] for o in sells} == {"STOP_LOSS", "MARKET"}  # cancelled stop + real sell
    assert all(o["status"] == "CANCELED" for o in sells if o["type"] == "STOP_LOSS")
    assert any(o["type"] == "MARKET" and o["status"] == "FILLED" for o in sells)


# ---------------------------------------------------------------------------
# F9/S1: the real OCO wire contract and its list-aware resolution
# ---------------------------------------------------------------------------


async def test_the_oco_wire_carries_the_list_and_leg_client_ids() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    seen: dict[str, Any] = {}
    original = fake.create_oco

    async def hook(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(args[5] or {})
        return await original(*args, **kwargs)

    fake.create_oco = hook
    resp = await ex.create_order(
        "BTCUSDT", "SELL", "OCO", D("0.00999"), price=D("103000"), stop_price=D("95000")
    )
    row = await ex.live_orders.by_client_order_id(resp["client_order_id"])
    assert row is not None
    assert seen["listClientOrderId"] == row.client_order_id
    assert seen["aboveClientOrderId"] == row.leg_client_order_ids[0]
    assert seen["belowClientOrderId"] == row.leg_client_order_ids[1]
    assert len(set(seen.values())) == 3  # three distinct 36-char hyd- ids
    assert all(len(str(cid)) <= 36 for cid in seen.values())
    # the list id and the order ids are distinct spaces; fetch_order never resolves a list
    assert (row.exchange_order_id, row.order_list_id) == (resp["id"], resp["id"])
    import ccxt

    with pytest.raises(ccxt.OrderNotFound):
        await fake.fetch_order(resp["id"], "BTCUSDT")


async def test_a_timed_out_oco_that_landed_resolves_by_its_list_and_books_its_fills() -> None:
    """F9's red case: the OCO placement timed out after landing; the order endpoint
    cannot speak for a list id, so the row resolves through the open-list lookup."""
    fake = FakeBinanceTradingClient()
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0")
    ex = _executor(fake)
    poller, _om = _poller(ex)
    await _buy_filled(ex)

    fake.fail_create = "timeout_after_land"
    with pytest.raises(SubmitUnknownError) as raised:
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "OCO",
            D("0.01"),
            price=D("103000"),
            stop_price=D("95000"),
            params={"tag": "protective"},
        )
    cid = raised.value.client_order_id
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None and row.status is LiveOrderStatus.UNKNOWN
    list_id = next(iter(fake.lists))
    assert all(fake.orders[leg]["status"] == "NEW" for leg in fake.lists[list_id])

    await poller.sweep()
    row = await ex.live_orders.by_client_order_id(cid)
    assert row is not None
    assert (row.status, row.exchange_order_id, row.order_list_id) == (
        LiveOrderStatus.NEW,
        str(list_id),
        str(list_id),
    )

    # the stop leg fires while the row was unknown: the fill must still book
    leg_below = fake.lists[list_id][1]
    fake.set_mark("BTCUSDT", "94000")
    fake._apply_fill(fake.orders[leg_below], D("94000"), D("0.01"))
    await poller.sweep()

    snap = ex.get_account_snapshot()
    assert not any(q > 0 for q in snap.base_totals.values())
    assert [f.side for f in ex.get_fills()] == ["BUY", "SELL"]


# ---------------------------------------------------------------------------
# F1: two live sessions on one account — each books only its own fills
# ---------------------------------------------------------------------------


async def test_two_sessions_on_one_account_book_only_their_own_fills() -> None:
    """F1's red case with the shared store (what one PgLiveOrderStore table really is):
    session B's poller must not claim session A's trade — the account-wide live_fills
    unique key would then block A forever."""
    fake = FakeBinanceTradingClient()
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0")
    shared = InMemoryLiveOrderStore()  # one live_orders/live_fills table
    a = _executor(fake, session="session-a")
    b = _executor(fake, session="session-b")
    a._live_orders = shared
    b._live_orders = shared
    om_a = OrderManager(a, InMemoryEventBus(), session_id="session-a")
    om_b = OrderManager(b, InMemoryEventBus(), session_id="session-b")

    await a.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    await b.create_order("BTCUSDT", "BUY", "MARKET", D("0.005"), params={"tag": "entry"})

    pa = LiveOrderPoller(a, om_a, interval=0.05, resolve_grace=0.0, not_found_window=0.0)
    pb = LiveOrderPoller(b, om_b, interval=0.05, resolve_grace=0.0, not_found_window=0.0)
    await pb.sweep()  # B's poller wins the round: it sees both account trades
    await pa.sweep()  # A's poller sees them too

    assert a.get_account_snapshot().base_totals == {"BTC": D("0.01")}
    assert b.get_account_snapshot().base_totals == {"BTC": D("0.005")}
    assert [(f.side, f.quantity) for f in a.get_fills()] == [("BUY", D("0.01"))]
    assert [(f.side, f.quantity) for f in b.get_fills()] == [("BUY", D("0.005"))]
    recorded = sorted((fill.session_id, fill.quantity) for fill in shared._fills.values())
    assert recorded == [("session-a", D("0.01")), ("session-b", D("0.005"))]


# ---------------------------------------------------------------------------
# F2: claim-apply-confirm — a transient apply failure is retried, never orphaned
# ---------------------------------------------------------------------------


async def test_a_transient_apply_failure_books_the_fill_on_the_next_sweep() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    poller, _om = _poller(ex)

    real = ex.apply_exchange_fill
    failed = asyncio.Event()

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if not failed.is_set():  # one transient bookkeeping failure
            failed.set()
            raise RuntimeError("transient bookkeeping failure")
        return await real(*args, **kwargs)

    ex.apply_exchange_fill = flaky
    await poller.sweep()  # the claim lands, the apply fails
    ex.apply_exchange_fill = real
    assert ex.get_account_snapshot().base_totals == {}

    await poller.sweep()  # the unapplied row is retried before any new claim

    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.00999")}
    assert len(ex.get_fills()) == 1
    store = ex._live_orders
    assert isinstance(store, InMemoryLiveOrderStore)
    assert await store.unapplied_fills("sess") == []


# ---------------------------------------------------------------------------
# F5: LIMIT sells floor to the lot step like MARKET
# ---------------------------------------------------------------------------


async def test_a_dust_limit_sell_floors_to_the_lot_step() -> None:
    fake = FakeBinanceTradingClient()
    fake.commission_asset = "USDT"
    fake.commission_amount = D("0")
    rules = MarketRules(
        symbol="BTCUSDT",
        amount_step=D("0.001"),
        min_qty=D("0.001"),
        min_notional=D("5"),
        price_tick=D("0.01"),
    )
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": D("10000")},
        db_pool=None,
        strategy_id="strat",
        session_id="sess",
        market_rules={"BTCUSDT": rules},
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))  # fills on the fake
    poller, _ = _poller(ex)
    await poller.sweep()
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.01")}

    resp = await ex.create_order("BTCUSDT", "SELL", "LIMIT", D("0.00999"), price=D("101000"))

    assert D(str(resp["amount"])) == D("0.009")  # floored, not refused
    leg = fake.orders[int(resp["id"])]
    assert (leg["origQty"], leg["status"]) == ("0.009", "NEW")


# ---------------------------------------------------------------------------
# F11: cancelling a submit still in flight answers ambiguous
# ---------------------------------------------------------------------------


async def test_cancelling_a_pending_submit_answers_ambiguous_and_the_landing_wins() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    answers: list[dict[str, Any]] = []
    original = fake.create_order

    async def hook(*args: Any, **kwargs: Any) -> dict[str, Any]:
        rows = await ex.live_orders.open_orders(ex._session_id)
        cid = rows[0].client_order_id  # the row is pending_submit right now
        answers.append(await ex.cancel_order(cid, "BTCUSDT"))
        return await original(*args, **kwargs)

    fake.create_order = hook
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    fake.create_order = original

    assert answers == [{"id": answers[0]["id"], "status": "AMBIGUOUS"}]
    # the submit landed anyway: the row acked, and the fill books on the sweep
    poller, _ = _poller(ex)
    await poller.sweep()
    assert ex.get_account_snapshot().base_totals == {"BTC": D("0.00999")}
    rows = await ex.live_orders.orders(ex._session_id)
    assert len(rows) == 1 and rows[0].status is LiveOrderStatus.FILLED
