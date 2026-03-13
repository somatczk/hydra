"""Tests for PositionSizer: all 4 methods with known inputs and expected outputs."""

from __future__ import annotations

from decimal import Decimal

from hydra.risk.sizing import PositionSizer


class TestFixedFractional:
    def test_basic_calculation(self) -> None:
        result = PositionSizer.fixed_fractional(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
        )
        assert result == Decimal("2000.00000000")

    def test_small_fraction(self) -> None:
        result = PositionSizer.fixed_fractional(
            portfolio_value=Decimal("50000"),
            risk_pct=Decimal("0.01"),
        )
        assert result == Decimal("500.00000000")

    def test_zero_portfolio_returns_zero(self) -> None:
        result = PositionSizer.fixed_fractional(
            portfolio_value=Decimal("0"),
            risk_pct=Decimal("0.02"),
        )
        assert result == Decimal("0")

    def test_zero_risk_returns_zero(self) -> None:
        result = PositionSizer.fixed_fractional(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0"),
        )
        assert result == Decimal("0")


class TestATRBased:
    def test_basic_calculation(self) -> None:
        # risk = 100000 * 0.02 = 2000
        # size = 2000 / (500 * 42000) = 2000 / 21000000 = 0.00009523...
        result = PositionSizer.atr_based(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
            atr_value=Decimal("500"),
            price=Decimal("42000"),
        )
        assert result > Decimal("0")
        assert result < Decimal("1")

    def test_inversely_proportional_to_atr(self) -> None:
        """Higher ATR should produce smaller position size."""
        low_atr = PositionSizer.atr_based(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
            atr_value=Decimal("200"),
            price=Decimal("42000"),
        )
        high_atr = PositionSizer.atr_based(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
            atr_value=Decimal("800"),
            price=Decimal("42000"),
        )
        assert low_atr > high_atr

    def test_zero_atr_returns_zero(self) -> None:
        result = PositionSizer.atr_based(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
            atr_value=Decimal("0"),
            price=Decimal("42000"),
        )
        assert result == Decimal("0")

    def test_zero_price_returns_zero(self) -> None:
        result = PositionSizer.atr_based(
            portfolio_value=Decimal("100000"),
            risk_pct=Decimal("0.02"),
            atr_value=Decimal("500"),
            price=Decimal("0"),
        )
        assert result == Decimal("0")


class TestKellyCriterion:
    def test_quarter_kelly_less_than_full(self) -> None:
        """Quarter-Kelly should be exactly 25% of full Kelly."""
        quarter = PositionSizer.kelly_criterion(
            win_rate=Decimal("0.55"),
            avg_win=Decimal("1.5"),
            avg_loss=Decimal("1.0"),
            fraction=0.25,
        )
        full = PositionSizer.kelly_criterion(
            win_rate=Decimal("0.55"),
            avg_win=Decimal("1.5"),
            avg_loss=Decimal("1.0"),
            fraction=1.0,
        )
        assert quarter < full
        # quarter should be approximately 25% of full
        assert abs(quarter * 4 - full) < Decimal("0.0001")

    def test_positive_edge_returns_positive(self) -> None:
        result = PositionSizer.kelly_criterion(
            win_rate=Decimal("0.6"),
            avg_win=Decimal("2.0"),
            avg_loss=Decimal("1.0"),
        )
        assert result > Decimal("0")

    def test_no_edge_returns_zero(self) -> None:
        """If the edge is negative or zero, Kelly returns 0."""
        result = PositionSizer.kelly_criterion(
            win_rate=Decimal("0.30"),
            avg_win=Decimal("1.0"),
            avg_loss=Decimal("1.0"),
        )
        # With 30% win rate and 1:1 payoff, Kelly should be negative -> capped at 0
        assert result == Decimal("0")

    def test_zero_win_rate_returns_zero(self) -> None:
        result = PositionSizer.kelly_criterion(
            win_rate=Decimal("0"),
            avg_win=Decimal("2.0"),
            avg_loss=Decimal("1.0"),
        )
        assert result == Decimal("0")

    def test_extreme_win_rate_capped(self) -> None:
        """Win rate of 1.0 is invalid (edge case)."""
        result = PositionSizer.kelly_criterion(
            win_rate=Decimal("1"),
            avg_win=Decimal("2.0"),
            avg_loss=Decimal("1.0"),
        )
        assert result == Decimal("0")


class TestVolatilityScaled:
    def test_basic_calculation(self) -> None:
        # (100000 * 0.15) / (0.30 * 42000) = 15000 / 12600 = 1.190...
        result = PositionSizer.volatility_scaled(
            portfolio_value=Decimal("100000"),
            target_vol=Decimal("0.15"),
            current_vol=Decimal("0.30"),
            price=Decimal("42000"),
        )
        assert result > Decimal("0")

    def test_high_vol_reduces_size(self) -> None:
        """Higher current volatility should produce smaller position."""
        low_vol = PositionSizer.volatility_scaled(
            portfolio_value=Decimal("100000"),
            target_vol=Decimal("0.15"),
            current_vol=Decimal("0.10"),
            price=Decimal("42000"),
        )
        high_vol = PositionSizer.volatility_scaled(
            portfolio_value=Decimal("100000"),
            target_vol=Decimal("0.15"),
            current_vol=Decimal("0.50"),
            price=Decimal("42000"),
        )
        assert low_vol > high_vol

    def test_zero_current_vol_returns_zero(self) -> None:
        result = PositionSizer.volatility_scaled(
            portfolio_value=Decimal("100000"),
            target_vol=Decimal("0.15"),
            current_vol=Decimal("0"),
            price=Decimal("42000"),
        )
        assert result == Decimal("0")

    def test_zero_price_returns_zero(self) -> None:
        result = PositionSizer.volatility_scaled(
            portfolio_value=Decimal("100000"),
            target_vol=Decimal("0.15"),
            current_vol=Decimal("0.30"),
            price=Decimal("0"),
        )
        assert result == Decimal("0")
