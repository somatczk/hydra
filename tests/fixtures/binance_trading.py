"""A scriptable in-memory Binance SPOT account speaking ccxt shapes.

This is the ONLY exchange boundary the live tests see: no keys, no network, no keyed client
construction. MARKET orders fill immediately against the settable last price; LIMIT orders
and STOP_LOSS/trailing legs rest until a test fills them (``fill_order``); the OCO list
answers the normalized paper shape ``ExchangeClient.create_oco`` returns and models the REAL
endpoint's client-id contract (``listClientOrderId`` + ``aboveClientOrderId`` /
``belowClientOrderId``; the list id and the order ids are distinct spaces, and ``fetch_order``
resolves order ids — legs included — never a list id), so CI catches wire-contract drift.
Failure modes are scriptable per call: ``timeout_after_land`` creates (and fills) the order
but raises ``ccxt.RequestTimeout`` anyway — the critical mid-submit case — plus
``timeout_not_landed``, ``insufficient_funds`` (-2010), ``invalid_order`` (-1013) and
``rate_limit`` (ccxt ``DDoSProtection``). Every method answers the same unified-plus-``info``
dicts ccxt 4.5.88 would, so ``normalize_open_orders`` and the poller read real shapes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt

_D = Decimal


def _raw_fee(commission: Decimal, asset: str) -> dict[str, Any]:
    return {"cost": commission, "currency": asset}


class FakeBinanceTradingClient:
    """One Binance SPOT account in memory; the wire truth lives in ``orders``/``trades``."""

    def __init__(
        self,
        *,
        balances: dict[str, Decimal] | None = None,
        last_price: str = "100000",
    ) -> None:
        self.orders: dict[int, dict[str, Any]] = {}
        self.lists: dict[int, list[int]] = {}
        self.list_client_ids: dict[int, str] = {}
        self.trades: list[dict[str, Any]] = []
        self.balances: dict[str, Decimal] = dict(balances or {"USDT": _D("10000")})
        self.locked: dict[str, Decimal] = {}  # resting LIMIT locks: free drops, total keeps
        self.last_prices: dict[str, Decimal] = {
            "BTCUSDT": _D(last_price),
            "ETHUSDT": _D("1000"),
        }
        self.commission_rate = _D("0.001")
        # per-trade overrides; None means the natural commission (base on BUY, quote on SELL)
        self.commission_asset: str | None = None
        self.commission_amount: Decimal | None = None
        self.fail_create: str | None = None
        self.cancel_all_calls: list[str] = []
        self.closed = False
        self._next_order_id = 100
        self._next_trade_id = 1
        self._next_list_id = 1

    # -- helpers --------------------------------------------------------------

    def set_mark(self, symbol: str, price: str) -> None:
        self.last_prices[symbol] = _D(price)

    def _order(self, order_id: int) -> dict[str, Any] | None:
        return self.orders.get(order_id)

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        for order in self.orders.values():
            if str(order["clientOrderId"]) == client_order_id:
                return order
        for legs in self.lists.values():
            for leg_id in legs:
                if str(self.orders[leg_id]["clientOrderId"]) == client_order_id:
                    return self.orders[leg_id]
        return None

    def open_orders_raw(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            o
            for o in self.orders.values()
            if o["status"] in ("NEW", "PARTIALLY_FILLED")
            and (symbol is None or o["symbol"] == symbol)
        ]

    async def fill_order(
        self, order_id: int, price: str | None = None, quantity: str | None = None
    ) -> dict[str, Any]:
        """Fill a resting order at *price* (the last price by default): the while-down case."""
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
        order["time"] = 1786000000000 + self._next_trade_id
        order["executedQty"] = str(_D(order["executedQty"]) + quantity)
        order["cummulativeQuoteQty"] = str(_D(order["cummulativeQuoteQty"]) + price * quantity)
        if _D(order["executedQty"]) >= _D(order["origQty"]):
            order["status"] = "FILLED"
        elif _D(order["executedQty"]) > 0:
            order["status"] = "PARTIALLY_FILLED"
        return self._record_trade(order, price, quantity)

    def _record_trade(
        self, order: dict[str, Any], price: Decimal, quantity: Decimal
    ) -> dict[str, Any]:
        trade_id = self._next_trade_id
        self._next_trade_id += 1
        base = str(order["symbol"]).removesuffix("USDT")
        if self.commission_asset is not None:
            asset = self.commission_asset
            amount = (
                self.commission_amount
                if self.commission_amount is not None
                else quantity * self.commission_rate
            )
        elif order["side"] == "BUY":
            asset, amount = base, quantity * self.commission_rate
        else:
            asset, amount = "USDT", quantity * price * self.commission_rate
        raw = {
            "symbol": order["symbol"],
            "id": trade_id,
            "orderId": order["orderId"],
            "orderListId": order.get("orderListId", -1),
            "price": format(price, "f"),
            "qty": format(quantity, "f"),
            "quoteQty": format(price * quantity, "f"),
            "commission": format(amount, "f"),
            "commissionAsset": asset,
            "time": order["time"],
            "isBuyer": order["side"] == "BUY",
            "isMaker": order["type"] == "LIMIT_MAKER",
            "isBestMatch": True,
        }
        self.trades.append(raw)
        self._move_balances(order, price, quantity, _D(raw["commission"]), asset)
        self._release_lock(order, quantity)
        return raw

    def _move_balances(
        self, order: dict[str, Any], price: Decimal, quantity: Decimal, fee: Decimal, asset: str
    ) -> None:
        base = str(order["symbol"]).removesuffix("USDT")
        notional = price * quantity
        if order["side"] == "BUY":
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
        self._normalize_balances()

    def _normalize_balances(self) -> None:
        for asset in list(self.balances):
            if self.balances[asset] <= 0:
                del self.balances[asset]

    def _lock_order(self, raw: dict[str, Any]) -> None:
        """A resting LIMIT locks its funds the way Binance does (free drops, total keeps)."""
        base = str(raw["symbol"]).removesuffix("USDT")
        if raw["side"] == "BUY":
            asset, amount = "USDT", _D(raw["price"]) * _D(raw["origQty"])
        else:
            asset, amount = base, _D(raw["origQty"])
        self.locked[asset] = self.locked.get(asset, _D("0")) + amount

    def _release_lock(self, raw: dict[str, Any], quantity: Decimal) -> None:
        if raw["type"] != "LIMIT" or quantity <= 0:
            return
        base = str(raw["symbol"]).removesuffix("USDT")
        if raw["side"] == "BUY":
            asset, amount = "USDT", _D(raw["price"]) * quantity
        else:
            asset, amount = base, quantity
        remaining = self.locked.get(asset, _D("0")) - amount
        if remaining > 0:
            self.locked[asset] = remaining
        else:
            self.locked.pop(asset, None)

    def _new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        client_order_id: str,
        order_list_id: int = -1,
        trailing_delta: int | None = None,
    ) -> dict[str, Any]:
        order_id = self._next_order_id
        self._next_order_id += 1
        raw = {
            "symbol": symbol,
            "orderId": order_id,
            "orderListId": order_list_id,
            "clientOrderId": client_order_id,
            "price": None if price is None else format(price, "f"),
            "origQty": format(quantity, "f"),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "type": order_type,
            "side": side,
            "stopPrice": None if stop_price is None else format(stop_price, "f"),
            "time": 1786000000000 + order_id,
            "updateTime": 1786000000000 + order_id,
        }
        if trailing_delta is not None:
            raw["trailingDelta"] = str(trailing_delta)
        self.orders[order_id] = raw
        return raw

    @staticmethod
    def _unified(raw: dict[str, Any]) -> dict[str, Any]:
        executed = _D(raw["executedQty"])
        status = {"FILLED": "closed", "CANCELED": "canceled"}.get(raw["status"], "open")
        average = (
            _D(raw["cummulativeQuoteQty"]) / executed if executed > 0 and raw["price"] else None
        )
        return {
            "id": str(raw["orderId"]),
            "clientOrderId": raw["clientOrderId"],
            "symbol": raw["symbol"],
            "side": raw["side"].lower(),
            "type": raw["type"].lower(),
            "price": raw["price"],
            "amount": raw["origQty"],
            "status": status,
            "filled": raw["executedQty"],
            "average": None if average is None else format(average, "f"),
            "timestamp": raw["time"],
            "stopPrice": raw.get("stopPrice"),
            "info": dict(raw),
        }

    def _check_wallet(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        base = symbol.removesuffix("USDT")
        if side == "BUY":
            if self.balances.get("USDT", _D("0")) < quantity * price:
                raise ccxt.InsufficientFunds("binance {-2010} Account has insufficient balance")
        elif self.balances.get(base, _D("0")) < quantity:
            raise ccxt.InsufficientFunds("binance {-2010} Account has insufficient balance")

    # -- the ExchangeClient surface -------------------------------------------

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        mode, self.fail_create = self.fail_create, None
        quantity = _D(str(quantity))
        if mode == "timeout_not_landed":
            raise ccxt.RequestTimeout("binance POST /api/v3/order request timed out")
        if mode == "insufficient_funds":
            raise ccxt.InsufficientFunds("binance {-2010} Account has insufficient balance")
        if mode == "invalid_order":
            raise ccxt.InvalidOrder("binance {-1013} Filter failure: MIN_NOTIONAL / LOT_SIZE")
        if mode == "rate_limit":
            raise ccxt.DDoSProtection("binance {418} IP has been auto-banned")
        price = None if price is None else _D(str(price))
        stop_price = None if stop_price is None else _D(str(stop_price))
        cid = str(params.get("newClientOrderId") or f"auto-{self._next_order_id}")
        wire_type = str(order_type).upper()
        if wire_type == "MARKET" and params.get("stopLossPrice"):
            wire_type = "STOP_LOSS"
            stop_price = _D(str(params["stopLossPrice"]))
        if wire_type == "MARKET" and params.get("trailingDelta"):
            wire_type = "STOP_LOSS"
            stop_price = self.last_prices[symbol] - _D(str(params["trailingDelta"])) * _D(
                self.last_prices[symbol]
            ) / _D("10000")
        self._check_wallet(symbol, side.upper(), quantity, price or self.last_prices[symbol])
        raw = self._new_order(
            symbol,
            side.upper(),
            wire_type,
            quantity,
            price,
            stop_price,
            cid,
            trailing_delta=params.get("trailingDelta"),
        )
        if wire_type == "LIMIT":
            self._lock_order(raw)
        if mode == "timeout_after_land":
            # the request landed and filled, but the answer never came back
            self._apply_fill(raw, self.last_prices[symbol], quantity)
            raise ccxt.RequestTimeout("binance POST /api/v3/order request timed out")
        if wire_type == "MARKET":
            self._apply_fill(raw, self.last_prices[symbol], quantity)
        return self._unified(raw)

    async def create_oco(
        self,
        symbol: str,
        side: str,
        quantity: str,
        take_profit: str,
        stop_loss: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v3/orderList/oco with the REAL client-id params: the list id and the
        two legs each carry the caller's explicit ids (listClientOrderId /
        aboveClientOrderId / belowClientOrderId); anything else is auto-generated the way
        Binance would, so a wire-contract drift fails the poller's list lookup in CI."""
        mode, self.fail_create = self.fail_create, None
        if mode == "timeout_not_landed":
            raise ccxt.RequestTimeout("binance POST /api/v3/orderList/oco request timed out")
        if mode == "insufficient_funds":
            raise ccxt.InsufficientFunds("binance {-2010} Account has insufficient balance")
        if mode == "rate_limit":
            raise ccxt.DDoSProtection("binance {418} IP has been auto-banned")
        list_id = self._next_list_id
        self._next_list_id += 1
        params = dict(params or {})
        cid = str(params.get("listClientOrderId") or f"auto-list-{list_id}")
        above_cid = str(params.get("aboveClientOrderId") or f"auto-above-{list_id}")
        below_cid = str(params.get("belowClientOrderId") or f"auto-below-{list_id}")
        qty = _D(quantity)
        self._check_wallet(symbol, side.upper(), qty, _D(stop_loss))
        above = self._new_order(
            symbol,
            side.upper(),
            "LIMIT_MAKER",
            qty,
            _D(take_profit),
            None,
            above_cid,
            list_id,
        )
        below = self._new_order(
            symbol,
            side.upper(),
            "STOP_LOSS",
            qty,
            None,
            _D(stop_loss),
            below_cid,
            list_id,
        )
        self.lists[list_id] = [above["orderId"], below["orderId"]]
        self.list_client_ids[list_id] = cid
        if mode == "timeout_after_land":
            raise ccxt.RequestTimeout("binance POST /api/v3/orderList/oco request timed out")
        return {
            "id": str(list_id),
            "symbol": symbol,
            "side": side.upper(),
            "type": "OCO",
            "amount": _D(quantity),
            "price": _D(take_profit),
            "stop_price": _D(stop_loss),
            "status": "EXEC_STARTED",
            "filled": False,
            "oco_link": str(list_id),
            "client_order_id": cid,
            "legs": [self._oco_leg(above), self._oco_leg(below)],
        }

    @staticmethod
    def _oco_leg(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(raw["orderId"]),
            "symbol": raw["symbol"],
            "side": raw["side"],
            "type": "LIMIT" if raw["type"] == "LIMIT_MAKER" else "STOP_MARKET",
            "amount": _D(raw["origQty"]),
            "price": None if raw["price"] is None else _D(raw["price"]),
            "stop_price": None if raw.get("stopPrice") is None else _D(raw["stopPrice"]),
            "status": "PENDING",
            "filled": False,
            "reason": "take_profit" if raw["type"] == "LIMIT_MAKER" else "stop_loss",
            "oco_id": str(raw["orderListId"]),
            "trail_pct": None,
            "client_order_id": raw["clientOrderId"],
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        raw = self._order_by_any_id(order_id)
        if raw is None or raw["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise ccxt.OrderNotFound("binance {-2011} Unknown order sent")
        raw["status"] = "CANCELED"
        raw["updateTime"] = raw["time"] + 1
        self._release_lock(raw, _D(raw["origQty"]) - _D(raw["executedQty"]))
        return self._unified(raw)

    def _order_by_any_id(self, order_id: str) -> dict[str, Any] | None:
        """An order by numeric orderId or by clientOrderId (DELETE /api/v3/order accepts
        origClientOrderId); a LIST id is never an order id."""
        if str(order_id).isdigit():
            if int(order_id) in self.lists:
                return None
            return self.orders.get(int(order_id))
        return self.order_by_client_id(str(order_id))

    async def cancel_order_list(self, order_list_id: str, symbol: str) -> dict[str, Any]:
        legs = self.lists.get(int(order_list_id))
        if not legs:
            raise ccxt.OrderNotFound("binance {-2011} Unknown order list id")
        for leg_id in legs:
            if self.orders[leg_id]["status"] in ("NEW", "PARTIALLY_FILLED"):
                leg = self.orders[leg_id]
                leg["status"] = "CANCELED"
                self._release_lock(leg, _D(leg["origQty"]) - _D(leg["executedQty"]))
        return {"orderListId": int(order_list_id), "listStatusType": "ALL_DONE"}

    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        self.cancel_all_calls.append(symbol)
        canceled = []
        for raw in self.open_orders_raw(symbol):
            raw["status"] = "CANCELED"
            self._release_lock(raw, _D(raw["origQty"]) - _D(raw["executedQty"]))
            canceled.append(self._unified(raw))
        return canceled

    async def fetch_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        client_id = params.get("clientOrderId") or params.get("origClientOrderId")
        raw = None
        if client_id is not None:
            raw = self.order_by_client_id(str(client_id))
        else:
            raw = self._order_by_any_id(order_id)
        if raw is None:
            raise ccxt.OrderNotFound("binance {-2013} Order does not exist")
        return self._unified(raw)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [self._unified(raw) for raw in self.open_orders_raw(symbol)]

    async def fetch_open_order_lists(self, symbol: str | None = None) -> list[dict[str, Any]]:
        out = []
        for list_id, legs in self.lists.items():
            if symbol is not None and any(self.orders[leg]["symbol"] != symbol for leg in legs):
                continue
            if all(self.orders[leg]["status"] == "CANCELED" for leg in legs):
                continue
            out.append(
                {
                    "orderListId": list_id,
                    "listClientOrderId": self.list_client_ids.get(list_id, f"auto-list-{list_id}"),
                    "contingencyType": "OCO",
                    "symbol": symbol or self.orders[legs[0]]["symbol"],
                    "listStatusType": "EXECUTING",
                }
            )
        return out

    async def fetch_my_trades(self, symbol: str, since: Any = None) -> list[dict[str, Any]]:
        since_ms = None if since is None else int(since.timestamp() * 1000)
        out = []
        for raw in self.trades:
            if raw["symbol"] != symbol:
                continue
            if since_ms is not None and raw["time"] < since_ms:
                continue
            out.append(
                {
                    "id": str(raw["id"]),
                    "order": str(raw["orderId"]),
                    "symbol": raw["symbol"],
                    "side": "buy" if raw["isBuyer"] else "sell",
                    "type": "limit" if raw["isMaker"] else "market",
                    "price": raw["price"],
                    "amount": raw["qty"],
                    "timestamp": raw["time"],
                    "fee": _raw_fee(_D(raw["commission"]), raw["commissionAsset"]),
                    "takerOrMaker": "maker" if raw["isMaker"] else "taker",
                    "info": dict(raw),
                }
            )
        return out

    async def fetch_balance(self) -> dict[str, Decimal]:
        free = {
            asset: amount - self.locked.get(asset, _D("0"))
            for asset, amount in self.balances.items()
            if amount - self.locked.get(asset, _D("0")) > 0
        }
        return free

    async def fetch_balance_totals(self) -> dict[str, Decimal]:
        return {asset: amount for asset, amount in self.balances.items() if amount > 0}

    async def close(self) -> None:
        self.closed = True
