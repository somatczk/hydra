"""E2E: Circuit breaker lifecycle -- drawdown triggers, tier escalation, reset.

Tests the full flow from losing trades through circuit breaker activation
to manual reset and resumed trading.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import CircuitBreakerEvent
from hydra.risk.circuit_breakers import CircuitBreakerManager

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCircuitBreakerFlow:
    """Drawdown -> breaker activation -> flatten -> alert -> reset."""

    async def test_drawdown_triggers_tier1(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """3% drawdown triggers tier 1 (reduce size 50%)."""
        cb_events: list[CircuitBreakerEvent] = []

        async def _on_cb(event):
            if isinstance(event, CircuitBreakerEvent):
                cb_events.append(event)

        await event_bus.subscribe("circuit_breaker", _on_cb)

        cbm = CircuitBreakerManager(event_bus=event_bus)

        # Initial state -- no active tier
        assert cbm.get_active_tier() == 0

        restrictions = cbm.get_restrictions()
        assert restrictions.can_open_new_trades is True
        assert restrictions.position_size_multiplier == Decimal("1.0")

        # Simulate 3% drawdown
        await cbm.update(
            current_drawdown=Decimal("0.03"),
            daily_loss=Decimal("0.01"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )

        assert cbm.get_active_tier() == 1

        restrictions = cbm.get_restrictions()
        assert restrictions.position_size_multiplier == Decimal("0.5")
        # Tier 1 does NOT halt new trades
        assert restrictions.can_open_new_trades is True

        # Should have published a circuit breaker event
        assert len(cb_events) == 1
        assert cb_events[0].tier == 1
        assert cb_events[0].action == "reduce_size_50"

    async def test_drawdown_triggers_tier2(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """5% drawdown triggers tier 2 (halt new trades)."""
        cbm = CircuitBreakerManager(event_bus=event_bus)

        await cbm.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0.01"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )

        assert cbm.get_active_tier() == 2
        restrictions = cbm.get_restrictions()
        assert restrictions.can_open_new_trades is False
        assert restrictions.position_size_multiplier == Decimal("0.5")

    async def test_tier3_flattens_all_positions(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """10% drawdown triggers tier 3 -> must flatten all positions."""
        cb_events: list[CircuitBreakerEvent] = []

        async def _on_cb(event):
            if isinstance(event, CircuitBreakerEvent):
                cb_events.append(event)

        await event_bus.subscribe("circuit_breaker", _on_cb)

        cbm = CircuitBreakerManager(event_bus=event_bus)

        await cbm.update(
            current_drawdown=Decimal("0.10"),
            daily_loss=Decimal("0.05"),
            consecutive_losses=3,
            current_vol=Decimal("0.04"),
            normal_vol=Decimal("0.02"),
        )

        assert cbm.get_active_tier() == 3

        restrictions = cbm.get_restrictions()
        assert restrictions.must_flatten is True
        assert restrictions.can_open_new_trades is False

        # Event should have been published
        tier3_events = [e for e in cb_events if e.tier == 3]
        assert len(tier3_events) == 1
        assert tier3_events[0].action == "flatten_all"

    async def test_tier_escalation_from_1_to_3(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """Drawdown increasing from 3% to 10% escalates through tiers."""
        cb_events: list[CircuitBreakerEvent] = []

        async def _on_cb(event):
            if isinstance(event, CircuitBreakerEvent):
                cb_events.append(event)

        await event_bus.subscribe("circuit_breaker", _on_cb)

        cbm = CircuitBreakerManager(event_bus=event_bus)

        # Tier 1 at 3%
        await cbm.update(
            current_drawdown=Decimal("0.03"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )
        assert cbm.get_active_tier() == 1

        # Tier 2 at 5%
        await cbm.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )
        assert cbm.get_active_tier() == 2

        # Tier 3 at 10%
        await cbm.update(
            current_drawdown=Decimal("0.10"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )
        assert cbm.get_active_tier() == 3

        # Should have 3 escalation events
        assert len(cb_events) == 3
        assert [e.tier for e in cb_events] == [1, 2, 3]

    async def test_circuit_breaker_reset(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """Manual reset restores trading after tier activation."""
        cbm = CircuitBreakerManager(event_bus=event_bus)

        # Activate tier 2
        await cbm.update(
            current_drawdown=Decimal("0.05"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )
        assert cbm.get_active_tier() == 2

        restrictions = cbm.get_restrictions()
        assert restrictions.can_open_new_trades is False

        # Manual reset
        cbm.reset(tier=2)

        assert cbm.get_active_tier() == 0
        restrictions = cbm.get_restrictions()
        assert restrictions.can_open_new_trades is True
        assert restrictions.position_size_multiplier == Decimal("1.0")

    async def test_consecutive_loss_pause(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """5 consecutive losses triggers a pause on new trades."""
        cbm = CircuitBreakerManager(event_bus=event_bus, max_consecutive_losses=5)

        await cbm.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0.01"),
            consecutive_losses=5,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )

        restrictions = cbm.get_restrictions()
        assert restrictions.consecutive_loss_pause is True
        assert restrictions.can_open_new_trades is False

    async def test_daily_loss_halt(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """Daily loss exceeding 3% halts trading for the rest of the day."""
        cbm = CircuitBreakerManager(event_bus=event_bus, daily_loss_limit=Decimal("0.03"))

        await cbm.update(
            current_drawdown=Decimal("0.01"),
            daily_loss=Decimal("0.03"),
            consecutive_losses=0,
            current_vol=Decimal("0.02"),
            normal_vol=Decimal("0.02"),
        )

        restrictions = cbm.get_restrictions()
        assert restrictions.daily_halt is True
        assert restrictions.can_open_new_trades is False

    async def test_volatility_spike_reduces_size(
        self,
        event_bus: InMemoryEventBus,
    ) -> None:
        """Volatility spike (>3x normal) reduces position sizes by 50%."""
        cbm = CircuitBreakerManager(
            event_bus=event_bus,
            volatility_spike_multiplier=Decimal("3.0"),
        )

        await cbm.update(
            current_drawdown=Decimal("0"),
            daily_loss=Decimal("0"),
            consecutive_losses=0,
            current_vol=Decimal("0.09"),  # 3x the normal vol
            normal_vol=Decimal("0.02"),
        )

        restrictions = cbm.get_restrictions()
        assert restrictions.volatility_reduction is True
        assert restrictions.position_size_multiplier == Decimal("0.5")
