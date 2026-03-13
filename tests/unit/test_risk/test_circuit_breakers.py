"""Tests for CircuitBreakerManager: 4 tiers, auto-reset, additional breakers."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock

from hydra.core.events import CircuitBreakerEvent
from hydra.risk.circuit_breakers import CircuitBreakerManager

# ---------------------------------------------------------------------------
# Tier activation tests
# ---------------------------------------------------------------------------


class TestTierActivation:
    async def test_tier_0_no_breaker(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 0

    async def test_tier_1_at_3pct_drawdown(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.03"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 1

    async def test_tier_2_at_5pct_drawdown(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 2

    async def test_tier_3_at_10pct_drawdown(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.10"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 3

    async def test_tier_4_at_15pct_drawdown(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.15"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 4

    async def test_tier_escalation_publishes_event(self) -> None:
        bus = AsyncMock()
        mgr = CircuitBreakerManager(event_bus=bus)
        await mgr.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        bus.publish.assert_awaited()
        event = bus.publish.call_args[0][0]
        assert isinstance(event, CircuitBreakerEvent)
        assert event.tier == 2
        assert event.action == "halt_new_trades"


# ---------------------------------------------------------------------------
# Restrictions tests
# ---------------------------------------------------------------------------


class TestRestrictions:
    async def test_tier_0_no_restrictions(self) -> None:
        mgr = CircuitBreakerManager()
        r = mgr.get_restrictions()
        assert r.can_open_new_trades is True
        assert r.position_size_multiplier == Decimal("1.0")
        assert r.must_flatten is False
        assert r.is_shutdown is False

    async def test_tier_1_reduces_size(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.03"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.can_open_new_trades is True
        assert r.position_size_multiplier == Decimal("0.5")

    async def test_tier_2_halts_new_trades(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.can_open_new_trades is False

    async def test_tier_3_must_flatten(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.10"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.must_flatten is True

    async def test_tier_4_shutdown(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.15"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.is_shutdown is True


# ---------------------------------------------------------------------------
# Auto-reset tests
# ---------------------------------------------------------------------------


class TestAutoReset:
    async def test_tier_1_auto_resets_after_24h(self) -> None:
        mgr = CircuitBreakerManager()
        # Activate tier 1
        await mgr.update(
            current_drawdown=Decimal("0.03"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 1

        # Simulate time passing by patching the activation timestamp
        # Move activation time 25 hours into the past
        mgr._tier_activated_at = time.monotonic() - (25 * 3600)

        # Update with low drawdown (no longer breaching)
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 0

    async def test_tier_4_no_auto_reset(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.15"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 4

        # Simulate time passing (even 1000 hours)
        mgr._tier_activated_at = time.monotonic() - (1000 * 3600)

        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        # Tier 4 has no auto-reset
        assert mgr.get_active_tier() == 4


# ---------------------------------------------------------------------------
# Manual reset
# ---------------------------------------------------------------------------


class TestManualReset:
    async def test_manual_reset(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.10"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_active_tier() == 3

        mgr.reset(tier=3)
        assert mgr.get_active_tier() == 0


# ---------------------------------------------------------------------------
# Additional breakers
# ---------------------------------------------------------------------------


class TestAdditionalBreakers:
    async def test_daily_loss_halt(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0.04"),  # exceeds 3%
            consecutive_losses=0,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.daily_halt is True
        assert r.can_open_new_trades is False

    async def test_consecutive_loss_pause(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=5,  # reaches threshold
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.consecutive_loss_pause is True
        assert r.can_open_new_trades is False

    async def test_consecutive_loss_pause_auto_resets(self) -> None:
        mgr = CircuitBreakerManager(consecutive_pause_hours=0.001)  # very short
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=5,
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_restrictions().consecutive_loss_pause is True

        # Simulate the pause expiring
        mgr._consecutive_pause_until = time.monotonic() - 1

        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=2,  # losses recovered
            current_vol=Decimal("0.2"),
            normal_vol=Decimal("0.2"),
        )
        assert mgr.get_restrictions().consecutive_loss_pause is False

    async def test_volatility_spike_reduces_size(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.9"),  # 4.5x normal
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.volatility_reduction is True
        assert r.position_size_multiplier == Decimal("0.5")

    async def test_no_volatility_spike(self) -> None:
        mgr = CircuitBreakerManager()
        await mgr.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.3"),  # 1.5x normal (below 3x)
            normal_vol=Decimal("0.2"),
        )
        r = mgr.get_restrictions()
        assert r.volatility_reduction is False
