"""Pre-trade risk manager implementing a 10-point check sequence.

Every order must pass all 10 checks before it is approved for submission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from hydra.core.events import RiskCheckResult
from hydra.core.types import OrderRequest, Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Portfolio state snapshot
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PortfolioState:
    """Snapshot of portfolio state used by the risk manager."""

    positions: list[Position] = field(default_factory=list)
    balances: dict[str, Decimal] = field(default_factory=dict)
    daily_pnl: Decimal = Decimal("0")
    consecutive_losses: int = 0
    current_drawdown: Decimal = Decimal("0")
    portfolio_value: Decimal = Decimal("10000")
    average_volume: Decimal = Decimal("1000000")
    correlation_map: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Risk configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Configuration for pre-trade risk checks."""

    max_position_pct: Decimal = Decimal("0.10")  # 10% of portfolio
    max_risk_per_trade: Decimal = Decimal("0.02")  # 2%
    max_portfolio_heat: Decimal = Decimal("0.06")  # 6%
    max_daily_loss_pct: Decimal = Decimal("0.03")  # 3%
    max_consecutive_losses: int = 5
    consecutive_loss_cooldown_hours: float = 4.0
    max_liquidity_pct: Decimal = Decimal("0.01")  # 1% of avg volume
    min_leverage: int = 1
    max_leverage: int = 20
    correlation_threshold: Decimal = Decimal("0.70")


# ---------------------------------------------------------------------------
# PreTradeRiskManager
# ---------------------------------------------------------------------------


class PreTradeRiskManager:
    """10-point pre-trade risk check sequence.

    Checks:
        1. Position size <= max_position_pct of portfolio
        2. Risk per trade <= max_risk_per_trade (2% default)
        3. Portfolio heat <= max_portfolio_heat (6% default)
        4. Correlation check -- no highly correlated duplicate exposure
        5. Circuit breaker status -- reject if tier 2+ active
        6. Daily loss limit -- reject if daily loss exceeds 3%
        7. Consecutive loss counter -- pause after 5 consecutive losses
        8. Sufficient balance/margin for the order
        9. Liquidity check -- order size < 1% of recent avg volume
       10. Leverage bounds -- within configured min/max
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        circuit_breaker_tier: int = 0,
    ) -> None:
        self._config = config or RiskConfig()
        self._circuit_breaker_tier = circuit_breaker_tier

    @property
    def circuit_breaker_tier(self) -> int:
        return self._circuit_breaker_tier

    @circuit_breaker_tier.setter
    def circuit_breaker_tier(self, value: int) -> None:
        self._circuit_breaker_tier = value

    async def check_order(
        self,
        order: OrderRequest,
        portfolio_state: PortfolioState,
    ) -> RiskCheckResult:
        """Run the full 10-point check sequence.  All must pass."""
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        checkers = [
            ("position_size", self._check_position_size),
            ("risk_per_trade", self._check_risk_per_trade),
            ("portfolio_heat", self._check_portfolio_heat),
            ("correlation", self._check_correlation),
            ("circuit_breaker", self._check_circuit_breaker),
            ("daily_loss", self._check_daily_loss),
            ("consecutive_losses", self._check_consecutive_losses),
            ("balance", self._check_balance),
            ("liquidity", self._check_liquidity),
            ("leverage", self._check_leverage),
        ]

        for name, checker in checkers:
            ok, reason = checker(order, portfolio_state)
            if ok:
                checks_passed.append(name)
            else:
                checks_failed.append(name)
                return RiskCheckResult(
                    order_request_id=order.request_id,
                    approved=False,
                    reason=reason,
                )

        return RiskCheckResult(
            order_request_id=order.request_id,
            approved=True,
            reason="All checks approved",
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_position_size(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """1. Position size <= max_position_pct of portfolio."""
        if state.portfolio_value == Decimal("0"):
            return False, "position size check failed: zero portfolio value"
        order_value = order.quantity * (order.price or Decimal("0"))
        max_value = state.portfolio_value * self._config.max_position_pct
        if order_value > max_value:
            return False, (
                f"position size {order_value} exceeds max "
                f"{self._config.max_position_pct * 100}% of portfolio ({max_value})"
            )
        return True, "position size ok"

    def _check_risk_per_trade(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """2. Risk per trade <= max_risk_per_trade."""
        if state.portfolio_value == Decimal("0"):
            return False, "risk per trade check failed: zero portfolio value"
        # Estimate risk as order value / portfolio value
        price = order.price or Decimal("1")
        order_value = order.quantity * price
        risk_pct = order_value / state.portfolio_value
        if risk_pct > self._config.max_risk_per_trade:
            return False, (
                f"risk per trade {risk_pct:.4f} exceeds max {self._config.max_risk_per_trade}"
            )
        return True, "risk per trade ok"

    def _check_portfolio_heat(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """3. Portfolio heat (sum of all position risks) <= max_portfolio_heat."""
        if state.portfolio_value == Decimal("0"):
            return False, "portfolio heat check failed: zero portfolio value"
        total_risk = Decimal("0")
        for pos in state.positions:
            pos_value = pos.quantity * pos.avg_entry_price
            total_risk += pos_value / state.portfolio_value

        # Add risk from the proposed order
        price = order.price or Decimal("1")
        proposed_risk = (order.quantity * price) / state.portfolio_value
        total_risk += proposed_risk

        if total_risk > self._config.max_portfolio_heat:
            return False, (
                f"portfolio heat {total_risk:.4f} exceeds max {self._config.max_portfolio_heat}"
            )
        return True, "portfolio heat ok"

    def _check_correlation(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """4. Correlation check -- don't add highly correlated position."""
        symbol = str(order.symbol)
        correlated_symbols = state.correlation_map.get(symbol, [])
        for pos in state.positions:
            if str(pos.symbol) in correlated_symbols and pos.quantity > Decimal("0"):
                return False, (
                    f"correlation check failed: {symbol} is highly correlated "
                    f"with existing position {pos.symbol}"
                )
        return True, "correlation check ok"

    def _check_circuit_breaker(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """5. Circuit breaker status -- reject if tier 2+ active."""
        if self._circuit_breaker_tier >= 2:
            return False, (
                f"circuit breaker tier {self._circuit_breaker_tier} active: new trades halted"
            )
        return True, "circuit breaker ok"

    def _check_daily_loss(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """6. Daily loss limit -- reject if daily loss exceeds 3%."""
        if state.portfolio_value == Decimal("0"):
            return False, "daily loss check failed: zero portfolio value"
        if state.daily_pnl < 0:
            daily_loss_pct = abs(state.daily_pnl) / state.portfolio_value
        else:
            daily_loss_pct = Decimal("0")
        if daily_loss_pct > self._config.max_daily_loss_pct:
            return False, (
                f"daily loss {daily_loss_pct:.4f} exceeds max {self._config.max_daily_loss_pct}"
            )
        return True, "daily loss ok"

    def _check_consecutive_losses(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """7. Consecutive loss counter -- pause after 5 consecutive losses."""
        if state.consecutive_losses >= self._config.max_consecutive_losses:
            return False, (
                f"consecutive losses ({state.consecutive_losses}) "
                f">= max ({self._config.max_consecutive_losses}): "
                f"{self._config.consecutive_loss_cooldown_hours}h cooldown"
            )
        return True, "consecutive losses ok"

    def _check_balance(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """8. Sufficient balance/margin for the order."""
        price = order.price or Decimal("1")
        required = order.quantity * price
        available = sum(state.balances.values()) if state.balances else Decimal("0")
        if required > available:
            return False, (f"insufficient balance: need {required}, have {available}")
        return True, "balance ok"

    def _check_liquidity(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """9. Liquidity check -- order size < 1% of recent average volume."""
        if state.average_volume <= Decimal("0"):
            return True, "liquidity check skipped (no volume data)"
        max_size = state.average_volume * self._config.max_liquidity_pct
        if order.quantity > max_size:
            return False, (
                f"liquidity check failed: order size {order.quantity} "
                f"> {self._config.max_liquidity_pct * 100}% of avg volume ({max_size})"
            )
        return True, "liquidity ok"

    def _check_leverage(
        self,
        order: OrderRequest,
        state: PortfolioState,
    ) -> tuple[bool, str]:
        """10. Leverage bounds -- within configured min/max."""
        if state.portfolio_value == Decimal("0"):
            return False, "leverage check failed: zero portfolio value"
        price = order.price or Decimal("1")
        order_value = order.quantity * price
        effective_leverage = order_value / state.portfolio_value
        if effective_leverage > Decimal(str(self._config.max_leverage)):
            return False, (
                f"effective leverage {effective_leverage:.2f} "
                f"exceeds max {self._config.max_leverage}"
            )
        return True, "leverage ok"
