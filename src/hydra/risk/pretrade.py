"""Pre-trade risk checks, priced at the mark and split by intent.

Every order is classified first:
    reduce - ``reduce_only`` is set, or the order sells (buys) at most the held long (short).
             It cannot add exposure, so only sanity checks apply: exits, stops and flattens
             are never blocked by the kill switch, breakers, limits, rate limit or balance.
    open   - everything else; it runs the full sequence below, priced at the latest mark
             (falling back to an explicit order price; neither means rejection).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from hydra.core.events import RiskCheckResult
from hydra.core.types import Direction, MarketType, OrderRequest, Position, Side

if TYPE_CHECKING:
    from hydra.risk.circuit_breakers import CircuitBreakerRestrictions
    from hydra.risk.market_rules import MarketRules

logger = logging.getLogger(__name__)

Intent = Literal["open", "reduce"]
OPEN: Intent = "open"
REDUCE: Intent = "reduce"
SPOT_MAX_LEVERAGE = Decimal("1")


# ---------------------------------------------------------------------------
# Portfolio state snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioState:
    """What the checks see for one order.

    ``portfolio_value`` is account equity (all sessions) and has no default. ``positions`` and
    ``balances`` are the ordering session's; ``account_positions`` covers every session (None
    means this session is the whole account). ``restrictions`` comes from the RiskService; None
    marks the pre-RiskService path, where the constructor tier and the legacy daily_pnl /
    consecutive_losses fields are used instead.
    """

    portfolio_value: Decimal
    positions: list[Position] = field(default_factory=list)
    balances: dict[str, Decimal] = field(default_factory=dict)
    marks: Mapping[str, Decimal] = field(default_factory=dict)
    sleeve_equity: Decimal | None = None
    account_positions: list[Position] | None = None
    restrictions: CircuitBreakerRestrictions | None = None
    kill_switch: bool = False
    state_available: bool = True
    opening_orders_last_minute: int = 0
    market_rules: Mapping[str, MarketRules] = field(default_factory=dict)
    average_volume: Decimal | None = None
    correlation_map: dict[str, list[str]] = field(default_factory=dict)
    quote_currency: str = "USDT"
    daily_pnl: Decimal = Decimal("0")
    consecutive_losses: int = 0
    current_drawdown: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# Risk configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Limits (risk_config 'global'). Fractions of equity unless stated otherwise.

    max_position_pct caps one order's notional against the sleeve; max_portfolio_heat caps gross
    exposure across all sessions against account equity; max_risk_per_trade applies only to
    orders that carry a stop (loss to stop vs sleeve).
    """

    max_position_pct: Decimal = Decimal("0.10")
    max_risk_per_trade: Decimal = Decimal("0.02")
    max_portfolio_heat: Decimal = Decimal("0.30")
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_consecutive_losses: int = 5
    consecutive_loss_cooldown_hours: float = 4.0
    max_liquidity_pct: Decimal = Decimal("0.01")
    correlation_threshold: Decimal = Decimal("0.70")
    max_drawdown_pct: Decimal = Decimal("0.15")
    max_concurrent_positions: int = 10
    max_orders_per_minute: int = 6
    fee_buffer: Decimal = Decimal("0.002")


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    check: str
    reason: str
    intent: Intent

    def to_result(self, order: OrderRequest) -> RiskCheckResult:
        return RiskCheckResult(
            order_request_id=order.request_id, approved=self.approved, reason=self.reason
        )


def _held(order: OrderRequest, positions: list[Position]) -> Position | None:
    for pos in positions:
        if (
            str(pos.symbol) == str(order.symbol)
            and pos.direction != Direction.FLAT
            and pos.quantity > 0
        ):
            return pos
    return None


def closes_position(order: OrderRequest, pos: Position) -> bool:
    return (pos.direction == Direction.LONG and order.side == Side.SELL) or (
        pos.direction == Direction.SHORT and order.side == Side.BUY
    )


def classify_intent(order: OrderRequest, positions: list[Position]) -> Intent:
    """reduce when flagged reduce_only, or when it closes at most the held position."""
    if order.reduce_only:
        return REDUCE
    pos = _held(order, positions)
    if pos is not None and closes_position(order, pos) and order.quantity <= pos.quantity:
        return REDUCE
    return OPEN


def reference_price(order: OrderRequest, marks: Mapping[str, Decimal]) -> Decimal | None:
    mark = marks.get(str(order.symbol))
    if mark is not None and mark > 0:
        return mark
    if order.price is not None and order.price > 0:
        return order.price
    return None


def gross_exposure(positions: list[Position], marks: Mapping[str, Decimal]) -> Decimal:
    """Sum of |qty x mark| (entry price when a symbol has no mark)."""
    total = Decimal("0")
    for pos in positions:
        if pos.direction == Direction.FLAT or pos.quantity <= 0:
            continue
        price = marks.get(str(pos.symbol)) or pos.avg_entry_price
        total += abs(pos.quantity * price)
    return total


# ---------------------------------------------------------------------------
# PreTradeRiskManager
# ---------------------------------------------------------------------------


class PreTradeRiskManager:
    """Stateless check sequence; account state arrives in the PortfolioState."""

    def __init__(
        self,
        config: RiskConfig | None = None,
        circuit_breaker_tier: int = 0,
        db_pool: Any = None,
    ) -> None:
        self._config = config or RiskConfig()
        self._circuit_breaker_tier = circuit_breaker_tier
        self._db_pool = db_pool

        # Legacy per-process counters for the pre-RiskService session path (P1-E removes it).
        self._daily_pnl = Decimal("0")
        self._daily_pnl_date: date = datetime.now(UTC).date()
        self._consecutive_losses = 0
        self._current_drawdown = Decimal("0")
        self._peak_portfolio_value = Decimal("0")

    @property
    def config(self) -> RiskConfig:
        return self._config

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
        return self.evaluate(order, portfolio_state).to_result(order)

    def evaluate(
        self,
        order: OrderRequest,
        state: PortfolioState,
        intent: Intent | None = None,
    ) -> RiskDecision:
        intent = intent or classify_intent(order, state.positions)
        if order.quantity <= 0:
            return RiskDecision(False, "sanity", f"non-positive quantity {order.quantity}", intent)
        if intent == REDUCE:
            return RiskDecision(True, "reduce", "reducing order: entry checks exempt", intent)
        for check in (
            self._check_price,
            self._check_state,
            self._check_kill_switch,
            self._check_restrictions,
            self._check_spot_short,
            self._check_position_size,
            self._check_risk_per_trade,
            self._check_portfolio_heat,
            self._check_concurrent_positions,
            self._check_balance,
            self._check_market_rules,
            self._check_leverage,
            self._check_liquidity,
            self._check_correlation,
            self._check_rate_limit,
        ):
            failed = check(order, state)
            if failed is not None:
                return RiskDecision(False, failed[0], failed[1], OPEN)
        return RiskDecision(True, "all", "All checks approved", OPEN)

    # ------------------------------------------------------------------
    # Legacy DB-backed counters (session path before P1-E wires the RiskService)
    # ------------------------------------------------------------------

    async def load_state(self, db_pool: Any = None) -> None:
        """Load the legacy 'global' risk_state row."""
        pool = db_pool or self._db_pool
        if pool is None:
            return
        self._db_pool = pool
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM risk_state WHERE scope = 'global'")
                if row is not None:
                    self._daily_pnl = Decimal(str(row["daily_pnl"]))
                    self._daily_pnl_date = row["daily_pnl_date"]
                    self._consecutive_losses = row["consecutive_losses"]
                    self._current_drawdown = Decimal(str(row["current_drawdown"]))
                    self._peak_portfolio_value = Decimal(str(row["peak_portfolio_value"]))
        except Exception:
            logger.exception("Failed to load risk state from DB")

    async def record_fill(self, pnl: Decimal, portfolio_value: Decimal) -> None:
        """Update the legacy 'global' counters; the RiskService path uses RiskService.on_fill."""
        today = datetime.now(UTC).date()
        if self._daily_pnl_date != today:
            self._daily_pnl = Decimal("0")
            self._daily_pnl_date = today
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        if portfolio_value > self._peak_portfolio_value:
            self._peak_portfolio_value = portfolio_value
        if self._peak_portfolio_value > 0:
            self._current_drawdown = (
                self._peak_portfolio_value - portfolio_value
            ) / self._peak_portfolio_value
        if self._db_pool is not None:
            try:
                async with self._db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE risk_state SET "
                        "daily_pnl = $1, daily_pnl_date = $2, "
                        "consecutive_losses = $3, "
                        "last_loss_at = CASE WHEN $4 < 0 THEN now() ELSE last_loss_at END, "
                        "current_drawdown = $5, "
                        "peak_portfolio_value = $6, "
                        "updated_at = now() "
                        "WHERE scope = 'global'",
                        self._daily_pnl,
                        self._daily_pnl_date,
                        self._consecutive_losses,
                        pnl,
                        self._current_drawdown,
                        self._peak_portfolio_value,
                    )
            except Exception:
                logger.exception("Failed to persist risk state to DB")

    # ------------------------------------------------------------------
    # Opening-order checks: each returns (check, reason) on failure
    # ------------------------------------------------------------------

    def _ref(self, order: OrderRequest, state: PortfolioState) -> Decimal:
        price = reference_price(order, state.marks)
        if price is None:
            raise ValueError(f"no mark price for {order.symbol}")
        return price

    def _sleeve(self, state: PortfolioState) -> Decimal:
        return state.sleeve_equity if state.sleeve_equity is not None else state.portfolio_value

    def _check_price(self, order: OrderRequest, state: PortfolioState) -> tuple[str, str] | None:
        if reference_price(order, state.marks) is None:
            return "no_mark", f"no mark price for {order.symbol}"
        return None

    def _check_state(self, order: OrderRequest, state: PortfolioState) -> tuple[str, str] | None:
        if not state.state_available:
            return "risk_state", "risk state unavailable"
        if state.portfolio_value <= 0:
            return "equity", f"non-positive account equity {state.portfolio_value}"
        return None

    def _check_kill_switch(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if state.kill_switch:
            return "kill_switch", "kill switch active"
        return None

    def _check_restrictions(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if state.restrictions is not None:
            return state.restrictions.blocking_reason()
        if self._circuit_breaker_tier >= 2:
            return (
                "circuit_breaker",
                f"circuit breaker tier {self._circuit_breaker_tier} active: new trades halted",
            )
        if state.daily_pnl < 0:
            loss = abs(state.daily_pnl) / state.portfolio_value
            if loss > self._config.max_daily_loss_pct:
                return "daily_loss_halt", (
                    f"daily loss {loss:.4f} exceeds max {self._config.max_daily_loss_pct}"
                )
        if state.consecutive_losses >= self._config.max_consecutive_losses:
            return "consecutive_loss_pause", (
                f"consecutive_loss_pause: {state.consecutive_losses} losses "
                f">= max {self._config.max_consecutive_losses}"
            )
        return None

    def _check_spot_short(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if order.market_type == MarketType.SPOT and order.side == Side.SELL:
            return "spot_short", f"opening SELL of {order.symbol} on spot (no shorting)"
        return None

    def _check_position_size(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        notional = order.quantity * self._ref(order, state)
        sleeve = self._sleeve(state)
        multiplier = (
            state.restrictions.position_size_multiplier
            if state.restrictions is not None
            else Decimal("1")
        )
        cap = self._config.max_position_pct * multiplier * sleeve
        if sleeve <= 0 or notional > cap:
            return "position_size", (
                f"position notional {notional:f} exceeds max_position_pct "
                f"{self._config.max_position_pct} x size multiplier {multiplier} "
                f"of sleeve equity {sleeve} ({cap:f})"
            )
        return None

    def _check_risk_per_trade(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if order.stop_price is None:
            return None
        loss = order.quantity * abs(self._ref(order, state) - order.stop_price)
        cap = self._config.max_risk_per_trade * self._sleeve(state)
        if loss > cap:
            return "risk_per_trade", (
                f"loss to stop {loss:f} exceeds max_risk_per_trade "
                f"{self._config.max_risk_per_trade} of sleeve ({cap:f})"
            )
        return None

    def _account_positions(self, state: PortfolioState) -> list[Position]:
        return state.account_positions if state.account_positions is not None else state.positions

    def _check_portfolio_heat(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        gross = gross_exposure(self._account_positions(state), state.marks)
        heat = (gross + order.quantity * self._ref(order, state)) / state.portfolio_value
        if heat > self._config.max_portfolio_heat:
            return "portfolio_heat", (
                f"gross exposure {heat:.4f} of account equity exceeds max "
                f"{self._config.max_portfolio_heat}"
            )
        return None

    def _check_concurrent_positions(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if _held(order, state.positions) is not None:
            return None
        open_count = sum(
            1
            for p in self._account_positions(state)
            if p.direction != Direction.FLAT and p.quantity > 0
        )
        if open_count >= self._config.max_concurrent_positions:
            return "concurrent_positions", (
                f"{open_count} open positions >= max {self._config.max_concurrent_positions}"
            )
        return None

    def _check_balance(self, order: OrderRequest, state: PortfolioState) -> tuple[str, str] | None:
        if order.side != Side.BUY:
            return None
        cost = order.quantity * self._ref(order, state) * (1 + self._config.fee_buffer)
        have = state.balances.get(state.quote_currency, Decimal("0"))
        if cost > have:
            return "balance", (f"insufficient {state.quote_currency}: need {cost:f}, have {have}")
        return None

    def _check_market_rules(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        rules = state.market_rules.get(str(order.symbol))
        if rules is None:
            return None
        reason = rules.validate_open(order.quantity, self._ref(order, state))
        return None if reason is None else ("market_rules", reason)

    def _check_leverage(self, order: OrderRequest, state: PortfolioState) -> tuple[str, str] | None:
        gross = gross_exposure(self._account_positions(state), state.marks)
        leverage = (gross + order.quantity * self._ref(order, state)) / state.portfolio_value
        if leverage > SPOT_MAX_LEVERAGE:
            return "leverage", f"spot leverage {leverage:.2f} exceeds {SPOT_MAX_LEVERAGE}"
        return None

    def _check_liquidity(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if state.average_volume is None or state.average_volume <= 0:
            return None
        max_size = state.average_volume * self._config.max_liquidity_pct
        if order.quantity > max_size:
            return "liquidity", (
                f"order size {order.quantity} > {self._config.max_liquidity_pct} "
                f"of average volume ({max_size})"
            )
        return None

    def _check_correlation(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        correlated = state.correlation_map.get(str(order.symbol), [])
        for pos in self._account_positions(state):
            if str(pos.symbol) in correlated and pos.quantity > 0:
                return "correlation", (
                    f"{order.symbol} is highly correlated with open position {pos.symbol}"
                )
        return None

    def _check_rate_limit(
        self, order: OrderRequest, state: PortfolioState
    ) -> tuple[str, str] | None:
        if state.opening_orders_last_minute >= self._config.max_orders_per_minute:
            return "rate_limit", (
                f"{state.opening_orders_last_minute} opening orders in the last minute "
                f">= max {self._config.max_orders_per_minute}"
            )
        return None
