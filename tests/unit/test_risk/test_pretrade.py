"""Parameterized tests for the 10-point PreTradeRiskManager check sequence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from hydra.core.types import (
    Direction,
    MarketType,
    OrderRequest,
    OrderType,
    Position,
    Side,
    Symbol,
)
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager, RiskConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(**overrides: Any) -> OrderRequest:
    defaults: dict[str, Any] = {
        "symbol": Symbol("BTCUSDT"),
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.01"),
        "price": Decimal("42000"),
        "strategy_id": "test",
        "exchange_id": "binance",
        "market_type": MarketType.SPOT,
    }
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _make_portfolio(**overrides: Any) -> PortfolioState:
    defaults: dict[str, Any] = {
        "positions": [],
        "balances": {"USDT": Decimal("10000")},
        "daily_pnl": Decimal("0"),
        "consecutive_losses": 0,
        "current_drawdown": Decimal("0"),
        "portfolio_value": Decimal("10000"),
        "average_volume": Decimal("1000000"),
        "correlation_map": {},
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)


def _make_position(
    symbol: str = "BTCUSDT",
    quantity: str = "0.01",
    entry: str = "42000",
) -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=Direction.LONG,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal(entry),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        strategy_id="test",
        exchange_id="binance",
    )


# ---------------------------------------------------------------------------
# Parameterized test: each check individually
# ---------------------------------------------------------------------------


class TestPreTradeRiskChecks:
    @pytest.mark.parametrize(
        ("scenario", "expected_approved", "reason_substring"),
        [
            ("position_too_large", False, "position size"),
            ("risk_exceeded", False, "risk per trade"),
            ("heat_exceeded", False, "portfolio heat"),
            ("circuit_breaker", False, "circuit breaker"),
            ("daily_loss", False, "daily loss"),
            ("consecutive_losses", False, "consecutive"),
            ("insufficient_balance", False, "balance"),
            ("low_liquidity", False, "liquidity"),
            ("valid_order", True, "approved"),
        ],
        ids=[
            "position_too_large",
            "risk_exceeded",
            "heat_exceeded",
            "circuit_breaker",
            "daily_loss",
            "consecutive_losses",
            "insufficient_balance",
            "low_liquidity",
            "valid_order",
        ],
    )
    async def test_risk_check(
        self,
        scenario: str,
        expected_approved: bool,
        reason_substring: str,
    ) -> None:
        order, portfolio, config_kwargs, cb_tier = self._build_scenario(scenario)
        config = RiskConfig(**config_kwargs) if config_kwargs else RiskConfig()
        mgr = PreTradeRiskManager(config=config, circuit_breaker_tier=cb_tier)

        result = await mgr.check_order(order, portfolio)
        assert result.approved is expected_approved
        assert reason_substring.lower() in result.reason.lower()

    @staticmethod
    def _build_scenario(
        name: str,
    ) -> tuple[OrderRequest, PortfolioState, dict[str, Any], int]:
        """Build order + portfolio + config for each scenario."""
        config_kwargs: dict[str, Any] = {}
        cb_tier = 0

        if name == "position_too_large":
            # Order value = 0.5 * 42000 = 21000 > 10% of 10000 = 1000
            order = _make_order(quantity=Decimal("0.5"), price=Decimal("42000"))
            portfolio = _make_portfolio(portfolio_value=Decimal("10000"))

        elif name == "risk_exceeded":
            # Order value / portfolio = 5000/10000 = 50% > 2%
            order = _make_order(quantity=Decimal("0.5"), price=Decimal("10000"))
            portfolio = _make_portfolio(portfolio_value=Decimal("10000"))
            config_kwargs = {"max_position_pct": Decimal("1.0")}  # pass position check

        elif name == "heat_exceeded":
            # Existing position risk = 420/10000 = 4.2%
            # New order risk = 420/10000 = 4.2%
            # Total = 8.4% > 6%
            pos = _make_position(quantity="0.01", entry="42000")
            order = _make_order(quantity=Decimal("0.01"), price=Decimal("42000"))
            portfolio = _make_portfolio(
                positions=[pos],
                portfolio_value=Decimal("10000"),
            )
            config_kwargs = {
                "max_position_pct": Decimal("1.0"),
                "max_risk_per_trade": Decimal("1.0"),
            }

        elif name == "circuit_breaker":
            order = _make_order(quantity=Decimal("0.001"), price=Decimal("42000"))
            portfolio = _make_portfolio()
            cb_tier = 2

        elif name == "daily_loss":
            order = _make_order(quantity=Decimal("0.001"), price=Decimal("42000"))
            portfolio = _make_portfolio(daily_pnl=Decimal("-500"))  # 5% of 10000

        elif name == "consecutive_losses":
            order = _make_order(quantity=Decimal("0.001"), price=Decimal("42000"))
            portfolio = _make_portfolio(consecutive_losses=5)

        elif name == "insufficient_balance":
            order = _make_order(quantity=Decimal("1"), price=Decimal("50000"))
            portfolio = _make_portfolio(
                balances={"USDT": Decimal("100")},
                portfolio_value=Decimal("1000000"),
                average_volume=Decimal("999999999"),
            )
            config_kwargs = {
                "max_position_pct": Decimal("1.0"),
                "max_risk_per_trade": Decimal("1.0"),
                "max_portfolio_heat": Decimal("1.0"),
            }

        elif name == "low_liquidity":
            order = _make_order(quantity=Decimal("200"), price=Decimal("1"))
            portfolio = _make_portfolio(
                average_volume=Decimal("10000"),
                balances={"USDT": Decimal("999999")},
                portfolio_value=Decimal("999999"),
            )
            config_kwargs = {
                "max_position_pct": Decimal("1.0"),
                "max_risk_per_trade": Decimal("1.0"),
                "max_portfolio_heat": Decimal("1.0"),
            }

        elif name == "valid_order":
            # Small order that passes everything
            order = _make_order(quantity=Decimal("0.001"), price=Decimal("42000"))
            portfolio = _make_portfolio(
                balances={"USDT": Decimal("10000")},
                portfolio_value=Decimal("10000"),
                average_volume=Decimal("1000000"),
            )

        else:
            raise ValueError(f"Unknown scenario: {name}")

        return order, portfolio, config_kwargs, cb_tier


class TestPreTradeEdgeCases:
    async def test_zero_portfolio_value_fails(self) -> None:
        mgr = PreTradeRiskManager()
        order = _make_order()
        portfolio = _make_portfolio(portfolio_value=Decimal("0"))
        result = await mgr.check_order(order, portfolio)
        assert result.approved is False

    async def test_no_price_uses_fallback(self) -> None:
        mgr = PreTradeRiskManager()
        order = _make_order(price=None, quantity=Decimal("0.001"))
        portfolio = _make_portfolio()
        result = await mgr.check_order(order, portfolio)
        # Should still run checks without crashing
        assert isinstance(result.approved, bool)
