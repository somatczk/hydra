"""The live session executor and its fill poller: real orders, one ledger, no simulation.

``LiveTradingExecutor`` subclasses the paper executor — the same spot ledger, the same
persistence and the same ``ExecutorState`` protocol — but never simulates: ``create_order``
is outbox-first (the PENDING_SUBMIT row exists before the exchange call, addressed by a
``hyd-``-prefixed ``newClientOrderId``), fills come back only from REST polling at the real
price and with the real commission, and the protective STOP_MARKET/OCO/TRAILING_STOP forms
delegate to ``exchange_stops``' ladder (seam #4) and answer the paper response shapes.

The exchange is the locker of resting funds: the sleeve ledger never locks for a resting
order, so wallet locks and ledger lots stay in their own layers.

``LiveOrderPoller`` is the single fill source (seam #1's runtime side): every sweep resolves
mid-submit UNKNOWNs by POSITIVE evidence only — a plain order by its client order id (a
single -2013 never terminalizes: rejection needs N consecutive not-founds across a window
plus a myTrades cross-check, F8), an OCO through the list-aware reads (the open-order-lists
lookup by ``listClientOrderId``, then its persisted leg ids, F9) — and applies the session's
OWN new trades (R5: a trade whose order id is not a known session order — the user's manual
order — never enters the ledger; lookups are session-scoped, F1). Fills are
claim-apply-confirm (F2): a claimed row that fails to apply stays unapplied and is retried by
every later sweep before new trades are claimed. Replay is harmless: the live_fills unique
constraint refuses the same Binance trade id twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast

from hydra.core.types import OHLCV, OrderType
from hydra.execution.errors import (
    ExchangeRateLimitedError,
    SubmitUnknownError,
)
from hydra.execution.exchange_stops import ExchangeStopPlacement, StopUnplaceableError
from hydra.execution.fill_rules import Liquidity
from hydra.execution.live_orders import (
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
    _STATE_FORMAT,
    InsufficientBalanceError,
    OrderRejectedError,
    PaperFill,
    PaperStateCorruptError,
    PaperStateUnavailableError,
    PaperTradingExecutor,
    _d,
    _opt_d,
    read_stored_account,
)
from hydra.execution.paper_trading import _exchange_id as _stored_exchange_id
from hydra.execution.protective_orders import ProtectionSpec
from hydra.execution.venue_stops import (
    arm_protective_stop,
    cancel_bybit_leg,
    fetch_bybit_leg,
    is_pair_venue,
)
from hydra.risk.market_rules import MarketRules

if TYPE_CHECKING:
    from hydra.execution.order_manager import OrderManager

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_LIVE_TYPES = (OrderType.MARKET, OrderType.LIMIT)
_LADDER_TYPES = (OrderType.STOP_MARKET, OrderType.OCO, OrderType.TRAILING_STOP)


class LivePollExecutor(Protocol):
    """The poller's structural view of a live executor (spot or perp).

    Type-only seam: the spot poller stays runtime-identical while the perp
    poller (``perp_live``) subclasses it over its own executor type.
    """

    _session_id: str
    resolve_grace: float
    exchange_client: Any

    @property
    def live_orders(self) -> LiveOrderStore: ...

    async def collect_exchange_fills(self, symbols: list[str] | None = None) -> list[PaperFill]: ...


def _order_not_found(exc: BaseException) -> bool:
    import ccxt

    return isinstance(exc, ccxt.OrderNotFound)


class _ClientIdClient:
    """Wraps the keyed client so every ladder order carries hydra's client ids.

    Plain orders send ``newClientOrderId``; an OCO sends the REAL wire contract (F9):
    ``listClientOrderId`` (the row's own id) plus explicit ``aboveClientOrderId`` /
    ``belowClientOrderId`` leg ids, each a distinct 36-character ``hyd-`` id.
    """

    def __init__(
        self, inner: Any, client_order_id: str, oco_leg_client_ids: tuple[str, ...] = ()
    ) -> None:
        self._inner = inner
        self._client_order_id = client_order_id
        self._oco_leg_client_ids = oco_leg_client_ids

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
        merged = {**(params or {}), "newClientOrderId": self._client_order_id}
        return await self._inner.create_order(
            symbol, side, order_type, quantity, price, stop_price, merged
        )

    async def create_oco(
        self,
        symbol: str,
        side: str,
        quantity: str,
        take_profit: str,
        stop_loss: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        above, below = self._oco_leg_client_ids[0], self._oco_leg_client_ids[-1]
        merged = {
            **(params or {}),
            "listClientOrderId": self._client_order_id,
            "aboveClientOrderId": above,
            "belowClientOrderId": below,
        }
        return await self._inner.create_oco(symbol, side, quantity, take_profit, stop_loss, merged)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LiveTradingExecutor(PaperTradingExecutor):
    """A Binance SPOT sleeve that sends real orders and books only real fills."""

    def __init__(
        self,
        client: Any,
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
        # the registry's kind: bybit's pair lifecycle replaces the binance OCO machinery
        self._pair_venue = is_pair_venue(self._exchange_id)
        store: LiveOrderStore
        if self._db_pool is not None:
            store = PgLiveOrderStore(self._db_pool)
        else:
            store = InMemoryLiveOrderStore()
        self._live_orders = store
        # one lock: a poller sweep and a flatten drain never apply one claimed fill twice
        self._collect_lock = asyncio.Lock()

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
        """Outbox-first: the pending_submit row, the exchange call, then the ack.

        MARKET and LIMIT go straight to the exchange. The SELL-side protective types
        (STOP_MARKET, OCO, TRAILING_STOP) delegate to the ``exchange_stops`` ladder and
        answer the paper response shapes (``id`` = the orderListId for an OCO, ``legs`` =
        the exchange legs, whose presence is the exchange ack). A synchronous FILLED ack is
        never applied as a fill: fills come only from the poll loop.
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
        if otype == OrderType.MARKET:
            mark, _ = self._market_mark(canon)
            if mark is None or mark <= 0:
                raise ValueError(f"No market price available for {symbol}")
            if side_u == "SELL":
                # a base-commission buy leaves a lot the step cannot express: sells floor
                quantity = self._floor_qty(canon, quantity)
            return await self._submit_plain(canon, side_u, otype, quantity, None, params)
        if otype == OrderType.LIMIT:
            if price is None or _d(price) <= 0:
                raise OrderRejectedError("LIMIT orders need a positive price")
            if side_u == "SELL":
                quantity = self._floor_qty(canon, quantity)
            return await self._submit_plain(canon, side_u, otype, quantity, _d(price), params)
        if otype in _LADDER_TYPES and side_u == "SELL":
            return await self._submit_protective(canon, otype, quantity, price, stop_price, params)
        raise OrderRejectedError(
            f"{otype} {side_u} is not a live-supported order type"
            f" (MARKET, LIMIT, and SELL STOP_MARKET/OCO/TRAILING_STOP)"
        )

    async def _submit_plain(
        self,
        canon: str,
        side_u: str,
        otype: str,
        quantity: Decimal,
        price: Decimal | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        ref = self._market_mark(canon)[0] if otype == OrderType.MARKET else price
        if ref is None or ref <= 0:
            raise ValueError(f"No market price available for {canon}")
        self._check_rules(canon, quantity, ref)
        self._check_free(canon, side_u, quantity, ref)
        if side_u == "BUY":
            await self._require_persisted()
        tag = str(params.get("tag") or "")
        reduce_only = bool(params.get("reduce_only"))
        cid = new_client_order_id()
        # the venue's client-id field: binance newClientOrderId, bybit's orderLinkId
        cid_param = "clientOrderId" if self._pair_venue else "newClientOrderId"
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
        try:
            resp = await self.exchange_client.create_order(
                self._name(canon),
                side_u,
                otype.lower(),
                quantity,
                price,
                None,
                {cid_param: cid},
            )
        except Exception as exc:
            raise await self._fail_submit(cid, otype, exc) from exc
        status = map_ccxt_status(resp.get("status"), resp.get("filled"))
        if not await self._live_orders.mark_ack(cid, str(resp["id"]), status):
            logger.info("Live ack for %s refused: the row was already resolved", cid)
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

    async def _submit_protective(
        self,
        canon: str,
        otype: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        lot = self._lots.get(canon)
        if lot is None or lot.base_qty <= 0:
            raise OrderRejectedError(f"{self._name(canon)} is not held: nothing to protect")
        quantity = self._floor_qty(canon, quantity)
        if quantity <= 0:
            raise OrderRejectedError(
                f"{self._name(canon)} protective quantity floors to zero at the lot step"
            )
        if quantity > lot.base_qty:
            raise OrderRejectedError(
                f"{self._name(canon)} protective quantity {quantity} "
                f"exceeds the held {lot.base_qty}"
            )
        spec = _protective_spec(otype, price, stop_price, params)
        mark, _ = self._market_mark(canon)
        if mark is None or mark <= 0:
            raise ValueError(f"No market price available for {canon}")
        entry = lot.price_basis / lot.base_qty
        self._check_rules(canon, quantity, spec.stop_loss or mark)
        tag = str(params.get("tag") or "protective")
        # build_live_executor validated these are real MarketRules, not the paper trio
        rules = cast(MarketRules, self._market_rules[canon])
        cid = new_client_order_id()
        leg_cids = (new_client_order_id(), new_client_order_id())
        # bybit: every order this row may place gets its own cid (the poller resolves by them)
        pair_leg_cids: tuple[str, ...]
        fallback_cids: tuple[str, ...]
        row_leg_ids: tuple[str, ...]
        if self._pair_venue:
            fallback_cids = (new_client_order_id(), new_client_order_id())
            if otype == OrderType.OCO.value:
                pair_leg_cids = (leg_cids[1], leg_cids[0])
                row_leg_ids = leg_cids + fallback_cids
            else:
                pair_leg_cids = ()
                row_leg_ids = fallback_cids
        else:
            pair_leg_cids = ()
            fallback_cids = ()
            row_leg_ids = leg_cids if otype == OrderType.OCO.value else ()
        await self._live_orders.create(
            LiveOrderRecord(
                client_order_id=cid,
                session_id=self._session_id,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
                symbol=self._name(canon),
                side="SELL",
                order_type=otype,
                quantity=quantity,
                price=spec.take_profit,
                stop_price=spec.stop_loss,
                status=LiveOrderStatus.PENDING_SUBMIT,
                reduce_only=True,
                tag=tag,
                leg_client_order_ids=row_leg_ids,
                submitted_at=datetime.now(UTC),
            )
        )
        try:
            if self._pair_venue:
                placement = await arm_protective_stop(
                    self._exchange_id,
                    self.exchange_client,
                    rules,
                    self._name(canon),
                    quantity,
                    spec,
                    mark,
                    entry,
                    group_cid=cid,
                    pair_leg_cids=pair_leg_cids,
                    fallback_cids=fallback_cids,
                )
            else:
                placement = await arm_protective_stop(
                    self._exchange_id,
                    _ClientIdClient(self.exchange_client, cid, leg_cids),
                    rules,
                    self._name(canon),
                    quantity,
                    spec,
                    mark,
                    entry,
                    group_cid=cid,
                    pair_leg_cids=(),
                    fallback_cids=(),
                )
        except StopUnplaceableError as exc:
            await self._live_orders.mark_submit_failed(cid, str(exc))
            raise
        except Exception as exc:
            if isinstance(exc, ValueError):
                # StopBreachedError and the ladder's own ValueErrors: the wiring decides (R18)
                raise
            raise await self._fail_submit(cid, otype, exc) from exc
        list_id = str(placement.order_id) if placement.kind in ("oco", "pair") else None
        if not await self._live_orders.mark_ack(
            cid, str(placement.order_id), LiveOrderStatus.NEW, order_list_id=list_id
        ):
            logger.info("Live protective ack for %s refused: the row was already resolved", cid)
        return _placement_response(
            symbol=self._name(canon),
            side="SELL",
            otype=otype,
            quantity=quantity,
            cid=cid,
            placement=placement,
        )

    async def _fail_submit(self, cid: str, otype: str, exc: Exception) -> Exception:
        """Classify a failed exchange call: definitive rejections mark the row rejected."""
        import ccxt

        # ccxt's rate-limit family is a NetworkError subclass but a definitive refusal
        if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
            await self._live_orders.mark_submit_failed(cid, str(exc))
            return ExchangeRateLimitedError(f"{self._exchange_id} refused the call: {exc}")
        if isinstance(exc, ccxt.InsufficientFunds):
            await self._live_orders.mark_submit_failed(cid, str(exc))
            return InsufficientBalanceError(str(exc))
        if isinstance(exc, ccxt.NetworkError):
            await self._live_orders.apply_status(cid, LiveOrderStatus.UNKNOWN, error=str(exc))
            return SubmitUnknownError(cid, f"{otype} submit outcome unknown: {exc}")
        if isinstance(exc, ccxt.ExchangeError):
            await self._live_orders.mark_submit_failed(cid, str(exc))
            return OrderRejectedError(str(exc))
        await self._live_orders.apply_status(cid, LiveOrderStatus.UNKNOWN, error=str(exc))
        return SubmitUnknownError(cid, f"{otype} submit failed without an answer: {exc}")

    def _check_free(self, canon: str, side_u: str, quantity: Decimal, ref: Decimal) -> None:
        """The sleeve must afford the order too — the wallet locking alone is not our ledger."""
        if side_u == "BUY":
            need = quantity * ref * (_ONE + max(self._maker_fee, self._taker_fee))
            if self._ledger.get_free(self._quote) < need:
                raise InsufficientBalanceError(
                    f"Insufficient {self._quote} balance for buy: need {need}, "
                    f"free {self._ledger.get_free(self._quote)}"
                )
        elif self._ledger.get_free(self._base(canon)) < quantity:
            raise InsufficientBalanceError(
                f"Insufficient {self._base(canon)} balance for sell: need {quantity}, "
                f"free {self._ledger.get_free(self._base(canon))} (spot cannot sell short)"
            )

    def _floor_qty(self, canon: str, quantity: Decimal) -> Decimal:
        """Floor a SELL quantity to the lot step: a base-commission buy leaves dust the
        step cannot express, and the exchange refuses any non-multiple."""
        rules = self._market_rules.get(canon)
        if rules is not None and rules.amount_step is not None:
            return rules.floor_qty(quantity)
        return quantity

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel on the exchange; an already-terminal row is a no-op success.

        A submit still in flight answers AMBIGUOUS (F11): the row stays for the
        resolution path — a definitive CANCELLED while the submit can still land would
        hide a resting order. A row that never acked has no exchange id, so its cancel
        goes out by client id and an OrderNotFound answer is ambiguous too. A bybit
        pair row cancels BOTH legs (bybit has no list-cancel; the not-found of a leg
        that never landed is a quiet no-op).
        """
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
        if row is not None and row.status in TERMINAL_STATUSES:
            return {"id": order_id, "status": "CANCELLED"}
        if row is not None and row.status in (
            LiveOrderStatus.PENDING_SUBMIT,
            LiveOrderStatus.SUBMITTING,
        ):
            return {"id": order_id, "status": "AMBIGUOUS"}
        if row is not None and self._pair_venue:
            if row.leg_client_order_ids:
                # bybit's no-list-id conditional: every leg cancels through its own cid
                for leg_cid in row.leg_client_order_ids:
                    try:
                        await cancel_bybit_leg(self.exchange_client, leg_cid, row.symbol)
                    except Exception as exc:
                        if _order_not_found(exc):
                            logger.info("Bybit leg %s already gone on the exchange", leg_cid)
                        else:
                            raise
            else:
                target = order_id if row.exchange_order_id is None else row.exchange_order_id
                await self.exchange_client.cancel_order(target, row.symbol, None)
        else:
            target = (
                order_id if row is None or row.exchange_order_id is None else row.exchange_order_id
            )
            try:
                if row is not None and row.order_type == OrderType.OCO.value:
                    await self.exchange_client.cancel_order_list(target, self._name(row.symbol))
                else:
                    await self.exchange_client.cancel_order(target, symbol)
            except Exception as exc:
                if _order_not_found(exc):
                    if row is not None and row.exchange_order_id is None:
                        # never acked: "not found" does not mean the submit cannot land
                        return {"id": order_id, "status": "AMBIGUOUS"}
                    logger.info("Live cancel of %s: already gone on the exchange", order_id)
                    return {"id": order_id, "status": "CANCELLED"}
                raise
        if row is not None:
            outcome = await self._live_orders.apply_status(
                row.client_order_id, LiveOrderStatus.CANCELED
            )
            if outcome == "refused":
                logger.info(
                    "Live cancel of %s left the row at %s: already resolved",
                    order_id,
                    row.status.value,
                )
        return {"id": order_id, "status": "CANCELLED"}

    async def cancel_all_orders(self, symbol: str) -> Any:
        """R4: the kill-switch sweep's duck-typed gate — every open order on the symbol."""
        return await self.exchange_client.cancel_all_orders(symbol)

    # ------------------------------------------------------------------
    # Closed bars: full bookkeeping minus the resting simulation (R17)
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

        Claim-apply-confirm (F2): unapplied rows from an earlier failed sweep are retried
        BEFORE new trades are claimed, so one transient bookkeeping failure never orphans
        a real fill. The whole pipeline runs under the collect lock, so a poller sweep and
        a flatten drain cannot apply the same claimed fill twice.
        """
        async with self._collect_lock:
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
                logger.exception("Live fill %s could not be re-applied", claimed.trade_id)
        wanted = None if symbols is None else {self._symbol(s) for s in symbols}
        tracked = {self._canon_of(s) for s in await self._live_orders.symbols(self._session_id)}
        tracked |= {c for c, lot in self._lots.items() if lot.base_qty > 0}
        targets = sorted(c for c in tracked if wanted is None or c in wanted)
        for canon in targets:
            name = self._name(canon)
            since = await self._live_orders.last_trade_ts(self._session_id, name)
            try:
                trades = await self.exchange_client.fetch_my_trades(name, since)
            except Exception as exc:
                logger.warning("Live trades of %s unreadable: %s", name, exc)
                continue
            for trade in trades:
                try:
                    fill = await self._fill_from_trade(canon, trade)
                except Exception:
                    logger.exception("Live trade of %s could not be applied", name)
                    continue
                if fill is not None:
                    fills.append(fill)
        return fills

    def _canon_of(self, symbol: str) -> str:
        try:
            return self._symbol(symbol)
        except OrderRejectedError:
            return symbol.split(":", 1)[0].replace("/", "").replace("-", "").upper()

    async def _fill_from_trade(self, canon: str, trade: Mapping[str, Any]) -> PaperFill | None:
        raw_info = trade.get("info")
        info: Mapping[str, Any] = raw_info if isinstance(raw_info, Mapping) else {}
        exchange_order_id = str(trade.get("order") or info.get("orderId") or "")
        list_id = str(info.get("orderListId") or "")
        row = None
        leg_cid: str | None = None
        if exchange_order_id:
            row = await self._live_orders.by_exchange_order_id(exchange_order_id, self._session_id)
        if row is None and list_id not in ("", "-1"):
            row = await self._live_orders.by_order_list_id(list_id, self._session_id)
        if row is None and self._pair_venue:
            # bybit has no list id: a leg's trade maps to its row through the client id
            row, leg_cid = await self._cross_check_trade_order(canon, trade)
        if row is None:
            return None  # R5: the user's orders never enter the session ledger
        if row.session_id != self._session_id:
            # F1: a trade of another session's order on the same account is never ours
            logger.error(
                "Live trade %s maps to session %s, not %s: skipped",
                trade.get("id"),
                row.session_id,
                self._session_id,
            )
            return None
        raw_fee_obj = trade.get("fee")
        fee: Mapping[str, Any] = raw_fee_obj if isinstance(raw_fee_obj, Mapping) else {}
        raw_fee = _d(fee.get("cost") or info.get("commission") or 0)
        fee_currency = str(fee.get("currency") or info.get("commissionAsset") or self._quote)
        timestamp = int(trade.get("timestamp") or info.get("time") or 0)
        fill = LiveFillRecord(
            trade_id=str(trade.get("id") or info.get("id")),
            client_order_id=row.client_order_id,
            session_id=self._session_id,
            exchange_id=self._exchange_id,
            symbol=self._name(canon),
            side=str(trade.get("side") or ("buy" if info.get("isBuyer") else "sell")).upper(),
            price=_d(trade.get("price") or info.get("price")),
            quantity=_d(trade.get("amount") or info.get("qty")),
            fee=raw_fee,
            fee_currency=fee_currency,
            liquidity=str(
                trade.get("takerOrMaker") or ("maker" if info.get("isMaker") else "taker")
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
            logger.info("Live trade order %s unreadable for the cross-check: %s", order_id, exc)
            return None, None
        client_id = str(resp.get("clientOrderId") or "")
        if not client_id:
            return None, None
        by_client = await self._live_orders.by_client_order_id(client_id)
        if by_client is not None and by_client.session_id == self._session_id:
            return by_client, None
        # the pair row may already be terminal: search every tracked row, not just open ones
        for row in await self._live_orders.orders(self._session_id, limit=200):
            if client_id in row.leg_client_order_ids:
                return row, client_id
        return None, None

    async def _apply_claimed_fill(
        self,
        fill: LiveFillRecord,
        row: LiveOrderRecord,
        *,
        filled_leg_cid: str | None = None,
    ) -> PaperFill:
        """Apply one claimed fill to the ledger, then confirm it as applied (F2).

        A bybit pair applies its own terminal status here (the exchange never cancels
        the sibling: hydra does) and cancels every other leg of the group — the fill
        path's half of the both-paths sibling-cancel; the reconcile path proves the
        same ending from the leg reads when the fill happened while the engine was
        down. The not-found of a leg that never landed is a quiet no-op.
        """
        parent = row.exchange_order_id if row.order_type == OrderType.OCO.value else None
        applied = await self.apply_exchange_fill(
            symbol=fill.symbol,
            side=fill.side,
            order_type=row.order_type,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fee_currency=fill.fee_currency,
            liquidity=cast(Liquidity, fill.liquidity),
            reason=row.tag,
            order_id=parent or row.exchange_order_id or fill.client_order_id,
            parent_order_id=parent,
            timestamp=fill.timestamp,
        )
        if self._pair_venue and row.order_type == OrderType.OCO.value:
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
            await self._cancel_bybit_siblings(row, filled_leg_cid)
        await self._live_orders.mark_fill_applied(fill)
        return applied

    async def _cancel_bybit_siblings(
        self, row: LiveOrderRecord, filled_leg_cid: str | None
    ) -> None:
        """SIBLING-CANCEL (the fill path): a pair leg filled — cancel every OTHER leg
        of the group that still rests. The unused rung cids of a degraded ladder rest
        nothing, so only what actually rests is cancelled: never a not-found no-op."""
        if not row.leg_client_order_ids:
            return
        try:
            resting = {
                str(o.get("clientOrderId") or "")
                for o in await self.exchange_client.fetch_open_orders(row.symbol, conditional=True)
            }
        except Exception:
            logger.exception("Bybit resting legs of %s unreadable", row.symbol)
            return
        for leg_cid in row.leg_client_order_ids:
            if leg_cid == filled_leg_cid or leg_cid not in resting:
                continue
            try:
                await cancel_bybit_leg(self.exchange_client, leg_cid, row.symbol)
            except Exception:
                logger.exception("Bybit sibling-cancel of %s failed", leg_cid)

    # ------------------------------------------------------------------
    # Flatten and reads
    # ------------------------------------------------------------------

    async def flatten(self, symbol: str | None = None, reason: str = "flatten") -> list[PaperFill]:
        """Cancel resting orders and reduce-only market-sell every holding for real.

        Completion is confirmed by the fill drain that follows the sells (the poll loop
        owns fills), so the returned list is the fills that already landed.
        """
        canon = None if symbol is None else self._symbol(symbol)
        if canon is not None:
            targets = {canon}
        else:
            targets = set(await self._live_orders.symbols(self._session_id))
            targets |= {c for c, lot in self._lots.items() if lot.base_qty > 0}
        for row in await self._live_orders.open_orders(self._session_id):
            if self._canon_of(row.symbol) not in targets or row.exchange_order_id is None:
                continue
            try:
                await self.cancel_order(row.exchange_order_id, row.symbol)
            except Exception as exc:
                logger.warning("Live cancel of %s during flatten failed: %s", row.symbol, exc)
        failures: list[Exception] = []
        for target in sorted(targets):
            lot = self._lots.get(target)
            qty = self._floor_qty(target, self._ledger.get_free(self._base(target)))
            if lot is None or qty <= 0:
                continue
            try:
                await self.create_order(
                    self._name(target),
                    "SELL",
                    OrderType.MARKET.value,
                    qty,
                    params={"tag": reason, "reduce_only": True},
                )
            except Exception as exc:
                failures.append(exc)
        fills = await self.collect_exchange_fills(None)
        if failures:
            raise failures[0]
        return fills

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """The tracked (DB) rows in the paper open-order shape plus status and filled qty.

        The row values are exact Decimals (the shared open-order shape); JSON-facing
        callers render them through ``exchange_stops.json_safe_open_order``.
        """
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
                    "amount": row.quantity,
                    "price": row.price,
                    "stop_price": row.stop_price,
                    "reason": _row_reason(row),
                    "oco_id": (
                        row.exchange_order_id if row.order_type == OrderType.OCO.value else None
                    ),
                    "trail_pct": None,
                    "status": row.status.value,
                    "filled_quantity": row.filled_quantity,
                    "client_order_id": row.client_order_id,
                    "leg_ids": list(row.leg_client_order_ids),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    @classmethod
    async def restore_live(
        cls,
        db_pool: Any,
        session_id: str,
        *,
        client: Any,
        market_rules: Mapping[str, MarketRules] | None = None,
        poll_interval: float = 5.0,
        resolve_grace: float | None = None,
    ) -> LiveTradingExecutor | None:
        """Rebuild the live sleeve from ``paper_accounts`` around a freshly built client."""
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
                raise ValueError(f"unknown paper state format {state.get('format')!r}")
            ex = cls(
                client,
                exchange_id=_stored_exchange_id(stored.exchange_id),
                initial_balances={str(state["quote_asset"]): _ZERO},
                slippage_pct=_d(state["slippage_pct"]),
                db_pool=db_pool,
                strategy_id=stored.strategy_id,
                session_id=stored.session_id,
                maker_fee=_d(state["maker_fee"]),
                taker_fee=_d(state["taker_fee"]),
                quote_asset=str(state["quote_asset"]),
                step_size=_opt_d(state["step_size"]),
                min_notional=_opt_d(state["min_notional"]),
                market_rules=market_rules,
                poll_interval=poll_interval,
                resolve_grace=resolve_grace,
            )
            ex._rebuild_from_state(state)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise PaperStateCorruptError(f"live state for {session_id!r}: {exc}") from exc
        ex._state_version = stored.state_version
        ex._filled = [ex._restored_fill(t) for t in trades]
        return ex


def _protective_spec(
    otype: str,
    price: Decimal | None,
    stop_price: Decimal | None,
    params: Mapping[str, Any],
) -> ProtectionSpec:
    if otype == OrderType.OCO.value:
        if price is None or stop_price is None:
            raise ValueError(
                "OCO orders require both price (take-profit limit) "
                "and stop_price (stop-loss trigger)"
            )
        tp, sl = _d(price), _d(stop_price)
        if not sl < tp:
            raise OrderRejectedError(f"SELL OCO needs stop {sl} below take-profit {tp}")
        return ProtectionSpec(stop_loss=sl, take_profit=tp)
    if otype == OrderType.STOP_MARKET.value:
        stop = _opt_d(stop_price) or _opt_d(price)
        if stop is None or stop <= 0:
            raise OrderRejectedError("STOP_MARKET orders need a positive stop_price")
        return ProtectionSpec(stop_loss=stop)
    raw = params.get("trail_pct")
    if raw is None:
        raise ValueError("trail_pct is required for TRAILING_STOP orders")
    return ProtectionSpec(trailing_pct=_d(raw))


def _placement_response(
    *,
    symbol: str,
    side: str,
    otype: str,
    quantity: Decimal,
    cid: str,
    placement: ExchangeStopPlacement,
) -> dict[str, Any]:
    # the exchange legs are the ack (even a single-leg stop carries itself)
    legs = [{"id": str(leg)} for leg in placement.leg_ids] or [{"id": str(placement.order_id)}]
    return {
        "id": str(placement.order_id),
        "symbol": symbol,
        "side": side,
        "type": otype,
        "amount": float(quantity),
        "price": None if placement.take_profit is None else float(placement.take_profit),
        "stop_price": None if placement.stop_price is None else float(placement.stop_price),
        "status": "PENDING",
        "filled": False,
        "oco_link": str(placement.order_id) if placement.kind in ("oco", "pair") else None,
        "client_order_id": cid,
        "legs": legs,
    }


def _row_reason(row: LiveOrderRecord) -> str:
    if row.order_type == OrderType.OCO.value:
        return "stop_loss"
    if row.order_type == OrderType.STOP_MARKET.value:
        return "stop_loss" if row.side == "SELL" else "stop_entry"
    if row.order_type == OrderType.TRAILING_STOP.value:
        return "trailing_stop"
    if row.order_type == OrderType.LIMIT.value:
        return "limit"
    return "market"


class LiveOrderPoller:
    """The single fill source for one live session (started by the runtime's ``wire``).

    An unsettled row is only ever resolved by POSITIVE evidence. A plain order resolves by
    its client id on the order endpoint; a single -2013 never terminalizes (F8): rejection
    needs N consecutive not-founds across a window AND a myTrades cross-check that finds no
    trade for the client id. An OCO row resolves ONLY through list-aware reads (F9): the
    open-order-lists lookup by ``listClientOrderId``, then the legs by their client ids —
    never ``fetch_order`` on the list id, and never a not-found off the order endpoint.
    """

    def __init__(
        self,
        executor: LivePollExecutor,
        om: OrderManager,
        *,
        interval: float,
        resolve_grace: float,
        not_found_window: float = 60.0,
    ) -> None:
        self._executor = executor
        self._om = om
        self._interval = interval
        self._resolve_grace = resolve_grace
        self._not_found_window = not_found_window
        self._missing: dict[str, tuple[datetime, int]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(), name=f"live-poller-{self._executor._session_id}"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("Live poll sweep failed for %s", self._executor._session_id)
            await asyncio.sleep(self._interval)

    async def sweep(self) -> None:
        await self._resolve_unsettled()
        await self._refresh_open_statuses()
        for fill in await self._executor.collect_exchange_fills(None):
            try:
                await self._om.on_external_fill(fill)
            except Exception:
                logger.exception("Publishing live fill %s failed", fill.order_id)

    async def _resolve_unsettled(self) -> None:
        store = self._executor.live_orders
        client = self._executor.exchange_client
        pair_venue = getattr(self._executor, "_pair_venue", False)
        deadline = datetime.now(UTC) - timedelta(seconds=self._resolve_grace)
        for row in await store.unsettled(self._executor._session_id, deadline):
            if row.order_type == OrderType.OCO.value or row.order_list_id is not None:
                if pair_venue:
                    await self._resolve_bybit_pair_row(row)
                else:
                    await self._resolve_oco_row(row)
                continue
            if pair_venue and row.leg_client_order_ids:
                # a bybit single conditional rests under a leg cid, never the group cid
                await self._resolve_bybit_pair_row(row)
                continue
            # the venue's client-id read: binance origClientOrderId, bybit the orderLinkId
            params = (
                {"orderLinkId": row.client_order_id}
                if pair_venue
                else {"clientOrderId": row.client_order_id}
            )
            try:
                resp = await client.fetch_order(row.client_order_id, row.symbol, params)
            except Exception as exc:
                if _order_not_found(exc):
                    await self._note_missing(row)
                else:
                    logger.info("Live submit %s not resolvable yet: %s", row.client_order_id, exc)
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
                    "Live submit %s could not be resolved: the row moved on", row.client_order_id
                )

    async def _resolve_bybit_pair_row(self, row: LiveOrderRecord) -> None:
        """A bybit pair or single-conditional row resolves through its legs' client ids
        in the conditional (tpslOrder) namespace — never a list id, which bybit does not
        have, and never the group cid, which is not the wire's orderLinkId of a single
        conditional. With no positive evidence it stays unknown, exactly like the OCO
        rule (F9)."""
        store = self._executor.live_orders
        client = self._executor.exchange_client
        try:
            resting = await client.fetch_open_orders(row.symbol, conditional=True)
        except Exception as exc:
            logger.info("Live bybit pair %s legs unreadable: %s", row.client_order_id, exc)
            return
        for leg in resting:
            cid = str(leg.get("clientOrderId") or "")
            if cid and cid in row.leg_client_order_ids:
                resolved = await store.resolve_unknown(
                    row.client_order_id, str(leg["id"]), LiveOrderStatus.NEW
                )
                logger.info(
                    "Live bybit pair %s resolved by its resting leg (%s)",
                    row.client_order_id,
                    resolved,
                )
                return
        if not await self._trades_cross_check(row):
            logger.info(
                "Live bybit pair %s has no resting leg or trades yet: stays unknown",
                row.client_order_id,
            )

    async def _note_missing(self, row: LiveOrderRecord) -> None:
        """One -2013 is evidence, not a verdict (F8): N consecutive across a window,
        then a myTrades cross-check, before any 'unresolved submit' rejection."""
        store = self._executor.live_orders
        now = datetime.now(UTC)
        first, count = self._missing.get(row.client_order_id, (now, 0))
        count += 1
        self._missing[row.client_order_id] = (first, count)
        if count < _NOT_FOUND_REQUIRED or (now - first).total_seconds() < self._not_found_window:
            logger.info(
                "Live submit %s not found on exchange (%d consecutive)", row.client_order_id, count
            )
            return
        if await self._trades_cross_check(row):
            self._missing.pop(row.client_order_id, None)
            return
        outcome = await store.apply_status(
            row.client_order_id,
            LiveOrderStatus.REJECTED,
            error="unresolved submit: not found on exchange",
        )
        logger.info("Live submit %s resolved not-landed (%s)", row.client_order_id, outcome)
        self._missing.pop(row.client_order_id, None)

    async def _resolve_oco_row(self, row: LiveOrderRecord) -> None:
        """An OCO row resolves only through list-aware reads (F9), never the order
        endpoint's word for a list id; with no positive evidence it stays unknown."""
        store = self._executor.live_orders
        client = self._executor.exchange_client
        try:
            lists = await client.fetch_open_order_lists(row.symbol)
        except Exception as exc:
            logger.info("Live OCO %s lists unreadable: %s", row.client_order_id, exc)
            return
        for entry in lists:
            if str(entry.get("listClientOrderId") or "") == row.client_order_id:
                list_id = str(entry.get("orderListId"))
                resolved = await store.resolve_unknown(
                    row.client_order_id, list_id, LiveOrderStatus.NEW, order_list_id=list_id
                )
                logger.info(
                    "Live OCO %s resolved by its open list (%s)", row.client_order_id, resolved
                )
                return
        for leg_cid in row.leg_client_order_ids:
            try:
                resp = await client.fetch_order(
                    leg_cid, row.symbol, params={"clientOrderId": leg_cid}
                )
            except Exception:
                logger.info("Live OCO %s leg %s unreadable", row.client_order_id, leg_cid)
                continue
            await self._resolve_from_order(row, resp)
            return
        if not await self._trades_cross_check(row):
            logger.info(
                "Live OCO %s has no list, legs or trades yet: stays unknown", row.client_order_id
            )

    async def _resolve_from_order(self, row: LiveOrderRecord, resp: Mapping[str, Any]) -> bool:
        """Resolve an unsettled row from one exchange order of ours (a leg or the order)."""
        store = self._executor.live_orders
        info = resp.get("info")
        list_id = ""
        if row.order_type == OrderType.OCO.value and isinstance(info, Mapping):
            list_id = str(info.get("orderListId") or "")
        has_list = list_id not in ("", "-1")
        exchange_id = list_id if has_list else str(resp.get("id") or "")
        status = map_ccxt_status(resp.get("status"), resp.get("filled"))
        resolved = await store.resolve_unknown(
            row.client_order_id,
            exchange_id,
            status,
            filled_quantity=_opt_d(resp.get("filled")),
            average_fill_price=_opt_d(resp.get("average")),
            order_list_id=list_id if has_list else None,
        )
        if resolved:
            logger.info(
                "Live submit %s resolved from its exchange order as %s",
                row.client_order_id,
                status.value,
            )
        return resolved

    async def _trades_cross_check(self, row: LiveOrderRecord) -> bool:
        """F8's guard before any rejection: a trade for this row's client id means the
        submit landed and filled — resolve it as such, never reject."""
        client = self._executor.exchange_client
        # submitted_at precedes any fill (outbox-first); the buffer absorbs clock skew
        since = None if row.submitted_at is None else row.submitted_at - timedelta(minutes=5)
        try:
            trades = await client.fetch_my_trades(row.symbol, since)
        except Exception as exc:
            logger.info("Live trades of %s unreadable for the cross-check: %s", row.symbol, exc)
            return False
        wanted = {row.client_order_id, *row.leg_client_order_ids}
        for trade in trades:
            order_id = str(trade.get("order") or "")
            if not order_id:
                continue
            try:
                resp = await client.fetch_order(order_id, row.symbol)
            except Exception:
                logger.info("Live trade order %s unreadable for the cross-check", order_id)
                continue
            if str(resp.get("clientOrderId") or "") in wanted:
                return await self._resolve_from_order(row, resp)
        return False

    async def _refresh_open_statuses(self) -> None:
        store = self._executor.live_orders
        client = self._executor.exchange_client
        pair_venue = getattr(self._executor, "_pair_venue", False)
        for row in await store.refreshable(self._executor._session_id):
            if row.order_list_id is not None:
                if pair_venue:
                    await self._refresh_bybit_pair_row(row)
                else:
                    await self._refresh_oco_row(row)
                continue
            try:
                resp = await client.fetch_order(str(row.exchange_order_id), row.symbol)
            except Exception as exc:
                logger.info("Live order %s status unreadable: %s", row.exchange_order_id, exc)
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
                    "Live order %s status refresh refused: the row is resolved",
                    row.exchange_order_id,
                )

    async def _refresh_oco_row(self, row: LiveOrderRecord) -> None:
        """An acked OCO refreshes list-aware (F9): resting while the list is open; the
        legs say how it ended once it is not."""
        store = self._executor.live_orders
        client = self._executor.exchange_client
        try:
            lists = await client.fetch_open_order_lists(row.symbol)
        except Exception as exc:
            logger.info("Live OCO %s lists unreadable: %s", row.exchange_order_id, exc)
            return
        if any(str(entry.get("orderListId")) == str(row.exchange_order_id) for entry in lists):
            return
        legs: list[Mapping[str, Any]] = []
        for leg_cid in row.leg_client_order_ids:
            try:
                resp = await client.fetch_order(
                    leg_cid, row.symbol, params={"clientOrderId": leg_cid}
                )
            except Exception:
                return  # the legs are unreadable: leave the row exactly as it is
            legs.append(resp)
        statuses = [map_ccxt_status(leg.get("status"), leg.get("filled")) for leg in legs]
        if LiveOrderStatus.FILLED in statuses:
            filled = next(
                leg for leg, s in zip(legs, statuses, strict=False) if s == LiveOrderStatus.FILLED
            )
            await store.apply_status(
                row.client_order_id,
                LiveOrderStatus.FILLED,
                filled_quantity=_opt_d(filled.get("filled")),
                average_fill_price=_opt_d(filled.get("average")),
            )
        elif statuses and all(s == LiveOrderStatus.CANCELED for s in statuses):
            await store.apply_status(row.client_order_id, LiveOrderStatus.CANCELED)

    async def _refresh_bybit_pair_row(self, row: LiveOrderRecord) -> None:
        """An acked bybit pair refreshes through its legs: resting while any leg is in
        the conditional namespace; once none rests, each leg is read by its client id
        (positive proof only) — a proven-filled leg applies FILLED and cancels the
        siblings (the reconcile path's half of the both-paths sibling-cancel), every
        leg cancelled applies CANCELED, an unreadable leg leaves the row as it is."""
        store = self._executor.live_orders
        client = self._executor.exchange_client
        import ccxt

        try:
            resting = {
                str(o.get("clientOrderId") or "")
                for o in await client.fetch_open_orders(row.symbol, conditional=True)
            }
        except Exception as exc:
            logger.info("Live bybit pair %s legs unreadable: %s", row.exchange_order_id, exc)
            return
        if any(cid in resting for cid in row.leg_client_order_ids):
            return
        legs: list[dict[str, Any]] = []
        for leg_cid in row.leg_client_order_ids:
            try:
                legs.append(await fetch_bybit_leg(client, leg_cid, row.symbol))
            except ccxt.OrderNotFound:
                continue  # that rung's leg never landed: nothing to prove about it
            except Exception:
                logger.info("Live bybit pair leg %s unreadable", leg_cid)
                return  # leave the row exactly as it is
        if not legs:
            return  # no leg ever answered: the resolution path owns this row
        statuses = [map_ccxt_status(leg.get("status"), leg.get("filled")) for leg in legs]
        if LiveOrderStatus.FILLED in statuses:
            filled = next(
                leg for leg, s in zip(legs, statuses, strict=False) if s == LiveOrderStatus.FILLED
            )
            await store.apply_status(
                row.client_order_id,
                LiveOrderStatus.FILLED,
                filled_quantity=_opt_d(filled.get("filled")),
                average_fill_price=_opt_d(filled.get("average")),
            )
            for leg, status in zip(legs, statuses, strict=False):
                if str(leg.get("clientOrderId") or "") == str(filled.get("clientOrderId") or ""):
                    continue
                if status in TERMINAL_STATUSES:
                    continue  # already ended: never a second cancel call
                try:
                    await cancel_bybit_leg(client, str(leg.get("clientOrderId") or ""), row.symbol)
                except Exception:
                    logger.exception("Bybit sibling-cancel failed during the refresh")
        elif statuses and all(s == LiveOrderStatus.CANCELED for s in statuses):
            await store.apply_status(row.client_order_id, LiveOrderStatus.CANCELED)


_NOT_FOUND_REQUIRED = 3
