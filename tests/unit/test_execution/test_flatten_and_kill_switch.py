"""Stop, kill switch and breaker flatten: reduce-only via the OrderManager, truthful remainder."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.core.types import OrderStatus
from hydra.execution.errors import KillSwitchActiveError
from hydra.risk.pretrade import RiskConfig
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
    rule,
    settle,
    usdt,
    write_strategy,
)

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal
ENTRY_AT_100 = rule(("less_than", 100.5), ("greater_than", 99.5))


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


async def _entered(h: Harness, strategies_dir: Path, sid: str, capital: str | None = None) -> Any:
    session = await h.session(write_strategy(strategies_dir, sid, entry_long=ENTRY_AT_100), capital)
    await h.bar(session, "100")
    assert await held(session) > 0
    return session


@pytest.fixture()
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    from hydra.core.websocket import manager

    sent: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(channel: str, payload: dict[str, Any]) -> None:
        sent.append((channel, payload))

    monkeypatch.setattr(manager, "broadcast", broadcast)
    return sent


class TestStop:
    async def test_stop_session_flattens_through_the_order_manager(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "stopper")
        assert h.mgr is not None and h.mgr.risk is not None
        await h.mgr.stop_session(session.session_id)

        assert session.status == "stopped"
        assert session.positions_at_stop == 0 and not session.flatten_incomplete
        assert not session.task_alive
        last = executor(session).get_fills()[-1]
        assert (last.side, last.quantity, last.price) == ("SELL", D("1.00000"), D("99.900"))
        om = session._order_manager
        flatten_ids = [oid for oid, t in om._orders.items() if t.request.tag == "flatten"]
        assert [om.get_order_status(oid) for oid in flatten_ids] == [OrderStatus.FILLED]
        assert await executor(session).fetch_open_orders() == []
        # 899.7999 + 99.9 - fee 0.0999; the stop it cancelled first released the locked BTC
        assert await usdt(session) == D("999.6000")
        assert h.mgr.risk.registered_sessions() == []
        assert h.mgr.risk.account().carried_pnl == D("-0.4000")


class TestKillSwitch:
    async def test_flag_is_set_first_and_every_session_is_flattened(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = await _entered(h, strategies_dir, "ks-a", "500")
        errored = await _entered(h, strategies_dir, "ks-b", "500")
        errored.status = "error"
        assert h.mgr is not None
        flags: list[bool] = []
        for session in (running, errored):
            ex = executor(session)
            real = ex.create_order

            async def spy(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
                if (args[1] if len(args) > 1 else kwargs.get("side")) == "SELL":
                    flags.append(h.mgr.kill_switch_active())
                return await _real(*args, **kwargs)

            monkeypatch.setattr(ex, "create_order", spy)

        report = await h.mgr.stop_all(actor="test", reason="drill")

        assert flags == [True, True]
        assert (report.sessions_stopped, report.positions_remaining, report.persisted) == (
            2,
            0,
            True,
        )
        assert await held(running) == 0 and await held(errored) == 0
        assert (running.status, errored.status) == ("stopped", "error")
        with pytest.raises(KillSwitchActiveError):
            await h.mgr.start_session(write_strategy(strategies_dir, "ks-c"), "paper")
        await h.mgr.release_kill_switch("test", "drill over")
        assert not h.mgr.kill_switch_active()

    async def test_flatten_incomplete_is_reported(
        self,
        h: Harness,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        broadcasts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        session = await _entered(h, strategies_dir, "stuck")
        ex = executor(session)
        real = ex.create_order

        async def no_sells(*args: Any, **kwargs: Any) -> Any:
            if (args[1] if len(args) > 1 else kwargs.get("side")) == "SELL" and (
                args[2] if len(args) > 2 else kwargs.get("order_type")
            ) == "MARKET":
                raise RuntimeError("exchange down")
            return await real(*args, **kwargs)

        async def no_flatten(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("exchange down")

        monkeypatch.setattr(ex, "create_order", no_sells)
        monkeypatch.setattr(ex, "flatten", no_flatten)
        assert h.mgr is not None
        with caplog.at_level(logging.ERROR, logger="hydra.execution.flatten"):
            report = await h.mgr.stop_all(actor="test", reason="drill")

        assert (report.positions_remaining, report.sessions_stopped) == (1, 1)
        assert report.flatten_failures and session.session_id in report.flatten_failures[0]
        assert session.positions_at_stop == 1 and session.flatten_incomplete
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        statuses = [p for c, p in broadcasts if p.get("event") == "session_status"]
        assert statuses[-1]["positions_remaining"] == 1
        assert statuses[-1]["flatten_incomplete"] is True


class TestBreakers:
    async def test_exit_is_not_blocked_by_the_heat_cap(self, strategies_dir: Path) -> None:
        h = Harness(strategies_dir, risk_config=RiskConfig(max_portfolio_heat=D("0.08")))
        await h.start()
        try:
            first = await _entered(h, strategies_dir, "heat-a", "500")
            second = await h.session(
                write_strategy(strategies_dir, "heat-b", entry_long=ENTRY_AT_100), "500"
            )
            await h.bar(second, "100")
            assert await held(second) == 0  # 50 + 50 USDT gross > 8 % of 1000
            assert h.mgr is not None
            from hydra.core.types import Side

            _, result = await h.mgr.submit_manual_order(
                symbol="BTCUSDT", side=Side.SELL, session_id=first.session_id
            )
            assert result.status == "FILLED"
            assert await held(first) == 0
        finally:
            await h.close()

    @pytest.mark.parametrize(("gap_open", "tier"), [("80", 3), ("60", 4)])
    async def test_breaker_flattens_every_session_and_tier_four_latches(
        self, strategies_dir: Path, gap_open: str, tier: int
    ) -> None:
        h = Harness(
            strategies_dir,
            risk_config=RiskConfig(
                max_position_pct=D("1"), max_risk_per_trade=D("1"), max_portfolio_heat=D("1")
            ),
        )
        await h.start()
        try:
            big = await h.session(
                write_strategy(
                    strategies_dir, "big", entry_long=ENTRY_AT_100, max_position_pct=100.0
                ),
                "500",
            )
            await h.bar(big, "100")
            other = await _entered(h, strategies_dir, "other", "500")
            assert await held(big) == D("4.99001") and await held(other) == D("0.50000")
            assert h.mgr is not None and h.mgr.risk is not None
            # big's stop gaps: it fills at the open, and the account mark drops to the open
            await h.bar(big, gap_open, open=gap_open, high=gap_open, low=gap_open)
            await settle()
            assert h.mgr.risk.account().tier == tier
            assert await held(other) == 0
            assert h.mgr.kill_switch_active() is (tier == 4)
            expected = "stopped" if tier == 4 else "running"
            assert (big.status, other.status) == (expected, expected)
        finally:
            await h.close()
