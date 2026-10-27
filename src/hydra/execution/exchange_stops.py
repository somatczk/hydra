"""Exchange-side protective stops on Binance spot: the P3-B placement seam.

Everything here works against an *injected* ``ExchangeClient``; this module never builds or
keys a client (the keyed acquisition is P3-A's). It owns four contracts the live executor
(P3-A) builds on:

* ``arm_exchange_stop`` — the placement ladder: OCO order-list first, then a stop-only
  ``STOP_LOSS``, then the 5 % default stop; every rung is tried only after a *definitive*
  exchange rejection (``ccxt.ExchangeError``). A network error or timeout means the outcome
  is unknown — the order may rest — so it propagates (R18) to the wiring's decision-17
  market-close path instead of falling through the ladder. No double-stop scenario.
* ``normalize_open_orders`` — the ONE open-orders shape (paper dicts) for adopt, flatten,
  the A8 live read and P3-A's reconcile; live fills carry ``parent_order_id`` = the
  orderListId of their OCO, and every leg carries its ``client_order_id`` — the
  ownership marker live adopt gates on (R5: the user's orders are never hijacked).
* ``verify_exchange_stop`` — the A8-live read per held position. ``confirmed`` only from a
  real openOrderList / resting-leg match at ``checked_at``; an unreadable exchange is
  ``unknown``, never ``missing``.
* ``cancel_all_symbol_orders`` — the kill-switch sweep (Q2): cancels EVERY open order on a
  symbol's account, hydra's or not.

Binance spot facts encoded here (verified against the spot API docs and ccxt 4.5.88): the
SELL OCO is POST /api/v3/orderList/oco with one quantity for both legs, aboveType
LIMIT_MAKER (the take-profit limit) and belowType STOP_LOSS (the stop, market when
triggered), and must satisfy TP > last > SL at placement time. A trailing stop is a single
STOP_LOSS order with ``trailingDelta`` in BIPS (1-1,000,000, bounded per symbol by the
TRAILING_DELTA filter — the ``minTrailingBelowDelta``/``maxTrailingBelowDelta`` pair for a
SELL). Cancelling either OCO leg cancels the whole list.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal, Protocol

from hydra.core.types import OrderType
from hydra.execution.protective_orders import DEFAULT_STOP_FRACTION, ProtectionSpec
from hydra.risk.market_rules import MarketRules

logger = logging.getLogger(__name__)

_ONE = Decimal("1")
_BIPS_PER_UNIT = Decimal("10000")
_STOP_TYPES = (OrderType.STOP_MARKET, OrderType.TRAILING_STOP)


class StopUnplaceableError(ValueError):
    """Every rung of the placement ladder was definitively rejected by the exchange."""


class StopBreachedError(ValueError):
    """The stop is not below the last price: close the position at market now."""


class TradeClient(Protocol):
    """The exchange calls this seam needs (an ``ExchangeClient``; fakes in tests)."""

    async def create_oco(
        self,
        symbol: str,
        side: str,
        quantity: str,
        take_profit: str,
        stop_loss: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]: ...

    async def cancel_order_list(self, order_list_id: str, symbol: str) -> dict[str, Any]: ...

    async def cancel_all_orders(self, symbol: str) -> Any: ...

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    async def fetch_open_order_lists(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ExchangeStopPlacement:
    """One resting exchange-side protective order (or OCO list) for a held long."""

    order_id: str
    kind: Literal["oco", "stop", "trailing"]
    leg_ids: tuple[str, ...] = ()
    quantity: Decimal = Decimal("0")
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None
    trailing_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExchangeStopState:
    """The A8-live read of one position's exchange-side stop (the pinned seam #3 shape)."""

    state: Literal["confirmed", "missing", "unknown"]
    checked_at: datetime
    stop_price: float | None = None
    take_profit: float | None = None
    kind: Literal["oco", "stop", "trailing"] | None = None
    order_list_id: str | None = None
    error: str | None = None


class ExpectedStop(Protocol):
    """What hydra believes protects a symbol: a placement or an adopted group."""

    order_id: str
    leg_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _round_price(rules: MarketRules, price: Decimal, direction: Literal["sl", "tp"]) -> Decimal:
    """The wire value of *price*: a SELL stop rounds UP to the tick, a TP limit DOWN.

    Up keeps the stop closer to the market (it triggers no later than asked for); down
    keeps a maker take-profit marketable.
    """
    tick = rules.price_tick
    if tick is None or tick <= 0:
        return price
    return price.quantize(tick, rounding=ROUND_DOWN if direction == "tp" else ROUND_UP)


def format_price(rules: MarketRules, price: Decimal, direction: Literal["sl", "tp"]) -> str:
    """Decimal -> exchange string (the tick-rounded wire value)."""
    return format(_round_price(rules, price, direction), "f")


def format_qty(rules: MarketRules, quantity: Decimal) -> str:
    """Decimal -> exchange string, floored to the lot step (never rounds up)."""
    return format(rules.floor_qty(quantity), "f")


# ---------------------------------------------------------------------------
# The placement ladder
# ---------------------------------------------------------------------------


def _definitive_rejection(exc: BaseException) -> bool:
    """The exchange answered 'no'; a network error is an unknown outcome, not a rejection."""
    import ccxt

    return isinstance(exc, ccxt.ExchangeError)


def _trailing_bips(trailing_pct: Decimal) -> int:
    return int((trailing_pct * _BIPS_PER_UNIT).to_integral_value(rounding=ROUND_DOWN))


async def arm_exchange_stop(
    client: TradeClient,
    rules: MarketRules,
    symbol: str,
    quantity: Decimal,
    spec: ProtectionSpec,
    last_price: Decimal,
    entry: Decimal,
) -> ExchangeStopPlacement:
    """Place the protective exit for a held long, safest resting form first.

    Pre-checks run on the ROUNDED wire values (A4: round first, then compare): a
    take-profit that would not rest above the last price after tick rounding is dropped
    (Binance refuses an OCO whose above leg is not above the market); a stop that would
    not rest below the last price raises ``StopBreachedError`` (the wiring
    market-closes); a trailing percentage outside the symbol's TRAILING_DELTA filter
    falls back to the default fixed stop. A quantity that floors to zero at the lot
    step raises ``StopUnplaceableError``.
    """
    steps: list[str] = []
    qty = format_qty(rules, quantity)
    if Decimal(qty) <= 0:
        raise StopUnplaceableError(
            f"{symbol}: quantity {quantity} floors to zero at lot step {rules.amount_step}"
        )
    if spec.trailing_pct is not None:
        bips = _trailing_bips(spec.trailing_pct)
        bounds = rules.trailing_delta_bips
        if bounds is not None and bounds[0] <= bips <= bounds[1]:
            try:
                resp = await client.create_order(
                    symbol,
                    "SELL",
                    "market",
                    Decimal(qty),
                    params={"trailingDelta": str(bips), "stopLossOrTakeProfit": "stopLoss"},
                )
            except Exception as exc:
                if not _definitive_rejection(exc):
                    raise
                steps.append(f"trailing stop rejected ({exc}); using the default fixed stop")
            else:
                return ExchangeStopPlacement(
                    order_id=str(resp["id"]),
                    kind="trailing",
                    leg_ids=tuple(str(leg["id"]) for leg in resp.get("legs") or []),
                    quantity=Decimal(qty),
                    trailing_pct=spec.trailing_pct,
                )
        else:
            steps.append(
                f"trailing {bips} BIPS outside the TRAILING_DELTA filter {bounds}; "
                "using the default fixed stop"
            )
    stop_loss = spec.stop_loss if spec.stop_loss is not None else _default_stop_price(entry)
    stop_loss = _round_price(rules, stop_loss, "sl")
    take_profit: Decimal | None = None
    if spec.take_profit is not None:
        rounded_tp = _round_price(rules, spec.take_profit, "tp")
        if rounded_tp > last_price:
            take_profit = rounded_tp
        else:
            steps.append(
                f"take-profit {spec.take_profit} would not rest above the last price "
                f"{last_price}; placing the stop only"
            )
    _require_below_last(stop_loss, last_price, symbol)
    if take_profit is not None:
        try:
            resp = await client.create_oco(
                symbol,
                "SELL",
                qty,
                format_price(rules, take_profit, "tp"),
                format_price(rules, stop_loss, "sl"),
            )
        except Exception as exc:
            if not _definitive_rejection(exc):
                raise
            steps.append(f"OCO rejected ({exc}); placing the stop only")
        else:
            legs = resp.get("legs") or []
            return ExchangeStopPlacement(
                order_id=str(resp["id"]),
                kind="oco",
                leg_ids=tuple(str(leg["id"]) for leg in legs),
                quantity=Decimal(qty),
                stop_price=stop_loss,
                take_profit=take_profit,
            )
    try:
        resp = await client.create_order(
            symbol,
            "SELL",
            "market",
            Decimal(qty),
            params={"stopLossPrice": format_price(rules, stop_loss, "sl")},
        )
    except Exception as exc:
        if not _definitive_rejection(exc):
            raise
        steps.append(f"stop rejected ({exc}); placing the 5% default")
    else:
        return ExchangeStopPlacement(
            order_id=str(resp["id"]),
            kind="stop",
            leg_ids=tuple(str(leg["id"]) for leg in resp.get("legs") or []),
            quantity=Decimal(qty),
            stop_price=stop_loss,
        )
    default_sl = _round_price(rules, _default_stop_price(entry), "sl")
    _require_below_last(default_sl, last_price, symbol)
    try:
        resp = await client.create_order(
            symbol,
            "SELL",
            "market",
            Decimal(qty),
            params={"stopLossPrice": format_price(rules, default_sl, "sl")},
        )
    except Exception as exc:
        if not _definitive_rejection(exc):
            raise
        steps.append(f"default stop rejected ({exc})")
    else:
        return ExchangeStopPlacement(
            order_id=str(resp["id"]),
            kind="stop",
            leg_ids=tuple(str(leg["id"]) for leg in resp.get("legs") or []),
            quantity=Decimal(qty),
            stop_price=default_sl,
        )
    raise StopUnplaceableError(f"{symbol}: every protective form rejected: " + "; ".join(steps))


def _default_stop_price(entry: Decimal) -> Decimal:
    return entry * (_ONE - DEFAULT_STOP_FRACTION)


def _require_below_last(stop: Decimal, last_price: Decimal, symbol: str) -> None:
    if stop >= last_price:
        raise StopBreachedError(
            f"{symbol}: stop {stop} is not below the last price {last_price}; close at market"
        )


async def cancel_exchange_stop(
    client: TradeClient, symbol: str, placement: ExchangeStopPlacement
) -> dict[str, Any]:
    """Cancel a resting protective order: the whole OCO list, or the single order."""
    if placement.kind == "oco":
        return await client.cancel_order_list(placement.order_id, symbol)
    return await client.cancel_order(placement.order_id, symbol)


async def cancel_all_symbol_orders(client: TradeClient, symbol: str) -> None:
    """The kill-switch sweep (Q2): cancel EVERY open order on *symbol*, hydra's or not."""
    await client.cancel_all_orders(symbol)


# ---------------------------------------------------------------------------
# Normalization: the one open-orders shape
# ---------------------------------------------------------------------------


def _leg_field(order: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a raw Binance leg field, from ``info`` on a unified ccxt order or the leg itself."""
    info = order.get("info")
    if isinstance(info, Mapping) and key in info:
        return info[key]
    return order.get(key, default)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _price(value: Any) -> float | None:
    dec = _dec(value)
    return float(dec) if dec is not None and dec > 0 else None


def _client_order_id(order: Mapping[str, Any]) -> str | None:
    """The leg's clientOrderId — the ownership marker for live adopt (R5)."""
    raw = _leg_field(order, "clientOrderId", order.get("clientOrderId"))
    return None if raw in (None, "") else str(raw)


def normalize_exchange_leg(order: Mapping[str, Any]) -> dict[str, Any]:
    """One raw Binance order (an ``openOrders`` leg or an OCO ``orderReport``) -> paper dict."""
    raw_type = str(_leg_field(order, "type", "") or "").upper()
    order_list_id = _leg_field(order, "orderListId")
    oco_id = None if order_list_id in (None, "", -1, "-1") else str(order_list_id)
    trailing_delta = _dec(_leg_field(order, "trailingDelta"))
    if trailing_delta is not None and trailing_delta > 0:
        order_type: OrderType = OrderType.TRAILING_STOP
        reason = "trailing_stop"
        trail_pct: float | None = float(trailing_delta / _BIPS_PER_UNIT)
    elif raw_type == "LIMIT_MAKER":
        order_type, reason, trail_pct = OrderType.LIMIT, "take_profit", None
    elif raw_type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
        order_type, reason, trail_pct = OrderType.STOP_MARKET, "stop_loss", None
    elif raw_type == "TAKE_PROFIT":
        order_type, reason, trail_pct = OrderType.TAKE_PROFIT_MARKET, "take_profit", None
    elif raw_type == "TAKE_PROFIT_LIMIT":
        order_type, reason, trail_pct = OrderType.LIMIT, "take_profit", None
    else:
        order_type, reason, trail_pct = OrderType.LIMIT, "limit", None
    amount = _dec(_leg_field(order, "origQty", order.get("amount")))
    return {
        "id": str(_leg_field(order, "orderId", order.get("id"))),
        "symbol": str(_leg_field(order, "symbol", order.get("symbol", ""))),
        "side": str(_leg_field(order, "side", "")).upper(),
        "type": order_type,
        "amount": float(amount) if amount is not None else 0.0,
        "price": _price(_leg_field(order, "price")),
        "stop_price": _price(_leg_field(order, "stopPrice", order.get("stopPrice"))),
        "status": str(_leg_field(order, "status", "PENDING") or "PENDING").upper(),
        "filled": False,
        "reason": reason,
        "oco_id": oco_id,
        "trail_pct": trail_pct,
        "client_order_id": _client_order_id(order),
    }


def normalize_open_orders(raw: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Exchange legs -> the paper open-order shape for adopt, flatten, A8 and reconcile."""
    return [normalize_exchange_leg(order) for order in raw]


# ---------------------------------------------------------------------------
# Verification: the A8-live read
# ---------------------------------------------------------------------------


async def verify_exchange_stop(
    client: TradeClient, symbol: str, expected: ExpectedStop | None = None
) -> ExchangeStopState:
    """Is a protective order resting on the exchange for *symbol* right now?

    Matches the expected placement/group by its order-list or order id (leg ids included),
    or — with no expectation, e.g. a stopped session — any resting SELL stop-family leg
    on the symbol (reason ``stop_loss``/``trailing_stop``: a take-profit leg is not
    downside protection). Exchange read errors answer ``unknown`` with the error, never
    ``missing``.
    """
    checked_at = datetime.now(UTC)
    try:
        lists = await client.fetch_open_order_lists(symbol)
        legs = normalize_open_orders(await client.fetch_open_orders(symbol))
    except Exception as exc:
        return ExchangeStopState(state="unknown", checked_at=checked_at, error=str(exc))
    expected_id = None if expected is None else str(expected.order_id)
    expected_legs = _leg_ids(expected)
    list_ids = {str(li.get("orderListId")) for li in lists if li.get("orderListId") is not None}
    matched = [
        o
        for o in legs
        if expected_id is not None
        and (o["oco_id"] == expected_id or o["id"] == expected_id or o["id"] in expected_legs)
    ]
    if not matched and expected_id is None:
        matched = [
            o
            for o in legs
            if o["side"] == "SELL"
            and o["type"] in _STOP_TYPES
            and o["reason"] in ("stop_loss", "trailing_stop")
        ]
    if not matched:
        if expected_id is not None and expected_id in list_ids:
            return ExchangeStopState(
                state="confirmed", kind="oco", order_list_id=expected_id, checked_at=checked_at
            )
        return ExchangeStopState(state="missing", checked_at=checked_at)
    stop = next((o for o in matched if o["stop_price"] is not None), None)
    take_profit = next(
        (o for o in matched if o["type"] == OrderType.LIMIT and o["oco_id"] is not None), None
    )
    if any(o["type"] == OrderType.TRAILING_STOP for o in matched):
        kind: Literal["oco", "stop", "trailing"] = "trailing"
    elif any(o["oco_id"] is not None for o in matched):
        kind = "oco"
    else:
        kind = "stop"
    return ExchangeStopState(
        state="confirmed",
        checked_at=checked_at,
        stop_price=stop["stop_price"] if stop is not None else None,
        take_profit=take_profit["price"] if take_profit is not None else None,
        kind=kind,
        order_list_id=next((o["oco_id"] for o in matched if o["oco_id"] is not None), None),
    )


def _leg_ids(expected: ExpectedStop | None) -> set[str]:
    return set() if expected is None else {str(leg_id) for leg_id in expected.leg_ids}
