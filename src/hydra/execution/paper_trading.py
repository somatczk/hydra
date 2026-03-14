"""Paper-trading executor with simulated fills and virtual balance tracking.

``PaperTradingExecutor`` provides the same interface as ``ExchangeClient`` but
executes entirely in-memory.  Market orders fill immediately at the current price
plus configurable slippage; limit / stop orders stay pending until
``check_pending_orders`` determines they should trigger.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hydra.core.types import (
    OHLCV,
    Direction,
    ExchangeId,
    OrderType,
    Side,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal pending order representation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Simulated position
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SimulatedPosition:
    symbol: str
    direction: Direction
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# PaperTradingExecutor
# ---------------------------------------------------------------------------


class PaperTradingExecutor:
    """Simulated exchange executor for paper trading.

    Parameters
    ----------
    exchange_id:
        Logical exchange this executor simulates.
    initial_balances:
        Starting virtual balances, e.g. ``{"USDT": Decimal("10000")}``.
    slippage_pct:
        Percentage slippage applied to market-order fills (default 0.1%).
    fee_pct:
        Simulated trading fee percentage (default 0.1%).
    """

    def __init__(
        self,
        exchange_id: ExchangeId = "binance",
        initial_balances: dict[str, Decimal] | None = None,
        slippage_pct: Decimal = Decimal("0.001"),
        fee_pct: Decimal = Decimal("0.001"),
        db_pool: Any = None,
        strategy_id: str = "",
    ) -> None:
        self._exchange_id = exchange_id
        self._balances: dict[str, Decimal] = dict(initial_balances or {"USDT": Decimal("10000")})
        self._slippage_pct = slippage_pct
        self._fee_pct = fee_pct
        self._db_pool = db_pool
        self._strategy_id = strategy_id

        # State
        self._pending_orders: list[_PendingOrder] = []
        self._filled_orders: list[dict[str, Any]] = []
        self._positions: dict[str, _SimulatedPosition] = {}
        self._last_prices: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def set_market_price(self, symbol: str, price: Decimal) -> None:
        """Update the latest known price for a symbol (for fill simulation)."""
        self._last_prices[symbol] = price

    def _apply_slippage(self, price: Decimal, side: str) -> Decimal:
        """Apply slippage in the direction unfavorable to the trader."""
        if side.upper() == "BUY":
            return price * (Decimal("1") + self._slippage_pct)
        return price * (Decimal("1") - self._slippage_pct)

    # ------------------------------------------------------------------
    # Order API (matches ExchangeClient interface)
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
        """Create an order.  Market orders fill immediately; limit/stop go pending."""
        order_id = str(uuid.uuid4())
        otype = order_type.upper()

        if otype == OrderType.MARKET:
            market_price = self._last_prices.get(symbol, price or Decimal("0"))
            if market_price == Decimal("0"):
                raise ValueError(f"No market price available for {symbol}")
            fill_price = self._apply_slippage(market_price, side)
            return self._execute_fill(order_id, symbol, side, quantity, fill_price)

        # Limit / stop orders go pending
        pending = _PendingOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=otype,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
        )
        self._pending_orders.append(pending)
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": otype,
            "amount": float(quantity),
            "price": float(price) if price else None,
            "status": "PENDING",
            "filled": False,
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel a pending order."""
        for i, pending in enumerate(self._pending_orders):
            if pending.order_id == order_id:
                self._pending_orders.pop(i)
                return {"id": order_id, "status": "CANCELLED"}
        raise ValueError(f"Order {order_id} not found in pending orders")

    # ------------------------------------------------------------------
    # Pending order checks
    # ------------------------------------------------------------------

    def check_pending_orders(self, current_bar: OHLCV) -> list[dict[str, Any]]:
        """Check whether any pending limit/stop orders should fill on this bar.

        Returns a list of fill dicts for orders that triggered.
        """
        fills: list[dict[str, Any]] = []
        remaining: list[_PendingOrder] = []

        for pending in self._pending_orders:
            bar_symbol = pending.symbol
            # Update last known price
            self._last_prices[bar_symbol] = current_bar.close

            triggered = self._should_trigger(pending, current_bar)
            if triggered:
                fill_price = self._determine_fill_price(pending, current_bar)
                fill = self._execute_fill(
                    pending.order_id,
                    pending.symbol,
                    pending.side,
                    pending.quantity,
                    fill_price,
                )
                fills.append(fill)
            else:
                remaining.append(pending)

        self._pending_orders = remaining
        return fills

    def _should_trigger(self, order: _PendingOrder, bar: OHLCV) -> bool:
        """Determine whether a pending order should trigger on this bar."""
        otype = order.order_type.upper()

        if otype in (OrderType.LIMIT, "LIMIT"):
            if order.price is None:
                return False
            if order.side.upper() == "BUY":
                return bar.low <= order.price
            return bar.high >= order.price

        if otype in (OrderType.STOP_MARKET, "STOP_MARKET", OrderType.STOP_LIMIT, "STOP_LIMIT"):
            trigger = order.stop_price or order.price
            if trigger is None:
                return False
            if order.side.upper() == "BUY":
                return bar.high >= trigger
            return bar.low <= trigger

        return False

    def _determine_fill_price(self, order: _PendingOrder, bar: OHLCV) -> Decimal:
        """Determine the simulated fill price for a triggered order."""
        otype = order.order_type.upper()

        if otype in (OrderType.LIMIT, "LIMIT") and order.price is not None:
            return order.price

        if otype in (OrderType.STOP_MARKET, "STOP_MARKET"):
            trigger = order.stop_price or order.price or bar.close
            return self._apply_slippage(trigger, order.side)

        if otype in (OrderType.STOP_LIMIT, "STOP_LIMIT") and order.price is not None:
            return order.price

        return self._apply_slippage(bar.close, order.side)

    # ------------------------------------------------------------------
    # Fill execution
    # ------------------------------------------------------------------

    def _execute_fill(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        fill_price: Decimal,
    ) -> dict[str, Any]:
        """Execute a simulated fill: update balances and positions."""
        cost = fill_price * quantity
        fee = cost * self._fee_pct

        # Update balances (simplified: assume quote currency is USDT)
        quote = "USDT"
        if side.upper() == "BUY":
            required = cost + fee
            if self._balances.get(quote, Decimal("0")) < required:
                raise ValueError(f"Insufficient {quote} balance for buy order")
            self._balances[quote] = self._balances.get(quote, Decimal("0")) - required
            # Update position
            self._update_position(symbol, Side.BUY, quantity, fill_price)
        else:
            self._balances[quote] = self._balances.get(quote, Decimal("0")) + cost - fee
            self._update_position(symbol, Side.SELL, quantity, fill_price)

        fill_dict: dict[str, Any] = {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": float(quantity),
            "price": float(fill_price),
            "cost": float(cost),
            "fee": {"cost": float(fee), "currency": quote},
            "status": "FILLED",
            "filled": True,
        }
        self._filled_orders.append(fill_dict)

        # Persist paper trade to DB (fire-and-forget)
        if self._db_pool is not None:
            import asyncio

            _task = asyncio.ensure_future(
                self._persist_fill(symbol, side, quantity, fill_price, fee)
            )
            # prevent GC before completion
            _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        return fill_dict

    async def _persist_fill(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
    ) -> None:
        """Persist a paper trading fill to the trades table."""
        try:
            now = datetime.now(UTC)
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades
                        (symbol, side, price, quantity, fee, pnl,
                         strategy_id, exchange_id, timestamp, source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'paper')
                    """,
                    symbol,
                    side.upper(),
                    price,
                    quantity,
                    fee,
                    Decimal("0"),
                    self._strategy_id,
                    self._exchange_id,
                    now,
                )
        except Exception:
            logger.exception("Failed to persist paper fill for %s", symbol)

    def _update_position(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        """Update or create a simulated position."""
        pos = self._positions.get(symbol)

        if pos is None:
            direction = Direction.LONG if side == Side.BUY else Direction.SHORT
            self._positions[symbol] = _SimulatedPosition(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                avg_entry_price=price,
            )
            return

        if (pos.direction == Direction.LONG and side == Side.BUY) or (
            pos.direction == Direction.SHORT and side == Side.SELL
        ):
            # Adding to position: weighted average entry price
            total_qty = pos.quantity + quantity
            if total_qty > Decimal("0"):
                pos.avg_entry_price = (
                    pos.avg_entry_price * pos.quantity + price * quantity
                ) / total_qty
            pos.quantity = total_qty
        else:
            # Reducing / closing / reversing position
            if quantity >= pos.quantity:
                remaining = quantity - pos.quantity
                if remaining > Decimal("0"):
                    # Reversal
                    direction = Direction.LONG if side == Side.BUY else Direction.SHORT
                    pos.direction = direction
                    pos.quantity = remaining
                    pos.avg_entry_price = price
                else:
                    # Flat
                    pos.direction = Direction.FLAT
                    pos.quantity = Decimal("0")
                    pos.avg_entry_price = Decimal("0")
            else:
                pos.quantity -= quantity

    # ------------------------------------------------------------------
    # Account queries (match ExchangeClient interface)
    # ------------------------------------------------------------------

    async def fetch_balance(self) -> dict[str, Decimal]:
        """Return virtual balances."""
        return dict(self._balances)

    async def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return simulated positions."""
        results: list[dict[str, Any]] = []
        for sym, pos in self._positions.items():
            if pos.direction == Direction.FLAT:
                continue
            if symbol is not None and sym != symbol:
                continue
            results.append(
                {
                    "symbol": pos.symbol,
                    "side": pos.direction.value.lower(),
                    "contracts": float(pos.quantity),
                    "entryPrice": float(pos.avg_entry_price),
                    "unrealizedPnl": float(pos.unrealized_pnl),
                }
            )
        return results

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return pending orders."""
        results: list[dict[str, Any]] = []
        for pending in self._pending_orders:
            if symbol is not None and pending.symbol != symbol:
                continue
            results.append(
                {
                    "id": pending.order_id,
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "type": pending.order_type,
                    "amount": float(pending.quantity),
                    "price": float(pending.price) if pending.price else None,
                    "status": "PENDING",
                }
            )
        return results
