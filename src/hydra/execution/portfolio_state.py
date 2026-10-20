"""Portfolio state for pre-trade risk checks, aggregated from a session executor."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from hydra.core.types import Position
from hydra.risk.pretrade import PortfolioState

logger = logging.getLogger(__name__)


async def build_portfolio_state(executor: Any, symbols: list[str], db_pool: Any) -> PortfolioState:
    """Aggregate current portfolio state from executor + DB risk_state."""
    positions: list[Position] = []
    balances: dict[str, Decimal] = {}
    daily_pnl = Decimal("0")
    consecutive_losses = 0
    current_drawdown = Decimal("0")
    portfolio_value = Decimal("10000")

    if executor is not None:
        balances = await executor.get_balance()
        for sym in symbols:
            pos_list = await executor.get_positions(sym)
            positions.extend(pos_list)

    # Sum portfolio value from balances + position notional values
    balance_total = sum(balances.values(), Decimal("0"))
    position_value = Decimal("0")
    for pos in positions:
        try:
            price = await executor.get_last_price(str(pos.symbol))
            position_value += pos.quantity * price
        except Exception:  # nosec B110
            pass  # price unavailable — skip position valuation
    portfolio_value = balance_total + position_value

    # Load risk state from DB if available
    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT daily_pnl, consecutive_losses, current_drawdown "
                    "FROM risk_state WHERE scope = 'global'"
                )
            if row:
                daily_pnl = Decimal(str(row["daily_pnl"])) if row["daily_pnl"] else Decimal("0")
                consecutive_losses = (
                    int(row["consecutive_losses"]) if row["consecutive_losses"] else 0
                )
                current_drawdown = (
                    Decimal(str(row["current_drawdown"]))
                    if row["current_drawdown"]
                    else Decimal("0")
                )
        except Exception:
            logger.debug("Could not load risk_state for symbols %s", symbols)

    try:
        from hydra.core.metrics import update_portfolio

        update_portfolio(float(portfolio_value), float(current_drawdown), float(daily_pnl))
    except Exception:
        pass

    return PortfolioState(
        positions=positions,
        balances=balances,
        daily_pnl=daily_pnl,
        consecutive_losses=consecutive_losses,
        current_drawdown=current_drawdown,
        portfolio_value=portfolio_value,
    )
