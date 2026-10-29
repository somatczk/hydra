"""A scriptable in-memory Binance USD⌂-M (binanceusdm) account speaking ccxt shapes.

This is the ONLY exchange boundary the live perp tests see: no keys, no network,
no keyed client construction. MARKET orders fill immediately against the settable
last price; LIMIT orders and the conditional ladder legs rest until a test fires
them. Futures conditional orders live in the fapi ALGO namespace (the endpoint
ccxt 4.5.88 routes them to): a leg rests as ``{algoId, clientAlgoId, algoStatus:
"NEW", ...}`` and ``trigger()`` models what the real wire does — the algo spawns
a REGULAR order that carries the algo's own client id (the fill-attribution
inheritance this executor depends on; the PR body names it as the testnet
battery's first verification item), fills it at the trigger price and records the
``userTrades`` row. Cancels resolve per namespace; ``cancel_all_orders`` hits
both. The wallet tracks isolated margins per position so ``fetch_balance_totals``
answers the margin balance the reconcile compares against, and
``fetch_position_risk`` serves the signed ``positionAmt`` truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import ccxt

_D = Decimal
_ZERO = _D("0")
_TAKER_FEE = _D("0.0004")


@dataclass
class _AlgoOrder:
    algo_id: int
    client_algo_id: str
    order_type: str  # STOP_MARKET | TAKE_PROFIT_MARKET | TRAILING_STOP_MARKET
    symbol: str
    side: str
    quantity: _D
    trigger_price: _D | None
    callback_rate: _D | None
    reduce_only: bool
    algo_status: str = "NEW"  # NEW | TRIGGERED | CANCELED
    executed_qty: _D = _ZERO
    average_price: _D | None = None

    def triggered_now(self) -> int:
        return 1786000000000 + self.algo_id


@dataclass
class _FakePosition:
    qty: _D = _ZERO
    entry: _D = _ZERO
    margin: _D = _ZERO


class FakeBinanceUsdmTradingClient:
    """One binanceusdm account in memory; the wire truth lives in the order tables."""

    def __init__(
        self,
        *,
        wallet: str = "10000",
        last_price: str = "100000",
        dual_side_position: bool = False,
    ) -> None:
        self.wallet = _D(wallet)
        self.available = _D(wallet)
        self.positions: dict[str, _FakePosition] = {}
        self.leverage: dict[str, int] = {}
        self.margin_modes: dict[str, str] = {}
        self.plain_orders: dict[int, dict[str, Any]] = {}
        self.algo_orders: dict[int, _AlgoOrder] = {}
        self.trades: list[dict[str, Any]] = []
        self.income: list[dict[str, Any]] = []
        self.last_prices: dict[str, _D] = {"BTCUSDT": _D(last_price), "ETHUSDT": _D("1000")}
        self.dual_side_position = dual_side_position
        self.set_leverage_calls: list[tuple[str, int]] = []
        self.set_margin_mode_calls: list[tuple[str, str]] = []
        self.set_position_mode_calls: list[bool] = []
        self.cancel_all_calls: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.fail_create: str | None = None
        self._next_order_id = 500
        self._next_algo_id = 900
        self._next_trade_id = 1

    # -- helpers ----------------------------------------------------------

    def set_mark(self, symbol: str, price: str) -> None:
        self.last_prices[symbol] = _D(price)

    def seed_income(self, symbol: str, income: str, time_ms: int, tran_id: str) -> None:
        self.income.append(
            {
                "symbol": symbol,
                "incomeType": "FUNDING_FEE",
                "income": income,
                "asset": "USDT",
                "time": time_ms,
                "tranId": tran_id,
            }
        )

    def algo_by_client_id(self, cid: str) -> _AlgoOrder | None:
        return next((o for o in self.algo_orders.values() if o.client_algo_id == cid), None)

    def _order_by_any_id(self, order_id: str) -> dict[str, Any] | None:
        if str(order_id).isdigit():
            return self.plain_orders.get(int(order_id))
        return next(
            (o for o in self.plain_orders.values() if str(o["clientOrderId"]) == order_id),
            None,
        )

    def open_plain(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            o
            for o in self.plain_orders.values()
            if o["status"] in ("NEW", "PARTIALLY_FILLED")
            and (symbol is None or o["symbol"] == symbol)
        ]

    def open_algo(self, symbol: str | None = None) -> list[_AlgoOrder]:
        return [
            o
            for o in self.algo_orders.values()
            if o.algo_status == "NEW" and (symbol is None or o.symbol == symbol)
        ]

    def _apply_fill(
        self, symbol: str, side: str, quantity: _D, price: _D, order: dict[str, Any]
    ) -> None:
        """Move the account: signed position, isolated margin, USDT wallet, fees."""
        pos = self.positions.setdefault(symbol, _FakePosition())
        notional = quantity * price
        fee = notional * _TAKER_FEE
        signed = quantity if side == "BUY" else -quantity
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            total = abs(pos.qty) + quantity
            pos.entry = (abs(pos.qty) * pos.entry + notional) / total
            pos.qty += signed
            margin = notional / self.leverage.get(symbol, 1)
            pos.margin += margin
            self.available -= margin
        else:
            reduce = min(quantity, abs(pos.qty))
            share = reduce / abs(pos.qty)
            direction = _D("1") if pos.qty > 0 else _D("-1")
            realized = reduce * (price - pos.entry) * direction
            pos.qty += signed
            released = share * pos.margin
            pos.margin -= released
            self.wallet += realized
            self.available += released + realized
            if pos.qty == 0:
                pos.entry = _ZERO
        self.wallet -= fee
        self.available -= fee
        if pos.qty != 0:
            self.positions[symbol] = pos
        order["executedQty"] = str(_D(order["executedQty"]) + quantity)
        order["status"] = "FILLED"
        order["updateTime"] = order["time"] + self._next_trade_id
        self.trades.append(
            {
                "symbol": symbol,
                "id": self._next_trade_id,
                "orderId": order["orderId"],
                "price": format(price, "f"),
                "qty": format(quantity, "f"),
                "quoteQty": format(notional, "f"),
                "commission": format(fee, "f"),
                "commissionAsset": "USDT",
                "realizedPnl": "0",
                "side": side,
                "time": order["updateTime"],
                "buyer": side == "BUY",
                "maker": False,
                "positionSide": "BOTH",
            }
        )
        self._next_trade_id += 1

    def trigger(self, client_algo_id: str, price: str | None = None) -> dict[str, Any]:
        """Fire a resting conditional leg: spawn + fill its real order at *price*.

        The spawned order carries the algo's own client id — the inheritance the
        fill pipeline attributes leg fills by (the testnet battery's first item).
        """
        algo = self.algo_by_client_id(client_algo_id)
        assert algo is not None, f"no resting algo order with client id {client_algo_id}"
        assert algo.algo_status == "NEW"
        fill_price = _D(price) if price is not None else self.last_prices[algo.symbol]
        order_id = self._next_order_id
        self._next_order_id += 1
        spawned = {
            "symbol": algo.symbol,
            "orderId": order_id,
            "orderListId": -1,
            "clientOrderId": algo.client_algo_id,
            "price": "0",
            "origQty": format(algo.quantity, "f"),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "type": "MARKET",
            "side": algo.side,
            "time": algo.triggered_now(),
            "updateTime": algo.triggered_now(),
        }
        self.plain_orders[order_id] = spawned
        self._apply_fill(algo.symbol, algo.side, algo.quantity, fill_price, spawned)
        algo.algo_status = "TRIGGERED"
        algo.executed_qty = algo.quantity
        algo.average_price = fill_price
        return spawned

    # -- the ExchangeClient surface --------------------------------------

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
        side_u = side.upper()
        wire_type = str(order_type).upper()
        self.create_calls.append(
            {"symbol": symbol, "side": side_u, "type": wire_type, "params": dict(params)}
        )
        if mode == "timeout_not_landed":
            raise ccxt.RequestTimeout("binanceusdm POST /fapi/v1/order request timed out")
        if mode == "insufficient_funds":
            raise ccxt.InsufficientFunds("binanceusdm {-2019} margin is insufficient")
        if mode == "invalid_order":
            raise ccxt.InvalidOrder("binanceusdm {-1013} Filter failure: MIN_NOTIONAL")
        if mode == "rate_limit":
            raise ccxt.DDoSProtection("binanceusdm {418} IP has been auto-banned")
        cid = str(params.get("newClientOrderId") or f"auto-{self._next_order_id}")
        reduce_only = bool(params.get("reduceOnly"))
        stop_loss = params.get("stopLossPrice")
        take_profit = params.get("takeProfitPrice")
        callback_rate = params.get("callbackRate")
        if mode == "timeout_after_land":
            self._land(
                symbol,
                side_u,
                wire_type,
                quantity,
                price,
                cid,
                reduce_only,
                stop_loss,
                take_profit,
                callback_rate,
            )
            raise ccxt.RequestTimeout("binanceusdm POST /fapi/v1/order request timed out")
        landed = self._land(
            symbol,
            side_u,
            wire_type,
            quantity,
            price,
            cid,
            reduce_only,
            stop_loss,
            take_profit,
            callback_rate,
        )
        if isinstance(landed, _AlgoOrder):
            return self._unified_algo(landed)
        assert landed is not None
        return self._unified(landed)

    def _land(
        self,
        symbol: str,
        side_u: str,
        wire_type: str,
        quantity: _D,
        price: _D | None,
        cid: str,
        reduce_only: bool,
        stop_loss: Any,
        take_profit: Any,
        callback_rate: Any,
    ) -> dict[str, Any] | _AlgoOrder | None:
        if stop_loss is not None or take_profit is not None or callback_rate is not None:
            algo_id = self._next_algo_id
            self._next_algo_id += 1
            order_type = (
                "TRAILING_STOP_MARKET"
                if callback_rate is not None
                else "STOP_MARKET"
                if stop_loss is not None
                else "TAKE_PROFIT_MARKET"
            )
            algo = _AlgoOrder(
                algo_id=algo_id,
                client_algo_id=cid,
                order_type=order_type,
                symbol=symbol,
                side=side_u,
                quantity=quantity,
                trigger_price=(
                    _D(str(stop_loss))
                    if stop_loss is not None
                    else _D(str(take_profit))
                    if take_profit is not None
                    else None
                ),
                callback_rate=(_D(str(callback_rate)) if callback_rate is not None else None),
                reduce_only=reduce_only,
            )
            self.algo_orders[algo_id] = algo
            return algo
        order_id = self._next_order_id
        self._next_order_id += 1
        raw = {
            "symbol": symbol,
            "orderId": order_id,
            "orderListId": -1,
            "clientOrderId": cid,
            "price": None if price is None else format(_D(str(price)), "f"),
            "origQty": format(quantity, "f"),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "type": wire_type,
            "side": side_u,
            "reduceOnly": reduce_only,
            "time": 1786000000000 + order_id,
            "updateTime": 1786000000000 + order_id,
        }
        self.plain_orders[order_id] = raw
        if wire_type == "MARKET":
            self._apply_fill(symbol, side_u, quantity, self.last_prices[symbol], raw)
        return raw

    @staticmethod
    def _unified(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(raw["orderId"]),
            "clientOrderId": raw["clientOrderId"],
            "symbol": raw["symbol"],
            "side": raw["side"].lower(),
            "type": raw["type"].lower(),
            "price": raw["price"],
            "amount": raw["origQty"],
            "status": {"FILLED": "closed", "CANCELED": "canceled"}.get(raw["status"], "open"),
            "filled": raw["executedQty"],
            "average": (
                format(_D(raw["cummulativeQuoteQty"]) / _D(raw["executedQty"]), "f")
                if _D(raw["executedQty"]) > 0
                else None
            ),
            "reduceOnly": raw.get("reduceOnly", False),
            "timestamp": raw["time"],
            "info": dict(raw),
        }

    def _unified_algo(self, algo: _AlgoOrder) -> dict[str, Any]:
        return {
            "id": str(algo.algo_id),
            "clientOrderId": algo.client_algo_id,
            "symbol": algo.symbol,
            "side": algo.side.lower(),
            "type": "market",
            "price": None,
            "amount": format(algo.quantity, "f"),
            "status": {"NEW": "open", "TRIGGERED": "closed"}.get(algo.algo_status, "canceled"),
            "filled": format(algo.executed_qty, "f"),
            "average": (None if algo.average_price is None else format(algo.average_price, "f")),
            "reduceOnly": algo.reduce_only,
            "timestamp": algo.triggered_now(),
            "triggerPrice": (
                None if algo.trigger_price is None else format(algo.trigger_price, "f")
            ),
            "info": {
                "algoId": algo.algo_id,
                "clientAlgoId": algo.client_algo_id,
                "algoType": "CONDITIONAL",
                "orderType": algo.order_type,
                "symbol": algo.symbol,
                "side": algo.side,
                "origQty": format(algo.quantity, "f"),
                "algoStatus": algo.algo_status,
                "triggerPrice": (
                    None if algo.trigger_price is None else format(algo.trigger_price, "f")
                ),
                "callbackRate": (
                    None if algo.callback_rate is None else format(algo.callback_rate, "f")
                ),
                "reduceOnly": algo.reduce_only,
                "executedQty": format(algo.executed_qty, "f"),
                "time": algo.triggered_now(),
                "updateTime": algo.triggered_now(),
            },
        }

    async def cancel_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        if params.get("clientAlgoId") or params.get("conditional"):
            cid = str(params.get("clientAlgoId") or order_id)
            algo = self.algo_by_client_id(cid)
            if algo is None or algo.algo_status != "NEW":
                raise ccxt.OrderNotFound(f"binanceusdm {{-2011}} Unknown client algo id {cid}")
            algo.algo_status = "CANCELED"
            return {"algoId": algo.algo_id, "code": "200", "msg": "success"}
        raw = self._order_by_any_id(order_id)
        if raw is None or raw["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise ccxt.OrderNotFound(f"binanceusdm {{-2011}} Unknown order {order_id}")
        raw["status"] = "CANCELED"
        return {"orderId": raw["orderId"], "code": "200", "msg": "success"}

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        """The routed ExchangeClient sweep: BOTH fapi namespaces."""
        self.cancel_all_calls.append(symbol)
        for raw in self.open_plain(symbol):
            raw["status"] = "CANCELED"
        for algo in self.open_algo(symbol):
            algo.algo_status = "CANCELED"
        return {"code": "200", "msg": "The operation of cancel all open order is done."}

    async def fetch_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = dict(params or {})
        if params.get("clientAlgoId") or params.get("conditional"):
            cid = str(params.get("clientOrderId") or params.get("clientAlgoId") or order_id)
            algo = self.algo_by_client_id(cid)
            if algo is None:
                raise ccxt.OrderNotFound(f"binanceusdm {{-2013}} Order does not exist: {cid}")
            return self._unified_algo(algo)
        raw = self._order_by_any_id(order_id)
        if raw is None:
            raise ccxt.OrderNotFound(f"binanceusdm {{-2013}} Order does not exist: {order_id}")
        return self._unified(raw)

    async def fetch_open_orders(
        self,
        symbol: str | None = None,
        since: Any = None,
        limit: Any = None,
        params: dict[str, Any] | None = None,
        *,
        conditional: bool = False,
    ) -> list[dict[str, Any]]:
        """ccxt 4.5.88's real positional signature ``(symbol, since, limit, params)``.

        The ``conditional`` flag rides ``params`` on the wire (the wrapper passes it
        positionally, slot 4); the keyword-only ``conditional`` serves hydra's own
        client seam. A dict in the ``since`` slot reaches neither branch — the
        silent misbinding the real exchange would do.
        """
        if conditional or (params or {}).get("conditional"):
            return [self._unified_algo(o) for o in self.open_algo(symbol)]
        return [self._unified(o) for o in self.open_plain(symbol)]

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
                    "side": "buy" if raw["buyer"] else "sell",
                    "type": "market",
                    "price": raw["price"],
                    "amount": raw["qty"],
                    "timestamp": raw["time"],
                    "fee": {"cost": raw["commission"], "currency": "USDT"},
                    "takerOrMaker": "taker",
                    "info": dict(raw),
                }
            )
        return out

    async def fetch_balance(self) -> dict[str, Decimal]:
        return {"USDT": self.available}

    async def fetch_balance_totals(self) -> dict[str, Decimal]:
        total = self.wallet
        for pos in self.positions.values():
            if pos.qty != 0:
                mark = self.last_prices["BTCUSDT"]
                total += pos.margin + pos.qty * (mark - pos.entry)
        return {"USDT": total}

    async def fetch_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for name, pos in sorted(self.positions.items()):
            if pos.qty == 0 or (symbol is not None and name != symbol):
                continue
            mark = self.last_prices[name]
            rows.append(
                {
                    "symbol": name,
                    "positionSide": "BOTH",
                    "positionAmt": format(pos.qty, "f"),
                    "entryPrice": format(pos.entry, "f"),
                    "markPrice": format(mark, "f"),
                    "unRealizedProfit": format(pos.qty * (mark - pos.entry), "f"),
                    "liquidationPrice": "0",
                    "isolatedMargin": format(pos.margin, "f"),
                    "marginType": "isolated",
                    "notional": format(abs(pos.qty) * mark, "f"),
                    "updateTime": "1786000000000",
                }
            )
        return rows

    async def fetch_funding_income(
        self, symbol: str, since_ms: int | None = None
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.income
            if row["symbol"] == symbol and (since_ms is None or int(row["time"]) >= since_ms)
        ]

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        if not 1 <= leverage <= 125:
            raise ccxt.BadRequest("binanceusdm leverage should be between 1 and 125")
        self.set_leverage_calls.append((symbol, leverage))
        self.leverage[symbol] = leverage
        return {"symbol": symbol, "leverage": leverage, "maxNotionalValue": "1000000"}

    async def set_margin_mode(self, symbol: str, mode: str) -> dict[str, Any]:
        self.set_margin_mode_calls.append((symbol, mode))
        self.margin_modes[symbol] = mode
        return {"code": 200, "msg": "success"}

    async def fetch_position_side_dual(self) -> bool:
        return self.dual_side_position

    async def set_one_way_position_mode(self) -> dict[str, Any]:
        self.set_position_mode_calls.append(False)
        if any(p.qty != 0 for p in self.positions.values()):
            raise ccxt.OperationRejected(
                "binanceusdm {-4059} Cannot change position side with open positions."
            )
        self.dual_side_position = False
        return {"code": 200, "msg": "success"}
