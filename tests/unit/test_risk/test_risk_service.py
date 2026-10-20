"""RiskService on real paper executors: ledger, breakers, kill switch, aggregate limits."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.types import Side
from hydra.execution.order_manager import RiskRejectedError
from hydra.risk.circuit_breakers import CircuitBreakerState, TierNotActiveError
from hydra.risk.pretrade import OPEN
from hydra.risk.service import RiskService
from hydra.risk.state import RiskState, RiskStateUnavailableError
from tests.unit.test_risk.harness import (
    BTC,
    T0,
    Account,
    Clock,
    make_account,
    order,
    permissive,
    rules_cache,
)

BUY, SELL = Side.BUY, Side.SELL


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def _buy_direct(acct: Account, sid: str, qty: str) -> None:
    await acct.executors[sid].create_order(BTC, "BUY", "MARKET", Decimal(qty))


async def _rejected(acct: Account, sid: str, side: Side, qty: str) -> RiskRejectedError:
    with pytest.raises(RiskRejectedError) as info:
        await acct.submit(sid, side, qty)
    return info.value


def _kinds(acct: Account) -> list[str]:
    return [e.kind for e in acct.store.events]


class TestConsecutiveLosses:
    async def test_consecutive_losses_pause_and_reset(self) -> None:
        acct = await make_account(permissive(max_consecutive_losses=5))
        await acct.add_session("s1", "10000")
        for i in range(5):
            await acct.mark("60000")
            assert await acct.submit("s1", BUY, "0.01")
            assert acct.store.state.consecutive_losses == i
            await acct.mark("59000")
            assert await acct.submit("s1", SELL, "0.01", reduce_only=True)
            assert acct.store.state.consecutive_losses == i + 1
        assert [f.realized_pnl for f in acct.buses["s1"].fills if f.reduce_only] == [
            Decimal("-10.0")
        ] * 5

        await acct.mark("60000")
        rejected = await _rejected(acct, "s1", BUY, "0.01")
        assert rejected.check == "consecutive_loss_pause"

        acct.clock.advance(hours=4)
        assert await acct.submit("s1", BUY, "0.01")
        assert acct.store.state.consecutive_losses == 0
        assert _kinds(acct).count("consecutive_pause") == 1
        assert "consecutive_reset" in _kinds(acct)


@pytest.fixture()
def los_angeles_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestDailyHalt:
    @pytest.mark.usefixtures("los_angeles_tz")
    async def test_daily_halt_latched_until_utc_rollover(self) -> None:
        clock = Clock(datetime(2026, 10, 21, 2, 0, tzinfo=UTC))
        acct = await make_account(permissive(), clock=clock)
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.1")
        assert acct.store.state.day_start_equity == Decimal("10000")

        await acct.mark("56400")
        assert acct.svc.account().equity == Decimal("9640.0")
        decision = await acct.svc.check(order(BUY, "0.001"), "s1", OPEN)
        assert (decision.approved, decision.check) == (False, "daily_loss_halt")

        await acct.mark("59900")
        assert acct.svc.account().equity == Decimal("9990.0")
        decision = await acct.svc.check(order(BUY, "0.001"), "s1", OPEN)
        assert (decision.approved, decision.check) == (False, "daily_loss_halt")

        clock.now = datetime(2026, 10, 22, 0, 0, 1, tzinfo=UTC)
        assert (await acct.svc.check(order(BUY, "0.001"), "s1", OPEN)).approved is True
        await acct.mark("59950")
        assert acct.store.state.day == date(2026, 10, 22)
        assert acct.store.state.day_start_equity == Decimal("9995.0")
        assert "daily_halt_lifted" in _kinds(acct)


async def _tiered_account(**kw: Any) -> tuple[Account, list[tuple[str, list[str]]]]:
    calls: list[tuple[str, list[str]]] = []
    holder: dict[str, Account] = {}

    async def flatten(reason: str) -> list[str]:
        calls.append((reason, holder["acct"].svc.registered_sessions()))
        return []

    acct = await make_account(
        permissive(max_drawdown_pct=Decimal("0.15"), max_daily_loss_pct=Decimal("0.5")),
        on_must_flatten=flatten,
        **kw,
    )
    holder["acct"] = acct
    await acct.add_session("s1", "10000")
    await acct.add_session("s2", "1000")
    await acct.mark("60000")
    await _buy_direct(acct, "s1", "0.1")
    return acct, calls


class TestTiers:
    async def test_tiers_from_max_drawdown(self) -> None:
        acct, flattens = await _tiered_account()

        await acct.mark("54800")
        assert acct.svc.account().equity == Decimal("9480.0")
        assert acct.svc.account().tier == 2
        assert (await _rejected(acct, "s2", BUY, "0.001")).check == "circuit_breaker"
        assert acct.svc.restrictions().position_size_multiplier == Decimal("0.5")
        assert flattens == []

        await acct.mark("49500")
        assert acct.svc.account().tier == 3
        assert flattens == [("circuit breaker tier 3", ["s1", "s2"])]

        await acct.mark("44900")
        assert acct.svc.account().equity == Decimal("8490.0")
        assert acct.svc.account().tier == 4
        assert acct.svc.kill_switch_active() is True
        assert acct.store.kill_switch is True
        assert _kinds(acct).count("cb_escalate") == 3
        assert _kinds(acct)[-1] == "kill_switch_on"
        assert acct.store.state.cb.tier == 4
        exported = REGISTRY.get_sample_value(
            "hydra_circuit_breaker_active_tier", {"scope": "paper"}
        )
        assert exported == 4.0
        assert REGISTRY.get_sample_value("hydra_kill_switch_active") == 1.0

    async def test_reset_semantics(self) -> None:
        acct, _ = await _tiered_account()
        await acct.mark("49500")
        svc = acct.svc
        assert svc.account().tier == 3

        with pytest.raises(ValueError, match="1-4"):
            await svc.reset_breaker(9, actor="ops", reason="typo", confirm_tier=9)
        with pytest.raises(TierNotActiveError):
            await svc.reset_breaker(2, actor="ops", reason="wrong", confirm_tier=2)
        with pytest.raises(ValueError, match="confirm_tier"):
            await svc.reset_breaker(3, actor="ops", reason="mismatch", confirm_tier=4)

        events_before = len(acct.store.events)
        real_append = acct.store.append_event
        acct.store.append_event = AsyncMock(side_effect=ConnectionError("audit down"))
        with pytest.raises(ConnectionError):
            await svc.reset_breaker(3, actor="ops", reason="reviewed", confirm_tier=3)
        assert svc.account().tier == 3
        assert acct.store.state.cb.tier == 3
        acct.store.append_event = real_append
        assert len(acct.store.events) == events_before

        await svc.reset_breaker(3, actor="alice", reason="reviewed", confirm_tier=3)
        last = acct.store.events[-1]
        assert (last.kind, last.actor, last.tier) == ("cb_reset", "alice", 3)
        assert last.details["old_peak"] == Decimal("10000")
        assert last.details["new_peak"] == Decimal("8950.0")
        assert svc.account().tier == 0
        assert acct.store.state.cb.tier == 0
        assert acct.store.state.peak_equity == Decimal("8950.0")

        await acct.mark("49500")
        assert svc.account().tier == 0
        assert svc.account().drawdown == Decimal("0")


class TestAggregateExposure:
    async def test_gross_exposure_across_sessions(self) -> None:
        acct = await make_account(permissive(max_portfolio_heat=Decimal("0.20")))
        for sid in ("s1", "s2", "s3", "s4"):
            await acct.add_session(sid, "2500")
        await acct.mark("60000")
        for sid in ("s1", "s2", "s3"):
            assert await acct.submit(sid, BUY, "0.01")
        rejected = await _rejected(acct, "s4", BUY, "0.01")
        assert rejected.check == "portfolio_heat"
        assert "0.2400" in rejected.reason
        assert (
            REGISTRY.get_sample_value(
                "hydra_risk_rejections_total", {"check": "portfolio_heat", "intent": "open"}
            )
            == 1.0
        )
        assert REGISTRY.get_sample_value("hydra_portfolio_heat_ratio") == pytest.approx(0.18)

    async def test_concurrent_submissions_take_the_lock(self) -> None:
        acct = await make_account(permissive(max_portfolio_heat=Decimal("0.18")))
        for sid in ("s1", "s2", "s3", "s4"):
            await acct.add_session(sid, "2500")
        await acct.mark("60000")
        await acct.submit("s1", BUY, "0.01")
        await acct.submit("s2", BUY, "0.01")

        results = await asyncio.gather(
            acct.submit("s3", BUY, "0.01"),
            acct.submit("s4", BUY, "0.01"),
            return_exceptions=True,
        )
        assert sum(isinstance(r, str) for r in results) == 1
        errors = [r for r in results if isinstance(r, RiskRejectedError)]
        assert [e.check for e in errors] == ["portfolio_heat"]
        held = [s for s, ex in acct.executors.items() if await ex.get_positions(BTC)]
        assert len(held) == 3

    async def test_lock_timeout_refuses_openings_but_reductions_wait(self) -> None:
        acct = await make_account(permissive(), lock_timeout=0.01)
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.01")

        await acct.svc.lock.acquire()
        rejected = await _rejected(acct, "s1", BUY, "0.001")
        assert rejected.check == "lock_timeout"
        exit_task = asyncio.create_task(acct.submit("s1", SELL, "0.01", reduce_only=True))
        await asyncio.sleep(0.05)
        assert not exit_task.done()
        acct.svc.lock.release()
        assert await exit_task
        assert await acct.executors["s1"].get_positions(BTC) == []
        assert REGISTRY.get_sample_value("hydra_risk_lock_timeouts_total") == 1.0


class TestFailClosed:
    async def test_load_failure_refuses_openings_allows_exits(self) -> None:
        acct = await make_account(permissive())
        acct.store.load_state = AsyncMock(side_effect=ConnectionError("db down"))
        svc = RiskService(acct.store, clock=acct.clock, market_rules=rules_cache(acct.clock))
        with pytest.raises(RiskStateUnavailableError, match="db down"):
            await svc.load()
        acct.svc = svc
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.01")

        rejected = await _rejected(acct, "s1", BUY, "0.001")
        assert (rejected.check, rejected.reason) == ("risk_state", "risk state unavailable")
        assert await acct.submit("s1", SELL, "0.01", reduce_only=True)
        assert await acct.executors["s1"].get_positions(BTC) == []

    async def test_save_failure_refuses_openings_until_next_save(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        real_save = acct.store.save_state
        acct.store.save_state = AsyncMock(side_effect=ConnectionError("db down"))
        await acct.mark("60000")
        assert acct.svc.available is False
        assert (await _rejected(acct, "s1", BUY, "0.001")).check == "risk_state"
        assert REGISTRY.get_sample_value("hydra_risk_state_persist_failures_total") == 1.0

        acct.store.save_state = real_save
        await acct.mark("60010")
        assert acct.svc.available is True
        assert await acct.submit("s1", BUY, "0.001")


class TestLedger:
    async def test_carried_pnl_survives_session_stop_and_restart(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.1")
        await acct.mark("57000")
        assert acct.svc.account().equity == Decimal("9700.0")

        assert await acct.svc.unregister_session("s1") == Decimal("-300.0")
        assert acct.svc.account().equity == Decimal("9700.0")
        await acct.add_session("s2", "9700")
        await acct.mark("57000")
        view = acct.svc.account()
        assert (view.equity, view.peak_equity, view.day_start_equity) == (
            Decimal("9700.0"),
            Decimal("10000"),
            Decimal("10000"),
        )
        assert view.drawdown == Decimal("0.03")
        assert view.tier == 1

        restarted = RiskService(acct.store, clock=acct.clock, market_rules=rules_cache(acct.clock))
        await restarted.load()
        await restarted.evaluate()
        assert restarted.account().carried_pnl == Decimal("-300.0")
        assert restarted.account().equity == Decimal("9700.0")
        assert restarted.account().peak_equity == Decimal("10000")
        assert restarted.account().tier == 1

    async def test_persisted_tier_survives_new_instance(self) -> None:
        state = RiskState(
            peak_equity=Decimal("10000"),
            cb=CircuitBreakerState(tier=3, tier_since=T0, requires_manual_review=True),
        )
        acct = await make_account(permissive(), state=state)
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        assert acct.svc.account().tier == 3
        assert (await _rejected(acct, "s1", BUY, "0.001")).check == "circuit_breaker"

    async def test_reload_config_rebases_on_paper_capital_change(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.1")
        await acct.mark("57000")

        acct.store.paper_capital = Decimal("20000")
        acct.store.config = permissive(max_position_pct=Decimal("0.01"))
        await acct.svc.reload_config(actor="bob")
        reset = [e for e in acct.store.events if e.kind == "paper_reset"]
        assert [(e.actor, e.details["new_capital"]) for e in reset] == [("bob", Decimal("20000"))]
        view = acct.svc.account()
        assert view.equity == view.peak_equity == view.day_start_equity == Decimal("19700.0")
        assert view.carried_pnl == Decimal("0")
        assert (await _rejected(acct, "s1", BUY, "0.01")).check == "position_size"


class TestKillSwitch:
    async def test_latched_when_persist_fails_and_release_is_audited(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "10000")
        await acct.mark("60000")
        await _buy_direct(acct, "s1", "0.01")
        real_set = acct.store.set_kill_switch
        acct.store.set_kill_switch = AsyncMock(side_effect=ConnectionError("db down"))

        result = await acct.svc.activate_kill_switch("alice", "manual stop")
        assert (result.active, result.persisted) == (True, False)
        assert acct.svc.kill_switch_active() is True
        assert (await _rejected(acct, "s1", BUY, "0.001")).check == "kill_switch"
        assert await acct.submit("s1", SELL, "0.01", reduce_only=True)

        with pytest.raises(ConnectionError):
            await acct.svc.release_kill_switch("alice", "resume")
        assert acct.svc.kill_switch_active() is True

        acct.store.set_kill_switch = real_set
        await acct.svc.release_kill_switch("alice", "resume")
        assert acct.svc.kill_switch_active() is False
        assert acct.store.kill_switch is False
        audit = [(e.kind, e.actor) for e in acct.store.events if e.kind.startswith("kill")]
        assert audit == [
            ("kill_switch_on", "alice"),
            ("kill_switch_release_failed", "alice"),
            ("kill_switch_off", "alice"),
        ]

    async def test_persisted_kill_switch_loaded(self) -> None:
        acct = await make_account(permissive(), kill_switch=True)
        assert acct.svc.kill_switch_active() is True


class TestSizing:
    async def test_size_entry_from_sleeve_with_multiplier_and_global_cap(self) -> None:
        acct = await make_account(permissive())
        await acct.add_session("s1", "2000")
        await acct.mark("61234.56")
        assert await acct.svc.size_entry("s1", BTC, Decimal("0.10")) == Decimal("0.00326")

        capped = await make_account(permissive(max_position_pct=Decimal("0.05")))
        await capped.add_session("s1", "2000")
        await capped.mark("61234.56")
        assert await capped.svc.size_entry("s1", BTC, Decimal("0.10")) == Decimal("0.00163")

        tier1 = await make_account(
            permissive(), state=RiskState(cb=CircuitBreakerState(tier=1, tier_since=T0))
        )
        await tier1.add_session("s1", "2000")
        await tier1.mark("61234.56")
        assert await tier1.svc.size_entry("s1", BTC, Decimal("0.10")) == Decimal("0.00163")

    async def test_unsupported_exchange_aborts_registration(self) -> None:
        acct = await make_account(permissive())
        from hydra.risk.market_rules import UnsupportedExchangeError

        with pytest.raises(UnsupportedExchangeError):
            await acct.svc.register_session(
                "s1",
                acct.executors.get("x") or AsyncMock(),
                allocated=Decimal("100"),
                symbols=[BTC],
                exchange_id="bybit",
            )
        assert acct.svc.registered_sessions() == []
