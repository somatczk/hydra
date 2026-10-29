"""The live perp executor: real leveraged orders on Binance USD⌂-M, no simulation.

``LivePerpTradingExecutor`` subclasses the perp paper executor — the same margin
sleeve, the same persistence and the same ``ExecutorState`` protocol — but never
simulates: ``create_order`` is outbox-first (the PENDING_SUBMIT row exists before
the exchange call, addressed by a ``hyd-``-prefixed client id), fills come back
only from REST polling at the real price with the real commission, and the paper
paths that model reality locally (resting-order fills, liquidation, funding
accrual from stored rates) are overridden away. The exchange is the source of
truth: funding is booked from the real ``/fapi/v1/income`` rows (so it never
reads as wallet drift), liquidation belongs to Binance (a vanished position
surfaces as the reconcile's position deficit, never a local force-close), and
``positionRisk`` is the account truth the reconcile compares the sleeve against.

Futures have no OCO, so a protective pair is TWO reduce-only conditional orders:
``TAKE_PROFIT_MARKET`` + ``STOP_MARKET`` (a trailing spec is a single
``TRAILING_STOP_MARKET`` with ``callbackRate`` in PERCENT — never spot's
``trailingDelta`` bips). On futures ccxt routes every conditional order through
the fapi ALGO endpoints, where a triggered order spawns a real order whose fills
land in ``userTrades``; the spawned order carries the algo order's client id,
which is how leg fills attribute back to the pair row. SIBLING-CANCEL: when one
leg fills, the other is cancelled — from the fill path and from the poller's
status refresh alike (a leg that filled while the engine was down is caught by
the next sweep, which the reconcile pass runs first).

The dead-man mirrors P3-B's exact shape (its binding user decision: stops +
alerts + checklist, no box-side auto-actions): the ladder rests on the exchange
and keeps working while the engine is down, the per-closed-bar coverage verify
sets/clears ``hydra_live_protective_gap`` over the ladder's legs, and
``HydraLiveProtectiveGap`` pages. No listen-key machinery and no
cancel-on-silence exist on either venue: an auto-cancel-on-silence would strip
the ladder exactly when a dead engine could not replace it — the opposite of
the binding invariant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast

from hydra.core.types import OHLCV, OrderType
from hydra.execution.errors import ExchangeRateLimitedError, SubmitUnknownError
from hydra.execution.exchange_stops import (
    ExchangeStopState,
    StopBreachedError,
    StopUnplaceableError,
)
from hydra.execution.fill_rules import FillPrice, Liquidity
from hydra.execution.live_executor import LiveOrderPoller
from hydra.execution.live_orders import (
    CLIENT_ORDER_ID_PREFIX,
    TERMINAL_STATUSES,
    InMemoryLiveOrderStore,
    LiveFillRecord,
    LiveOrderRecord,
    LiveOrderStatus,
    LiveOrderStore,
    PgLiveOrderStore,
    map_ccxt_status,
    new_client_order_id,
)
from hydra.execution.paper_trading import (
    InsufficientBalanceError,
    LedgerInvariantError,
    OrderRejectedError,
    PaperFill,
    PaperStateCorruptError,
    PaperStateUnavailableError,
    _d,
    _opt_d,
    read_stored_account,
)
from hydra.execution.perp_trading import (
    _STATE_FORMAT,
    PERP_EXCHANGE_ID,
    PerpPaperTradingExecutor,
    _PerpPosition,
)
from hydra.execution.reconcile import _apply_proven, _balance_findings, _status
from hydra.risk.market_rules import MarketRules

if TYPE_CHECKING:
    from hydra.execution.order_manager import OrderManager

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_CONDITIONAL_TYPES = (
    OrderType.STOP_MARKET.value,
    OrderType.TAKE_PROFIT_MARKET.value,
    OrderType.TRAILING_STOP.value,
)
# A trigger rounds TOWARD the market (it fires no later than asked for):
# a SELL's stop sits below it and rounds UP, its take-profit above it rounds DOWN;
# a BUY's stop sits above it and rounds DOWN, its take-profit below it rounds UP.
_ROUND_DIR: dict[tuple[str, str], str] = {
    ("SELL", "stop"): "sl",
    ("SELL", "take_profit"): "tp",
    ("BUY", "stop"): "tp",
    ("BUY", "take_profit"): "sl",
}


def _is_conditional(order_type: str) -> bool:
    return str(order_type).upper() in _CONDITIONAL_TYPES


def _order_not_found(exc: BaseException) -> bool:
    import ccxt

    return isinstance(exc, ccxt.OrderNotFound)


async def _classify_submit_failure(
    store: LiveOrderStore, cid: str, otype: str, exc: Exception
) -> Exception:
    """A failed exchange call, classified with the spot live executor's exact taxonomy."""
    import ccxt

    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        await store.mark_submit_failed(cid, str(exc))
        return ExchangeRateLimitedError(f"binanceusdm refused the call: {exc}")
    if isinstance(exc, ccxt.InsufficientFunds):
        await store.mark_submit_failed(cid, str(exc))
        return InsufficientBalanceError(str(exc))
    if isinstance(exc, ccxt.NetworkError):
        await store.apply_status(cid, LiveOrderStatus.UNKNOWN, error=str(exc))
        return SubmitUnknownError(cid, f"{otype} submit outcome unknown: {exc}")
    if isinstance(exc, ccxt.ExchangeError):
        await store.mark_submit_failed(cid, str(exc))
        return OrderRejectedError(str(exc))
    await store.apply_status(cid, LiveOrderStatus.UNKNOWN, error=str(exc))
    return SubmitUnknownError(cid, f"{otype} submit failed without an answer: {exc}")


def _round_trigger(rules: MarketRules, trigger: Decimal, side: str, kind: str) -> Decimal:
    tick = rules.price_tick
    if tick is None or tick <= 0:
        return trigger
    rounding = ROUND_UP if _ROUND_DIR[(side, kind)] == "sl" else ROUND_DOWN
    return trigger.quantize(tick, rounding=rounding)


def _wire_price(rules: MarketRules, trigger: Decimal, side: str, kind: str) -> str:
    return format(_round_trigger(rules, trigger, side, kind), "f")


class _PerpTradeClient(Protocol):
    """The perp executor's exchange calls (an ``ExchangeClient``; fakes in tests)."""

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
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def cancel_all_orders(self, symbol: str) -> Any: ...

    async def fetch_order(
        self, order_id: str, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def fetch_my_trades(
        self, symbol: str, since: datetime | None = None
    ) -> list[dict[str, Any]]: ...

    async def fetch_funding_income(
        self, symbol: str, since_ms: int | None = None
    ) -> list[dict[str, Any]]: ...

    async def fetch_balance(self) -> dict[str, Decimal]: ...

    async def fetch_balance_totals(self) -> dict[str, Decimal]: ...

    async def fetch_open_orders(
        self, symbol: str | None = None, *, conditional: bool = False
    ) -> list[dict[str, Any]]: ...

    async def fetch_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]: ...


class LivePerpTradingExecutor(PerpPaperTradingExecutor):
    """A Binance USD⌂-M isolated-margin sleeve that sends real leveraged orders."""

    def __init__(
        self,
        client: _PerpTradeClient,
        *,
        poll_interval: float = 5.0,
        resolve_grace: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.exchange_client = client
        self.poll_interval = poll_interval
        # mid-flight submits stay unresolved until the call window has surely passed
        self.resolve_grace = (
            resolve_grace if resolve_grace is not None else max(2.0 * poll_interval, 10.0)
        )
        self._source = "live"
        store: LiveOrderStore
        if self._db_pool is not None:
            store = PgLiveOrderStore(self._db_pool)
        else:
            store = InMemoryLiveOrderStore()
        self._live_orders = store
        # one lock: a poller sweep and a flatten drain never apply one claimed fill twice
        self._collect_lock = asyncio.Lock()
        self._funding_cursor_ms = 0

    @property
    def live_orders(self) -> LiveOrderStore:
        return self._live_orders

    # ------------------------------------------------------------------
    # Orders
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
        """Outbox-first: the pending_submit row, the exchange call(s), then the ack.

        MARKET and LIMIT go straight to the exchange (a SELL is a SHORT entry on
        futures, not an exit). The protective types build the NO-OCO ladder: an
        OCO is the reduce-only TAKE_PROFIT_MARKET + STOP_MARKET pair, a lone
        STOP_MARKET/TAKE_PROFIT_MARKET one conditional leg, a trail one
        TRAILING_STOP_MARKET with callbackRate PERCENT. Never a local fill.
        """
        self._require_running()
        params = dict(params or {})
        canon = self._symbol(symbol)
        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            raise OrderRejectedError(f"side must be BUY or SELL, got {side!r}")
        quantity = _d(quantity)
        if quantity <= 0:
            raise OrderRejectedError(f"quantity must be > 0, got {quantity}")
        otype = str(order_type).upper()
        if otype in _CONDITIONAL_TYPES:
            return await self._submit_conditional(
                canon, side_u, otype, quantity, price, stop_price, params
            )
        if otype == OrderType.OCO.value:
            return await self._submit_ladder_pair(
                canon, side_u, quantity, price, stop_price, params
            )
        if otype not in (OrderType.MARKET.value, OrderType.LIMIT.value):
            raise OrderRejectedError(
                f"{otype} is not a live-supported perp order type (MARKET, LIMIT and "
                "the protective STOP_MARKET/TAKE_PROFIT_MARKET/TRAILING_STOP/OCO)"
            )
        return await self._submit_plain(canon, side_u, otype, quantity, price, params)

    async def _submit_plain(
        self,
        canon: str,
        side_u: str,
        otype: str,
        quantity: Decimal,
        price: Decimal | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """A regular (non-conditional) order on POST /fapi/v1/order."""
        mark, _ = self._market_mark(canon)
        ref = price if otype == OrderType.LIMIT.value else mark
        if ref is None or ref <= 0:
            raise ValueError(f"No market price available for {canon}")
        self._check_rules(canon, quantity, ref)
        self._require_live_margin(canon, side_u, quantity, ref)
        if self._opens(canon, side_u) or otype == OrderType.LIMIT.value:
            await self._require_persisted()
        tag = str(params.get("tag") or "")
        reduce_only = bool(params.get("reduce_only"))
        cid = new_client_order_id()
        await self._live_orders.create(
            LiveOrderRecord(
                client_order_id=cid,
                session_id=self._session_id,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
                symbol=self._name(canon),
                side=side_u,
                order_type=otype,
                quantity=quantity,
                price=price,
                status=LiveOrderStatus.PENDING_SUBMIT,
                reduce_only=reduce_only,
                tag=tag,
                submitted_at=datetime.now(UTC),
            )
        )
        wire: dict[str, Any] = {"newClientOrderId": cid}
        if reduce_only:
            wire["reduceOnly"] = True
        try:
            resp = await self.exchange_client.create_order(
                self._name(canon), side_u, otype.lower(), quantity, price, None, wire
            )
        except Exception as exc:
            raise await _classify_submit_failure(self._live_orders, cid, otype, exc) from exc
        status = map_ccxt_status(resp.get("status"), resp.get("filled"))
        if not await self._live_orders.mark_ack(cid, str(resp["id"]), status):
            logger.info("Live perp ack for %s refused: the row was already resolved", cid)
        return {
            "id": str(resp["id"]),
            "symbol": self._name(canon),
            "side": side_u,
            "type": otype,
            "amount": float(quantity),
            "price": None if price is None else float(price),
            "stop_price": None,
            "status": "PENDING",
            "filled": False,
            "client_order_id": cid,
        }

    async def _submit_conditional(
        self,
        canon: str,
        side_u: str,
        otype: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """One reduce-only conditional leg through the fapi ALGO namespace.

        A trailing spec carries ``callbackRate`` in PERCENT (the fapi order API's
        fixed 0.1-10 range, ``MarketRules.trailing_callback_rate_pct``); spot's
        ``trailingDelta`` bips never appear on the futures wire. A trail outside
        the bounds is refused: the protective manager then leaves no group and
        the wiring's decision-17 market-close fires.
        """
        rules = self._require_rules(canon)
        trail_pct: Decimal | None = None
        callback_rate: str | None = None
        trigger: Decimal
        if otype == OrderType.TRAILING_STOP.value:
            raw = params.get("trail_pct")
            if raw is None:
                raise ValueError("trail_pct is required for TRAILING_STOP orders")
            trail_pct = _d(raw)
            if not _ZERO < trail_pct < _ONE:
                raise OrderRejectedError(f"trail_pct must be in (0, 1), got {trail_pct}")
            percent = trail_pct * _HUNDRED
            bounds = rules.trailing_callback_rate_pct
            if bounds is None or not bounds[0] <= percent <= bounds[1]:
                raise OrderRejectedError(
                    f"{self._name(canon)}: trailing {percent} % is outside the fapi "
                    f"callbackRate bounds {bounds}; a live trail cannot be placed"
                )
            callback_rate = format(percent.normalize(), "f")
            trigger = _ZERO
        else:
            raw_trigger = _d(stop_price) if stop_price is not None else _d(price) if price else None
            if raw_trigger is None or raw_trigger <= 0:
                raise OrderRejectedError(
                    f"{otype} orders need a positive trigger, got {raw_trigger}"
                )
            kind = "take_profit" if otype == OrderType.TAKE_PROFIT_MARKET.value else "stop"
            trigger = _round_trigger(rules, raw_trigger, side_u, kind)
        tag = str(params.get("tag") or "protective")
        mark, _ = self._market_mark(canon)
        self._check_rules(canon, quantity, trigger if trigger > 0 else (mark or _ONE))
        cid = new_client_order_id()
        await self._live_orders.create(
            LiveOrderRecord(
                client_order_id=cid,
                session_id=self._session_id,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
                symbol=self._name(canon),
                side=side_u,
                order_type=otype,
                quantity=quantity,
                price=None,
                stop_price=None if callback_rate is not None else trigger,
                status=LiveOrderStatus.PENDING_SUBMIT,
                reduce_only=True,
                tag=tag,
                submitted_at=datetime.now(UTC),
            )
        )
        wire: dict[str, Any] = {"newClientOrderId": cid, "reduceOnly": True}
        if callback_rate is not None:
            wire["callbackRate"] = callback_rate
        elif otype == OrderType.STOP_MARKET.value:
            wire["stopLossPrice"] = _wire_price(rules, trigger, side_u, "stop")
        else:
            wire["takeProfitPrice"] = _wire_price(rules, trigger, side_u, "take_profit")
        try:
            resp = await self.exchange_client.create_order(
                self._name(canon), side_u, "market", quantity, None, None, wire
            )
        except Exception as exc:
            raise await _classify_submit_failure(self._live_orders, cid, otype, exc) from exc
        if not await self._live_orders.mark_ack(cid, str(resp["id"]), LiveOrderStatus.NEW):
            logger.info("Live perp leg ack for %s refused: the row was already resolved", cid)
        return {
            "id": str(resp["id"]),
            "symbol": self._name(canon),
            "side": side_u,
            "type": otype,
            "amount": float(quantity),
            "price": None,
            "stop_price": None if callback_rate is not None else float(trigger),
            "status": "PENDING",
            "filled": False,
            "client_order_id": cid,
            "trail_pct": None if trail_pct is None else float(trail_pct),
            "reason": "trailing_stop" if otype == OrderType.TRAILING_STOP.value else kind,
            "legs": [{"id": str(resp["id"])}],  # a single conditional leg carries itself
        }

    async def _submit_ladder_pair(
        self,
        canon: str,
        side_u: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """The NO-OCO ladder: a reduce-only TAKE_PROFIT_MARKET + STOP_MARKET pair.

        The STOP leg goes first (protection before profit). A definitive
        rejection of either leg cancels whatever landed and raises
        ``StopUnplaceableError`` — the wiring's decision-17 market-close; an
        unknown outcome propagates (R18: never act over an unknown). A
        take-profit that would not rest on its side of the market drops the leg
        and places the stop alone, like the spot ladder.
        """
        if price is None or stop_price is None:
            raise ValueError(
                "OCO orders require both price (take-profit trigger) "
                "and stop_price (stop-loss trigger)"
            )
        tp, sl = _d(price), _d(stop_price)
        if side_u == "SELL" and not sl < tp:
            raise OrderRejectedError(f"SELL OCO needs stop {sl} below take-profit {tp}")
        if side_u == "BUY" and not tp < sl:
            raise OrderRejectedError(f"BUY OCO needs take-profit {tp} below stop {sl}")
        mark, _ = self._market_mark(canon)
        if mark is None or mark <= 0:
            raise ValueError(f"No market price available for {canon}")
        rules = self._require_rules(canon)
        toward = _ONE if side_u == "SELL" else -_ONE
        if (sl - mark) * toward >= 0:
            raise StopBreachedError(
                f"{self._name(canon)}: stop {sl} would not rest on the market side of "
                f"the last price {mark}; close at market"
            )
        tp_rests = (tp - mark) * toward > 0
        tag = str(params.get("tag") or "protective")
        pair_cid = new_client_order_id()
        sl_cid = new_client_order_id()
        tp_cid = new_client_order_id()
        leg_cids = (tp_cid, sl_cid)
        self._check_rules(canon, quantity, min(tp, sl))
        await self._live_orders.create(
            LiveOrderRecord(
                client_order_id=pair_cid,
                session_id=self._session_id,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
                symbol=self._name(canon),
                side=side_u,
                order_type=OrderType.OCO.value,
                quantity=quantity,
                price=tp if tp_rests else None,
                stop_price=sl,
                status=LiveOrderStatus.PENDING_SUBMIT,
                reduce_only=True,
                tag=tag,
                leg_client_order_ids=leg_cids,
                submitted_at=datetime.now(UTC),
            )
        )
        legs: list[dict[str, Any]] = []
        legs.append(
            await self._place_pair_leg(
                canon,
                side_u,
                OrderType.STOP_MARKET.value,
                quantity,
                _wire_price(rules, sl, side_u, "stop"),
                sl_cid,
                "stopLossPrice",
                sl,
            )
        )
        if tp_rests:
            legs.append(
                await self._place_pair_leg(
                    canon,
                    side_u,
                    OrderType.TAKE_PROFIT_MARKET.value,
                    quantity,
                    _wire_price(rules, tp, side_u, "take_profit"),
                    tp_cid,
                    "takeProfitPrice",
                    tp,
                )
            )
        else:
            logger.warning(
                "Perp pair for %s drops the take-profit leg %s at or beyond the mark %s",
                self._name(canon),
                tp,
                mark,
            )
        if not await self._live_orders.mark_ack(
            pair_cid, pair_cid, LiveOrderStatus.NEW, order_list_id=pair_cid
        ):
            logger.info("Live perp pair ack for %s refused: the row moved on", pair_cid)
        return {
            "id": pair_cid,
            "symbol": self._name(canon),
            "side": side_u,
            "type": OrderType.OCO.value,
            "amount": float(quantity),
            "price": float(tp) if tp_rests else None,
            "stop_price": float(sl),
            "status": "PENDING",
            "filled": False,
            "oco_link": pair_cid,
            "take_profit_order_id": tp_cid,
            "stop_loss_order_id": sl_cid,
            "legs": legs,
        }

    async def _place_pair_leg(
        self,
        canon: str,
        side_u: str,
        otype: str,
        quantity: Decimal,
        trigger_wire: str,
        leg_cid: str,
        trigger_param: str,
        trigger: Decimal,
    ) -> dict[str, Any]:
        """Place one leg of the pair; a definitive failure unwinds the whole pair."""
        wire: dict[str, Any] = {
            "newClientOrderId": leg_cid,
            "reduceOnly": True,
            trigger_param: trigger_wire,
        }
        try:
            resp = await self.exchange_client.create_order(
                self._name(canon), side_u, "market", quantity, None, None, wire
            )
        except Exception as exc:
            failure = await _classify_submit_failure(self._live_orders, leg_cid, otype, exc)
            if isinstance(failure, OrderRejectedError):
                await self._unwind_pair(canon, leg_cid)
                raise StopUnplaceableError(
                    f"{self._name(canon)}: the {otype} leg was rejected: {exc}"
                ) from exc
            raise failure from exc
        return {
            "id": str(resp["id"]),
            "symbol": self._name(canon),
            "side": side_u,
            "type": otype,
            "amount": float(quantity),
            "price": None,
            "stop_price": float(trigger),
            "status": "PENDING",
            "filled": False,
            "client_order_id": leg_cid,
            "reason": "take_profit" if otype == OrderType.TAKE_PROFIT_MARKET.value else "stop_loss",
        }

    async def _unwind_pair(self, canon: str, placed_cid: str) -> None:
        """A leg landed and the pair failed: cancel the landed leg, best-effort."""
        try:
            await self.exchange_client.cancel_order(
                placed_cid, self._name(canon), {"clientAlgoId": placed_cid, "conditional": True}
            )
        except Exception:
            logger.exception("Unwinding the landed perp leg %s failed", placed_cid)

    def _require_rules(self, canon: str) -> MarketRules:
        # build_live_executor validated real MarketRules; the paper trio cannot price a leg
        rules = self._market_rules.get(canon)
        if not isinstance(rules, MarketRules):
            raise OrderRejectedError(
                f"{self._name(canon)} has no MarketRules; live perp legs need the tick"
            )
        return rules

    def _require_live_margin(
        self, canon: str, side_u: str, quantity: Decimal, ref: Decimal
    ) -> None:
        """The sleeve must afford the OPEN leg too (pre-wire, fail-closed).

        After the wire the fill is a fact and always books; this check refuses an
        entry BEFORE the exchange call when the sleeve could not fund its margin.
        """
        if self._opens(canon, side_u):
            need = self._margin_needed(quantity * ref) + quantity * ref * self._taker_fee
            if self._wallet < need:
                raise InsufficientBalanceError(
                    f"Insufficient {self._quote} margin: need {need}, wallet {self._wallet}"
                )

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel on the exchange; an already-terminal row is a no-op success.

        A pair row cancels BOTH legs (futures have no list-cancel); a submit
        still in flight answers AMBIGUOUS (F11), exactly like the spot rule.
        """
        row = await self._find_row(order_id)
        if row is not None and row.status in TERMINAL_STATUSES:
            return {"id": order_id, "status": "CANCELLED"}
        if row is not None and row.status in (
            LiveOrderStatus.PENDING_SUBMIT,
            LiveOrderStatus.SUBMITTING,
        ):
            return {"id": order_id, "status": "AMBIGUOUS"}
        if row is not None and row.leg_client_order_ids:
            for leg_cid in row.leg_client_order_ids:
                await self._cancel_leg(leg_cid, row.symbol)
        elif row is not None and _is_conditional(row.order_type):
            await self._cancel_leg(row.client_order_id, row.symbol)
        elif row is not None:
            target = order_id if row.exchange_order_id is None else row.exchange_order_id
            await self.exchange_client.cancel_order(target, row.symbol, None)
        if row is not None:
            outcome = await self._live_orders.apply_status(
                row.client_order_id, LiveOrderStatus.CANCELED
            )
            if outcome == "refused":
                logger.info(
                    "Live perp cancel of %s left the row at %s: already resolved",
                    order_id,
                    row.status.value,
                )
        return {"id": order_id, "status": "CANCELLED"}

    async def _cancel_leg(self, leg_cid: str, symbol: str) -> dict[str, Any]:
        """Cancel one conditional leg by its client id through the algo namespace."""
        try:
            return await self.exchange_client.cancel_order(
                leg_cid, symbol, {"clientAlgoId": leg_cid, "conditional": True}
            )
        except Exception as exc:
            if _order_not_found(exc):
                logger.info("Perp leg %s already gone on the exchange", leg_cid)
                return {"id": leg_cid, "status": "CANCELLED"}
            raise

    async def cancel_all_orders(self, symbol: str) -> Any:
        """R4: the kill-switch sweep — every open order on the symbol, BOTH namespaces."""
        return await self.exchange_client.cancel_all_orders(symbol)

    async def _find_row(self, order_id: str) -> LiveOrderRecord | None:
        row = await self._live_orders.by_exchange_order_id(order_id, self._session_id)
        if row is None:
            row = await self._live_orders.by_order_list_id(order_id, self._session_id)
        if row is None:
            by_client = await self._live_orders.by_client_order_id(order_id)
            row = (
                by_client
                if by_client is not None and by_client.session_id == self._session_id
                else None
            )
        return row

    async def cancel_sibling_of(self, row: LiveOrderRecord, filled_leg_cid: str) -> None:
        """SIBLING-CANCEL: a pair leg filled — cancel the other leg (both paths)."""
        if not row.leg_client_order_ids:
            return
        sibling = next((cid for cid in row.leg_client_order_ids if cid != filled_leg_cid), None)
        if sibling is None:
            return
        try:
            await self._cancel_leg(sibling, row.symbol)
        except Exception:
            logger.exception("Sibling-cancel of %s failed; the reconcile reports it", sibling)
            return
        await self._live_orders.apply_status(row.client_order_id, LiveOrderStatus.CANCELED)

    # ------------------------------------------------------------------
    # Closed bars: marks only (no resting fills, no liquidation, no funding accrual)
    # ------------------------------------------------------------------

    async def on_closed_bar(self, symbol: str, ohlcv: OHLCV) -> list[PaperFill]:
        canon = self._symbol(symbol)
        last = self._last_bar_ts.get(canon)
        if last is not None and ohlcv.timestamp <= last:
            return []
        self._marks[canon] = ohlcv.close
        self._live_marks.pop(canon, None)
        self._stale_marks.discard(canon)
        self._last_bar_ts[canon] = ohlcv.timestamp
        self._update_peak()
        await self._persist(snapshot_at=ohlcv.timestamp)
        return []

    # ------------------------------------------------------------------
    # Fills: the poll path is the single source
    # ------------------------------------------------------------------

    async def collect_exchange_fills(self, symbols: list[str] | None = None) -> list[PaperFill]:
        """New trades of this session's own orders, claimed, applied and confirmed.

        The funding income books FIRST (the sweep's own cadence keeps the sleeve
        ahead of the fills it prices), then the same pipeline as the spot
        executor: unapplied rows re-apply before new trades are claimed. The
        whole run holds the collect lock, so a poller sweep and a flatten drain
        cannot apply the same claimed fill twice.
        """
        async with self._collect_lock:
            await self._book_funding_income()
            return await self._collect_fills_locked(symbols)

    async def _collect_fills_locked(self, symbols: list[str] | None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        for claimed in await self._live_orders.unapplied_fills(self._session_id):
            row = await self._live_orders.by_client_order_id(claimed.client_order_id)
            if row is None or row.session_id != self._session_id:
                continue
            try:
                fills.append(await self._apply_claimed_fill(claimed, row))
            except Exception:
                logger.exception("Live perp fill %s could not be re-applied", claimed.trade_id)
        wanted = None if symbols is None else {self._symbol(s) for s in symbols}
        tracked = {self._canon_of(s) for s in await self._live_orders.symbols(self._session_id)}
        tracked |= {c for c, lot in self._positions.items() if lot.qty != _ZERO}
        targets = sorted(c for c in tracked if wanted is None or c in wanted)
        for canon in targets:
            name = self._name(canon)
            since = await self._live_orders.last_trade_ts(self._session_id, name)
            try:
                trades = await self.exchange_client.fetch_my_trades(name, since)
            except Exception as exc:
                logger.warning("Live perp trades of %s unreadable: %s", name, exc)
                continue
            for trade in trades:
                try:
                    fill = await self._fill_from_trade(canon, trade)
                except Exception:
                    logger.exception("Live perp trade of %s could not be applied", name)
                    continue
                if fill is not None:
                    fills.append(fill)
        return fills

    async def _fill_from_trade(self, canon: str, trade: Mapping[str, Any]) -> PaperFill | None:
        """One ``userTrades`` row -> a sleeve fill of this session's own order.

        A plain order's trade maps by its exchange order id; a conditional leg's
        spawned order carries the leg's client id (the fapi trigger inheritance),
        recovered by one ``fetch_order`` cross-check when the id does not map
        directly — the spot F8 family's futures shape.
        """
        exchange_order_id = str(trade.get("order") or "")
        row = None
        if exchange_order_id:
            row = await self._live_orders.by_exchange_order_id(exchange_order_id, self._session_id)
        leg_cid: str | None = None
        if row is None:
            row, leg_cid = await self._cross_check_trade_order(canon, trade)
        if row is None:
            return None  # R5: the user's orders never enter the session ledger
        if row.session_id != self._session_id:
            logger.error(
                "Live perp trade %s maps to session %s, not %s: skipped",
                trade.get("id"),
                row.session_id,
                self._session_id,
            )
            return None
        raw_fee_obj = trade.get("fee")
        fee: Mapping[str, Any] = raw_fee_obj if isinstance(raw_fee_obj, Mapping) else {}
        info = trade.get("info")
        raw_info: Mapping[str, Any] = info if isinstance(info, Mapping) else {}
        raw_fee = _d(fee.get("cost") or raw_info.get("commission") or 0)
        fee_currency = str(fee.get("currency") or raw_info.get("commissionAsset") or self._quote)
        timestamp = int(trade.get("timestamp") or raw_info.get("time") or 0)
        fill = LiveFillRecord(
            trade_id=str(trade.get("id") or raw_info.get("id")),
            client_order_id=row.client_order_id,
            session_id=self._session_id,
            exchange_id=self._exchange_id,
            symbol=self._name(canon),
            side=str(trade.get("side") or ("buy" if raw_info.get("buyer") else "sell")).upper(),
            price=_d(trade.get("price") or raw_info.get("price")),
            quantity=_d(trade.get("amount") or raw_info.get("qty")),
            fee=raw_fee,
            fee_currency=fee_currency,
            liquidity=str(
                trade.get("takerOrMaker") or ("maker" if raw_info.get("maker") else "taker")
            ),
            timestamp=datetime.fromtimestamp(timestamp / 1000, UTC),
        )
        if not await self._live_orders.record_fill(fill):
            return None  # already claimed: applied, or waiting for its own retry
        return await self._apply_claimed_fill(fill, row, filled_leg_cid=leg_cid)

    async def _cross_check_trade_order(
        self, canon: str, trade: Mapping[str, Any]
    ) -> tuple[LiveOrderRecord | None, str | None]:
        """Fetch the trade's order and map its client id to a leg or a mid-submit row."""
        order_id = str(trade.get("order") or "")
        if not order_id:
            return None, None
        try:
            resp = await self.exchange_client.fetch_order(order_id, self._name(canon), None)
        except Exception as exc:
            logger.info("Perp trade order %s unreadable for the cross-check: %s", order_id, exc)
            return None, None
        client_id = str(resp.get("clientOrderId") or "")
        if not client_id:
            return None, None
        by_client = await self._live_orders.by_client_order_id(client_id)
        if by_client is not None and by_client.session_id == self._session_id:
            return by_client, None
        # the pair row may already be terminal (the sweep refreshes statuses before
        # it collects), so search every tracked row, not just the open ones
        for row in await self._live_orders.orders(self._session_id, limit=200):
            if client_id in row.leg_client_order_ids:
                return row, client_id
        return None, None

    async def _apply_claimed_fill(
        self, fill: LiveFillRecord, row: LiveOrderRecord, *, filled_leg_cid: str | None = None
    ) -> PaperFill:
        """Apply one claimed fill to the sleeve, confirm it, cancel the sibling.

        A real exchange fill always books (the exchange accepted the trade; a
        local margin refusal here would orphan it forever), so the live sleeve
        tolerates a transient wallet shortfall — unbooked income is the usual
        cause and books on this same sweep — while the reconcile reports a
        persistent one.
        """
        canon = self._canon_of(fill.symbol)
        applied = self._apply_fill(
            canon,
            fill.side,
            row.order_type,
            fill.quantity,
            FillPrice(price=fill.price, liquidity=cast(Liquidity, fill.liquidity)),
            row.tag,
            row.exchange_order_id or fill.client_order_id,
            None,
            fill.timestamp,
        )
        filled_qty = row.filled_quantity + fill.quantity
        status = (
            LiveOrderStatus.FILLED
            if filled_qty >= row.quantity
            else LiveOrderStatus.PARTIALLY_FILLED
        )
        await self._live_orders.apply_status(
            row.client_order_id,
            status,
            filled_quantity=filled_qty,
            average_fill_price=fill.price,
        )
        await self._live_orders.mark_fill_applied(fill)
        await self._persist(snapshot_at=fill.timestamp)
        if row.order_type == OrderType.OCO.value:
            await self.cancel_sibling_of(row, filled_leg_cid or fill.client_order_id)
        return applied

    def _canon_of(self, symbol: str) -> str:
        try:
            return self._symbol(symbol)
        except OrderRejectedError:
            return symbol.replace("/", "").replace("-", "").upper()

    # ------------------------------------------------------------------
    # The funding income book (the exchange's own rows, never wallet drift)
    # ------------------------------------------------------------------

    async def _book_funding_income(self) -> None:
        """Book the settled FUNDING_FEE rows of every tracked symbol.

        The cursor rides the persisted state and the funding fills persist in
        the same ``paper_accounts`` write, so a crash either keeps both or
        re-books neither. Each row adjusts the open position's isolated margin,
        or the wallet when the position is already closed — the margin the close
        released was the stale one, and the correction is exactly the funding.
        """
        held = {c for c, p in self._positions.items() if p.qty != _ZERO}
        for canon in sorted(set(self._names) | held):
            try:
                rows = await self.exchange_client.fetch_funding_income(
                    self._name(canon), self._funding_cursor_ms or None
                )
            except Exception as exc:
                logger.warning("Funding income of %s unreadable: %s", self._name(canon), exc)
                continue
            booked = 0
            for raw in rows:
                income = _d(raw.get("income") or 0)
                settled_ms = int(raw.get("time") or 0)
                if settled_ms <= self._funding_cursor_ms or income == 0:
                    continue
                tran_id = str(raw.get("tranId") or f"{canon}:{settled_ms}")
                pos = self._positions.get(canon)
                if pos is not None and pos.qty != _ZERO:
                    pos.margin += income
                    pos.funding_paid -= income
                    qty, price = abs(pos.qty), self._marks.get(canon) or pos.entry_price
                else:
                    self._wallet += income
                    qty, price = _ZERO, self._marks.get(canon) or _ZERO
                self._funding_applied += 1
                self._fill_seq += 1
                self._filled.append(
                    PaperFill(
                        order_id=f"funding-{tran_id}",
                        parent_order_id=None,
                        symbol=self._name(canon),
                        side="FUNDING",
                        order_type="FUNDING",
                        quantity=qty,
                        price=price,
                        fee=max(-income, _ZERO),
                        fee_currency=self._quote,
                        liquidity="taker",
                        gross_pnl=None,
                        net_pnl=income,
                        reason="funding",
                        timestamp=datetime.fromtimestamp(settled_ms / 1000, UTC),
                        fill_seq=self._fill_seq,
                    )
                )
                if self._db_pool is not None:
                    self._outbox.append(self._filled[-1])
                self._funding_cursor_ms = max(self._funding_cursor_ms, settled_ms)
                booked += 1
            if booked:
                await self._persist(snapshot_at=None)
                logger.info("Booked %d funding row(s) for %s", booked, self._name(canon))

    # ------------------------------------------------------------------
    # Flatten and reads
    # ------------------------------------------------------------------

    async def flatten(self, symbol: str | None = None, reason: str = "flatten") -> list[PaperFill]:
        """Cancel resting orders and reduce-only market-close every position for real."""
        canon = None if symbol is None else self._symbol(symbol)
        targets = (
            {canon}
            if canon is not None
            else set(await self._live_orders.symbols(self._session_id))
            | {c for c, p in self._positions.items() if p.qty != _ZERO}
        )
        for row in await self._live_orders.open_orders(self._session_id):
            if self._canon_of(row.symbol) not in targets:
                continue
            try:
                await self.cancel_order(row.exchange_order_id or row.client_order_id, row.symbol)
            except Exception as exc:
                logger.warning("Perp cancel of %s during flatten failed: %s", row.symbol, exc)
        failures: list[Exception] = []
        for target in sorted(targets):
            pos = self._positions.get(target)
            if pos is None or pos.qty == _ZERO:
                continue
            mark, _ = self._market_mark(target)
            if mark is None or mark <= 0:
                failures.append(ValueError(f"no market price to flatten {self._name(target)}"))
                continue
            side = "SELL" if pos.qty > 0 else "BUY"
            try:
                await self.create_order(
                    self._name(target),
                    side,
                    OrderType.MARKET.value,
                    abs(pos.qty),
                    params={"tag": reason, "reduce_only": True},
                )
            except Exception as exc:
                failures.append(exc)
        fills = await self.collect_exchange_fills(None)
        if failures:
            raise failures[0]
        return fills

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """The tracked (DB) rows in the paper open-order shape plus status and filled qty."""
        wanted = None if symbol is None else self._symbol(symbol)
        rows = await self._live_orders.open_orders(self._session_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            if wanted is not None and self._canon_of(row.symbol) != wanted:
                continue
            out.append(
                {
                    "id": row.exchange_order_id or row.client_order_id,
                    "symbol": row.symbol,
                    "side": row.side,
                    "type": row.order_type,
                    "amount": float(row.quantity),
                    "price": None if row.price is None else float(row.price),
                    "stop_price": None if row.stop_price is None else float(row.stop_price),
                    "reason": _row_reason(row),
                    "oco_id": row.order_list_id,
                    "trail_pct": None,
                    "status": row.status.value,
                    "filled_quantity": float(row.filled_quantity),
                    "client_order_id": row.client_order_id,
                    "reduce_only": row.reduce_only,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Live sleeve semantics: a real fill always books
    # ------------------------------------------------------------------

    def _require_fill_margin(self, canon: str, side: str, quantity: Decimal, fp: FillPrice) -> None:
        """Overridden to a no-op: the exchange already accepted this fill.

        The pre-wire check is ``_require_live_margin`` at order time; after the
        wire the fill is a fact, and a local wallet shortfall must never orphan
        it — the income book heals the usual cause and the reconcile reports it.
        """

    def _open_part(
        self, canon: str, side: str, quantity: Decimal, fp: FillPrice, fee: Decimal
    ) -> None:
        """Overridden: take the margin even from a transiently short wallet.

        An unbooked funding credit (shorts receive funding) is the normal cause;
        the next sweep books it and heals the wallet, and a persistent shortfall
        is the reconcile's wallet-deficit finding — never a dropped fill.
        """
        pos = self._positions.setdefault(canon, _PerpPosition())
        notional = quantity * fp.price
        margin = self._margin_needed(notional)
        if self._wallet < margin + fee:
            logger.warning(
                "Live perp sleeve wallet short by %s while booking a real fill on %s",
                margin + fee - self._wallet,
                canon,
            )
        self._wallet -= margin + fee
        if pos.qty == _ZERO or (pos.qty > 0) == (side == "BUY"):
            total_qty = abs(pos.qty) + quantity
            pos.entry_price = (abs(pos.qty) * pos.entry_price + notional) / total_qty
            pos.qty += (_ONE if side == "BUY" else -_ONE) * quantity
            pos.margin += margin
            pos.entry_fees_open += fee

    def _check_invariants(self) -> None:
        """Overridden: a transiently negative live wallet logs, never halts.

        The margin and flat-position invariants stay (they catch real ledger
        bugs); a persisted negative wallet is drift the reconcile reports.
        """
        if self._wallet < 0:
            logger.warning("Live perp sleeve wallet is negative: %s", self._wallet)
        for canon, pos in self._positions.items():
            if pos.margin < 0 or pos.entry_fees_open < 0:
                raise LedgerInvariantError(f"negative margin for {canon}")
            if pos.qty == _ZERO and (pos.margin != _ZERO or pos.entry_fees_open != _ZERO):
                raise LedgerInvariantError(f"flat position {canon} still holds margin")

    # ------------------------------------------------------------------
    # State: the funding cursor rides the persisted sleeve state
    # ------------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {**super().to_state(), "funding_cursor_ms": self._funding_cursor_ms}

    def _rebuild_from_state(self, state: Mapping[str, Any]) -> None:
        super()._rebuild_from_state(state)
        self._funding_cursor_ms = int(state.get("funding_cursor_ms") or 0)

    @classmethod
    async def restore_live(
        cls,
        db_pool: Any,
        session_id: str,
        *,
        client: _PerpTradeClient,
        market_rules: Mapping[str, MarketRules] | None = None,
        poll_interval: float = 5.0,
        resolve_grace: float | None = None,
    ) -> LivePerpTradingExecutor | None:
        """Rebuild the live perp sleeve from ``paper_accounts`` around a fresh client."""
        stored, orphaned, trades = await read_stored_account(db_pool, session_id)
        if stored is None:
            if orphaned:
                raise PaperStateUnavailableError(
                    f"live state unavailable for {session_id!r}: "
                    "open positions exist without saved state"
                )
            return None
        state = stored.state
        try:
            if state.get("format") != _STATE_FORMAT:
                raise ValueError(f"unknown perp state format {state.get('format')!r}")
            ex = cls(
                client,
                exchange_id=PERP_EXCHANGE_ID,
                initial_balances={str(state["quote_asset"]): _ZERO},
                db_pool=db_pool,
                strategy_id=stored.strategy_id,
                session_id=stored.session_id,
                maker_fee=_d(state["maker_fee"]),
                taker_fee=_d(state["taker_fee"]),
                quote_asset=str(state["quote_asset"]),
                market_rules=market_rules,
                leverage=int(state["leverage"]),
                maintenance_margin_rate=_d(state["maintenance_margin_rate"]),
                poll_interval=poll_interval,
                resolve_grace=resolve_grace,
            )
            ex._rebuild_from_state(state)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise PaperStateCorruptError(f"live perp state for {session_id!r}: {exc}") from exc
        ex._state_version = stored.state_version
        ex._filled = [ex._restored_fill(t) for t in trades]
        return ex


def _row_reason(row: LiveOrderRecord) -> str:
    if row.order_type == OrderType.TAKE_PROFIT_MARKET.value:
        return "take_profit"
    if row.order_type == OrderType.TRAILING_STOP.value:
        return "trailing_stop"
    if row.order_type in (OrderType.OCO.value, OrderType.STOP_MARKET.value):
        return "stop_loss"
    if row.order_type == OrderType.LIMIT.value:
        return "limit"
    return "market"


# ---------------------------------------------------------------------------
# The poller's perp half: algo-aware resolution and status refresh
# ---------------------------------------------------------------------------


class LivePerpOrderPoller(LiveOrderPoller):
    """The single fill source for one live perp session.

    Inherits the spot poller's whole resolution discipline (the F8 window, the
    myTrades cross-check, the claim-apply-confirm sweep) and overrides only the
    exchange reads: futures conditional orders resolve through the fapi ALGO
    namespace by their client ids, and a pair row refreshes through BOTH legs —
    a leg that proves FILLED cancels its sibling right here (the
    reconcile-driven sibling-cancel: the reconcile pass runs this sweep first).
    """

    def __init__(
        self,
        executor: LivePerpTradingExecutor,
        om: OrderManager,
        *,
        interval: float,
        resolve_grace: float,
        not_found_window: float = 60.0,
    ) -> None:
        super().__init__(
            executor,
            om,
            interval=interval,
            resolve_grace=resolve_grace,
            not_found_window=not_found_window,
        )
        self._perp = executor

    async def _fetch_conditional(
        self, cid: str, symbol: str, *, by_client_id: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"conditional": True}
        if by_client_id:
            params["clientOrderId"] = cid
        return await self._perp.exchange_client.fetch_order(cid, symbol, params)

    async def _resolve_unsettled(self) -> None:
        store = self._perp.live_orders
        deadline = datetime.now(UTC) - timedelta(seconds=self._perp.resolve_grace)
        for row in await store.unsettled(self._perp._session_id, deadline):
            if row.leg_client_order_ids:
                await self._resolve_pair_row(row)
                continue
            try:
                if _is_conditional(row.order_type):
                    resp = await self._fetch_conditional(
                        row.client_order_id, row.symbol, by_client_id=True
                    )
                else:
                    resp = await self._perp.exchange_client.fetch_order(
                        row.client_order_id,
                        row.symbol,
                        {"clientOrderId": row.client_order_id},
                    )
            except Exception as exc:
                if _order_not_found(exc):
                    await self._note_missing(row)
                else:
                    logger.info("Perp submit %s not resolvable yet: %s", row.client_order_id, exc)
                continue
            self._missing.pop(row.client_order_id, None)
            status = map_ccxt_status(resp.get("status"), resp.get("filled"))
            resolved = await store.resolve_unknown(
                row.client_order_id,
                str(resp["id"]),
                status,
                filled_quantity=_opt_d(resp.get("filled")),
                average_fill_price=_opt_d(resp.get("average")),
            )
            if not resolved:
                logger.info(
                    "Perp submit %s could not be resolved: the row moved on",
                    row.client_order_id,
                )

    async def _resolve_pair_row(self, row: LiveOrderRecord) -> None:
        """A mid-pair crash: resolve the pair from whichever legs actually rest.

        A leg that already traded resolves through the inherited trades
        cross-check (overridden below to keep the pair's own ids); resting legs
        resolve the pair as NEW under its pair id; nothing at all falls to the
        F8 not-found window.
        """
        store = self._perp.live_orders
        resting = 0
        for leg_cid in row.leg_client_order_ids:
            try:
                await self._fetch_conditional(leg_cid, row.symbol, by_client_id=True)
            except Exception as exc:
                if not _order_not_found(exc):
                    logger.info("Perp pair leg %s unreadable: %s", leg_cid, exc)
                continue
            resting += 1
        if resting:
            resolved = await store.resolve_unknown(
                row.client_order_id,
                row.client_order_id,
                LiveOrderStatus.NEW,
                order_list_id=row.client_order_id,
            )
            logger.info(
                "Perp pair %s resolved with %d resting leg(s) (%s)",
                row.client_order_id,
                resting,
                "resolved" if resolved else "the row moved on",
            )
            return
        await self._note_missing(row)

    async def _trades_cross_check(self, row: LiveOrderRecord) -> bool:
        """The inherited F8 cross-check, pair-aware: a matched trade resolves the
        pair under its own pair id (never the spawned order's)."""
        if not row.leg_client_order_ids:
            return await super()._trades_cross_check(row)
        client = self._perp.exchange_client
        since = None if row.submitted_at is None else row.submitted_at - timedelta(minutes=5)
        try:
            trades = await client.fetch_my_trades(row.symbol, since)
        except Exception as exc:
            logger.info("Perp pair %s trades unreadable: %s", row.client_order_id, exc)
            return False
        wanted = set(row.leg_client_order_ids)
        for trade in trades:
            order_id = str(trade.get("order") or "")
            if not order_id:
                continue
            try:
                resp = await client.fetch_order(order_id, row.symbol, None)
            except Exception:
                logger.info("Perp pair trade order %s unreadable", order_id)
                continue
            if str(resp.get("clientOrderId") or "") in wanted:
                resolved = await self._perp.live_orders.resolve_unknown(
                    row.client_order_id,
                    row.client_order_id,
                    LiveOrderStatus.NEW,
                    order_list_id=row.client_order_id,
                )
                logger.info(
                    "Perp pair %s resolved by a leg's trade (%s)",
                    row.client_order_id,
                    "resolved" if resolved else "the row moved on",
                )
                return True
        return False

    async def _refresh_open_statuses(self) -> None:
        store = self._perp.live_orders
        for row in await store.refreshable(self._perp._session_id):
            if row.leg_client_order_ids:
                await self._refresh_pair_row(row)
                continue
            try:
                if _is_conditional(row.order_type):
                    resp = await self._fetch_conditional(
                        row.client_order_id, row.symbol, by_client_id=True
                    )
                else:
                    resp = await self._perp.exchange_client.fetch_order(
                        str(row.exchange_order_id), row.symbol, None
                    )
            except Exception as exc:
                logger.info("Perp order %s status unreadable: %s", row.exchange_order_id, exc)
                continue
            status = map_ccxt_status(resp.get("status"), resp.get("filled"))
            outcome = await store.apply_status(
                row.client_order_id,
                status,
                filled_quantity=_opt_d(resp.get("filled")),
                average_fill_price=_opt_d(resp.get("average")),
            )
            if outcome == "refused":
                logger.info(
                    "Perp order %s status refresh refused: the row is resolved",
                    row.exchange_order_id,
                )

    async def _refresh_pair_row(self, row: LiveOrderRecord) -> None:
        """Refresh a pair through its legs; a proven-filled leg cancels the sibling."""
        statuses: list[LiveOrderStatus] = []
        answers: list[dict[str, Any]] = []
        for leg_cid in row.leg_client_order_ids:
            try:
                resp = await self._fetch_conditional(leg_cid, row.symbol, by_client_id=True)
            except Exception as exc:
                logger.info("Perp pair %s leg %s unreadable: %s", row.client_order_id, leg_cid, exc)
                return  # no proof, no action: leave the row exactly as it is
            statuses.append(map_ccxt_status(resp.get("status"), resp.get("filled")))
            answers.append(resp)
        if LiveOrderStatus.FILLED in statuses:
            fired = next(
                a for a, s in zip(answers, statuses, strict=True) if s == LiveOrderStatus.FILLED
            )
            leg_cid = str(fired.get("clientOrderId") or "")
            await self._perp.live_orders.apply_status(
                row.client_order_id,
                LiveOrderStatus.FILLED,
                filled_quantity=_opt_d(fired.get("filled")),
                average_fill_price=_opt_d(fired.get("average")),
            )
            await self._perp.cancel_sibling_of(row, leg_cid)
            return
        if statuses and all(s == LiveOrderStatus.CANCELED for s in statuses):
            await self._perp.live_orders.apply_status(row.client_order_id, LiveOrderStatus.CANCELED)


# ---------------------------------------------------------------------------
# The A8-live verify over both fapi namespaces (the dead-man's continuous read)
# ---------------------------------------------------------------------------


def normalize_perp_leg(order: Mapping[str, Any]) -> dict[str, Any]:
    """One unified ccxt futures order (plain or algo) -> the paper open-order shape."""
    trigger = order.get("triggerPrice")
    info = order.get("info")
    raw: Mapping[str, Any] = info if isinstance(info, Mapping) else {}
    raw_type = str(raw.get("orderType") or "").upper()
    reason = (
        "take_profit"
        if raw_type == "TAKE_PROFIT_MARKET"
        else "trailing_stop"
        if raw_type == "TRAILING_STOP_MARKET"
        else "stop_loss"
        if raw_type == "STOP_MARKET" or trigger is not None
        else "limit"
    )
    raw_price = order.get("price")
    stop_price: float | None = None
    price: float | None = None
    if raw_price is not None and raw_price != 0 and raw_price != "0":
        price = float(raw_price)
    if trigger is not None and trigger != 0 and trigger != "0":
        stop_price = float(trigger)
    return {
        "id": str(order.get("id") or ""),
        "symbol": str(order.get("symbol") or ""),
        "side": str(order.get("side") or "").upper(),
        "type": order.get("type"),
        "amount": float(order.get("amount") or 0),
        "price": price,
        "stop_price": stop_price,
        "status": str(order.get("status") or "").upper(),
        "filled": False,
        "reason": reason,
        "oco_id": None,
        "trail_pct": None,
        "client_order_id": (
            None if order.get("clientOrderId") in (None, "") else str(order.get("clientOrderId"))
        ),
        "reduce_only": bool(order.get("reduceOnly")),
    }


async def verify_perp_ladder(
    client: Any, symbol: str, expected: Any | None = None
) -> ExchangeStopState:
    """Is the ladder resting on the exchange for *symbol* right now?

    With an expectation (a running session's armed group) EVERY expected leg
    must rest — both legs of a pair, the single leg of a stop-only or trailing
    group — matched by order id or leg client id. Without one (a stopped
    session) any resting reduce-only leg of hydra's own ``hyd-`` client ids
    counts, on either side (shorts buy back). Read errors answer ``unknown``.
    """
    checked_at = datetime.now(UTC)
    try:
        plain = [normalize_perp_leg(o) for o in await client.fetch_open_orders(symbol)]
        algo = [
            normalize_perp_leg(o) for o in await client.fetch_open_orders(symbol, conditional=True)
        ]
    except Exception as exc:
        return ExchangeStopState(state="unknown", checked_at=checked_at, error=str(exc))
    legs = plain + algo
    if expected is None:
        matched = [
            leg
            for leg in legs
            if leg["reduce_only"]
            and (leg["client_order_id"] or "").startswith(CLIENT_ORDER_ID_PREFIX)
            and leg["status"] not in ("CANCELED", "REJECTED", "EXPIRED")
        ]
        if not matched:
            return ExchangeStopState(state="missing", checked_at=checked_at)
        stop = next((leg["stop_price"] for leg in matched if leg["stop_price"] is not None), None)
        return ExchangeStopState(
            state="confirmed", checked_at=checked_at, kind="stop", stop_price=stop
        )
    expected_id = str(expected.order_id)
    expected_legs = {str(leg) for leg in expected.leg_ids}
    matched = [
        leg
        for leg in legs
        if leg["id"] == expected_id
        or (leg["client_order_id"] or "") == expected_id
        or (leg["client_order_id"] or "") in expected_legs
        or leg["id"] in expected_legs
    ]
    distinct = {leg["client_order_id"] or leg["id"] for leg in matched}
    if len(distinct) >= (len(expected_legs) or 1):
        # a conditional take-profit carries its trigger in stop_price, like the stop
        stop = next(
            (
                leg["stop_price"]
                for leg in matched
                if leg["stop_price"] is not None and leg["reason"] != "take_profit"
            ),
            None,
        )
        take_profit = next(
            (
                leg["stop_price"]
                for leg in matched
                if leg["stop_price"] is not None and leg["reason"] == "take_profit"
            ),
            None,
        )
        return ExchangeStopState(
            state="confirmed",
            checked_at=checked_at,
            kind="oco" if len(expected_legs) > 1 else "stop",
            stop_price=stop,
            take_profit=take_profit,
            order_list_id=expected_id,
        )
    return ExchangeStopState(state="missing", checked_at=checked_at)


# ---------------------------------------------------------------------------
# The perp reconcile: deficit-only positions, booked funding, the same cadence
# ---------------------------------------------------------------------------


POSITION_DEFICIT_THRESHOLD = Decimal("0.001")
"""Relative tolerance for the position compare: the wallet drift's 0.1 %."""


async def reconcile_perp_session(
    store: LiveOrderStore,
    client: Any,
    executor: LivePerpTradingExecutor,
    *,
    poller: LiveOrderPoller | None = None,
    previous: Any | None = None,
    now: datetime | None = None,
) -> Any:
    """One reconcile pass for a live perp session; the A7 shape over perp truth.

    The spot pass's structure with the perp reads: the sweep first (it books
    funding income, resolves submits and cancels filled-leg siblings), then the
    drift pass — exchange legs from BOTH fapi namespaces against the tracked
    rows, proven terminal statuses applied monotonically, the position-side
    DEFICIT-ONLY compare of the sleeve against ``positionRisk`` (the exchange
    showing more than the sleeve is the user's own position on the shared
    one-way account, never drift; the sleeve's claim unbacked by the exchange
    is), and the same wallet-total compare. Nothing is adopted or rebased from
    a compare: findings page, exchange facts book.
    """
    ran_at = datetime.now(UTC) if now is None else now
    try:
        if poller is not None:
            await poller.sweep()
        else:
            await executor.collect_exchange_fills(None)
    except Exception as exc:
        logger.warning("Perp reconcile sweep of %s failed: %s", executor._session_id, exc)
        return _status("unknown", ran_at, f"resolution and fill sweep failed: {exc}", previous)
    try:
        return await _perp_drift_pass(store, client, executor, ran_at, previous)
    except Exception as exc:
        # A4≡S5: a failed dependent read is unknown — never a latched stale ok
        logger.warning(
            "Perp reconcile of %s failed on a dependent read: %s", executor._session_id, exc
        )
        return _status("unknown", ran_at, f"dependent read failed: {exc}", previous)


async def _perp_drift_pass(
    store: LiveOrderStore,
    client: Any,
    executor: LivePerpTradingExecutor,
    ran_at: datetime,
    previous: Any,
) -> Any:
    symbols = sorted(
        set(await store.symbols(executor._session_id))
        | {str(p.symbol) for p in await executor.get_positions() if p.quantity > 0}
    )
    legs: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            legs.extend(normalize_perp_leg(o) for o in await client.fetch_open_orders(symbol))
            legs.extend(
                normalize_perp_leg(o)
                for o in await client.fetch_open_orders(symbol, conditional=True)
            )
        except Exception as exc:
            logger.warning("Perp reconcile open orders of %s unreadable: %s", symbol, exc)
            return _status(
                "unknown", ran_at, f"open orders of {symbol} unreadable: {exc}", previous
            )
    findings: list[str] = []

    # (b) an exchange order no session knows is reported, never adopted (R5)
    for leg in legs:
        if not await _perp_tracked(store, leg):
            findings.append(f"untracked order {leg['id']} on {leg['symbol']} (not managed)")

    # (c) a row we hold open that the exchange no longer lists: prove how it ended
    for row in await store.open_orders(executor._session_id):
        if any(_perp_leg_matches(leg, row) for leg in legs):
            continue
        finding = await _perp_terminalize(client, executor, row)
        if finding is not None:
            findings.append(finding)

    # (d) what is still unsettled past the resolve grace is honestly unresolved
    deadline = ran_at - timedelta(seconds=executor.resolve_grace)
    for row in await store.unsettled(executor._session_id, deadline):
        findings.append(f"unresolved submit {row.client_order_id}")

    findings.extend(await _position_deficit_findings(client, executor, symbols))

    # (f) the sleeve's own wallet claim against the futures wallet total
    try:
        wallet = await client.fetch_balance_totals()
    except Exception as exc:
        logger.warning("Perp reconcile balance of %s unreadable: %s", executor._session_id, exc)
        return _status("unknown", ran_at, f"exchange balance unreadable: {exc}", previous)
    findings.extend(_balance_findings(await executor.fetch_balance(), wallet))
    if findings:
        return _status("mismatch", ran_at, "; ".join(findings), previous)
    return _status("ok", ran_at, None, previous)


async def _perp_tracked(store: LiveOrderStore, leg: Mapping[str, Any]) -> bool:
    """Is this exchange leg tracked by ANY session (a leg's own client id rides the pair row)?"""
    leg_id = str(leg.get("id") or "")
    if leg_id and await store.tracked_by_exchange_order_id(leg_id):
        return True
    client_id = str(leg.get("client_order_id") or "")
    if not client_id:
        return False
    if await store.by_client_order_id(client_id) is not None:
        return True
    return await store.tracked_by_leg_client_order_id(client_id)


def _perp_leg_matches(leg: Mapping[str, Any], row: LiveOrderRecord) -> bool:
    client_id = str(leg.get("client_order_id") or "")
    if client_id and (client_id == row.client_order_id or client_id in row.leg_client_order_ids):
        return True
    leg_id = str(leg.get("id") or "")
    return bool(leg_id and row.exchange_order_id is not None and leg_id == row.exchange_order_id)


async def _perp_terminalize(
    client: Any, executor: LivePerpTradingExecutor, row: LiveOrderRecord
) -> str | None:
    """Prove how a row the exchange no longer lists ended, and apply it monotonically."""
    if row.exchange_order_id is None:
        return None  # never acked: the poller's resolution path owns this row
    if row.leg_client_order_ids:
        answers: list[dict[str, Any]] = []
        for leg_cid in row.leg_client_order_ids:
            try:
                resp = await client.fetch_order(
                    leg_cid, row.symbol, {"clientOrderId": leg_cid, "conditional": True}
                )
            except Exception as exc:
                if _order_not_found(exc):
                    return f"pair {row.client_order_id} has a leg missing on the exchange"
                logger.info("Perp pair %s leg %s unreadable: %s", row.client_order_id, leg_cid, exc)
                return None  # unproven: nothing is applied, nothing is claimed
            answers.append(resp)
        return await _apply_perp_proven(executor, row, answers)
    try:
        if _is_conditional(row.order_type):
            answer = await client.fetch_order(
                row.client_order_id,
                row.symbol,
                {"clientOrderId": row.client_order_id, "conditional": True},
            )
        else:
            answer = await client.fetch_order(str(row.exchange_order_id), row.symbol, None)
    except Exception as exc:
        if _order_not_found(exc):
            return f"order {row.client_order_id} open locally, not found on the exchange"
        logger.info("Perp order %s status unreadable: %s", row.exchange_order_id, exc)
        return None
    return await _apply_perp_proven(executor, row, [answer])


async def _apply_perp_proven(
    executor: LivePerpTradingExecutor,
    row: LiveOrderRecord,
    answers: list[dict[str, Any]],
) -> str | None:
    """Apply the proven terminal status; a pair whose leg proves FILLED cancels the sibling."""
    statuses = [map_ccxt_status(a.get("status"), a.get("filled")) for a in answers]
    finding = await _apply_proven(executor.live_orders, row, answers)
    if finding is not None and row.order_type == OrderType.OCO.value:
        filled = next(
            (
                str(a.get("clientOrderId") or "")
                for a, s in zip(answers, statuses, strict=True)
                if s == LiveOrderStatus.FILLED
            ),
            None,
        )
        if filled:
            await executor.cancel_sibling_of(row, filled)
    return finding


async def _position_deficit_findings(
    client: Any, executor: LivePerpTradingExecutor, symbols: list[str]
) -> list[str]:
    """The sleeve's position claim vs ``positionRisk``, DEFICIT-ONLY.

    The exchange showing MORE than the sleeve (or the other side entirely) is
    the user's own position on the shared one-way account — not drift. The
    sleeve's claim not backed by the exchange is: a liquidation, a leg the fill
    pipeline has not caught, or the user closing the shared position. Reported,
    never adopted, never rebased.
    """
    try:
        rows = await client.fetch_position_risk()
    except Exception as exc:
        raise RuntimeError(f"positionRisk unreadable: {exc}") from exc
    by_symbol = {str(r.get("symbol")): _d(r.get("positionAmt") or 0) for r in rows}
    findings: list[str] = []
    for name in symbols:
        canon = executor._canon_of(name)
        pos = executor._positions.get(canon)
        sleeve = pos.qty if pos is not None else _ZERO
        if sleeve == _ZERO:
            continue
        exchange = by_symbol.get(name, _ZERO)
        backed = min(abs(sleeve), abs(exchange)) if (sleeve > 0) == (exchange > 0) else _ZERO
        deficit = abs(sleeve) - backed
        if deficit > POSITION_DEFICIT_THRESHOLD * abs(sleeve):
            findings.append(
                f"position deficit {name} sleeve {sleeve} exchange {exchange} "
                "(not backed by positionRisk)"
            )
    return findings
