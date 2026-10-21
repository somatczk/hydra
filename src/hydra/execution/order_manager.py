"""Order lifecycle management: validation, dedup, submission, tracking, fill emission.

The ``OrderManager`` sits between strategy signals and the exchange executor. An order is
reducing exactly when ``reduce_only`` is set; it is clamped to the held position. With a
RiskService, the account lock is held from the position read (clamp and classification) through
the risk check and ``create_order``, and fill events are published after it is released.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast, runtime_checkable

from hydra.core.events import OrderCancelEvent, OrderFillEvent
from hydra.core.metrics import record_risk_rejection
from hydra.core.types import (
    ExchangeId,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Symbol,
)
from hydra.risk.pretrade import (
    OPEN,
    REDUCE,
    Intent,
    RiskDecision,
    classify_intent,
    closes_position,
)
from hydra.risk.service import RiskLockTimeoutError

if TYPE_CHECKING:
    from hydra.execution.paper_trading import PaperFill
    from hydra.risk.service import RiskService

logger = logging.getLogger(__name__)

PROTECTIVE_TAG = "protective"

# ---------------------------------------------------------------------------
# Lightweight protocol so we don't depend on concrete ExchangeClient/Paper
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutorBackend(Protocol):
    """Minimal interface the OrderManager expects from an executor."""

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

    async def get_positions(self, symbol: str | None = None) -> list[Position]: ...


@runtime_checkable
class EventBusLike(Protocol):
    async def publish(self, event: Any) -> None: ...


# ---------------------------------------------------------------------------
# Errors and helpers
# ---------------------------------------------------------------------------


class RiskRejectedError(ValueError):
    """A risk check refused the order (a ValueError, so existing callers still catch it)."""

    def __init__(self, reason: str, check: str, intent: Intent) -> None:
        super().__init__(f"Risk check failed: {reason}")
        self.reason = reason
        self.check = check
        self.intent = intent


def _dedup_key(order: OrderRequest) -> str:
    """Build a deduplication key from symbol + side + quantity."""
    return f"{order.symbol}|{order.side}|{order.quantity}"


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _realized_pnl(
    resp: dict[str, Any], fee: Decimal, side: Side
) -> tuple[Decimal | None, Decimal | None]:
    """(net, gross) PnL of a closing fill from the executor response.

    Without net_pnl, the pro-rata entry fee is taken at this fill's fee rate (exact for the
    fixed-rate paper executor): entry notional = cost - gross on a sell, cost + gross on a buy.
    """
    gross = _dec(resp.get("pnl"))
    for key in ("net_pnl", "realized_pnl"):
        net = _dec(resp.get(key))
        if net is not None:
            return net, gross
    if gross is None:
        return None, None
    cost = _dec(resp.get("cost"))
    entry_fee = Decimal("0")
    if cost is not None and cost > 0 and fee > 0:
        entry_notional = cost - gross if side == Side.SELL else cost + gross
        entry_fee = fee * entry_notional / cost
    return gross - fee - entry_fee, gross


# ---------------------------------------------------------------------------
# Tracked order wrapper
# ---------------------------------------------------------------------------


class _TrackedOrder:
    """Internal bookkeeping for an in-flight order."""

    __slots__ = ("order_id", "request", "status", "submitted_at")

    def __init__(self, request: OrderRequest, order_id: str, submitted_at: float) -> None:
        self.request = request
        self.order_id = order_id
        self.status: OrderStatus = OrderStatus.SUBMITTED
        self.submitted_at = submitted_at


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


class OrderManager:
    """Signal -> OrderRequest -> risk check -> submit -> track -> emit fill.

    With ``risk`` (a RiskService) every order is risk-checked for ``session_id``; without it
    orders go straight to the executor (tests and tools only). ``clock`` (seconds) drives dedup
    and stale-order age; a replay passes bar time.
    """

    def __init__(
        self,
        executor: ExecutorBackend,
        event_bus: EventBusLike,
        stale_order_timeout: float = 300.0,
        dedup_window: float = 1.0,
        *,
        risk: RiskService | None = None,
        session_id: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executor = executor
        self._clock = clock
        self._event_bus = event_bus
        self._risk = risk
        self._session_id = session_id

        # Config
        self._stale_order_timeout = stale_order_timeout
        self._dedup_window = dedup_window

        # State
        self._orders: dict[str, _TrackedOrder] = {}
        self._dedup_cache: dict[str, float] = {}  # key -> timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_order(self, order: OrderRequest) -> str | None:
        """Classify, clamp, dedup, risk-check, submit; returns the order id.

        A reduce_only order with nothing to reduce is a no-op and returns None. A refused order
        raises RiskRejectedError.
        """
        now = time.monotonic()
        if order.quantity <= 0:
            self._reject(
                RiskDecision(
                    False,
                    "sanity",
                    f"non-positive quantity {order.quantity}",
                    REDUCE if order.reduce_only else OPEN,
                )
            )

        if self._risk is not None:
            return await self._submit_with_risk(self._risk, order, now)

        positions: list[Position] = []
        if order.reduce_only:
            positions = list(await self._executor.get_positions(str(order.symbol)))
        resolved = self._resolve(order, positions)
        if resolved is None:
            return None
        order, intent = resolved
        if intent == OPEN:
            self._dedup(order)
        resp = await self._create(order)
        return await self._track_and_publish(order, intent, resp, now)

    async def _submit_with_risk(
        self, risk: RiskService, order: OrderRequest, now: float
    ) -> str | None:
        """Positions are read, and the order clamped and classified, under the account lock."""
        slot: Intent = REDUCE if order.reduce_only else OPEN
        try:
            async with risk.order_slot(slot):
                positions = list(await self._executor.get_positions(str(order.symbol)))
                resolved = self._resolve(order, positions)
                if resolved is None:
                    return None
                order, intent = resolved
                if intent == OPEN:
                    self._dedup(order)
                decision = await risk.check(order, self._session_id, intent)
                if not decision.approved:
                    self._reject(decision)
                resp = await self._create(order)
                if intent == OPEN:
                    risk.record_opening(self._session_id)
        except RiskLockTimeoutError as exc:
            self._reject(RiskDecision(False, "lock_timeout", str(exc), slot))
        return await self._track_and_publish(order, intent, resp, now)

    def _resolve(
        self, order: OrderRequest, positions: list[Position]
    ) -> tuple[OrderRequest, Intent] | None:
        """Clamp a reduce_only order to the held position (None: nothing to reduce); classify."""
        if order.reduce_only:
            held = next(
                (
                    p
                    for p in positions
                    if str(p.symbol) == str(order.symbol)
                    and p.quantity > 0
                    and closes_position(order, p)
                ),
                None,
            )
            if held is None:
                logger.info("reduce_only %s %s: no position to reduce", order.side, order.symbol)
                return None
            if order.quantity > held.quantity:
                order = replace(order, quantity=held.quantity)
        return order, classify_intent(order)

    def _dedup(self, order: OrderRequest) -> None:
        key = _dedup_key(order)
        now = self._clock()
        if key in self._dedup_cache and now - self._dedup_cache[key] < self._dedup_window:
            self._reject(
                RiskDecision(
                    False,
                    "duplicate",
                    f"Duplicate order rejected (same symbol+side+quantity within "
                    f"{self._dedup_window}s window)",
                    OPEN,
                )
            )
        self._dedup_cache[key] = now

    def _reject(self, decision: RiskDecision) -> NoReturn:
        record_risk_rejection(decision.check, decision.intent)
        logger.warning(
            "Order rejected (%s, %s): %s", decision.check, decision.intent, decision.reason
        )
        raise RiskRejectedError(decision.reason, decision.check, decision.intent)

    async def _create(self, order: OrderRequest) -> dict[str, Any]:
        return await self._executor.create_order(
            symbol=str(order.symbol),
            side=str(order.side),
            order_type=str(order.order_type),
            quantity=order.quantity,
            price=order.price,
            stop_price=order.stop_price,
        )

    async def _track_and_publish(
        self, order: OrderRequest, intent: Intent, resp: dict[str, Any], started: float
    ) -> str:
        order_id = str(resp.get("id", str(uuid.uuid4())))
        tracked = _TrackedOrder(request=order, order_id=order_id, submitted_at=self._clock())
        self._orders[order_id] = tracked

        status_str = str(resp.get("status", "")).upper()
        if status_str == "FILLED" or resp.get("filled", False):
            tracked.status = OrderStatus.FILLED
            fill_price = _dec(resp.get("average")) or _dec(resp.get("price")) or Decimal("0")
            raw_fee = resp.get("fee")
            if isinstance(raw_fee, dict):
                fee = Decimal(str(raw_fee.get("cost", "0")))
                fee_currency = str(raw_fee.get("currency", ""))
            else:
                fee = Decimal("0")
                fee_currency = ""
            realized, gross = (
                _realized_pnl(resp, fee, order.side) if intent == REDUCE else (None, None)
            )
            await self._event_bus.publish(
                OrderFillEvent(
                    order_id=order_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    quantity=order.quantity,
                    price=fill_price,
                    fee=fee,
                    fee_currency=fee_currency,
                    exchange_id=order.exchange_id,
                    status=OrderStatus.FILLED,
                    session_id=self._session_id,
                    strategy_id=order.strategy_id,
                    tag=order.tag,
                    reduce_only=order.reduce_only,
                    realized_pnl=realized,
                    gross_pnl=gross,
                    mark_source=_opt_str(resp.get("mark_source")),
                )
            )
            try:
                from hydra.core.metrics import observe_order_fill_latency, record_trade

                record_trade(
                    str(order.symbol),
                    str(order.side),
                    order.strategy_id,
                    order.exchange_id,
                    pnl=None if realized is None else float(realized),
                )
                observe_order_fill_latency(time.monotonic() - started)
            except Exception:
                logger.debug("trade metrics not recorded", exc_info=True)

        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.  Returns True on success."""
        tracked = self._orders.get(order_id)
        if tracked is None:
            return False

        if tracked.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return False

        try:
            await self._executor.cancel_order(order_id, str(tracked.request.symbol))
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

        tracked.status = OrderStatus.CANCELLED
        await self._event_bus.publish(
            OrderCancelEvent(
                order_id=order_id,
                symbol=tracked.request.symbol,
                exchange_id=tracked.request.exchange_id,
                reason="user_cancel",
            )
        )
        return True

    def get_open_orders(self, symbol: str | None = None) -> list[OrderRequest]:
        """Return open (non-terminal) order requests, optionally filtered by symbol."""
        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        results: list[OrderRequest] = []
        for tracked in self._orders.values():
            if tracked.status in terminal:
                continue
            if symbol is not None and str(tracked.request.symbol) != symbol:
                continue
            results.append(tracked.request)
        return results

    def get_order_status(self, order_id: str) -> OrderStatus:
        """Return the current status of an order."""
        tracked = self._orders.get(order_id)
        if tracked is None:
            raise KeyError(f"Unknown order_id: {order_id}")
        return tracked.status

    def mark_cancelled(self, order_id: str) -> bool:
        """Record that the executor dropped a tracked order (e.g. a gap-cancelled BUY)."""
        tracked = self._orders.get(order_id)
        if tracked is None or tracked.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return False
        tracked.status = OrderStatus.CANCELLED
        return True

    async def on_external_fill(self, fill: PaperFill) -> OrderFillEvent | None:
        """Publish a fill the executor made later (resting order, OCO leg, flatten).

        A ``CANCELLED`` record traded nothing: the tracked order is marked cancelled and no
        event is published. The OCO parent id is the tracked id.
        """
        order_id = fill.parent_order_id or fill.order_id
        if fill.status != "FILLED":
            self.mark_cancelled(order_id)
            return None
        tracked = self._orders.get(order_id)
        request = tracked.request if tracked is not None else None
        if tracked is not None:
            tracked.status = OrderStatus.FILLED
        event = OrderFillEvent(
            order_id=order_id,
            symbol=Symbol(fill.symbol),
            side=Side(fill.side),
            order_type=OrderType(fill.order_type),
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fee_currency=fill.fee_currency,
            exchange_id=request.exchange_id if request is not None else self._exchange_id(),
            status=OrderStatus.FILLED,
            session_id=self._session_id,
            strategy_id=request.strategy_id if request is not None else "",
            tag=request.tag if request is not None else fill.reason,
            reduce_only=request.reduce_only if request is not None else fill.side == "SELL",
            realized_pnl=fill.net_pnl,
            gross_pnl=fill.gross_pnl,
            mark_source=fill.mark_source,
        )
        await self._event_bus.publish(event)
        try:
            from hydra.core.metrics import record_trade

            record_trade(
                fill.symbol,
                fill.side,
                event.strategy_id,
                event.exchange_id,
                pnl=None if fill.net_pnl is None else float(fill.net_pnl),
            )
        except Exception:
            logger.debug("trade metrics not recorded", exc_info=True)
        return event

    def _exchange_id(self) -> ExchangeId:
        return cast(ExchangeId, getattr(self._executor, "_exchange_id", "binance"))

    async def cleanup_stale_orders(self) -> list[str]:
        """Cancel orders that exceed the configured stale timeout.  Returns cancelled ids.

        Protective orders (stops, OCOs) rest until the position closes and are never stale.
        """
        now = self._clock()
        cancelled: list[str] = []
        for order_id, tracked in list(self._orders.items()):
            if tracked.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                continue
            if tracked.request.tag == PROTECTIVE_TAG:
                continue
            age = now - tracked.submitted_at
            if age > self._stale_order_timeout:
                ok = await self.cancel_order(order_id)
                if ok:
                    cancelled.append(order_id)
        return cancelled
