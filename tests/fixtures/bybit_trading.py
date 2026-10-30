"""A scriptable in-memory Bybit SPOT account speaking ccxt's unified shapes.

This is the ONLY exchange boundary the bybit live tests see: no keys, no network, no
keyed client construction. The fake stands where the real ccxt.bybit instance stands
(behind ``ExchangeClient``), so it answers the unified order/trade/balance structures
ccxt 4.5.88 parses — and it OWNS the wire contract the bybit pair is built on until the
on-box testnet battery runs:

* ``create_order`` with ``params.stopLossPrice``/``params.takeProfitPrice`` places a
  resting conditional leg behind the ``tpslOrder`` filter (ccxt's ``create_order_request``
  derives that filter for spot + a trigger price), the client id riding ``orderLinkId``
  (ccxt's ``clientOrderId``); a plain MARKET fills immediately against the settable last
  price and a LIMIT rests until a test fills it (``fill_order``). The CREATE response is
  the real stub — ``{orderId, orderLinkId}``, no status — exactly what bybit answers and
  what ccxt's ``parse_order`` turns into ``status: None``; the fill itself arrives
  through ``fetch_my_trades``, never the create response.
* A resting tpsl leg keeps its ``orderId`` through the trigger (``fill_order`` executes
  it and emits the trade under the same id) — the fill pipeline maps it by exchange id
  or by the order's client id.
* ``fetch_open_orders`` honours the ``orderFilter`` param: ``tpslOrder`` reads the
  conditional namespace, the plain read the regular orders — the two namespaces are
  separate, exactly as the drift pass and the verify read assume.
* ``fetch_order`` answers by ``orderId`` or, with ``params.orderLinkId``, by client id
  alone — the primitive ``venue_stops.fetch_bybit_leg`` rides.
* ``cancel_order``/``cancel_all_orders`` cancel through the named ``orderFilter`` only:
  a tpsl leg is invisible to the plain sweep, so the missing second sweep is caught.

Failure modes are scriptable per call, mirroring the Binance fake: ``timeout_after_land``
creates (and fills) the order but raises ``ccxt.RequestTimeout`` anyway — the critical
mid-submit case — plus ``timeout_not_landed``, ``insufficient_funds`` and
``invalid_order`` (bybit's own retCode shapes). ``privateGetV5UserQueryApi`` answers the
key-info document (the Settings connect and the R8 arm re-fetch read).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt

_D = Decimal

TPSL_FILTER = "tpslOrder"


def _raw_fee(commission: Decimal, asset: str) -> dict[str, Any]:
    return {"cost": commission, "currency": asset}


class FakeBybitTradingClient:
    """One Bybit SPOT account in memory; the wire truth lives in ``orders``/``trades``."""

    def __init__(
        self,
        *,
        balances: dict[str, Decimal] | None = None,
        last_price: str = "100000",
    ) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.balances: dict[str, Decimal] = dict(balances or {"USDT": _D("10000")})
        self.last_prices: dict[str, Decimal] = {
            "BTCUSDT": _D(last_price),
            "ETHUSDT": _D("1000"),
        }
        self.commission_rate = _D("0.001")
        self.fail_create: str | None = None
        self.cancel_all_calls: list[tuple[str, str | None]] = []
        self.closed = False
        self.key_info: dict[str, Any] = {
            "retCode": 0,
            "retMsg": "",
            "result": {
                "id": "42",
                "note": "hydra",
                "readOnly": 0,
                "permissions": {
                    "ContractTrade": [],
                    "Spot": ["SpotTrade"],
                    "Wallet": ["AccountTransfer"],
                },
                "ips": [],
            },
        }
        self._next_order_id = 100
        self._next_trade_id = 1

    # -- helpers --------------------------------------------------------------

    def set_mark(self, symbol: str, price: str) -> None:
        self.last_prices[symbol] = _D(price)

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return next(
            (o for o in self.orders.values() if o["clientOrderId"] == client_order_id), None
        )

    def _open_raw(self, symbol: str | None, order_filter: str) -> list[dict[str, Any]]:
        """The resting orders of one namespace: the two filters never see each other."""
        return [
            o
            for o in self.orders.values()
            if o["status"] == "New"
            and o["orderFilter"] == order_filter
            and (symbol is None or o["symbol"] == symbol)
        ]

    async def fill_order(
        self, order_id: str, price: str | None = None, quantity: str | None = None
    ) -> dict[str, Any]:
        """Execute a resting order at *price* (the last price by default): a tpsl leg
        triggered by the market, or a resting LIMIT crossing."""
        order = self.orders[order_id]
        fill_price = _D(price) if price is not None else self.last_prices[order["symbol"]]
        qty = (
            _D(quantity)
            if quantity is not None
            else _D(order["origQty"]) - _D(order["executedQty"])
        )
        return self._apply_fill(order, fill_price, qty)

    def _apply_fill(
        self, order: dict[str, Any], price: Decimal, quantity: Decimal
    ) -> dict[str, Any]:
        order["executedQty"] = format(_D(order["executedQty"]) + quantity, "f")
        order["cumExecValue"] = format(_D(order["cumExecValue"]) + price * quantity, "f")
        if _D(order["executedQty"]) >= _D(order["origQty"]):
            order["status"] = "Filled"
        elif _D(order["executedQty"]) > 0:
            order["status"] = "PartiallyFilled"
        return self._record_trade(order, price, quantity)

    def _record_trade(
        self, order: dict[str, Any], price: Decimal, quantity: Decimal
    ) -> dict[str, Any]:
        trade_id = str(self._next_trade_id)
        self._next_trade_id += 1
        base = str(order["symbol"]).removesuffix("USDT")
        if order["side"] == "Buy":
            asset, amount = base, quantity * self.commission_rate
        else:
            asset, amount = "USDT", quantity * price * self.commission_rate
        raw = {
            "symbol": order["symbol"],
            "id": trade_id,
            "orderId": order["orderId"],
            "orderLinkId": order["clientOrderId"],
            "orderFilter": order["orderFilter"],
            "price": format(price, "f"),
            "qty": format(quantity, "f"),
            "execQty": format(quantity, "f"),
            "execFee": format(amount, "f"),
            "feeCurrency": asset,
            "side": order["side"],
            "orderPrice": order.get("triggerPrice"),
            "createTime": str(1786000000000 + self._next_trade_id),
            "isBuyer": order["side"] == "Buy",
            "isMaker": False,
        }
        self.trades.append(raw)
        self._move_balances(order, price, quantity, amount, asset)
        return raw

    def _move_balances(
        self, order: dict[str, Any], price: Decimal, quantity: Decimal, fee: Decimal, asset: str
    ) -> None:
        base = str(order["symbol"]).removesuffix("USDT")
        notional = price * quantity
        if order["side"] == "Buy":
            self.balances["USDT"] = self.balances.get("USDT", _D("0")) - notional
            if asset == base:
                self.balances[base] = self.balances.get(base, _D("0")) + quantity - fee
            else:
                self.balances[base] = self.balances.get(base, _D("0")) + quantity
                self.balances[asset] = self.balances.get(asset, _D("0")) - fee
        else:
            if asset == base:
                self.balances[base] = self.balances.get(base, _D("0")) - quantity - fee
            else:
                self.balances[base] = self.balances.get(base, _D("0")) - quantity
                self.balances[asset] = self.balances.get(asset, _D("0")) - fee
            self.balances["USDT"] = self.balances.get("USDT", _D("0")) + notional
        for held in list(self.balances):
            if self.balances[held] <= 0:
                del self.balances[held]

    def _new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        trigger_price: Decimal | None,
        client_order_id: str,
        order_filter: str,
        trigger_direction: int | None = None,
    ) -> dict[str, Any]:
        order_id = f"bybit-{self._next_order_id}"
        self._next_order_id += 1
        raw = {
            "symbol": symbol,
            "orderId": order_id,
            "orderLinkId": client_order_id,
            "orderFilter": order_filter,
            "clientOrderId": client_order_id,
            "orderType": str(order_type).capitalize(),
            "side": side.capitalize(),
            "origQty": format(quantity, "f"),
            "qty": format(quantity, "f"),
            "executedQty": "0",
            "cumExecQty": "0",
            "cumExecValue": "0",
            "avgPrice": "",
            "price": "" if price is None else format(price, "f"),
            "triggerPrice": "" if trigger_price is None else format(trigger_price, "f"),
            "triggerDirection": trigger_direction or 0,
            "reduceOnly": trigger_price is not None,
            "orderStatus": "New",
            "status": "New",
            "createdTime": str(1786000000000 + self._next_order_id),
        }
        self.orders[order_id] = raw
        return raw

    @staticmethod
    def _unified(raw: dict[str, Any]) -> dict[str, Any]:
        """ccxt's ``parse_order`` shape for a bybit v5 order row (the ingest side of the
        boundary): a SELL descending conditional reads ``stopLossPrice``, an ascending one
        ``takeProfitPrice``; the raw row rides in ``info``."""
        status = {"Filled": "closed", "Cancelled": "canceled", "Rejected": "rejected"}.get(
            raw["status"], "open"
        )
        average = _D(raw["avgPrice"]) if raw["avgPrice"] else None
        stop = _D(raw["triggerPrice"]) if raw["triggerPrice"] else None
        side = raw["side"].lower()
        stop_loss: Decimal | None = None
        take_profit: Decimal | None = None
        if stop is not None and raw["reduceOnly"]:
            descending = int(raw.get("triggerDirection") or 0) == 2
            if side == "sell" and descending:
                stop_loss = stop
            elif side == "sell":
                take_profit = stop
            elif side == "buy" and not descending:
                stop_loss = stop
            else:
                take_profit = stop
        return {
            "id": str(raw["orderId"]),
            "clientOrderId": raw["clientOrderId"],
            "symbol": raw["symbol"],
            "side": side,
            "type": raw["orderType"].lower(),
            "price": None if not raw["price"] else raw["price"],
            "amount": raw["origQty"],
            "status": status,
            "filled": raw["executedQty"],
            "average": None if average is None else format(average, "f"),
            "timestamp": int(raw["createdTime"]),
            "triggerPrice": None if stop is None else format(stop, "f"),
            "stopLossPrice": None if stop_loss is None else format(stop_loss, "f"),
            "takeProfitPrice": None if take_profit is None else format(take_profit, "f"),
            "reduceOnly": bool(raw["reduceOnly"]),
            "info": dict(raw),
        }

    def _check_wallet(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        base = symbol.removesuffix("USDT")
        if side == "BUY":
            if self.balances.get("USDT", _D("0")) < quantity * price:
                raise ccxt.InsufficientFunds("bybit {10001}Ab not enough available balance")
        elif self.balances.get(base, _D("0")) < quantity:
            raise ccxt.InsufficientFunds("bybit {10001}Ab not enough available balance")

    # -- the ExchangeClient surface -------------------------------------------
    # ``create_order`` speaks ccxt's keyword convention: the REAL ExchangeClient wraps
    # this fake in every test (``symbol=..., type=..., side=..., amount=..., params=...``).

    async def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: Decimal,
        price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        mode, self.fail_create = self.fail_create, None
        quantity = _D(str(amount))
        if mode == "timeout_not_landed":
            raise ccxt.RequestTimeout("bybit POST /v5/order/create request timed out")
        if mode == "insufficient_funds":
            raise ccxt.InsufficientFunds("bybit {10001}Ab not enough available balance")
        if mode == "invalid_order":
            raise ccxt.InvalidOrder(
                "bybit {10001}s the order qty is invalid or exceeds the max limitation"
            )
        if mode == "rate_limit":
            raise ccxt.DDoSProtection("bybit {10006} too many requests")
        side_u = side.upper()
        cid = str(params.get("clientOrderId") or f"auto-{self._next_order_id}")
        sl = params.get("stopLossPrice")
        tp = params.get("takeProfitPrice")
        if sl is not None or tp is not None:
            # ccxt derives orderFilter 'tpslOrder' for spot + a trigger price, and the
            # triggerDirection from the side (SELL stop descends, SELL take-profit rises)
            descending = sl is not None
            raw = self._new_order(
                symbol,
                side_u,
                "market",
                quantity,
                None,
                _D(str(sl if sl is not None else tp)),
                cid,
                TPSL_FILTER,
                trigger_direction=2 if descending else 1,
            )
        else:
            self._check_wallet(symbol, side_u, quantity, price or self.last_prices[symbol])
            raw = self._new_order(symbol, side_u, type, quantity, price, None, cid, "Order")
            if mode == "timeout_after_land":
                self._apply_fill(raw, self.last_prices[symbol], quantity)
                raise ccxt.RequestTimeout("bybit POST /v5/order/create request timed out")
            if str(type).upper() == "MARKET":
                self._apply_fill(raw, self.last_prices[symbol], quantity)
        # the create answer is the real stub: {orderId, orderLinkId}, no status
        return {
            "id": str(raw["orderId"]),
            "clientOrderId": raw["clientOrderId"],
            "symbol": raw["symbol"],
            "side": raw["side"].lower(),
            "type": raw["orderType"].lower(),
            "status": None,
            "filled": None,
            "info": {"orderId": raw["orderId"], "orderLinkId": raw["clientOrderId"]},
        }

    async def cancel_order(
        self, order_id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        order_filter = str(params.get("orderFilter") or "Order")
        target: dict[str, Any] | None = None
        if params.get("orderLinkId") is not None:
            target = self.order_by_client_id(str(params["orderLinkId"]))
        elif order_id is not None:
            target = self.orders.get(order_id)
        if target is None or target["orderFilter"] != order_filter or target["status"] != "New":
            raise ccxt.OrderNotFound(f"bybit {{10001}}s order not exists: {order_id}")
        target["status"] = "Cancelled"
        return {"id": str(target["orderId"]), "status": "canceled"}

    async def cancel_all_orders(
        self, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        order_filter = str(params.get("orderFilter") or "Order")
        self.cancel_all_calls.append((str(symbol), order_filter))
        cancelled = []
        for order in self._open_raw(symbol, order_filter):
            order["status"] = "Cancelled"
            cancelled.append({"id": str(order["orderId"])})
        return cancelled

    async def fetch_open_orders(
        self,
        symbol: str | None = None,
        since: Any = None,
        limit: Any = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        order_filter = str(params.get("orderFilter") or "Order")
        return [self._unified(o) for o in self._open_raw(symbol, order_filter)]

    async def fetch_order(
        self, order_id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        order_filter = str(params.get("orderFilter") or "Order")
        target: dict[str, Any] | None = None
        if params.get("orderLinkId") is not None:
            target = self.order_by_client_id(str(params["orderLinkId"]))
        elif order_id is not None:
            target = self.orders.get(order_id)
        if target is None or target["symbol"] != symbol or target["orderFilter"] != order_filter:
            raise ccxt.OrderNotFound(f"bybit {{10001}}s order not exists: {order_id}")
        return self._unified(target)

    async def fetch_my_trades(
        self,
        symbol: str,
        since: Any = None,
        limit: Any = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        since_ms = 0 if since is None else int(since)
        out: list[dict[str, Any]] = []
        for raw in self.trades:
            if raw["symbol"] != symbol or int(raw["createTime"]) < since_ms:
                continue
            base = str(raw["symbol"]).removesuffix("USDT")
            out.append(
                {
                    "id": raw["id"],
                    "order": str(raw["orderId"]),
                    "symbol": raw["symbol"],
                    "side": "buy" if raw["isBuyer"] else "sell",
                    "price": raw["price"],
                    "amount": raw["qty"],
                    "timestamp": int(raw["createTime"]),
                    "fee": _raw_fee(_D(raw["execFee"]), base if raw["isBuyer"] else "USDT"),
                    "takerOrMaker": "taker",
                    "info": dict(raw),
                }
            )
        return out

    async def fetch_balance(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        free = {asset: format(amount, "f") for asset, amount in self.balances.items()}
        return {"free": dict(free), "total": dict(free)}

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": float(self.last_prices[symbol])}

    async def close(self) -> None:
        self.closed = True


async def _fake_query_api(
    fake: FakeBybitTradingClient, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return dict(fake.key_info)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBybitTradingClient.privateGetV5UserQueryApi = _fake_query_api
