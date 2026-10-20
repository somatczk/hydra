"""CircuitBreakerManager: tiers from max_drawdown_pct, wall-clock latching, strict reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.risk.circuit_breakers import (
    CircuitBreakerConfig,
    CircuitBreakerManager,
    CircuitBreakerState,
    TierNotActiveError,
    next_utc_midnight,
)

T0 = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def _update(
    mgr: CircuitBreakerManager,
    dd: str = "0",
    *,
    daily: str = "0",
    losses: int = 0,
    now: datetime = T0,
) -> list[str]:
    return [
        t.kind
        for t in mgr.update(
            drawdown=Decimal(dd), daily_loss=Decimal(daily), consecutive_losses=losses, now=now
        )
    ]


class TestThresholds:
    def test_tiers_derive_from_max_drawdown(self) -> None:
        cfg = CircuitBreakerConfig(max_drawdown_pct=Decimal("0.15"))
        assert cfg.thresholds() == (
            Decimal("0.03"),
            Decimal("0.05"),
            Decimal("0.10"),
            Decimal("0.15"),
        )
        assert CircuitBreakerConfig(max_drawdown_pct=Decimal("0.30")).thresholds() == (
            Decimal("0.06"),
            Decimal("0.10"),
            Decimal("0.20"),
            Decimal("0.30"),
        )

    def test_zero_max_drawdown_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_drawdown_pct"):
            CircuitBreakerConfig(max_drawdown_pct=Decimal("0"))

    @pytest.mark.parametrize(
        ("dd", "tier"),
        [("0.0299", 0), ("0.03", 1), ("0.0499", 1), ("0.05", 2), ("0.10", 3), ("0.15", 4)],
    )
    def test_tier_for(self, dd: str, tier: int) -> None:
        assert CircuitBreakerManager().tier_for(Decimal(dd)) == tier


class TestEscalation:
    def test_escalates_one_transition_per_update(self) -> None:
        mgr = CircuitBreakerManager()
        assert _update(mgr, "0.052") == ["cb_escalate"]
        assert mgr.get_active_tier() == 2
        assert _update(mgr, "0.105") == ["cb_escalate"]
        assert mgr.state.requires_manual_review is True
        assert _update(mgr, "0.151") == ["cb_escalate"]
        assert mgr.get_active_tier() == 4

    def test_recovery_does_not_lower_tier_before_auto_reset(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.04")
        assert _update(mgr, "0.0", now=T0 + timedelta(hours=23)) == []
        assert mgr.get_active_tier() == 1

    @pytest.mark.parametrize(
        ("tier_dd", "expected"),
        [
            ("0.0", (True, Decimal("1"), False, False)),
            ("0.03", (True, Decimal("0.5"), False, False)),
            ("0.05", (False, Decimal("0.5"), False, False)),
            ("0.10", (False, Decimal("0.5"), True, False)),
            ("0.15", (False, Decimal("0.5"), True, True)),
        ],
    )
    def test_restrictions_per_tier(
        self, tier_dd: str, expected: tuple[bool, Decimal, bool, bool]
    ) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, tier_dd)
        r = mgr.restrictions(T0)
        assert (
            r.can_open_new_trades,
            r.position_size_multiplier,
            r.must_flatten,
            r.is_shutdown,
        ) == expected


class TestAutoReset:
    def test_tier1_resets_only_after_24h_and_below_threshold(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.035")
        assert _update(mgr, "0.031", now=T0 + timedelta(hours=25)) == []
        assert mgr.get_active_tier() == 1
        assert _update(mgr, "0.029", now=T0 + timedelta(hours=23, minutes=59)) == []
        assert mgr.get_active_tier() == 1
        assert _update(mgr, "0.029", now=T0 + timedelta(hours=24)) == ["cb_auto_reset"]
        assert mgr.get_active_tier() == 0
        assert mgr.state.tier_since is None

    def test_tier2_auto_reset_lands_on_current_tier(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.06")
        assert _update(mgr, "0.04", now=T0 + timedelta(hours=48)) == ["cb_auto_reset"]
        assert mgr.get_active_tier() == 1

    @pytest.mark.parametrize("dd", ["0.10", "0.15"])
    def test_tier3_and_4_never_auto_reset(self, dd: str) -> None:
        mgr = CircuitBreakerManager()
        tier = mgr.tier_for(Decimal(dd))
        _update(mgr, dd)
        assert _update(mgr, "0", now=T0 + timedelta(days=30)) == []
        assert mgr.get_active_tier() == tier


class TestDailyHalt:
    def test_latched_until_utc_midnight(self) -> None:
        mgr = CircuitBreakerManager()
        assert _update(mgr, daily="0.036") == ["daily_halt"]
        assert mgr.state.daily_halt_until == datetime(2026, 10, 21, tzinfo=UTC)
        assert _update(mgr, daily="0.001", now=T0 + timedelta(hours=10)) == []
        assert mgr.restrictions(T0 + timedelta(hours=10)).daily_halt is True
        rollover = datetime(2026, 10, 21, 0, 0, 1, tzinfo=UTC)
        assert mgr.restrictions(rollover).can_open_new_trades is True
        assert _update(mgr, daily="0", now=rollover) == ["daily_halt_lifted"]
        assert mgr.state.daily_halt_until is None

    def test_next_midnight_is_utc_for_aware_non_utc_input(self) -> None:
        from zoneinfo import ZoneInfo

        la = datetime(2026, 10, 20, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert next_utc_midnight(la) == datetime(2026, 10, 22, tzinfo=UTC)


class TestConsecutivePause:
    def test_pause_then_counter_reset_on_expiry(self) -> None:
        mgr = CircuitBreakerManager()
        assert _update(mgr, losses=5) == ["consecutive_pause"]
        assert mgr.restrictions(T0 + timedelta(hours=3)).consecutive_loss_pause is True
        later = T0 + timedelta(hours=4)
        assert mgr.restrictions(later).consecutive_loss_pause is False
        assert _update(mgr, losses=5, now=later) == ["consecutive_reset"]
        assert mgr.state.consecutive_pause_until is None


class TestReset:
    def test_reset_rejects_out_of_range_tier(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.16")
        with pytest.raises(ValueError, match="1-4"):
            mgr.reset(9)
        assert mgr.get_active_tier() == 4

    def test_reset_rejects_non_active_tier(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.11")
        with pytest.raises(TierNotActiveError):
            mgr.reset(2)
        assert mgr.get_active_tier() == 3

    def test_reset_active_tier(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.11")
        mgr.reset(3)
        assert mgr.get_active_tier() == 0
        assert mgr.state.requires_manual_review is False


class TestPersistence:
    def test_row_round_trip_preserves_deadlines(self) -> None:
        mgr = CircuitBreakerManager()
        _update(mgr, "0.11", daily="0.04", losses=5)
        row = mgr.state.to_row()
        restored = CircuitBreakerState.from_row(row)
        assert restored == mgr.state
        assert restored.tier_since == T0
        assert restored.daily_halt_until == datetime(2026, 10, 21, tzinfo=UTC)
        assert restored.consecutive_pause_until == T0 + timedelta(hours=4)

    def test_new_manager_from_persisted_state_keeps_tier(self) -> None:
        first = CircuitBreakerManager()
        _update(first, "0.11")
        second = CircuitBreakerManager(state=CircuitBreakerState.from_row(first.state.to_row()))
        assert second.get_active_tier() == 3
        assert second.restrictions(T0).must_flatten is True

    def test_out_of_range_persisted_tier_raises(self) -> None:
        row = CircuitBreakerState().to_row() | {"cb_tier": 7}
        with pytest.raises(ValueError, match="out of range"):
            CircuitBreakerState.from_row(row)
