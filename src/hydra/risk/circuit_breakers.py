"""Account circuit breakers: four drawdown tiers, the daily-loss halt and the loss-streak pause.

Tier 4 sits at ``max_drawdown_pct``; tiers 1-3 at 1/5, 1/3 and 2/3 of it (3/5/10/15% at 15%):
    1 -> size x0.5 (auto-reset after 24h once drawdown is back below 1's threshold)
    2 -> no new entries (auto-reset after 48h, same condition)
    3 -> flatten every session (manual reset only)
    4 -> emergency shutdown, kill switch (manual reset only, with confirmation)

The daily halt is latched until the next 00:00 UTC. The consecutive-loss pause ends after
``consecutive_pause_hours`` and then asks the caller to reset its loss counter. All times are
wall-clock UTC and the whole state is serialisable, so it survives a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

TIER_FRACTIONS: tuple[Decimal, ...] = (
    Decimal(1) / Decimal(5),
    Decimal(1) / Decimal(3),
    Decimal(2) / Decimal(3),
    Decimal(1),
)
TIER_ACTIONS: tuple[str, ...] = (
    "reduce_size_50",
    "halt_new_trades",
    "flatten_all",
    "emergency_shutdown",
)
TIER_LABELS: tuple[str, ...] = (
    "Reduce size 50%",
    "Halt new entries",
    "Flatten all sessions",
    "Emergency shutdown",
)
AUTO_RESET_HOURS: tuple[float | None, ...] = (24.0, 48.0, None, None)
_THRESHOLD_QUANTUM = Decimal("0.00000001")


class TierNotActiveError(ValueError):
    """A reset named a tier other than the active one."""


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    max_drawdown_pct: Decimal = Decimal("0.15")
    daily_loss_limit: Decimal = Decimal("0.03")
    max_consecutive_losses: int = 5
    consecutive_pause_hours: float = 4.0

    def __post_init__(self) -> None:
        if self.max_drawdown_pct <= 0:
            raise ValueError("max_drawdown_pct must be > 0")

    def thresholds(self) -> tuple[Decimal, ...]:
        return tuple(
            (self.max_drawdown_pct * f).quantize(_THRESHOLD_QUANTUM) for f in TIER_FRACTIONS
        )


@dataclass(slots=True)
class CircuitBreakerState:
    """Persisted breaker state (risk_state cb_* columns)."""

    tier: int = 0
    tier_since: datetime | None = None
    requires_manual_review: bool = False
    daily_halt_until: datetime | None = None
    consecutive_pause_until: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "cb_tier": self.tier,
            "cb_tier_since": self.tier_since,
            "cb_requires_review": self.requires_manual_review,
            "daily_halt_until": self.daily_halt_until,
            "consecutive_pause_until": self.consecutive_pause_until,
        }

    @classmethod
    def from_row(cls, row: Any) -> CircuitBreakerState:
        tier = int(row["cb_tier"] or 0)
        if tier not in range(5):
            raise ValueError(f"persisted circuit-breaker tier {tier} out of range")
        return cls(
            tier=tier,
            tier_since=row["cb_tier_since"],
            requires_manual_review=bool(row["cb_requires_review"]),
            daily_halt_until=row["daily_halt_until"],
            consecutive_pause_until=row["consecutive_pause_until"],
        )


@dataclass(frozen=True, slots=True)
class Transition:
    """One state change for the audit log: kind is cb_escalate, cb_auto_reset, daily_halt,
    daily_halt_lifted, consecutive_pause or consecutive_reset."""

    kind: str
    tier: int
    previous_tier: int
    detail: str


@dataclass(frozen=True, slots=True)
class CircuitBreakerRestrictions:
    """What the breakers allow right now."""

    tier: int = 0
    can_open_new_trades: bool = True
    position_size_multiplier: Decimal = Decimal("1")
    must_flatten: bool = False
    is_shutdown: bool = False
    daily_halt: bool = False
    consecutive_loss_pause: bool = False
    daily_halt_until: datetime | None = None
    consecutive_pause_until: datetime | None = None

    def blocking_reason(self) -> tuple[str, str] | None:
        """(check, reason) for the first restriction that blocks an opening order."""
        if self.is_shutdown:
            return "circuit_breaker", "circuit breaker tier 4 active: emergency shutdown"
        if self.tier >= 2:
            return "circuit_breaker", f"circuit breaker tier {self.tier} active: new trades halted"
        if self.daily_halt:
            return "daily_loss_halt", f"daily loss halt until {self.daily_halt_until}"
        if self.consecutive_loss_pause:
            return (
                "consecutive_loss_pause",
                f"consecutive_loss_pause until {self.consecutive_pause_until}",
            )
        return None


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TierSpec:
    tier: int
    drawdown_threshold: Decimal
    action: str
    auto_reset_hours: float | None


def tier_specs(config: CircuitBreakerConfig) -> tuple[TierSpec, ...]:
    return tuple(
        TierSpec(i + 1, threshold, TIER_ACTIONS[i], AUTO_RESET_HOURS[i])
        for i, threshold in enumerate(config.thresholds())
    )


_TIERS = tier_specs(CircuitBreakerConfig())


class CircuitBreakerManager:
    """Pure state machine; the RiskService feeds it account metrics and persists its state."""

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        state: CircuitBreakerState | None = None,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state = state or CircuitBreakerState()

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    def reconfigure(self, config: CircuitBreakerConfig) -> None:
        self._config = config

    def thresholds(self) -> tuple[Decimal, ...]:
        return self._config.thresholds()

    def get_active_tier(self) -> int:
        return self._state.tier

    def tier_for(self, drawdown: Decimal) -> int:
        tier = 0
        for i, threshold in enumerate(self.thresholds(), start=1):
            if drawdown >= threshold:
                tier = i
        return tier

    def expire(self, now: datetime) -> list[Transition]:
        """Lift the daily halt after UTC rollover and end an elapsed consecutive-loss pause."""
        st = self._state
        out: list[Transition] = []
        if st.daily_halt_until is not None and now >= st.daily_halt_until:
            out.append(Transition("daily_halt_lifted", st.tier, st.tier, "UTC day rolled over"))
            st.daily_halt_until = None
        if st.consecutive_pause_until is not None and now >= st.consecutive_pause_until:
            out.append(
                Transition("consecutive_reset", st.tier, st.tier, "pause elapsed; counter reset")
            )
            st.consecutive_pause_until = None
        return out

    def update(
        self,
        *,
        drawdown: Decimal,
        daily_loss: Decimal,
        consecutive_losses: int,
        now: datetime,
    ) -> list[Transition]:
        """Apply account metrics (fractions) at wall-clock *now*; return the transitions."""
        st = self._state
        out = self.expire(now)
        expired_pause = any(t.kind == "consecutive_reset" for t in out)
        thresholds = self.thresholds()

        hours = AUTO_RESET_HOURS[st.tier - 1] if st.tier else None
        if (
            hours is not None
            and st.tier_since is not None
            and now - st.tier_since >= timedelta(hours=hours)
            and drawdown < thresholds[st.tier - 1]
        ):
            new_tier = self.tier_for(drawdown)
            out.append(
                Transition(
                    "cb_auto_reset",
                    new_tier,
                    st.tier,
                    f"drawdown {drawdown:.4f} below tier {st.tier} after {hours:g}h",
                )
            )
            st.tier = new_tier
            st.tier_since = now if new_tier else None

        target = self.tier_for(drawdown)
        if target > st.tier:
            out.append(
                Transition(
                    "cb_escalate",
                    target,
                    st.tier,
                    f"drawdown {drawdown:.4f} >= {thresholds[target - 1]} "
                    f"({TIER_ACTIONS[target - 1]})",
                )
            )
            st.tier = target
            st.tier_since = now
            if target >= 3:
                st.requires_manual_review = True

        if st.daily_halt_until is None and daily_loss >= self._config.daily_loss_limit:
            st.daily_halt_until = next_utc_midnight(now)
            out.append(
                Transition(
                    "daily_halt",
                    st.tier,
                    st.tier,
                    f"daily loss {daily_loss:.4f} >= {self._config.daily_loss_limit}",
                )
            )

        if (
            not expired_pause
            and st.consecutive_pause_until is None
            and consecutive_losses >= self._config.max_consecutive_losses
        ):
            st.consecutive_pause_until = now + timedelta(hours=self._config.consecutive_pause_hours)
            out.append(
                Transition(
                    "consecutive_pause",
                    st.tier,
                    st.tier,
                    f"{consecutive_losses} consecutive losing closes",
                )
            )

        for t in out:
            logger.warning("Circuit breaker %s: %s", t.kind, t.detail)
        return out

    def restrictions(self, now: datetime) -> CircuitBreakerRestrictions:
        st = self._state
        halted = st.daily_halt_until is not None and now < st.daily_halt_until
        paused = st.consecutive_pause_until is not None and now < st.consecutive_pause_until
        return CircuitBreakerRestrictions(
            tier=st.tier,
            can_open_new_trades=st.tier < 2 and not halted and not paused,
            position_size_multiplier=Decimal("0.5") if st.tier >= 1 else Decimal("1"),
            must_flatten=st.tier >= 3,
            is_shutdown=st.tier >= 4,
            daily_halt=halted,
            consecutive_loss_pause=paused,
            daily_halt_until=st.daily_halt_until if halted else None,
            consecutive_pause_until=st.consecutive_pause_until if paused else None,
        )

    def reset_check(self, tier: int) -> None:
        """Raise ValueError / TierNotActiveError unless *tier* is exactly the active tier."""
        if tier not in range(1, 5):
            raise ValueError(f"tier must be 1-4, got {tier}")
        if tier != self._state.tier:
            raise TierNotActiveError(f"tier {tier} is not active (active tier {self._state.tier})")

    def reset(self, tier: int) -> None:
        """Clear the active tier; *tier* must name it exactly."""
        self.reset_check(tier)
        self._state.tier = 0
        self._state.tier_since = None
        self._state.requires_manual_review = False

    def restore(self, state: CircuitBreakerState) -> None:
        """Put back a state taken with snapshot_state (a reset that could not be persisted)."""
        self._state = replace(state)

    def snapshot_state(self) -> CircuitBreakerState:
        return replace(self._state)


__all__ = [
    "AUTO_RESET_HOURS",
    "TIER_ACTIONS",
    "TIER_LABELS",
    "CircuitBreakerConfig",
    "CircuitBreakerManager",
    "CircuitBreakerRestrictions",
    "CircuitBreakerState",
    "TierNotActiveError",
    "TierSpec",
    "Transition",
    "next_utc_midnight",
    "tier_specs",
]
