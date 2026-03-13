"""Exchange-side safety order management.

Places stop-loss orders directly on the exchange so they persist even if
Hydra goes down.  Handles exchange-specific order type differences:
    - Binance: STOP_MARKET (futures), STOP_LOSS_LIMIT (spot)
    - Bybit: conditional stop
    - Kraken: stop-loss
    - OKX: TP/SL orders
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from hydra.core.types import (
    Direction,
    ExchangeId,
    MarketType,
    Position,
    Side,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol for the exchange client
# ---------------------------------------------------------------------------


@runtime_checkable
class ExchangeClientLike(Protocol):
    """Minimal interface for placing / cancelling safety orders."""

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


# ---------------------------------------------------------------------------
# Exchange-specific stop order configuration
# ---------------------------------------------------------------------------


def _stop_params_for_exchange(
    exchange_id: ExchangeId,
    market_type: MarketType,
) -> dict[str, Any]:
    """Return exchange-specific order params for a stop-loss."""
    if exchange_id == "binance":
        if market_type == MarketType.FUTURES:
            return {"type": "STOP_MARKET", "closePosition": True}
        return {"type": "STOP_LOSS_LIMIT", "timeInForce": "GTC"}

    if exchange_id == "bybit":
        return {"triggerDirection": "below", "orderType": "Market"}

    if exchange_id == "kraken":
        return {"type": "stop-loss"}

    if exchange_id == "okx":
        return {"tpslMode": "full", "slOrdPx": "-1"}

    return {}


# ---------------------------------------------------------------------------
# ExchangeSafetyManager
# ---------------------------------------------------------------------------


class ExchangeSafetyManager:
    """Manages exchange-side stop-loss (safety) orders.

    Every open position should have a corresponding stop-loss on the exchange
    so that if Hydra goes offline, the position is still protected.
    """

    def __init__(self) -> None:
        # Track placed safety orders: position_key -> list[order_id]
        self._safety_orders: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Place safety orders
    # ------------------------------------------------------------------

    async def place_safety_orders(
        self,
        position: Position,
        exchange_client: ExchangeClientLike,
        stop_distance_atr: Decimal,
        market_type: MarketType = MarketType.FUTURES,
    ) -> list[str]:
        """Place exchange-side stop-loss for a position.

        Places two stops:
            1. Normal stop at ``entry +/- stop_distance_atr``
            2. Emergency stop at 2x the normal distance

        Returns the list of exchange order IDs.
        """
        if position.direction == Direction.FLAT or position.quantity <= Decimal("0"):
            return []

        symbol = str(position.symbol)
        exchange_id = position.exchange_id
        order_ids: list[str] = []

        # Determine stop side (opposite of position direction)
        stop_side = Side.SELL if position.direction == Direction.LONG else Side.BUY

        # Calculate stop prices
        if position.direction == Direction.LONG:
            normal_stop = position.avg_entry_price - stop_distance_atr
            emergency_stop = position.avg_entry_price - (stop_distance_atr * Decimal("2"))
            # Prices should not go below zero
            normal_stop = max(normal_stop, Decimal("0.01"))
            emergency_stop = max(emergency_stop, Decimal("0.01"))
        else:
            normal_stop = position.avg_entry_price + stop_distance_atr
            emergency_stop = position.avg_entry_price + (stop_distance_atr * Decimal("2"))

        params = _stop_params_for_exchange(exchange_id, market_type)

        # 1. Normal stop
        try:
            resp = await exchange_client.create_order(
                symbol=symbol,
                side=str(stop_side),
                order_type="STOP_MARKET",
                quantity=position.quantity,
                stop_price=normal_stop,
                params=params,
            )
            oid = str(resp.get("id", ""))
            if oid:
                order_ids.append(oid)
        except Exception:
            logger.exception("Failed to place normal stop for %s", symbol)

        # 2. Emergency stop (2x distance)
        try:
            emergency_params = dict(params)
            emergency_params["emergency"] = True
            resp = await exchange_client.create_order(
                symbol=symbol,
                side=str(stop_side),
                order_type="STOP_MARKET",
                quantity=position.quantity,
                stop_price=emergency_stop,
                params=emergency_params,
            )
            oid = str(resp.get("id", ""))
            if oid:
                order_ids.append(oid)
        except Exception:
            logger.exception("Failed to place emergency stop for %s", symbol)

        # Track
        pos_key = f"{position.exchange_id}:{position.symbol}:{position.strategy_id}"
        self._safety_orders[pos_key] = order_ids

        return order_ids

    # ------------------------------------------------------------------
    # Cancel safety orders
    # ------------------------------------------------------------------

    async def cancel_safety_orders(
        self,
        position: Position,
        exchange_client: ExchangeClientLike,
    ) -> None:
        """Cancel all safety orders for a position."""
        pos_key = f"{position.exchange_id}:{position.symbol}:{position.strategy_id}"
        order_ids = self._safety_orders.pop(pos_key, [])
        symbol = str(position.symbol)

        for oid in order_ids:
            try:
                await exchange_client.cancel_order(oid, symbol)
            except Exception:
                logger.exception("Failed to cancel safety order %s for %s", oid, symbol)
