"""Cancel open orders and close positions when a trading session stops."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)


async def flatten_session(session: TradingSession) -> None:
    """Cancel open orders, market-close positions and record ``positions_at_stop``."""
    # 1. Cancel all open orders
    if session._order_manager is not None:
        try:
            open_ids = [
                oid
                for oid, t in session._order_manager._orders.items()
                if t.status.name in ("PENDING", "SUBMITTED", "PARTIALLY_FILLED")
            ]
            for oid in open_ids:
                await session._order_manager.cancel_order(oid)
            if open_ids:
                logger.info(
                    "Cancelled %d open orders in session %s",
                    len(open_ids),
                    session.session_id,
                )
        except Exception:
            logger.exception("Failed to cancel open orders in session %s", session.session_id)

    # 2. Flatten all open positions (market close)
    if session._executor is not None:
        try:
            positions = await session._executor.fetch_positions()
            for pos in positions:
                symbol = pos["symbol"]
                side = pos.get("side", "")
                qty = abs(float(pos.get("contracts", 0)))
                if qty <= 0:
                    continue
                # Close: sell longs, buy shorts
                close_side = "sell" if side == "long" else "buy"
                await session._executor.create_order(
                    symbol=symbol,
                    order_type="market",
                    side=close_side,
                    amount=qty,
                )
                logger.info(
                    "Closed %s position %.8f %s in session %s",
                    side,
                    qty,
                    symbol,
                    session.session_id,
                )
        except Exception:
            logger.exception("Failed to flatten positions in session %s", session.session_id)

        # Re-check: warn if any positions remain open after flatten
        try:
            remaining = await session._executor.fetch_positions()
            remaining = [p for p in remaining if abs(float(p.get("contracts", 0))) > 0]
            session.positions_at_stop = len(remaining)
            if remaining:
                logger.warning(
                    "Session %s stopped with %d positions still open",
                    session.session_id,
                    len(remaining),
                )
        except Exception:
            logger.exception("Failed to re-check positions in session %s", session.session_id)
