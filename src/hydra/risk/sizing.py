"""Position sizing algorithms: fixed-fractional, ATR-based, Kelly, volatility-scaled.

All methods return position size in **base currency units** as ``Decimal``.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


class PositionSizer:
    """Collection of position sizing methods.

    Each static / class method is self-contained and returns the recommended
    position size in base currency units.
    """

    @staticmethod
    def fixed_fractional(
        portfolio_value: Decimal,
        risk_pct: Decimal,
    ) -> Decimal:
        """Simple fixed-fraction position size.

        Returns ``portfolio_value * risk_pct`` as the dollar-risk amount.
        This represents the maximum dollar amount risked, not the number of
        units.  To convert to units, divide by (entry - stop).
        """
        if portfolio_value <= Decimal("0") or risk_pct <= Decimal("0"):
            return Decimal("0")
        return (portfolio_value * risk_pct).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    @staticmethod
    def atr_based(
        portfolio_value: Decimal,
        risk_pct: Decimal,
        atr_value: Decimal,
        price: Decimal,
    ) -> Decimal:
        """ATR-based position sizing: risk / (ATR * price).

        The position size is inversely proportional to ATR -- when volatility
        rises the position shrinks.

        Returns the number of base-currency units to trade.
        """
        if (
            portfolio_value <= Decimal("0")
            or risk_pct <= Decimal("0")
            or atr_value <= Decimal("0")
            or price <= Decimal("0")
        ):
            return Decimal("0")

        dollar_risk = portfolio_value * risk_pct
        position_size = dollar_risk / (atr_value * price)
        return position_size.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    @staticmethod
    def kelly_criterion(
        win_rate: Decimal,
        avg_win: Decimal,
        avg_loss: Decimal,
        fraction: float = 0.25,
    ) -> Decimal:
        """Quarter-Kelly (default) position sizing.

        Full Kelly = (win_rate / avg_loss) - ((1 - win_rate) / avg_win)
        We return ``fraction`` of the full Kelly value (0.25 = quarter-Kelly).

        Returns the recommended fraction of capital to risk (0.0 .. 1.0).
        """
        if (
            win_rate <= Decimal("0")
            or win_rate >= Decimal("1")
            or avg_win <= Decimal("0")
            or avg_loss <= Decimal("0")
        ):
            return Decimal("0")

        # Kelly formula: f* = W/L - (1-W)/G
        # where W=win_rate, L=avg_loss ratio, G=avg_win ratio
        kelly = (win_rate / avg_loss) - ((Decimal("1") - win_rate) / avg_win)

        if kelly <= Decimal("0"):
            return Decimal("0")

        result = kelly * Decimal(str(fraction))
        # Cap at 1.0 (100% of capital)
        result = min(result, Decimal("1"))
        return result.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    @staticmethod
    def volatility_scaled(
        portfolio_value: Decimal,
        target_vol: Decimal,
        current_vol: Decimal,
        price: Decimal,
    ) -> Decimal:
        """Volatility-scaled position sizing targeting 15% annualized vol.

        position_size = (portfolio_value * target_vol) / (current_vol * price)

        When current volatility is high, the position shrinks.  When low, it grows.

        Returns the number of base-currency units.
        """
        if (
            portfolio_value <= Decimal("0")
            or target_vol <= Decimal("0")
            or current_vol <= Decimal("0")
            or price <= Decimal("0")
        ):
            return Decimal("0")

        position_size = (portfolio_value * target_vol) / (current_vol * price)
        return position_size.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
