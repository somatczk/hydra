"""The per-venue protective adapter: the live executor's dispatch on the registry's kind.

Binance spot keeps its own implementation — ``exchange_stops`` — where the OCO ladder
lives; this module dispatches on the venue's registry ``protective_kind`` and owns the
bybit pair: Bybit spot has no OCO or list orders, so a stop-plus-take-profit is a TP+SL
conditional pair (the perp pattern) with hydra-side sibling-cancel on both the fill and
the reconcile path, the SL leg placed FIRST — protection before profit. Bybit's
conditional orders rest behind the spot ``tpslOrder`` filter of the realtime order
endpoint, so every leg read goes through ``fetch_bybit_leg`` (the tpsl namespace first,
then the regular one a triggered order graduates to) and every leg cancel carries the
``orderFilter``; a cancel of a never-acked leg goes out by ``orderLinkId`` alone, never
a bogus ``orderId``.

Wire contract (verified against the installed ccxt 4.5.88 ``bybit`` source; the fake in
``tests/fixtures/bybit_trading.py`` owns it until the on-box testnet battery runs): a spot
``create_order`` whose params carry ``stopLossPrice``/``takeProfitPrice`` maps to
``orderFilter: 'tpslOrder'`` on POST /v5/order/create with ``triggerDirection`` derived
from the side and ``reduceOnly: true``, the client id rides ``orderLinkId`` (ccxt's
``clientOrderId``), and GET /v5/order/realtime takes either ``orderId`` or ``orderLinkId``
plus the filter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from hydra.core.types import MarketType, OrderType
from hydra.core.venues import ProtectiveKind, find_venue
from hydra.execution.exchange_stops import (
    ExchangeStopPlacement,
    ExchangeStopState,
    ExpectedStop,
    StopUnplaceableError,
    _default_stop_price,
    _definitive_rejection,
    _require_below_last,
    _round_price,
    format_qty,
)
from hydra.execution.protective_orders import ProtectionSpec
from hydra.risk.market_rules import MarketRules

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_STOP_TYPES = (OrderType.STOP_MARKET, OrderType.TRAILING_STOP)


class BybitProtectiveClient(Protocol):
    """The bybit seam (an ``ExchangeClient``; the wire-contract fake in tests)."""

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

    async def cancel_order(
        self, order_id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def fetch_order(
        self, order_id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def fetch_open_orders(
        self, symbol: str | None = None, *, conditional: bool = False
    ) -> list[dict[str, Any]]: ...


def protective_kind(exchange_id: str, market_type: MarketType) -> ProtectiveKind | None:
    """The venue's registry protective kind, or None when the pair is not registered."""
    spec = find_venue(exchange_id, market_type)
    return None if spec is None else spec.protective_kind


def is_pair_venue(exchange_id: str, market_type: MarketType = MarketType.SPOT) -> bool:
    """True when the venue's protective form is the no-OCO conditional pair."""
    return protective_kind(exchange_id, market_type) is ProtectiveKind.BYBIT_PAIR


# ---------------------------------------------------------------------------
# The dispatch (the live executor's seam)
# ---------------------------------------------------------------------------


async def arm_protective_stop(
    exchange_id: str,
    client: Any,
    rules: MarketRules,
    symbol: str,
    quantity: Decimal,
    spec: ProtectionSpec,
    last_price: Decimal,
    entry: Decimal,
    *,
    group_cid: str,
    pair_leg_cids: tuple[str, ...],
    fallback_cids: tuple[str, ...],
) -> ExchangeStopPlacement:
    """Place the protective exit on the venue the registry names for *exchange_id*.

    The binance path is ``exchange_stops.arm_exchange_stop`` verbatim (the caller wraps
    the client with the per-order client ids); the bybit path is the pair ladder below.
    """
    if protective_kind(exchange_id, MarketType.SPOT) is ProtectiveKind.BYBIT_PAIR:
        return await arm_bybit_protective(
            client,
            rules,
            symbol,
            quantity,
            spec,
            last_price,
            entry,
            group_cid=group_cid,
            pair_leg_cids=pair_leg_cids,
            fallback_cids=fallback_cids,
        )
    from hydra.execution.exchange_stops import arm_exchange_stop

    return await arm_exchange_stop(client, rules, symbol, quantity, spec, last_price, entry)


async def cancel_protective_stop(
    exchange_id: str, client: Any, symbol: str, placement: ExchangeStopPlacement
) -> dict[str, Any]:
    """Cancel a resting protective group on its venue: the whole OCO list on binance,
    every leg of the pair on bybit (by exchange id when acked, by link id when not)."""
    if placement.kind == "pair":
        for leg_id in placement.leg_ids:
            await cancel_bybit_leg(client, leg_id, symbol)
        return {"id": str(placement.order_id), "status": "CANCELLED"}
    from hydra.execution.exchange_stops import cancel_exchange_stop

    return await cancel_exchange_stop(client, symbol, placement)


async def verify_protective_stop(
    exchange_id: str,
    client: Any,
    symbol: str,
    expected: ExpectedStop | None = None,
) -> ExchangeStopState:
    """The A8-live read on its venue: the open-order-lists match on binance, the leg
    match on bybit."""
    if protective_kind(exchange_id, MarketType.SPOT) is ProtectiveKind.BYBIT_PAIR:
        return await verify_bybit_pair(client, symbol, expected)
    from hydra.execution.exchange_stops import verify_exchange_stop

    return await verify_exchange_stop(client, symbol, expected)


def normalize_open_orders(
    exchange_id: str, raw: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Exchange legs -> the ONE paper open-order shape, on the leg's own venue."""
    if protective_kind(exchange_id, MarketType.SPOT) is ProtectiveKind.BYBIT_PAIR:
        return [normalize_bybit_leg(order) for order in raw]
    from hydra.execution.exchange_stops import normalize_open_orders as _binance

    return _binance(raw)


# ---------------------------------------------------------------------------
# The bybit pair ladder
# ---------------------------------------------------------------------------


async def arm_bybit_protective(
    client: BybitProtectiveClient,
    rules: MarketRules,
    symbol: str,
    quantity: Decimal,
    spec: ProtectionSpec,
    last_price: Decimal,
    entry: Decimal,
    *,
    group_cid: str,
    pair_leg_cids: tuple[str, ...],
    fallback_cids: tuple[str, ...],
) -> ExchangeStopPlacement:
    """The bybit ladder: the TP+SL conditional pair first (SL leg placed first), then
    the SL-only conditional at the strategy's stop, then the 5 % default.

    Every rung is tried only after the previous one was definitively rejected
    (``ccxt.ExchangeError``); an unknown outcome propagates (R18) — the SL leg that
    already rests is resolved by the poller through the pair's leg ids, so no rung ever
    runs over an unknown. A trailing spec goes straight to the fixed stop (the registry
    carries no percent trail for bybit — ``createTrailingAmountOrder`` is amount-based,
    and no bounds are invented). A definitive rejection of the last rung raises
    ``StopUnplaceableError`` — the wiring's decision-17 market-close.
    """
    steps: list[str] = []
    qty = format_qty(rules, quantity)
    if Decimal(qty) <= 0:
        raise StopUnplaceableError(
            f"{symbol}: quantity {quantity} floors to zero at lot step {rules.amount_step}"
        )
    if spec.trailing_pct is not None:
        steps.append(
            f"bybit has no percent trailing (asked {spec.trailing_pct}); "
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
    placed: dict[str, Any] | None = None
    if take_profit is not None and pair_leg_cids:
        sl_cid, tp_cid = pair_leg_cids[0], pair_leg_cids[-1]
        sl_leg = await _place_bybit_leg(
            client, symbol, Decimal(qty), stop_loss, sl_cid, "stopLossPrice", steps
        )
        if sl_leg is not None:
            tp_leg = await _place_bybit_leg(
                client, symbol, Decimal(qty), take_profit, tp_cid, "takeProfitPrice", steps
            )
            if tp_leg is None:
                # the stop rests alone: unwind it and let the rungs re-place protection
                await _cancel_bybit_leg_quietly(client, sl_cid, symbol)
            else:
                placed = {
                    "kind": "pair",
                    "order_id": group_cid,
                    "leg_ids": (str(sl_leg["id"]), str(tp_leg["id"])),
                    "stop_price": stop_loss,
                    "take_profit": take_profit,
                }
    if placed is None:
        placed = await _place_bybit_rung(
            client, symbol, Decimal(qty), stop_loss, fallback_cids[0], steps
        )
    if placed is None:
        default_sl = _round_price(rules, _default_stop_price(entry), "sl")
        _require_below_last(default_sl, last_price, symbol)
        placed = await _place_bybit_rung(
            client, symbol, Decimal(qty), default_sl, fallback_cids[-1], steps
        )
    if placed is None:
        raise StopUnplaceableError(f"{symbol}: every protective form rejected: " + "; ".join(steps))
    return ExchangeStopPlacement(
        order_id=str(placed["order_id"]),
        kind=placed["kind"],
        leg_ids=tuple(placed["leg_ids"]),
        quantity=Decimal(qty),
        stop_price=placed["stop_price"],
        take_profit=placed.get("take_profit"),
    )


async def _place_bybit_rung(
    client: BybitProtectiveClient,
    symbol: str,
    quantity: Decimal,
    stop_price: Decimal,
    leg_cid: str,
    steps: list[str],
) -> dict[str, Any] | None:
    """One SL-only conditional rung; None after a definitive rejection."""
    leg = await _place_bybit_leg(
        client, symbol, quantity, stop_price, leg_cid, "stopLossPrice", steps
    )
    if leg is None:
        return None
    return {
        "kind": "stop",
        "order_id": str(leg["id"]),
        "leg_ids": (str(leg["id"]),),
        "stop_price": stop_price,
        "take_profit": None,
    }


async def _place_bybit_leg(
    client: BybitProtectiveClient,
    symbol: str,
    quantity: Decimal,
    trigger: Decimal,
    leg_cid: str,
    trigger_param: Literal["stopLossPrice", "takeProfitPrice"],
    steps: list[str],
) -> dict[str, Any] | None:
    """Place one conditional leg; None records a definitive rejection in *steps*.

    A network error or timeout propagates — the outcome is unknown and the leg may
    rest, so the ladder never runs over it (R18).
    """
    try:
        resp = await client.create_order(
            symbol,
            "SELL",
            "market",
            quantity,
            None,
            None,
            {"clientOrderId": leg_cid, trigger_param: format(trigger, "f")},
        )
    except Exception as exc:
        if not _definitive_rejection(exc):
            raise
        steps.append(f"the {trigger_param} leg was rejected ({exc})")
        return None
    return {"id": str(resp["id"])}


# ---------------------------------------------------------------------------
# The bybit leg reads, cancels and normalization
# ---------------------------------------------------------------------------


async def fetch_bybit_leg(
    client: BybitProtectiveClient, leg_id: str, symbol: str
) -> dict[str, Any]:
    """One bybit leg by its client id or exchange id: the tpsl namespace first, then the
    regular one a triggered order graduates to.

    *leg_id* is a hydra client id (``hyd-``) or an exchange order id; a client id goes
    out as ``orderLinkId`` alone — never a bogus ``orderId`` on the wire.
    """
    by_link = leg_id.startswith("hyd-")
    order_id = None if by_link else leg_id
    params: dict[str, Any] = {"acknowledged": True}
    if by_link:
        params["orderLinkId"] = leg_id
    try:
        return await client.fetch_order(order_id, symbol, {**params, "orderFilter": "tpslOrder"})
    except Exception:
        return await client.fetch_order(order_id, symbol, {**params, "orderFilter": "Order"})


async def cancel_bybit_leg(
    client: BybitProtectiveClient, leg_id: str, symbol: str
) -> dict[str, Any]:
    """Cancel one leg through the ``tpslOrder`` filter it rests behind: by exchange id
    when it acked, else by ``orderLinkId`` alone — never a bogus ``orderId``."""
    if leg_id.startswith("hyd-"):
        return await client.cancel_order(
            None, symbol, {"orderLinkId": leg_id, "orderFilter": "tpslOrder"}
        )
    return await client.cancel_order(leg_id, symbol, {"orderFilter": "tpslOrder"})


async def _cancel_bybit_leg_quietly(
    client: BybitProtectiveClient, leg_cid: str, symbol: str
) -> None:
    """Unwind one leg of the pair; a leg that never landed answers not-found, quietly."""
    try:
        await cancel_bybit_leg(client, leg_cid, symbol)
    except Exception:
        logger.warning("Unwinding bybit leg %s failed", leg_cid, exc_info=True)


def _bybit_trigger(order: Mapping[str, Any]) -> tuple[str, Decimal | None]:
    """The leg's protective role and trigger from ccxt's unified order (the bybit raw
    fields are the fallback): a SELL stop leg carries ``stopLossPrice``, a SELL
    take-profit leg ``takeProfitPrice``."""
    info = order.get("info")
    raw: Mapping[str, Any] = info if isinstance(info, Mapping) else {}
    stop = order.get("stopLossPrice", raw.get("stopLoss"))
    take = order.get("takeProfitPrice", raw.get("takeProfit"))
    if stop not in (None, "", 0, "0"):
        return "stop_loss", Decimal(str(stop))
    if take not in (None, "", 0, "0"):
        return "take_profit", Decimal(str(take))
    trigger = order.get("triggerPrice", raw.get("triggerPrice"))
    if trigger not in (None, "", 0, "0"):
        return "stop_loss", Decimal(str(trigger))
    return "", None


def normalize_bybit_leg(order: Mapping[str, Any]) -> dict[str, Any]:
    """One bybit unified order -> the ONE paper open-order shape.

    The exchange's decimal strings (amount, price, trigger) become exact Decimals; a
    take-profit carries its trigger in ``stop_price`` like the stop (the perp
    convention); ``oco_id`` is always None — bybit has no list orders.
    """
    reason, trigger = _bybit_trigger(order)
    if reason == "stop_loss":
        order_type: OrderType = OrderType.STOP_MARKET
    elif reason == "take_profit":
        order_type = OrderType.TAKE_PROFIT_MARKET
    elif str(order.get("type") or "").lower() == "market":
        order_type = OrderType.MARKET
    else:
        order_type = OrderType.LIMIT
    price = order.get("price")
    amount = order.get("amount") or 0
    return {
        "id": str(order.get("id") or ""),
        "symbol": str(order.get("symbol") or ""),
        "side": str(order.get("side") or "").upper(),
        "type": order_type,
        "amount": Decimal(str(amount)),
        "price": None if price in (None, "", 0, "0") else Decimal(str(price)),
        "stop_price": trigger,
        "status": str(order.get("status") or "").upper(),
        "filled": False,
        "reason": reason or ("market" if order_type is OrderType.MARKET else "limit"),
        "oco_id": None,
        "trail_pct": None,
        "client_order_id": (
            None if order.get("clientOrderId") in (None, "") else str(order.get("clientOrderId"))
        ),
        "reduce_only": bool(order.get("reduceOnly")),
    }


async def bybit_resting_legs(client: BybitProtectiveClient, symbol: str) -> list[dict[str, Any]]:
    """The venue's resting conditional legs on *symbol*, normalized (the tpsl filter)."""
    return [
        normalize_bybit_leg(o) for o in await client.fetch_open_orders(symbol, conditional=True)
    ]


async def verify_bybit_pair(
    client: BybitProtectiveClient, symbol: str, expected: ExpectedStop | None = None
) -> ExchangeStopState:
    """Is the bybit protective pair resting on the exchange for *symbol* right now?

    With an expectation (a running session's armed group) every expected leg must rest,
    matched by exchange id, client id or leg id; without one (a stopped session) any
    resting reduce-only leg of hydra's own ``hyd-`` client ids counts. A leg that is
    resting-but-triggered still protects (it fires at the market), so both the
    conditional and the regular namespace are read. Read errors answer ``unknown``
    with the error, never ``missing``.
    """
    checked_at = datetime.now(UTC)
    try:
        conditional = await bybit_resting_legs(client, symbol)
        regular = normalize_open_orders("bybit", await client.fetch_open_orders(symbol))
    except Exception as exc:
        return ExchangeStopState(state="unknown", checked_at=checked_at, error=str(exc))
    legs = conditional + regular
    if expected is None:
        matched = [
            leg
            for leg in legs
            if leg["reduce_only"]
            and (leg["client_order_id"] or "").startswith("hyd-")
            and leg["side"] == "SELL"
            and leg["type"] in (*_STOP_TYPES, OrderType.TAKE_PROFIT_MARKET)
        ]
        if not matched:
            return ExchangeStopState(state="missing", checked_at=checked_at)
        stop = next((leg["stop_price"] for leg in matched if leg["reason"] == "stop_loss"), None)
        return ExchangeStopState(
            state="confirmed",
            checked_at=checked_at,
            kind="oco" if len(matched) > 1 else "stop",
            stop_price=None if stop is None else float(stop),
        )
    expected_id = None if expected is None else str(expected.order_id)
    expected_legs = set() if expected is None else {str(leg) for leg in expected.leg_ids}
    matched = [
        leg
        for leg in legs
        if (
            expected_id is not None
            and (
                leg["id"] == expected_id
                or (leg["client_order_id"] or "") == expected_id
                or (leg["client_order_id"] or "") in expected_legs
                or leg["id"] in expected_legs
            )
        )
    ]
    if not matched:
        return ExchangeStopState(state="missing", checked_at=checked_at)
    stop = next((leg["stop_price"] for leg in matched if leg["reason"] == "stop_loss"), None)
    take_profit = next(
        (leg["stop_price"] for leg in matched if leg["reason"] == "take_profit"), None
    )
    return ExchangeStopState(
        state="confirmed",
        checked_at=checked_at,
        kind="oco" if take_profit is not None else "stop",
        stop_price=None if stop is None else float(stop),
        take_profit=None if take_profit is None else float(take_profit),
        order_list_id=expected_id,
    )
