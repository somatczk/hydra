"""The exchange boundary's Decimal contract (P7-B3): one outgoing float, exact ingest.

The single outgoing boundary is ``ExchangeClient.create_order``'s Decimal->float wire
conversion (ccxt's typed API); it stays exact for lot/tick-aligned values because
Python's shortest-repr round-trip parses the float back to the same Decimal value
(``Decimal(str(float(d))) == d``) — pinned here against the wire strings
``exchange_stops`` actually formats. Everything coming back from the exchange converts
at use to exact Decimals (``Decimal(str(raw))``), never through ``float``: the spot and
perp raw order rows, and the OCO echo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hydra.execution.exchange_client import ExchangeClient
from hydra.execution.exchange_stops import format_price, format_qty
from hydra.execution.perp_live import normalize_perp_leg
from hydra.risk.market_rules import MarketRules

D = Decimal

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)

# lot/tick-aligned values across the magnitudes the wire carries, including the
# exactness fixture's own pair (a resting limit price and a stop trigger)
ALIGNED = [
    D("0.00012345"),
    D("9876.5"),
    D("0.01000"),
    D("103.00"),
    D("98.00"),
    D("0.00123000"),
    D("119999.99"),
    D("0.10000"),
]


class _RecordingCcxt:
    """Stands in for the ccxt exchange instance; records the outgoing call kwargs."""

    def __init__(self) -> None:
        self.create_order_kwargs: dict[str, Any] | None = None
        self.oco_request: dict[str, Any] | None = None

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.create_order_kwargs = kwargs
        return {"id": "101", "status": "NEW"}

    async def _post_oco(self, params: dict[str, Any]) -> dict[str, Any]:
        self.oco_request = params
        return {
            "orderListId": 5,
            "listClientOrderId": "lcid",
            "listStatusType": "EXECUTING",
            "orderReports": [],
        }


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
_RecordingCcxt.privatePostOrderListOco = _RecordingCcxt._post_oco


def _client(fake: _RecordingCcxt) -> ExchangeClient:
    client = ExchangeClient("binance", {})
    client._exchange = fake
    return client


def test_lot_and_tick_aligned_decimals_round_trip_through_float() -> None:
    """The exactness argument, as a property: for every lot/tick-aligned Decimal the
    shortest repr of its float parses back to the exact same value."""
    for value in ALIGNED:
        assert D(str(float(value))) == value, value


def test_wire_formatted_prices_and_quantities_round_trip() -> None:
    """The values the wire actually carries — ``format_price``/``format_qty`` output —
    survive the outgoing float conversion with their exact value."""
    for price in (D("9876.5"), D("103.004"), D("119999.99"), D("0.01")):
        wire = format_price(RULES, price, "sl")
        assert D(str(float(D(wire)))) == D(wire)
    for qty in (D("0.00012345"), D("1.00000"), D("0.09999")):
        wire = format_qty(RULES, qty)
        assert D(str(float(D(wire)))) == D(wire)


async def test_create_order_converts_decimal_to_float_on_the_wire() -> None:
    """The single outgoing boundary: the ccxt call receives floats whose shortest repr
    parses back to the exact Decimal, so the wire value is exact."""
    fake = _RecordingCcxt()

    await _client(fake).create_order(
        "BTCUSDT", "BUY", "limit", D("0.00012345"), price=D("9876.5"), stop_price=D("98")
    )

    kwargs = fake.create_order_kwargs
    assert kwargs is not None
    assert isinstance(kwargs["amount"], float)
    assert isinstance(kwargs["price"], float)
    assert isinstance(kwargs["params"]["stopPrice"], float)
    assert (
        D(str(kwargs["amount"])),
        D(str(kwargs["price"])),
        D(str(kwargs["params"]["stopPrice"])),
    ) == (D("0.00012345"), D("9876.5"), D("98"))


async def test_create_oco_echoes_the_callers_strings_as_exact_decimal() -> None:
    """The OCO echo carries the caller's own pre-formatted strings back as exact
    Decimals — never through ``float``."""
    fake = _RecordingCcxt()

    resp = await _client(fake).create_oco("BTCUSDT", "SELL", "0.01000", "103.00", "98.00")

    assert fake.oco_request is not None
    assert (fake.oco_request["quantity"], fake.oco_request["abovePrice"]) == ("0.01000", "103.00")
    assert (resp["amount"], resp["price"], resp["stop_price"]) == (
        D("0.01000"),
        D("103.00"),
        D("98.00"),
    )
    assert all(isinstance(resp[key], Decimal) for key in ("amount", "price", "stop_price"))


def test_perp_raw_row_ingest_preserves_the_exchange_decimal_strings() -> None:
    """A raw fapi order row's price, trigger and amount keep the exchange's own decimal
    strings exactly."""
    raw = {
        "id": "301",
        "symbol": "BTCUSDT",
        "side": "sell",
        "type": "STOP_MARKET",
        "price": "0.00012345",
        "amount": "0.00123000",
        "triggerPrice": "9876.5000",
        "status": "new",
        "clientOrderId": "hyd-abc",
        "reduceOnly": True,
    }

    leg = normalize_perp_leg(raw)

    assert leg["price"] == D("0.00012345")
    assert leg["stop_price"] == D("9876.5000")
    assert leg["amount"] == D("0.00123000")
    assert all(isinstance(leg[key], Decimal) for key in ("price", "stop_price", "amount"))


def test_perp_raw_row_without_trigger_or_price_stays_none() -> None:
    leg = normalize_perp_leg(
        {"id": "302", "symbol": "BTCUSDT", "side": "buy", "type": "market", "amount": "0.5"}
    )

    assert leg["price"] is None
    assert leg["stop_price"] is None
    assert leg["amount"] == D("0.5")
