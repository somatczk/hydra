"""Unified exchange client wrapping CCXT Pro for Binance/Bybit/Kraken/OKX.

The ``ExchangeClient`` is exchange-agnostic: all exchange-specific behavior is
handled internally by CCXT.  ``ccxt`` is **not** imported at module level so that
test environments without the package installed can still import this module.

The single outgoing money boundary: every outgoing ccxt call converts Decimal to
float at the wire (``create_order``'s amount/price/stopPrice) — ccxt's typed API is
float-based.  The conversion is exact for lot/tick-aligned values: Python's
shortest-repr round-trip parses the float back to the same Decimal value
(``Decimal(str(float(d))) == d``), so the pre-formatted exchange strings
(``exchange_stops.format_price``/``format_qty``) carry their exact value over the
wire.  Everything coming back is converted at use to exact Decimals
(``Decimal(str(raw))``), never through ``float``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from hydra.core.metrics import observe_exchange_call, record_ws_reconnect
from hydra.core.types import Direction, ExchangeId, MarketType, OrderType, Position, Symbol
from hydra.execution.client_factory import ccxt_class_for
from hydra.execution.exchange_stops import normalize_exchange_leg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ExchangeClient
# ---------------------------------------------------------------------------


class ExchangeClient:
    """Unified async wrapper around a CCXT Pro exchange instance.

    Parameters
    ----------
    exchange_id:
        One of the supported exchange identifiers.
    config:
        CCXT-compatible configuration dict (apiKey, secret, etc.).
    testnet:
        Whether to connect to the testnet/sandbox endpoint.
    market_type:
        Selects the ccxt class through ``client_factory.ccxt_class_for`` (binance SPOT,
        binanceusdm FUTURES).
    """

    def __init__(
        self,
        exchange_id: ExchangeId,
        config: dict[str, Any],
        testnet: bool = True,
        market_type: MarketType = MarketType.SPOT,
    ) -> None:
        self._exchange_id: ExchangeId = exchange_id
        self._market_type = market_type
        self._config = dict(config)
        self._testnet = testnet
        self._exchange: Any | None = None
        self._ws_connected: bool = False

    # ------------------------------------------------------------------
    # Lazy CCXT initialization
    # ------------------------------------------------------------------

    def _get_exchange(self) -> Any:
        """Lazily create the CCXT exchange instance."""
        if self._exchange is not None:
            return self._exchange

        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:
            raise ImportError(
                "ccxt is required for live exchange connectivity. "
                "Install with: pip install 'ccxt[async]'"
            ) from exc

        class_name = ccxt_class_for(self._exchange_id, self._market_type)
        exchange_cls = getattr(ccxt_async, class_name, None)
        if exchange_cls is None:
            raise ValueError(f"Unknown exchange class: {class_name}")

        opts: dict[str, Any] = {
            "enableRateLimit": True,
            **self._config,
        }
        if self._testnet:
            opts["sandbox"] = True

        self._exchange = exchange_cls(opts)
        return self._exchange

    # ------------------------------------------------------------------
    # Order API
    # ------------------------------------------------------------------

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
        """Place an order on the exchange.  Returns the CCXT order dict.

        The single outgoing Decimal->float boundary: amount, price and stopPrice go to
        the wire as floats (ccxt's typed API), exact for lot/tick-aligned values by the
        shortest-repr round-trip.
        """
        exchange = self._get_exchange()
        extra: dict[str, Any] = dict(params or {})

        if stop_price is not None:
            extra["stopPrice"] = float(stop_price)

        price_f: float | None = float(price) if price is not None else None
        with observe_exchange_call(self._exchange_id, "create_order"):
            result: dict[str, Any] = await exchange.create_order(
                symbol=symbol,
                type=order_type.lower(),
                side=side.lower(),
                amount=float(quantity),
                price=price_f,
                params=extra,
            )
        return result

    async def cancel_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Cancel an order on the exchange.

        *params* reaches ccxt untouched: a perp conditional leg cancels through the
        fapi ALGO namespace by passing ``{'clientAlgoId': ..., 'conditional': True}``.
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "cancel_order"):
            result: dict[str, Any] = await exchange.cancel_order(order_id, symbol, params or {})
        return result

    async def create_oco(
        self,
        symbol: str,
        side: str,
        quantity: str,
        take_profit: str,
        stop_loss: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a spot OCO (POST /api/v3/orderList/oco); returns the paper OCO shape.

        *quantity*, *take_profit* and *stop_loss* are pre-formatted exchange strings
        (tick/lot aligned by the caller, ``exchange_stops``). The response is normalized to
        the paper shape with ``id`` = the orderListId and per-leg dicts in ``legs``; the
        echoed amount/price/stop are the caller's own strings as exact Decimals (never
        through ``float``).
        """
        exchange = self._get_exchange()
        request: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "quantity": quantity,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": take_profit,
            "belowType": "STOP_LOSS",
            "belowStopPrice": stop_loss,
            "newOrderRespType": "FULL",
        }
        if params:
            request.update(params)
        with observe_exchange_call(self._exchange_id, "create_oco"):
            raw: dict[str, Any] = await exchange.privatePostOrderListOco(request)
        order_list_id = str(raw.get("orderListId"))
        list_client_id = raw.get("listClientOrderId")
        return {
            "id": order_list_id,
            "symbol": symbol,
            "side": side.upper(),
            "type": OrderType.OCO.value,
            "amount": Decimal(quantity),
            "price": Decimal(take_profit),
            "stop_price": Decimal(stop_loss),
            "status": str(raw.get("listStatusType") or "EXEC_STARTED"),
            "filled": False,
            "oco_link": order_list_id,
            "client_order_id": None if list_client_id in (None, "") else str(list_client_id),
            "legs": [normalize_exchange_leg(r) for r in raw.get("orderReports") or []],
        }

    async def cancel_order_list(self, order_list_id: str, symbol: str) -> dict[str, Any]:
        """Cancel a whole OCO list (DELETE /api/v3/orderList)."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "cancel_order_list"):
            result: dict[str, Any] = await exchange.privateDeleteOrderList(
                {"symbol": symbol, "orderListId": order_list_id}
            )
        return result

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        """Cancel EVERY open order on *symbol*, hydra's or not.

        Futures conditional (ladder) orders rest in their own fapi algo namespace,
        so the futures sweep cancels both: the regular open orders
        (DELETE /fapi/v1/allOpenOrders) and the open algo orders
        (DELETE /fapi/v1/algoOpenOrders). Spot keeps the single historic call.
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "cancel_all_orders"):
            if self._market_type is MarketType.FUTURES:
                result: dict[str, Any] = await exchange.fapiPrivateDeleteAllOpenOrders(
                    {"symbol": symbol}
                )
                await exchange.fapiPrivateDeleteAlgoOpenOrders({"symbol": symbol})
                return result
            result = await exchange.privateDeleteOpenOrders({"symbol": symbol})
        return result

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> dict[str, Decimal]:
        """Fetch account balances.  Returns {currency: free_balance}."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_balance"):
            raw: dict[str, Any] = await exchange.fetch_balance()
        balances: dict[str, Decimal] = {}
        free: dict[str, Any] = raw.get("free", {})
        for currency, amount in free.items():
            if amount is not None and float(amount) > 0:
                balances[currency] = Decimal(str(amount))
        return balances

    async def fetch_balance_totals(self) -> dict[str, Decimal]:
        """Per-asset wallet totals (free + locked): the reconcile truth for resting funds."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_balance"):
            raw: dict[str, Any] = await exchange.fetch_balance()
        totals: dict[str, Decimal] = {}
        for currency, amount in (raw.get("total") or {}).items():
            if amount is not None and float(amount) > 0:
                totals[currency] = Decimal(str(amount))
        return totals

    async def fetch_api_restrictions(self) -> dict[str, Any]:
        """The key's permissions (GET /sapi/v1/account/apiRestrictions): a read-only
        sapi call that never trades; the caller maps ccxt errors."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_api_restrictions"):
            result: dict[str, Any] = await exchange.sapiGetAccountApiRestrictions()
        return result

    async def fetch_open_orders(
        self, symbol: str | None = None, *, conditional: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch open orders from the exchange.

        Futures conditional (ladder) orders live in the separate fapi algo namespace:
        ``conditional=True`` reads the open algo orders instead of the regular ones.
        The regular call is unchanged — one argument, no params.
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_open_orders"):
            if conditional:
                result: list[dict[str, Any]] = await exchange.fetch_open_orders(
                    symbol, None, None, {"conditional": True}
                )
            else:
                result = await exchange.fetch_open_orders(symbol)
        return result

    async def fetch_open_order_lists(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch open OCO lists (GET /api/v3/openOrderList): the A8 verification read."""
        exchange = self._get_exchange()
        request: dict[str, Any] = {"symbol": symbol} if symbol else {}
        with observe_exchange_call(self._exchange_id, "fetch_open_order_lists"):
            result: list[dict[str, Any]] = await exchange.privateGetOpenOrderList(request)
        return result

    async def fetch_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One order by exchange id, or by ``params={'clientOrderId': ...}`` (ccxt maps it
        to Binance's ``origClientOrderId``; a lookup by our own id never needs the exchange
        id, so mid-submit timeouts stay resolvable)."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_order"):
            result: dict[str, Any] = await exchange.fetch_order(order_id, symbol, params or {})
        return result

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """This account's executed trades on *symbol* since *since* (inclusive; replays are
        harmless — the live_fills unique constraint is the double-apply guard)."""
        exchange = self._get_exchange()
        since_ms = None if since is None else int(since.timestamp() * 1000)
        with observe_exchange_call(self._exchange_id, "fetch_my_trades"):
            result: list[dict[str, Any]] = await exchange.fetch_my_trades(symbol, since=since_ms)
        return result

    async def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch open positions (futures only)."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_positions"):
            if symbol:
                result: list[dict[str, Any]] = await exchange.fetch_positions([symbol])
            else:
                result = await exchange.fetch_positions()
        return result

    # ------------------------------------------------------------------
    # ExecutorState protocol implementation
    # ------------------------------------------------------------------

    async def get_balance(self) -> dict[str, Decimal]:
        """Return exchange balances (ExecutorState protocol)."""
        return await self.fetch_balance()

    async def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Return exchange positions as Position objects (ExecutorState protocol)."""
        raw = await self.fetch_positions(symbol)
        results: list[Position] = []
        for p in raw:
            contracts = p.get("contracts") or p.get("contractSize") or 0
            if float(contracts) == 0:
                continue
            side = str(p.get("side", "")).upper()
            direction = Direction.LONG if side == "LONG" else Direction.SHORT
            results.append(
                Position(
                    symbol=Symbol(p.get("symbol", "")),
                    direction=direction,
                    quantity=Decimal(str(contracts)),
                    avg_entry_price=Decimal(str(p.get("entryPrice", 0))),
                    unrealized_pnl=Decimal(str(p.get("unrealizedPnl", 0))),
                    realized_pnl=Decimal("0"),
                    strategy_id="",
                    exchange_id=self._exchange_id,
                )
            )
        return results

    async def get_equity(self, quote: str = "USDT") -> Decimal:
        """Quote total plus every other asset's total at its last ``<asset>/<quote>`` price."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_balance"):
            raw: dict[str, Any] = await exchange.fetch_balance()
        totals: dict[str, Any] = raw.get("total") or {}
        equity = Decimal("0")
        for asset, amount in totals.items():
            if amount is None or Decimal(str(amount)) == 0:
                continue
            qty = Decimal(str(amount))
            equity += qty if asset == quote else qty * await self.get_last_price(f"{asset}/{quote}")
        return equity

    async def get_filled_orders(self) -> list[dict[str, Any]]:
        """Return filled orders. Live fills are persisted to DB, not held in-memory."""
        return []

    async def get_last_price(self, symbol: str) -> Decimal:
        """Fetch the last price via CCXT fetch_ticker (ExecutorState protocol)."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_ticker"):
            ticker: dict[str, Any] = await exchange.fetch_ticker(symbol)
        last = ticker.get("last")
        if last is None:
            raise ValueError(f"No last price in ticker for {symbol}")
        return Decimal(str(last))

    # ------------------------------------------------------------------
    # Futures-specific
    # ------------------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a futures symbol."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "set_leverage"):
            await exchange.set_leverage(leverage, symbol)

    async def set_margin_mode(self, symbol: str, mode: str) -> None:
        """Set margin mode ('cross' or 'isolated') for a futures symbol.

        Binance answers -4046 when the mode is already the asked one; ccxt maps
        that to ``MarginModeAlreadySet`` and swallows it by default.
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "set_margin_mode"):
            await exchange.set_margin_mode(mode, symbol)

    async def fetch_position_side_dual(self) -> bool:
        """GET /fapi/v1/positionSide/dual: True when the account is in hedge mode."""
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_position_side_dual"):
            result: dict[str, Any] = await exchange.fapiPrivateGetPositionSideDual()
        return bool(result.get("dualSidePosition"))

    async def set_one_way_position_mode(self) -> None:
        """POST /fapi/v1/positionSide/dual with dualSidePosition=false.

        Hydra's perp model is one signed position per symbol; a hedged account
        cannot carry it, so the live start switches the account to one-way (the
        call raises when positions exist — Binance refuses the change).
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "set_position_mode"):
            await exchange.set_position_mode(False)

    async def fetch_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET /fapi/v3/positionRisk rows, optionally filtered to *symbol*.

        The wallet/margin/position truth for live perps: signed ``positionAmt``,
        ``entryPrice``, ``markPrice``, ``unRealizedProfit``, ``isolatedMargin``.
        """
        exchange = self._get_exchange()
        with observe_exchange_call(self._exchange_id, "fetch_position_risk"):
            rows: list[dict[str, Any]] = await exchange.fapiPrivateV3GetPositionRisk()
        if symbol is None:
            return rows
        return [row for row in rows if str(row.get("symbol")) == symbol]

    async def fetch_funding_income(
        self, symbol: str, since_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """GET /fapi/v1/income: the settled FUNDING_FEE rows for one symbol.

        Each row carries the signed ``income`` (negative = paid), the ``asset``,
        the settlement ``time`` and the idempotency key ``tranId``.
        """
        exchange = self._get_exchange()
        request: dict[str, Any] = {"symbol": symbol, "incomeType": "FUNDING_FEE"}
        if since_ms:
            request["startTime"] = int(since_ms)
        with observe_exchange_call(self._exchange_id, "fetch_funding_income"):
            rows: list[dict[str, Any]] = await exchange.fapiPrivateGetIncome(request)
        return rows

    # ------------------------------------------------------------------
    # WebSocket reconnection helper
    # ------------------------------------------------------------------

    async def start_user_data_stream(self) -> None:
        """Start the WebSocket user-data stream with auto-reconnect logic."""
        exchange = self._get_exchange()
        self._ws_connected = True
        max_retries = 5
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                if hasattr(exchange, "watch_orders"):
                    await exchange.watch_orders()
                break
            except Exception:
                logger.warning(
                    "WebSocket reconnect attempt %d/%d",
                    attempt + 1,
                    max_retries,
                )
                record_ws_reconnect(self._exchange_id)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the exchange connection."""
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            self._ws_connected = False
