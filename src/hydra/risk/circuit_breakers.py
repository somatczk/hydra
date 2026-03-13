"""Circuit breaker system with 4 tiers of protection and additional breakers.

Tiers:
    1: 3% drawdown  -> reduce_size_50  (auto-reset 24h)
    2: 5% drawdown  -> halt_new_trades (auto-reset 48h)
    3: 10% drawdown -> flatten_all     (auto-reset 72h, requires manual review)
    4: 15% drawdown -> emergency_shutdown (no auto-reset, Telegram alert)

Additional breakers:
    - Daily loss 3%  -> halt rest of day
    - 5 consecutive losses -> pause 4h
    - Volatility spike (>3x normal) -> reduce position sizes 50%
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from hydra.core.events import CircuitBreakerEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBusLike(Protocol):
    async def publish(self, event: Any) -> None: ...


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TierSpec:
    tier: int
    drawdown_threshold: Decimal
    action: str
    auto_reset_hours: float | None  # None = no auto-reset


_TIERS: list[_TierSpec] = [
    _TierSpec(
        tier=1,
        drawdown_threshold=Decimal("0.03"),
        action="reduce_size_50",
        auto_reset_hours=24.0,
    ),
    _TierSpec(
        tier=2,
        drawdown_threshold=Decimal("0.05"),
        action="halt_new_trades",
        auto_reset_hours=48.0,
    ),
    _TierSpec(
        tier=3,
        drawdown_threshold=Decimal("0.10"),
        action="flatten_all",
        auto_reset_hours=72.0,
    ),
    _TierSpec(
        tier=4,
        drawdown_threshold=Decimal("0.15"),
        action="emergency_shutdown",
        auto_reset_hours=None,
    ),
]


# ---------------------------------------------------------------------------
# Restrictions dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CircuitBreakerRestrictions:
    """What actions are currently allowed/restricted."""

    can_open_new_trades: bool = True
    position_size_multiplier: Decimal = Decimal("1.0")
    must_flatten: bool = False
    is_shutdown: bool = False
    daily_halt: bool = False
    consecutive_loss_pause: bool = False
    volatility_reduction: bool = False


# ---------------------------------------------------------------------------
# CircuitBreakerManager
# ---------------------------------------------------------------------------


class CircuitBreakerManager:
    """Manages circuit breaker tiers and additional safety breakers.

    Parameters
    ----------
    event_bus:
        Optional event bus for publishing ``CircuitBreakerEvent`` on tier changes.
    daily_loss_limit:
        Fraction of portfolio for the daily loss breaker (default 3%).
    max_consecutive_losses:
        Number of consecutive losses before the pause breaker fires.
    consecutive_pause_hours:
        How long the consecutive-loss pause lasts.
    volatility_spike_multiplier:
        Current vol / normal vol threshold that triggers size reduction.
    """

    def __init__(
        self,
        event_bus: EventBusLike | None = None,
        daily_loss_limit: Decimal = Decimal("0.03"),
        max_consecutive_losses: int = 5,
        consecutive_pause_hours: float = 4.0,
        volatility_spike_multiplier: Decimal = Decimal("3.0"),
    ) -> None:
        self._event_bus = event_bus

        # Config
        self._daily_loss_limit = daily_loss_limit
        self._max_consecutive_losses = max_consecutive_losses
        self._consecutive_pause_hours = consecutive_pause_hours
        self._volatility_spike_multiplier = volatility_spike_multiplier

        # State
        self._active_tier: int = 0
        self._tier_activated_at: float | None = None
        self._requires_manual_review: bool = False

        # Additional breaker state
        self._daily_halt: bool = False
        self._consecutive_pause: bool = False
        self._consecutive_pause_until: float | None = None
        self._volatility_reduction: bool = False

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        current_drawdown: Decimal,
        daily_loss: Decimal,
        consecutive_losses: int,
        current_vol: Decimal,
        normal_vol: Decimal,
    ) -> None:
        """Update circuit breaker state from current market/portfolio metrics.

        Parameters
        ----------
        current_drawdown:
            Current portfolio drawdown as a positive decimal fraction (0.05 = 5%).
        daily_loss:
            Today's loss as a positive decimal fraction.
        consecutive_losses:
            Number of consecutive losing trades.
        current_vol:
            Current realized volatility.
        normal_vol:
            Normal (baseline) realized volatility.
        """
        now = time.monotonic()

        # --- Check auto-reset of tier breaker ---
        if self._active_tier > 0 and self._tier_activated_at is not None:
            spec = _TIERS[self._active_tier - 1]
            if spec.auto_reset_hours is not None:
                elapsed_hours = (now - self._tier_activated_at) / 3600.0
                if elapsed_hours >= spec.auto_reset_hours:
                    if self._active_tier == 3 and self._requires_manual_review:
                        pass  # Tier 3 stays until manual review + auto-reset time
                    else:
                        old_tier = self._active_tier
                        self._active_tier = 0
                        self._tier_activated_at = None
                        logger.info("Circuit breaker tier %d auto-reset", old_tier)

        # --- Check consecutive loss pause auto-reset ---
        if (
            self._consecutive_pause
            and self._consecutive_pause_until is not None
            and now >= self._consecutive_pause_until
        ):
            self._consecutive_pause = False
            self._consecutive_pause_until = None
            logger.info("Consecutive loss pause auto-reset")

        # --- Evaluate tier breakers (highest tier wins) ---
        new_tier = 0
        for spec in reversed(_TIERS):
            if current_drawdown >= spec.drawdown_threshold:
                new_tier = spec.tier
                break

        if new_tier > self._active_tier:
            old_tier = self._active_tier
            self._active_tier = new_tier
            self._tier_activated_at = now

            if new_tier == 3:
                self._requires_manual_review = True

            tier_spec = _TIERS[new_tier - 1]
            logger.warning(
                "Circuit breaker escalated: tier %d -> %d (%s)",
                old_tier,
                new_tier,
                tier_spec.action,
            )

            if self._event_bus is not None:
                await self._event_bus.publish(
                    CircuitBreakerEvent(
                        tier=new_tier,
                        action=tier_spec.action,
                        drawdown_pct=current_drawdown,
                    )
                )

        # --- Additional breakers ---

        # Daily loss halt
        self._daily_halt = daily_loss >= self._daily_loss_limit

        # Consecutive loss pause
        if consecutive_losses >= self._max_consecutive_losses and not self._consecutive_pause:
            self._consecutive_pause = True
            self._consecutive_pause_until = now + self._consecutive_pause_hours * 3600.0
            logger.warning("Consecutive loss pause activated (%d losses)", consecutive_losses)

        # Volatility spike
        if normal_vol > Decimal("0") and current_vol > Decimal("0"):
            vol_ratio = current_vol / normal_vol
            self._volatility_reduction = vol_ratio > self._volatility_spike_multiplier
        else:
            self._volatility_reduction = False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active_tier(self) -> int:
        """Return the active circuit breaker tier (0 = none, 1-4 = active)."""
        return self._active_tier

    def get_restrictions(self) -> CircuitBreakerRestrictions:
        """Return the current set of restrictions based on all active breakers."""
        r = CircuitBreakerRestrictions()

        # Tier-based restrictions
        if self._active_tier >= 1:
            r.position_size_multiplier = Decimal("0.5")
        if self._active_tier >= 2:
            r.can_open_new_trades = False
        if self._active_tier >= 3:
            r.must_flatten = True
        if self._active_tier >= 4:
            r.is_shutdown = True

        # Additional breakers
        if self._daily_halt:
            r.daily_halt = True
            r.can_open_new_trades = False

        if self._consecutive_pause:
            r.consecutive_loss_pause = True
            r.can_open_new_trades = False

        if self._volatility_reduction:
            r.volatility_reduction = True
            r.position_size_multiplier = min(
                r.position_size_multiplier,
                Decimal("0.5"),
            )

        return r

    # ------------------------------------------------------------------
    # Manual reset
    # ------------------------------------------------------------------

    def reset(self, tier: int) -> None:
        """Manually reset a circuit breaker tier.

        If the currently active tier is equal to the specified tier (or lower),
        the breaker is cleared.
        """
        if tier >= self._active_tier:
            self._active_tier = 0
            self._tier_activated_at = None
            self._requires_manual_review = False
            logger.info("Circuit breaker manually reset (was tier %d)", tier)
