"""PnL calculation utilities for the Hydra trading platform.

All financial arithmetic uses ``Decimal`` for precision. No floating-point
intermediaries are introduced.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from hydra.core.types import Direction, Position


class PnLCalculator:
    """Stateless calculator for profit-and-loss metrics."""

    # ------------------------------------------------------------------
    # Per-position
    # ------------------------------------------------------------------

    @staticmethod
    def unrealized_pnl(position: Position, current_price: Decimal) -> Decimal:
        """Compute unrealized PnL for a single position.

        LONG:  (current_price - avg_entry_price) * quantity
        SHORT: (avg_entry_price - current_price) * quantity
        """
        if position.direction == Direction.LONG:
            return (current_price - position.avg_entry_price) * position.quantity
        if position.direction == Direction.SHORT:
            return (position.avg_entry_price - current_price) * position.quantity
        return Decimal("0")

    @staticmethod
    def realized_pnl_for_trade(
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
        direction: Direction,
        fees: Decimal = Decimal("0"),
        funding: Decimal = Decimal("0"),
    ) -> Decimal:
        """Compute realized PnL for a single round-trip trade.

        Returns gross PnL minus fees and funding costs.
        """
        if direction == Direction.LONG:
            gross = (exit_price - entry_price) * quantity
        elif direction == Direction.SHORT:
            gross = (entry_price - exit_price) * quantity
        else:
            gross = Decimal("0")
        return gross - fees - funding

    # ------------------------------------------------------------------
    # Portfolio-level
    # ------------------------------------------------------------------

    @staticmethod
    def total_portfolio_pnl(positions: list[Position]) -> Decimal:
        """Sum of unrealized + realized PnL across all positions."""
        return sum(
            (p.unrealized_pnl + p.realized_pnl for p in positions),
            Decimal("0"),
        )

    @staticmethod
    def daily_pnl(
        trades_today: list[dict],
        positions: list[Position],
    ) -> Decimal:
        """Realized from today's closed trades + current unrealized on open positions.

        ``trades_today`` is a list of dicts with at least a ``"pnl"`` key
        (``Decimal``).
        """
        realized = sum(
            (Decimal(str(t["pnl"])) for t in trades_today),
            Decimal("0"),
        )
        unrealized = sum(
            (p.unrealized_pnl for p in positions),
            Decimal("0"),
        )
        return realized + unrealized

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------

    @staticmethod
    def strategy_attribution(
        positions: list[Position],
        trades: list[dict],
    ) -> dict[str, Decimal]:
        """Aggregate PnL per strategy_id.

        Combines unrealized PnL from open positions with realized PnL from
        closed trades (each trade dict must have ``strategy_id`` and ``pnl``).
        """
        result: dict[str, Decimal] = {}
        for pos in positions:
            sid = pos.strategy_id
            result[sid] = result.get(sid, Decimal("0")) + pos.unrealized_pnl + pos.realized_pnl
        for trade in trades:
            sid = trade["strategy_id"]
            result[sid] = result.get(sid, Decimal("0")) + Decimal(str(trade["pnl"]))
        return result

    # ------------------------------------------------------------------
    # Fee analysis
    # ------------------------------------------------------------------

    @staticmethod
    def fee_breakdown(trades: list[dict]) -> dict[str, Decimal]:
        """Summarize fees across trades.

        Each trade dict should have ``fees`` (trading fee) and ``funding_cost``
        keys, both ``Decimal``-convertible.

        Returns a dict with ``trading_fees``, ``funding_fees``, ``total``.
        """
        trading = sum(
            (Decimal(str(t.get("fees", "0"))) for t in trades),
            Decimal("0"),
        )
        funding = sum(
            (Decimal(str(t.get("funding_cost", "0"))) for t in trades),
            Decimal("0"),
        )
        return {
            "trading_fees": trading,
            "funding_fees": funding,
            "total": trading + funding,
        }

    # ------------------------------------------------------------------
    # Returns
    # ------------------------------------------------------------------

    @staticmethod
    def monthly_returns(
        equity_curve: list[tuple[datetime, Decimal]],
    ) -> dict[str, Decimal]:
        """Compute monthly return percentages from an equity time-series.

        Each entry is ``(datetime, equity_value)``. Returns a dict keyed by
        ``"YYYY-MM"`` with the percentage return for that month. The first data
        point of each month is used as the opening equity.
        """
        if not equity_curve:
            return {}

        # Sort by time
        sorted_curve = sorted(equity_curve, key=lambda x: x[0])

        # Group into months: first and last value per month
        months: dict[str, list[tuple[datetime, Decimal]]] = {}
        for ts, equity in sorted_curve:
            month_key = ts.strftime("%Y-%m")
            months.setdefault(month_key, []).append((ts, equity))

        result: dict[str, Decimal] = {}
        prev_close: Decimal | None = None

        for month_key in sorted(months.keys()):
            points = months[month_key]
            month_open = points[0][1] if prev_close is None else prev_close
            month_close = points[-1][1]

            if month_open != Decimal("0"):
                pct = ((month_close - month_open) / month_open) * Decimal("100")
            else:
                pct = Decimal("0")

            result[month_key] = pct
            prev_close = month_close

        return result
