"""Position tracking for the Hydra trading platform.

Maintains an in-memory dictionary of open positions keyed by
(symbol, exchange_id, strategy_id). Designed so the backing store can later
be swapped to Redis (real-time reads) + PostgreSQL (audit trail) without
changing the public API.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from hydra.core.events import OrderFillEvent
from hydra.core.types import Direction, Position, Side, Symbol

logger = logging.getLogger(__name__)

# Internal key for the position dictionary.
type _PosKey = tuple[str, str, str]  # (symbol, exchange_id, strategy_id)


class PositionTracker:
    """Tracks all open positions across exchanges and strategies.

    Positions are keyed by ``(symbol, exchange_id, strategy_id)`` so that a
    single symbol can have independent positions per strategy and exchange.
    """

    def __init__(self) -> None:
        # Core state: (symbol, exchange_id, strategy_id) -> mutable Position
        self._positions: dict[_PosKey, Position] = {}
        # Mark prices for unrealized PnL: (symbol, exchange_id) -> price
        self._mark_prices: dict[tuple[str, str], Decimal] = {}
        # Associated stop/TP order IDs per position key
        self._associated_orders: dict[_PosKey, list[str]] = {}

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    async def update_on_fill(
        self,
        fill: OrderFillEvent,
        strategy_id: str = "",
    ) -> None:
        """Update position state based on an order fill.

        This handles four cases:
        1. Open a new position (no existing position for the key).
        2. Add to an existing position in the same direction.
        3. Reduce an existing position (partial close).
        4. Close a position entirely (remaining quantity reaches zero).

        Parameters
        ----------
        fill:
            The order fill event from the exchange.
        strategy_id:
            The strategy that owns this position.  Because ``OrderFillEvent``
            does not carry ``strategy_id``, the caller must provide it.
        """
        key: _PosKey = (fill.symbol, fill.exchange_id, strategy_id)
        existing = self._positions.get(key)

        if existing is None:
            # Case 1 -- new position
            direction = Direction.LONG if fill.side == Side.BUY else Direction.SHORT
            self._positions[key] = Position(
                symbol=Symbol(fill.symbol),
                direction=direction,
                quantity=fill.quantity,
                avg_entry_price=fill.price,
                unrealized_pnl=Decimal("0"),
                realized_pnl=-fill.fee,
                strategy_id=strategy_id,
                exchange_id=fill.exchange_id,
            )
            return

        # Determine whether the fill is in the same direction or opposing.
        same_direction = (existing.direction == Direction.LONG and fill.side == Side.BUY) or (
            existing.direction == Direction.SHORT and fill.side == Side.SELL
        )

        if same_direction:
            # Case 2 -- adding to position, recalculate weighted average price
            total_qty = existing.quantity + fill.quantity
            if total_qty > Decimal("0"):
                new_avg = (
                    existing.avg_entry_price * existing.quantity + fill.price * fill.quantity
                ) / total_qty
            else:
                new_avg = existing.avg_entry_price

            existing.quantity = total_qty
            existing.avg_entry_price = new_avg
            existing.realized_pnl -= fill.fee
        else:
            # Opposing side -- reduce or close
            if fill.quantity >= existing.quantity:
                # Case 4 -- full close (or overshoot, which we treat as close)
                close_qty = existing.quantity
                realized = self._compute_realized_pnl(
                    existing.direction,
                    existing.avg_entry_price,
                    fill.price,
                    close_qty,
                )
                existing.realized_pnl += realized - fill.fee
                existing.quantity = Decimal("0")
                existing.direction = Direction.FLAT
                existing.unrealized_pnl = Decimal("0")
            else:
                # Case 3 -- partial close
                realized = self._compute_realized_pnl(
                    existing.direction,
                    existing.avg_entry_price,
                    fill.price,
                    fill.quantity,
                )
                existing.realized_pnl += realized - fill.fee
                existing.quantity -= fill.quantity

        # Refresh unrealized PnL if we have a mark price
        self._refresh_unrealized(key)

    async def update_price(
        self,
        symbol: str,
        exchange_id: str,
        price: Decimal,
    ) -> None:
        """Update the mark price used for unrealized PnL calculation."""
        self._mark_prices[(symbol, exchange_id)] = price
        # Re-compute unrealized PnL for every strategy holding this symbol/exchange
        for key, pos in self._positions.items():
            if key[0] == symbol and key[1] == exchange_id and pos.quantity > Decimal("0"):
                self._refresh_unrealized(key)

    def attach_order(self, key: _PosKey, order_id: str) -> None:
        """Associate a stop-loss or take-profit order ID with a position."""
        self._associated_orders.setdefault(key, []).append(order_id)

    def get_associated_orders(self, key: _PosKey) -> list[str]:
        """Return order IDs associated with a position key."""
        return list(self._associated_orders.get(key, []))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_position(
        self,
        symbol: str,
        exchange_id: str | None = None,
    ) -> Position | None:
        """Return the first matching open position, or ``None``."""
        for key, pos in self._positions.items():
            if (
                key[0] == symbol
                and pos.quantity > Decimal("0")
                and (exchange_id is None or key[1] == exchange_id)
            ):
                return pos
        return None

    async def get_all_positions(self) -> list[Position]:
        """Return all positions with non-zero quantity."""
        return [p for p in self._positions.values() if p.quantity > Decimal("0")]

    async def get_positions_by_exchange(self, exchange_id: str) -> list[Position]:
        """Return all open positions for a given exchange."""
        return [
            p
            for key, p in self._positions.items()
            if key[1] == exchange_id and p.quantity > Decimal("0")
        ]

    async def get_positions_by_strategy(self, strategy_id: str) -> list[Position]:
        """Return all open positions for a given strategy."""
        return [
            p
            for key, p in self._positions.items()
            if key[2] == strategy_id and p.quantity > Decimal("0")
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_realized_pnl(
        direction: Direction,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        """Compute realized PnL for a closed (or partially closed) trade."""
        if direction == Direction.LONG:
            return (exit_price - entry_price) * quantity
        if direction == Direction.SHORT:
            return (entry_price - exit_price) * quantity
        return Decimal("0")

    def _refresh_unrealized(self, key: _PosKey) -> None:
        """Recompute unrealized PnL for a position from the current mark price."""
        pos = self._positions.get(key)
        if pos is None or pos.quantity <= Decimal("0"):
            return
        mark = self._mark_prices.get((key[0], key[1]))
        if mark is None:
            return
        if pos.direction == Direction.LONG:
            pos.unrealized_pnl = (mark - pos.avg_entry_price) * pos.quantity
        elif pos.direction == Direction.SHORT:
            pos.unrealized_pnl = (pos.avg_entry_price - mark) * pos.quantity
