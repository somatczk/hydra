"""Tests for PnLCalculator: unrealized, realized, attribution, fees, returns."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.types import Direction, Position, Symbol
from hydra.portfolio.pnl import PnLCalculator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

calc = PnLCalculator()


def _pos(
    direction: Direction = Direction.LONG,
    quantity: str = "1",
    entry: str = "40000",
    unrealized: str = "0",
    realized: str = "0",
    strategy_id: str = "test",
    exchange_id: str = "binance",
    symbol: str = "BTCUSDT",
) -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=direction,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal(entry),
        unrealized_pnl=Decimal(unrealized),
        realized_pnl=Decimal(realized),
        strategy_id=strategy_id,
        exchange_id=exchange_id,
    )


# ---------------------------------------------------------------------------
# Unrealized PnL
# ---------------------------------------------------------------------------


class TestUnrealizedPnL:
    def test_long_profit(self) -> None:
        pos = _pos(direction=Direction.LONG, quantity="2", entry="40000")
        result = calc.unrealized_pnl(pos, Decimal("42000"))
        # (42000 - 40000) * 2 = 4000
        assert result == Decimal("4000")

    def test_long_loss(self) -> None:
        pos = _pos(direction=Direction.LONG, quantity="1", entry="40000")
        result = calc.unrealized_pnl(pos, Decimal("38000"))
        # (38000 - 40000) * 1 = -2000
        assert result == Decimal("-2000")

    def test_short_profit(self) -> None:
        pos = _pos(direction=Direction.SHORT, quantity="3", entry="50000")
        result = calc.unrealized_pnl(pos, Decimal("48000"))
        # (50000 - 48000) * 3 = 6000
        assert result == Decimal("6000")

    def test_short_loss(self) -> None:
        pos = _pos(direction=Direction.SHORT, quantity="1", entry="50000")
        result = calc.unrealized_pnl(pos, Decimal("52000"))
        # (50000 - 52000) * 1 = -2000
        assert result == Decimal("-2000")

    def test_flat_returns_zero(self) -> None:
        pos = _pos(direction=Direction.FLAT, quantity="0")
        result = calc.unrealized_pnl(pos, Decimal("42000"))
        assert result == Decimal("0")


# ---------------------------------------------------------------------------
# Realized PnL
# ---------------------------------------------------------------------------


class TestRealizedPnL:
    @pytest.mark.parametrize(
        ("entry", "exit_", "qty", "direction", "fees", "funding", "expected"),
        [
            ("40000", "42000", "1", Direction.LONG, "10", "5", "1985"),
            ("50000", "48000", "2", Direction.SHORT, "20", "10", "3970"),
            ("40000", "40000", "1", Direction.LONG, "10", "0", "-10"),
            ("40000", "42000", "1", Direction.LONG, "0", "0", "2000"),
            ("40000", "38000", "1", Direction.LONG, "10", "5", "-2015"),
        ],
        ids=[
            "long_profit_with_fees",
            "short_profit_with_fees",
            "breakeven_minus_fees",
            "long_no_fees",
            "long_loss_with_fees",
        ],
    )
    def test_realized_pnl_for_trade(
        self,
        entry: str,
        exit_: str,
        qty: str,
        direction: Direction,
        fees: str,
        funding: str,
        expected: str,
    ) -> None:
        result = calc.realized_pnl_for_trade(
            entry_price=Decimal(entry),
            exit_price=Decimal(exit_),
            quantity=Decimal(qty),
            direction=direction,
            fees=Decimal(fees),
            funding=Decimal(funding),
        )
        assert result == Decimal(expected)


# ---------------------------------------------------------------------------
# Portfolio-level
# ---------------------------------------------------------------------------


class TestTotalPortfolioPnL:
    def test_sums_unrealized_and_realized(self) -> None:
        positions = [
            _pos(unrealized="500", realized="100"),
            _pos(unrealized="-200", realized="300"),
        ]
        total = calc.total_portfolio_pnl(positions)
        # (500+100) + (-200+300) = 700
        assert total == Decimal("700")

    def test_empty_positions(self) -> None:
        assert calc.total_portfolio_pnl([]) == Decimal("0")


class TestDailyPnL:
    def test_combines_realized_trades_and_unrealized_positions(self) -> None:
        trades = [{"pnl": Decimal("500")}, {"pnl": Decimal("-100")}]
        positions = [_pos(unrealized="200"), _pos(unrealized="-50")]
        result = calc.daily_pnl(trades, positions)
        # realized = 500 + (-100) = 400, unrealized = 200 + (-50) = 150
        assert result == Decimal("550")


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class TestStrategyAttribution:
    def test_groups_by_strategy_id(self) -> None:
        positions = [
            _pos(strategy_id="alpha", unrealized="100", realized="50"),
            _pos(strategy_id="beta", unrealized="200", realized="-30"),
            _pos(strategy_id="alpha", unrealized="10", realized="20", symbol="ETHUSDT"),
        ]
        trades = [
            {"strategy_id": "alpha", "pnl": Decimal("500")},
            {"strategy_id": "beta", "pnl": Decimal("100")},
        ]
        result = calc.strategy_attribution(positions, trades)
        # alpha: (100+50) + (10+20) + 500 = 680
        assert result["alpha"] == Decimal("680")
        # beta: (200+(-30)) + 100 = 270
        assert result["beta"] == Decimal("270")

    def test_empty_inputs(self) -> None:
        result = calc.strategy_attribution([], [])
        assert result == {}


# ---------------------------------------------------------------------------
# Fee breakdown
# ---------------------------------------------------------------------------


class TestFeeBreakdown:
    def test_sums_trading_and_funding(self) -> None:
        trades = [
            {"fees": Decimal("10"), "funding_cost": Decimal("2")},
            {"fees": Decimal("15"), "funding_cost": Decimal("3")},
        ]
        result = calc.fee_breakdown(trades)
        assert result["trading_fees"] == Decimal("25")
        assert result["funding_fees"] == Decimal("5")
        assert result["total"] == Decimal("30")

    def test_empty_trades(self) -> None:
        result = calc.fee_breakdown([])
        assert result["total"] == Decimal("0")

    def test_missing_keys_default_to_zero(self) -> None:
        trades: list[dict] = [{}]
        result = calc.fee_breakdown(trades)
        assert result["trading_fees"] == Decimal("0")
        assert result["funding_fees"] == Decimal("0")
        assert result["total"] == Decimal("0")


# ---------------------------------------------------------------------------
# Monthly returns
# ---------------------------------------------------------------------------


class TestMonthlyReturns:
    def test_single_month(self) -> None:
        curve = [
            (datetime(2026, 1, 1, tzinfo=UTC), Decimal("10000")),
            (datetime(2026, 1, 15, tzinfo=UTC), Decimal("10500")),
            (datetime(2026, 1, 31, tzinfo=UTC), Decimal("11000")),
        ]
        result = calc.monthly_returns(curve)
        assert "2026-01" in result
        # (11000 - 10000) / 10000 * 100 = 10%
        assert result["2026-01"] == Decimal("10") * Decimal("1")

    def test_two_months(self) -> None:
        curve = [
            (datetime(2026, 1, 1, tzinfo=UTC), Decimal("10000")),
            (datetime(2026, 1, 31, tzinfo=UTC), Decimal("11000")),
            (datetime(2026, 2, 1, tzinfo=UTC), Decimal("11000")),
            (datetime(2026, 2, 28, tzinfo=UTC), Decimal("10450")),
        ]
        result = calc.monthly_returns(curve)
        # January: (11000 - 10000) / 10000 * 100 = 10
        assert result["2026-01"] == Decimal("10") * Decimal("1")
        # February: (10450 - 11000) / 11000 * 100 = -5
        assert result["2026-02"] == Decimal("-5") * Decimal("1")

    def test_empty_curve(self) -> None:
        assert calc.monthly_returns([]) == {}
