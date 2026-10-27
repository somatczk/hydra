"""Exchange-side protective stops on Binance spot: exact request shapes at the ccxt boundary.

The fake stands in for the ccxt exchange and records every call's params; the real
``ExchangeClient`` is the layer under test, so the asserted shapes are exactly what hydra
sends to ccxt. The ccxt-internal mapping (spot market + ``stopLossPrice`` -> ``STOP_LOSS``,
``trailingDelta`` in BIPS passing through) was verified against ccxt 4.5.88's
``create_order_request``; no test constructs a keyed client or touches the network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt
import pytest

from hydra.execution.exchange_client import ExchangeClient
from hydra.execution.exchange_stops import (
    ExchangeStopPlacement,
    StopBreachedError,
    StopUnplaceableError,
    arm_exchange_stop,
    cancel_all_symbol_orders,
    cancel_exchange_stop,
    format_price,
    format_qty,
    normalize_open_orders,
    verify_exchange_stop,
)
from hydra.execution.protective_orders import ProtectionSpec
from hydra.risk.market_rules import MarketRules

D = Decimal


def rules(trailing: tuple[str, str] | None = ("10", "2000")) -> MarketRules:
    return MarketRules(
        symbol="BTCUSDT",
        amount_step=D("0.00001"),
        min_qty=D("0.00001"),
        min_notional=D("5"),
        price_tick=D("0.01"),
        trailing_delta_bips=((D(trailing[0]), D(trailing[1])) if trailing is not None else None),
    )


def oco_raw() -> dict[str, Any]:
    return {
        "orderListId": 5,
        "contingencyType": "OCO",
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "listClientOrderId": "lcid",
        "transactionTime": 1700000000000,
        "symbol": "BTCUSDT",
        "orders": [
            {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "a"},
            {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "b"},
        ],
        "orderReports": [
            {
                "symbol": "BTCUSDT",
                "orderId": 11,
                "orderListId": 5,
                "clientOrderId": "a",
                "transactTime": 1700000000000,
                "price": "103.00000000",
                "origQty": "0.01000000",
                "executedQty": "0.00000000",
                "cummulativeQuoteQty": "0.00000000",
                "status": "NEW",
                "timeInForce": "GTC",
                "type": "LIMIT_MAKER",
                "side": "SELL",
                "stopPrice": "0.00000000",
            },
            {
                "symbol": "BTCUSDT",
                "orderId": 12,
                "orderListId": 5,
                "clientOrderId": "b",
                "transactTime": 1700000000000,
                "price": "0.00000000",
                "origQty": "0.01000000",
                "executedQty": "0.00000000",
                "status": "NEW",
                "timeInForce": "GTC",
                "type": "STOP_LOSS",
                "side": "SELL",
                "stopPrice": "98.00000000",
            },
        ],
    }


def oco_legs() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "orderId": 11,
            "orderListId": "5",
            "clientOrderId": "a",
            "price": "103.00000000",
            "origQty": "0.01000000",
            "executedQty": "0.00000000",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "LIMIT_MAKER",
            "side": "SELL",
            "stopPrice": "0.00000000",
            "trailingDelta": "0",
        },
        {
            "symbol": "BTCUSDT",
            "orderId": 12,
            "orderListId": "5",
            "clientOrderId": "b",
            "price": "0.00000000",
            "origQty": "0.01000000",
            "executedQty": "0.00000000",
            "status": "NEW",
            "timeInForce": "GTC",
            "type": "STOP_LOSS",
            "side": "SELL",
            "stopPrice": "98.00000000",
            "trailingDelta": "0",
        },
    ]


def trailing_leg() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "orderId": 13,
        "orderListId": "-1",
        "clientOrderId": "c",
        "price": "0.00000000",
        "origQty": "0.01000000",
        "executedQty": "0.00000000",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": "STOP_LOSS",
        "side": "SELL",
        "stopPrice": "95.00000000",
        "trailingDelta": "200",
    }


def open_lists() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "orderListId": 5,
            "contingencyType": "OCO",
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "listClientOrderId": "lcid",
            "transactionTime": 1700000000000,
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "a"},
                {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "b"},
            ],
        }
    ]


class FakeCcxt:
    """Stands in for the ccxt exchange; records params, serves scripted data or raises.

    The ``private*`` methods mirror ccxt's camelCase implicit API names, so they are bound
    as class attributes rather than defined (pep8-naming would flag the ``def`` names).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.oco: dict[str, Any] = oco_raw()
        self.order: dict[str, Any] = {"id": "9", "status": "open", "type": "STOP_LOSS"}
        self.lists: list[dict[str, Any]] = open_lists()
        self.legs: list[dict[str, Any]] = oco_legs()
        self.fail: dict[str, BaseException] = {}
        self.create_order_errors: list[BaseException] = []

    def _record(self, name: str, params: dict[str, Any]) -> None:
        self.calls.append((name, dict(params)))

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self._record("fetch_open_orders", {"symbol": symbol})
        if "legs" in self.fail:
            raise self.fail["legs"]
        return [{"info": leg} for leg in self.legs]

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_order", kwargs)
        if self.create_order_errors:
            raise self.create_order_errors.pop(0)
        if "create_order" in self.fail:
            raise self.fail["create_order"]
        return self.order

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self._record("cancel_order", {"order_id": order_id, "symbol": symbol})
        return {"id": order_id, "status": "CANCELED"}


async def _fake_post_oco(fake: FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    fake._record("privatePostOrderListOco", params)
    if "oco" in fake.fail:
        raise fake.fail["oco"]
    return fake.oco


async def _fake_delete_order_list(fake: FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    fake._record("privateDeleteOrderList", params)
    return {"code": 200, "msg": "The operation of cancel all open order is done."}


async def _fake_delete_open_orders(fake: FakeCcxt, params: dict[str, Any]) -> dict[str, Any]:
    fake._record("privateDeleteOpenOrders", params)
    return {"code": 200, "msg": "The operation of cancel all open order is done."}


async def _fake_get_open_order_lists(
    fake: FakeCcxt, params: dict[str, Any]
) -> list[dict[str, Any]]:
    fake._record("privateGetOpenOrderList", params)
    if "lists" in fake.fail:
        raise fake.fail["lists"]
    return fake.lists


FakeCcxt.privatePostOrderListOco = _fake_post_oco
FakeCcxt.privateDeleteOrderList = _fake_delete_order_list
FakeCcxt.privateDeleteOpenOrders = _fake_delete_open_orders
FakeCcxt.privateGetOpenOrderList = _fake_get_open_order_lists


def client(fake: FakeCcxt) -> ExchangeClient:
    wrapped = ExchangeClient("binance", {})
    wrapped._exchange = fake
    return wrapped


OCO_SPEC = ProtectionSpec(stop_loss=D("98"), take_profit=D("103"))
STOP_SPEC = ProtectionSpec(stop_loss=D("98"))
TRAIL_SPEC = ProtectionSpec(trailing_pct=D("0.02"))
OCO_PLACEMENT = ExchangeStopPlacement(
    order_id="5",
    kind="oco",
    leg_ids=("11", "12"),
    quantity=D("0.01"),
    stop_price=D("98"),
    take_profit=D("103"),
)


async def arm(fake: FakeCcxt, spec: ProtectionSpec, *, last: str = "100", qty: str = "0.01"):
    return await arm_exchange_stop(
        client(fake), rules(), "BTCUSDT", D(qty), spec, D(last), D("100")
    )


class TestLadder:
    async def test_oco_request_shape_and_placement(self) -> None:
        fake = FakeCcxt()
        placement = await arm(fake, OCO_SPEC)

        assert fake.calls == [
            (
                "privatePostOrderListOco",
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "quantity": "0.01000",
                    "aboveType": "LIMIT_MAKER",
                    "abovePrice": "103.00",
                    "belowType": "STOP_LOSS",
                    "belowStopPrice": "98.00",
                    "newOrderRespType": "FULL",
                },
            )
        ]
        assert placement == OCO_PLACEMENT

    async def test_create_oco_returns_the_paper_oco_shape(self) -> None:
        fake = FakeCcxt()
        resp = await client(fake).create_oco("BTCUSDT", "SELL", "0.01000", "103.00", "98.00")

        assert resp == {
            "id": "5",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "OCO",
            "amount": 0.01,
            "price": 103.0,
            "stop_price": 98.0,
            "status": "EXEC_STARTED",
            "filled": False,
            "oco_link": "5",
            "legs": [
                {
                    "id": "11",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "type": "LIMIT",
                    "amount": 0.01,
                    "price": 103.0,
                    "stop_price": None,
                    "status": "NEW",
                    "filled": False,
                    "reason": "take_profit",
                    "oco_id": "5",
                    "trail_pct": None,
                },
                {
                    "id": "12",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "amount": 0.01,
                    "price": None,
                    "stop_price": 98.0,
                    "status": "NEW",
                    "filled": False,
                    "reason": "stop_loss",
                    "oco_id": "5",
                    "trail_pct": None,
                },
            ],
        }

    async def test_no_take_profit_places_stop_only(self) -> None:
        fake = FakeCcxt()
        placement = await arm(fake, STOP_SPEC)

        assert fake.calls == [
            (
                "create_order",
                {
                    "symbol": "BTCUSDT",
                    "type": "market",
                    "side": "sell",
                    "amount": 0.01,
                    "price": None,
                    "params": {"stopLossPrice": "98.00"},
                },
            )
        ]
        assert placement == ExchangeStopPlacement(
            order_id="9", kind="stop", leg_ids=(), quantity=D("0.01"), stop_price=D("98")
        )

    async def test_trailing_places_a_single_stop_with_bips(self) -> None:
        fake = FakeCcxt()
        placement = await arm(fake, TRAIL_SPEC)

        assert fake.calls == [
            (
                "create_order",
                {
                    "symbol": "BTCUSDT",
                    "type": "market",
                    "side": "sell",
                    "amount": 0.01,
                    "price": None,
                    "params": {"trailingDelta": "200", "stopLossOrTakeProfit": "stopLoss"},
                },
            )
        ]
        assert placement == ExchangeStopPlacement(
            order_id="9", kind="trailing", quantity=D("0.01"), trailing_pct=D("0.02")
        )

    async def test_take_profit_at_or_below_the_last_price_is_dropped(self) -> None:
        fake = FakeCcxt()
        placement = await arm(fake, OCO_SPEC, last="103.5")

        assert [name for name, _ in fake.calls] == ["create_order"]
        assert fake.calls[0][1]["params"] == {"stopLossPrice": "98.00"}
        assert placement.kind == "stop" and placement.take_profit is None

    async def test_stop_at_or_above_the_last_price_breaches(self) -> None:
        fake = FakeCcxt()
        with pytest.raises(StopBreachedError, match="close at market"):
            await arm(fake, STOP_SPEC, last="97.5")
        assert fake.calls == []

    async def test_oco_rejection_falls_back_to_the_stop_only_rung(self) -> None:
        fake = FakeCcxt()
        fake.fail["oco"] = ccxt.InvalidOrder("OCO above price is not valid")

        placement = await arm(fake, OCO_SPEC)

        assert [name for name, _ in fake.calls] == ["privatePostOrderListOco", "create_order"]
        assert fake.calls[1][1]["params"] == {"stopLossPrice": "98.00"}
        assert placement.kind == "stop"

    async def test_stop_rejection_falls_back_to_the_five_percent_default(self) -> None:
        fake = FakeCcxt()
        fake.fail["oco"] = ccxt.InvalidOrder("no oco")
        fake.create_order_errors = [ccxt.InvalidOrder("no stops")]

        placement = await arm(fake, OCO_SPEC)

        assert [name for name, _ in fake.calls] == [
            "privatePostOrderListOco",
            "create_order",
            "create_order",
        ]
        assert fake.calls[2][1]["params"] == {"stopLossPrice": "95.00"}
        assert placement.stop_price == D("95")

    async def test_every_rung_rejected_raises_unplaceable(self) -> None:
        fake = FakeCcxt()
        fake.fail["oco"] = ccxt.InvalidOrder("no oco")
        fake.fail["create_order"] = ccxt.InvalidOrder("no stops")

        with pytest.raises(StopUnplaceableError, match="every protective form rejected"):
            await arm(fake, OCO_SPEC)
        assert [name for name, _ in fake.calls] == [
            "privatePostOrderListOco",
            "create_order",
            "create_order",
        ]

    async def test_a_timeout_propagates_instead_of_falling_through(self) -> None:
        fake = FakeCcxt()
        fake.fail["oco"] = ccxt.RequestTimeout("binance POST timed out")

        with pytest.raises(ccxt.RequestTimeout):
            await arm(fake, OCO_SPEC)
        assert [name for name, _ in fake.calls] == ["privatePostOrderListOco"]

    async def test_a_stop_timeout_propagates_instead_of_placing_the_default(self) -> None:
        fake = FakeCcxt()
        fake.fail["create_order"] = ccxt.NetworkError("connection reset")

        with pytest.raises(ccxt.NetworkError):
            await arm(fake, STOP_SPEC)
        assert [name for name, _ in fake.calls] == ["create_order"]

    async def test_trailing_outside_the_filter_uses_the_default_fixed_stop(self) -> None:
        for bounds in ((("10", "100")), None):
            fake = FakeCcxt()
            r = rules(bounds)
            placement = await arm_exchange_stop(
                client(fake), r, "BTCUSDT", D("0.01"), TRAIL_SPEC, D("100"), D("100")
            )

            assert [name for name, _ in fake.calls] == ["create_order"]
            assert fake.calls[0][1]["params"] == {"stopLossPrice": "95.00"}
            assert placement.kind == "stop" and placement.stop_price == D("95")

    async def test_quantity_floored_to_zero_is_unplaceable(self) -> None:
        fake = FakeCcxt()
        with pytest.raises(StopUnplaceableError, match="floors to zero"):
            await arm(fake, STOP_SPEC, qty="0.000004")
        assert fake.calls == []


class TestCancel:
    async def test_cancel_cancels_the_whole_order_list(self) -> None:
        fake = FakeCcxt()
        await cancel_exchange_stop(client(fake), "BTCUSDT", OCO_PLACEMENT)

        assert fake.calls == [("privateDeleteOrderList", {"symbol": "BTCUSDT", "orderListId": "5"})]

    async def test_cancel_of_a_single_order_cancels_the_order(self) -> None:
        fake = FakeCcxt()
        placement = ExchangeStopPlacement(order_id="9", kind="stop", quantity=D("0.01"))
        await cancel_exchange_stop(client(fake), "BTCUSDT", placement)

        assert fake.calls == [("cancel_order", {"order_id": "9", "symbol": "BTCUSDT"})]

    async def test_the_kill_switch_sweep_cancels_everything_on_the_symbol(self) -> None:
        fake = FakeCcxt()
        await cancel_all_symbol_orders(client(fake), "BTCUSDT")

        assert fake.calls == [("privateDeleteOpenOrders", {"symbol": "BTCUSDT"})]


class TestNormalize:
    async def test_two_ccxt_legs_become_the_paper_shape(self) -> None:
        normalized = normalize_open_orders([{"info": leg} for leg in oco_legs()])

        assert normalized == [
            {
                "id": "11",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "LIMIT",
                "amount": 0.01,
                "price": 103.0,
                "stop_price": None,
                "status": "NEW",
                "filled": False,
                "reason": "take_profit",
                "oco_id": "5",
                "trail_pct": None,
            },
            {
                "id": "12",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "amount": 0.01,
                "price": None,
                "stop_price": 98.0,
                "status": "NEW",
                "filled": False,
                "reason": "stop_loss",
                "oco_id": "5",
                "trail_pct": None,
            },
        ]

    async def test_a_trailing_leg_maps_to_the_paper_trailing_stop(self) -> None:
        (normalized,) = normalize_open_orders([{"info": trailing_leg()}])

        assert normalized == {
            "id": "13",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "TRAILING_STOP",
            "amount": 0.01,
            "price": None,
            "stop_price": 95.0,
            "status": "NEW",
            "filled": False,
            "reason": "trailing_stop",
            "oco_id": None,
            "trail_pct": 0.02,
        }

    async def test_raw_binance_legs_normalize_the_same_way(self) -> None:
        assert normalize_open_orders(oco_legs()) == normalize_open_orders(
            [{"info": leg} for leg in oco_legs()]
        )


class TestVerify:
    async def test_the_expected_list_resting_is_confirmed_with_its_levels(self) -> None:
        fake = FakeCcxt()
        state = await verify_exchange_stop(client(fake), "BTCUSDT", OCO_PLACEMENT)

        assert state.state == "confirmed"
        assert state.kind == "oco"
        assert state.stop_price == 98.0
        assert state.take_profit == 103.0
        assert state.order_list_id == "5"
        assert state.error is None
        assert state.checked_at.tzinfo is not None

    async def test_nothing_resting_is_missing(self) -> None:
        fake = FakeCcxt()
        fake.lists = []
        fake.legs = []

        state = await verify_exchange_stop(client(fake), "BTCUSDT", OCO_PLACEMENT)

        assert state.state == "missing"
        assert state.stop_price is None and state.order_list_id is None

    async def test_an_unreadable_exchange_is_unknown_never_missing(self) -> None:
        fake = FakeCcxt()
        fake.fail["legs"] = ccxt.ExchangeNotAvailable("503")

        state = await verify_exchange_stop(client(fake), "BTCUSDT", OCO_PLACEMENT)

        assert state.state == "unknown"
        assert "503" in (state.error or "")

    async def test_without_an_expectation_any_resting_sell_stop_confirms(self) -> None:
        fake = FakeCcxt()
        fake.lists = []
        fake.legs = [trailing_leg()]

        state = await verify_exchange_stop(client(fake), "BTCUSDT")

        assert state.state == "confirmed"
        assert state.kind == "trailing"
        assert state.stop_price == 95.0
        assert state.order_list_id is None

    async def test_without_an_expectation_a_lone_take_profit_leg_is_missing(self) -> None:
        fake = FakeCcxt()
        fake.lists = []
        fake.legs = [oco_legs()[0]]

        state = await verify_exchange_stop(client(fake), "BTCUSDT")

        assert state.state == "missing"

    async def test_a_single_order_expected_confirms_from_its_resting_leg(self) -> None:
        fake = FakeCcxt()
        fake.lists = []
        fake.legs = [trailing_leg()]
        expected = ExchangeStopPlacement(order_id="13", kind="trailing", quantity=D("0.01"))

        state = await verify_exchange_stop(client(fake), "BTCUSDT", expected)

        assert state.state == "confirmed" and state.kind == "trailing"


class TestFormatting:
    def test_the_stop_rounds_up_the_take_profit_down(self) -> None:
        r = rules()
        assert format_price(r, D("97.987"), "sl") == "97.99"
        assert format_price(r, D("103.013"), "tp") == "103.01"

    def test_the_quantity_floors_to_the_lot_step(self) -> None:
        assert format_qty(rules(), D("0.010004")) == "0.01000"
        assert format_qty(rules(), D("0.01")) == "0.01000"

    def test_a_market_without_a_tick_passes_the_price_through(self) -> None:
        r = MarketRules(symbol="BTCUSDT", amount_step=D("1"), min_qty=D("1"), min_notional=D("5"))
        assert format_price(r, D("98.123456"), "sl") == "98.123456"
