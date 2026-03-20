"""Order lifecycle management: validation, dedup, submission, tracking, fill emission.

The ``OrderManager`` sits between strategy signals and the exchange executor.  It
performs duplicate-order detection, delegates pre-trade risk checks, submits to the
configured executor, and publishes fill / cancel events to the event bus.
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from hydra.core.events import (
    OrderCancelEvent,
    OrderFillEvent,
    RiskCheckResult,
)
from hydra.core.types import (
    OrderRequest,
    OrderStatus,
)

logger = logging.getLogger(__name__)

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


@runtime_checkable
class EventBusLike(Protocol):
    async def publish(self, event: Any) -> None: ...


@runtime_checkable
class RiskCheckerLike(Protocol):
    async def check_order(self, order: OrderRequest, portfolio_state: Any) -> RiskCheckResult: ...


# ---------------------------------------------------------------------------
# Dedup key helper
# ---------------------------------------------------------------------------


def _dedup_key(order: OrderRequest) -> str:
    """Build a deduplication key from symbol + side + quantity."""
    return f"{order.symbol}|{order.side}|{order.quantity}"


# ---------------------------------------------------------------------------
# Tracked order wrapper
# ---------------------------------------------------------------------------


class _TrackedOrder:
    """Internal bookkeeping for an in-flight order."""

    __slots__ = ("order_id", "request", "status", "submitted_at")

    def __init__(self, request: OrderRequest, order_id: str) -> None:
        self.request = request
        self.order_id = order_id
        self.status: OrderStatus = OrderStatus.SUBMITTED
        self.submitted_at: float = time.monotonic()


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


class OrderManager:
    """Signal -> OrderRequest -> risk check -> submit -> track -> emit fill."""

    def __init__(
        self,
        executor: ExecutorBackend,
        event_bus: EventBusLike,
        risk_checker: RiskCheckerLike | None = None,
        portfolio_state: Any = None,
        portfolio_state_builder: Any = None,
        stale_order_timeout: float = 300.0,
        dedup_window: float = 1.0,
    ) -> None:
        self._executor = executor
        self._event_bus = event_bus
        self._risk_checker = risk_checker
        self._portfolio_state = portfolio_state
        self._portfolio_state_builder = portfolio_state_builder

        # Config
        self._stale_order_timeout = stale_order_timeout
        self._dedup_window = dedup_window

        # State
        self._orders: dict[str, _TrackedOrder] = {}
        self._dedup_cache: dict[str, float] = {}  # key -> timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_order(self, order: OrderRequest) -> str:
        """Validate, dedup check, optionally risk-check, submit to executor, return order_id."""

        # 1. Deduplication
        key = _dedup_key(order)
        now = time.monotonic()
        if key in self._dedup_cache:
            elapsed = now - self._dedup_cache[key]
            if elapsed < self._dedup_window:
                raise ValueError(
                    f"Duplicate order rejected (same symbol+side+quantity within "
                    f"{self._dedup_window}s window)"
                )
        self._dedup_cache[key] = now

        # 2. Pre-trade risk check (if checker configured)
        if self._risk_checker is not None:
            # Refresh portfolio state if a builder is available
            if self._portfolio_state_builder is not None:
                self._portfolio_state = await self._portfolio_state_builder()
            result = await self._risk_checker.check_order(order, self._portfolio_state)
            if not result.approved:
                raise ValueError(f"Risk check failed: {result.reason}")

        # 3. Submit to executor backend
        resp = await self._executor.create_order(
            symbol=str(order.symbol),
            side=str(order.side),
            order_type=str(order.order_type),
            quantity=order.quantity,
            price=order.price,
            stop_price=order.stop_price,
        )

        order_id = str(resp.get("id", str(uuid.uuid4())))

        # 4. Track
        tracked = _TrackedOrder(request=order, order_id=order_id)
        self._orders[order_id] = tracked

        # 5. If the response indicates an immediate fill, emit fill event
        status_str = str(resp.get("status", "")).upper()
        if status_str == "FILLED" or resp.get("filled", False):
            tracked.status = OrderStatus.FILLED
            fill_price = Decimal(str(resp.get("price", resp.get("average", "0"))))
            raw_fee = resp.get("fee")
            if isinstance(raw_fee, dict):
                fee = Decimal(str(raw_fee.get("cost", "0")))
                fee_currency = str(raw_fee.get("currency", ""))
            else:
                fee = Decimal("0")
                fee_currency = ""
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
                )
            )
            try:
                from hydra.dashboard.metrics import observe_order_fill_latency, record_trade

                record_trade(
                    str(order.symbol),
                    str(order.side),
                    order.strategy_id,
                    order.exchange_id,
                )
                observe_order_fill_latency(time.monotonic() - now)
            except Exception:
                pass

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

    async def cleanup_stale_orders(self) -> list[str]:
        """Cancel orders that exceed the configured stale timeout.  Returns cancelled ids."""
        now = time.monotonic()
        cancelled: list[str] = []
        for order_id, tracked in list(self._orders.items()):
            if tracked.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                continue
            age = now - tracked.submitted_at
            if age > self._stale_order_timeout:
                ok = await self.cancel_order(order_id)
                if ok:
                    cancelled.append(order_id)
        return cancelled
