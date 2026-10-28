"""Stop, kill switch and breaker flatten: reduce-only via the OrderManager, truthful remainder."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY

from hydra.core.types import OrderStatus
from hydra.execution.errors import KillSwitchActiveError, SessionPersistError
from hydra.execution.flatten import FlattenResult
from hydra.execution.session_manager import SessionManager, TradingSession
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


class _Pool:
    def __init__(self) -> None:
        self.down = True
        self.rows: dict[str, str] = {}

    def acquire(self) -> Any:
        if self.down:
            raise ConnectionError("database down")
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.rows[args[0]] = args[3]
        return "INSERT 0 1"


class _FakeLiveExecutor:
    """Exposes the live executor's duck-typed sweep method (R4a) and held positions."""

    def __init__(self, *, error: Exception | None = None, held: list[str] | None = None) -> None:
        self.cancel_all: list[str] = []
        self.error = error
        self.held = held or []

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_all.append(symbol)
        if self.error is not None:
            raise self.error

    async def get_positions(self, symbol: str | None = None) -> list[Any]:
        if symbol is None:
            return []
        return [SimpleNamespace(quantity=D("0.01"))] if symbol in self.held else []


class _StubRuntime:
    """The flatten/shutdown surface ``_stop_session_internal`` needs of a live runtime."""

    def __init__(self, session: Any = None, remaining: int = 0) -> None:
        self.closing = False
        self.flattens = 0
        self._session = session
        self._remaining = remaining

    async def flatten(self) -> Any:
        self.flattens += 1
        if self._session is not None:
            self._session.positions_at_stop = self._remaining
            self._session.flatten_incomplete = self._remaining > 0
        return FlattenResult(self._remaining)

    async def shutdown(self) -> None:
        return None

    async def broadcast_status(self) -> None:
        return None


def _live_session(
    mgr: SessionManager,
    session_id: str,
    executor: Any,
    symbols: list[str],
    *,
    remaining: int = 0,
) -> Any:
    session = TradingSession(
        session_id=session_id,
        strategy_id=f"strat-{session_id}",
        trading_mode="live",
        status="running",
        exchange_id="binance",
        symbols=symbols,
        timeframe="1h",
    )
    session._executor = executor
    session._runtime = _StubRuntime(session, remaining)
    mgr._sessions[session_id] = session
    return session


class TestKillSwitchSweep:
    async def test_stop_all_sweeps_cancel_all_once_per_live_symbol(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        assert h.mgr is not None
        shared = _FakeLiveExecutor(held=["BTCUSDT", "ETHUSDT"])
        first = _live_session(h.mgr, "live-a", shared, ["BTCUSDT", "ETHUSDT"])
        second = _live_session(h.mgr, "live-b", shared, ["BTCUSDT"])
        other = _live_session(h.mgr, "live-c", _FakeLiveExecutor(held=["ETHUSDT"]), ["ETHUSDT"])
        paper = await h.session(write_strategy(strategies_dir, "sweep-paper"))
        await h.mgr.stop_all(actor="test", reason="drill")

        assert shared.cancel_all == ["BTCUSDT", "ETHUSDT"]  # deduped across its sessions
        assert other._executor.cancel_all == ["ETHUSDT"]
        assert [s.flattens for s in (first._runtime, second._runtime, other._runtime)] == [1, 1, 1]
        assert not hasattr(paper._executor, "cancel_all")  # paper executors are never swept
        assert all(s.status == "stopped" for s in (first, second, other))

    async def test_a_single_stop_session_never_sweeps(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        assert h.mgr is not None
        live = _live_session(h.mgr, "live-one", _FakeLiveExecutor(), ["BTCUSDT"])

        await h.mgr.stop_session(live.session_id)

        assert live._executor.cancel_all == []
        assert live.status == "stopped"

    async def test_a_failing_sweep_is_reported(self, h: Harness) -> None:
        assert h.mgr is not None
        executor = _FakeLiveExecutor(error=RuntimeError("binance down"), held=["BTCUSDT"])
        _live_session(h.mgr, "live-stuck", executor, ["BTCUSDT"])

        report = await h.mgr.stop_all(actor="test", reason="drill")

        assert executor.cancel_all == ["BTCUSDT"]
        assert any("BTCUSDT" in f and "binance down" in f for f in report.flatten_failures)

    async def test_the_sweep_covers_only_symbols_the_session_traded(self, h: Harness) -> None:
        """A9: the blast radius is each live-traded symbol, not the configured superset."""
        assert h.mgr is not None
        executor = _FakeLiveExecutor(held=["BTCUSDT"])
        _live_session(h.mgr, "live-traded", executor, ["BTCUSDT", "ETHUSDT"])

        await h.mgr.stop_all(actor="test", reason="drill")

        assert executor.cancel_all == ["BTCUSDT"]  # ETHUSDT was configured, never traded


class TestLiveStopGap:
    """R27(5): a live stop that leaves positions sets the protective gap at stop time."""

    @pytest.fixture(autouse=True)
    def _fresh_gauge(self) -> Iterator[None]:
        from hydra.core import metrics

        metrics._reset_metrics()
        yield
        metrics._reset_metrics()

    def _gap(self, session: Any) -> float | None:
        return REGISTRY.get_sample_value(
            "hydra_live_protective_gap",
            {
                "session_id": session.session_id,
                "strategy_id": session.strategy_id,
                "exchange_id": "binance",
                "symbol": "BTCUSDT",
            },
        )

    async def test_a_live_stop_with_a_remaining_position_sets_the_gap(self, h: Harness) -> None:
        """R13's battery expectation: the flatten sell was definitively rejected, the
        session holds on with no stop — the alert must keep firing from stop time on."""
        assert h.mgr is not None
        executor = _FakeLiveExecutor(held=["BTCUSDT"])
        session = _live_session(h.mgr, "live-gap", executor, ["BTCUSDT"], remaining=1)

        await h.mgr.stop_session(session.session_id)

        assert session.status == "stopped"
        assert session.positions_at_stop == 1
        assert self._gap(session) == 1.0

    async def test_a_clean_live_flatten_clears_the_gap(self, h: Harness) -> None:
        from hydra.core import metrics

        assert h.mgr is not None
        executor = _FakeLiveExecutor()
        session = _live_session(h.mgr, "live-clean", executor, ["BTCUSDT"])
        metrics.set_live_protective_gap(
            session.session_id, session.strategy_id, "binance", "BTCUSDT", True
        )
        assert self._gap(session) == 1.0

        await h.mgr.stop_session(session.session_id)

        assert session.positions_at_stop == 0
        assert self._gap(session) is None


class TestConcurrentStopAll:
    async def test_two_concurrent_stop_alls_never_interleave_their_sweeps(self, h: Harness) -> None:
        """R30/A4: a concurrent second caller waits — it must not sweep the same session."""

        assert h.mgr is not None
        session = _live_session(h.mgr, "swept-twice", _FakeLiveExecutor(), ["BTCUSDT"])
        runtime = session._runtime
        assert runtime is not None
        real_flatten = runtime.flatten

        async def slow_flatten() -> Any:
            await asyncio.sleep(0.05)  # an unserialized second sweep would enter here
            return await real_flatten()

        runtime.flatten = slow_flatten

        first = asyncio.create_task(h.mgr.stop_all(actor="operator", reason="drill"))
        second = asyncio.create_task(h.mgr.stop_all(actor="db-latch", reason="drill"))
        reports = await asyncio.gather(first, second)

        assert runtime.flattens == 1  # one sweep: the second waited, then found it stopped
        assert session.status == "stopped"
        assert sorted(r.sessions_stopped for r in reports) == [0, 1]
        assert [r.positions_remaining for r in reports] == [0, 0]


class TestRaces:
    async def test_kill_switch_during_a_start_refuses_the_new_session(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hydra.execution.session_wiring import SessionRuntime

        entered, gate = asyncio.Event(), asyncio.Event()
        real = SessionRuntime.warm_up

        async def slow_warm_up(self: SessionRuntime) -> Any:
            entered.set()
            await gate.wait()
            return await real(self)

        monkeypatch.setattr(SessionRuntime, "warm_up", slow_warm_up)
        assert h.mgr is not None and h.mgr.risk is not None
        start = asyncio.create_task(
            h.mgr.start_session(write_strategy(strategies_dir, "late"), "paper")
        )
        await entered.wait()
        report = await h.mgr.stop_all(actor="test", reason="drill")
        gate.set()
        with pytest.raises(KillSwitchActiveError):
            await start
        assert report.sessions_stopped == 0
        assert h.mgr.list_sessions() == []
        assert h.mgr.risk.registered_sessions() == []

    async def test_tier_three_flatten_survives_a_session_starting_meanwhile(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = await _entered(h, strategies_dir, "t3-a", "400")
        second = await _entered(h, strategies_dir, "t3-b", "400")
        assert h.mgr is not None and first._runtime is not None
        real = first._runtime.flatten

        async def flatten_while_a_session_starts() -> Any:
            if len(h.mgr.list_sessions()) == 2:
                await h.mgr.start_session(write_strategy(strategies_dir, "t3-c"), "paper")
            return await real()

        monkeypatch.setattr(first._runtime, "flatten", flatten_while_a_session_starts)
        failures = await h.mgr._flatten_for_risk("tier 3")
        assert failures == []
        assert (await held(first), await held(second)) == (0, 0)
        assert len(h.mgr.list_sessions()) == 3


class TestTerminalStatus:
    async def test_unpersisted_stop_is_reported_and_retried(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "unsaved")
        assert h.mgr is not None
        pool = _Pool()
        h.mgr._db_pool = pool
        with pytest.raises(SessionPersistError):
            await h.mgr.stop_session(session.session_id)
        assert session.status == "stopped" and await held(session) == 0
        snap = await h.mgr.health_snapshot()
        assert [x.status_persisted for x in snap.sessions] == [False]

        await h.mgr.retry_unpersisted()
        assert session.persist_error is not None
        pool.down = False
        await h.mgr.retry_unpersisted()
        assert session.persist_error is None
        assert pool.rows == {session.session_id: "stopped"}
        h.mgr._db_pool = None
