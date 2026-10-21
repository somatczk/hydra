"""SessionManager start gates, capital allocation, detail and health, on the real session seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution import client_factory, exchange_client
from hydra.execution.errors import (
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveTradingDisabledError,
    PaperCapitalUnavailableError,
    UnsupportedStrategyError,
    UnsupportedVenueError,
)
from hydra.execution.session_manager import SessionManager
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    rule,
    settle,
    write_strategy,
)

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


class _Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture()
def construction_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    """Records any executor or exchange-client construction on the session path."""
    spy = _Spy()
    real_paper = client_factory.build_paper_executor
    real_feed = client_factory.feed_exchange_factory

    def paper(*args: Any, **kwargs: Any) -> Any:
        spy.calls.append("paper_executor")
        return real_paper(*args, **kwargs)

    def client(*args: Any, **kwargs: Any) -> Any:
        spy.calls.append("exchange_client")
        raise AssertionError("ExchangeClient constructed on the session path")

    monkeypatch.setattr(client_factory, "build_paper_executor", paper)
    monkeypatch.setattr(exchange_client, "ExchangeClient", client)
    monkeypatch.setattr(client_factory, "feed_exchange_factory", lambda eid: real_feed(eid))
    return spy


class TestStartGates:
    async def test_paper_session_runs_with_a_live_task(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "s-ok"), "250")
        await settle()
        assert session.status == "running"
        assert session.task_alive
        assert await executor(session).get_balance() == {"USDT": Decimal("250")}
        assert h.mgr is not None and h.mgr.risk is not None
        assert h.mgr.risk.registered_sessions() == [session.session_id]

    async def test_live_start_is_refused_before_anything_is_built(
        self, h: Harness, strategies_dir: Path, construction_spy: _Spy
    ) -> None:
        assert h.mgr is not None
        with pytest.raises(LiveTradingDisabledError) as err:
            await h.mgr.start_session(write_strategy(strategies_dir, "s-live"), "live")
        assert err.value.code == "live_trading_disabled"
        assert construction_spy.calls == []
        assert h.mgr.list_sessions() == []

    @pytest.mark.parametrize(
        ("exchange_id", "market_type"),
        [("bybit", "SPOT"), ("kraken", "SPOT"), ("binance", "FUTURES")],
    )
    async def test_non_binance_and_futures_are_refused_before_anything_is_built(
        self,
        h: Harness,
        strategies_dir: Path,
        construction_spy: _Spy,
        exchange_id: str,
        market_type: str,
    ) -> None:
        assert h.mgr is not None
        sid = write_strategy(
            strategies_dir, "s-venue", exchange_id=exchange_id, market_type=market_type
        )
        with pytest.raises(UnsupportedVenueError):
            await h.mgr.start_session(sid, "paper")
        assert construction_spy.calls == []
        assert h.ws == []

    @pytest.mark.parametrize(
        "strategy_class",
        [
            "hydra.strategy.builtin.dca.DCAStrategy",
            "hydra.strategy.builtin.momentum.NoSuchStrategy",
            "hydra.core.types.OHLCV",
            "hydra.strategy.base.BaseStrategy",
        ],
        ids=["module-missing", "class-missing", "not-a-strategy", "abstract"],
    )
    async def test_unloadable_strategy_class_is_refused(
        self, h: Harness, strategies_dir: Path, construction_spy: _Spy, strategy_class: str
    ) -> None:
        assert h.mgr is not None
        sid = write_strategy(strategies_dir, "s-bad", strategy_class=strategy_class)
        with pytest.raises(UnsupportedStrategyError) as err:
            await h.mgr.start_session(sid, "paper")
        assert err.value.code == "strategy_unsupported"
        assert strategy_class in str(err.value)
        assert construction_spy.calls == []
        assert h.mgr.list_sessions() == []

    async def test_malformed_config_of_a_loadable_class_is_invalid_not_unsupported(
        self, h: Harness, strategies_dir: Path, construction_spy: _Spy
    ) -> None:
        assert h.mgr is not None
        sid = write_strategy(strategies_dir, "s-malformed")
        path = strategies_dir / f"{sid}.yaml"
        data = yaml.safe_load(path.read_text())
        data["parameters"]["rules"] = [1]
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match="invalid strategy config for s-malformed") as err:
            await h.mgr.start_session(sid, "paper")
        assert not isinstance(err.value, UnsupportedStrategyError)
        assert construction_spy.calls == []
        assert h.mgr.list_sessions() == []

    async def test_kill_switch_blocks_new_sessions(self, h: Harness, strategies_dir: Path) -> None:
        assert h.mgr is not None and h.mgr.risk is not None
        await h.mgr.risk.activate_kill_switch("test", "drill")
        with pytest.raises(KillSwitchActiveError):
            await h.mgr.start_session(write_strategy(strategies_dir, "s-ks"), "paper")

    async def test_duplicate_running_strategy(self, h: Harness, strategies_dir: Path) -> None:
        sid = write_strategy(strategies_dir, "s-dup")
        await h.session(sid, "100")
        assert h.mgr is not None
        with pytest.raises(DuplicateSessionError):
            await h.mgr.start_session(sid, "paper", Decimal("100"))

    async def test_unknown_strategy(self, h: Harness) -> None:
        assert h.mgr is not None
        with pytest.raises(KeyError):
            await h.mgr.start_session("nope", "paper")

    async def test_without_database_or_capital_nothing_starts(self, strategies_dir: Path) -> None:
        mgr = SessionManager(db_pool=None, config=HydraConfig())
        await mgr.startup()
        with pytest.raises(PaperCapitalUnavailableError):
            await mgr.start_session(write_strategy(strategies_dir, "s-nocap"), "paper")
        await mgr.graceful_shutdown()

    async def test_insufficient_history_aborts_and_unregisters(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(strategies_dir, "s-hist")
        path = strategies_dir / f"{sid}.yaml"
        path.write_text(path.read_text().replace("required_history: 2", "required_history: 50"))
        assert h.mgr is not None and h.mgr.risk is not None
        with pytest.raises(InsufficientHistoryError) as err:
            await h.mgr.start_session(sid, "paper")
        assert (err.value.have, err.value.need) == (20, 50)
        assert h.mgr.list_sessions() == []
        assert h.mgr.risk.registered_sessions() == []


class TestCapital:
    async def test_default_allocation_is_the_remaining_account_capital(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        first = await h.session(write_strategy(strategies_dir, "c1"), "300")
        second = await h.session(write_strategy(strategies_dir, "c2"))
        assert (first.paper_capital, second.paper_capital) == (Decimal("300"), Decimal("700"))
        assert h.mgr is not None
        with pytest.raises(ValueError, match="Insufficient paper capital"):
            await h.mgr.start_session(write_strategy(strategies_dir, "c3"), "paper")

    async def test_request_above_the_remaining_capital_is_capped(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        await h.session(write_strategy(strategies_dir, "c4"), "900")
        capped = await h.session(write_strategy(strategies_dir, "c5"), "500")
        assert capped.paper_capital == Decimal("100")


class TestDetailAndHealth:
    async def test_detail_reports_net_pnl_from_the_ledger(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(
            strategies_dir,
            "d1",
            entry_long=rule(("less_than", 100.5), ("greater_than", 99.5)),
            exit_long=rule(("greater_than", 105)),
        )
        session = await h.session(sid)
        await h.bar(session, "100")
        await h.bar(session, "110")
        assert h.mgr is not None
        detail = await h.mgr.get_session_detail(session.session_id)
        assert detail["metrics"]["total_pnl"] == pytest.approx(9.58001)
        assert detail["metrics"]["total_trades"] == 2
        assert detail["metrics"]["win_rate"] == 100.0
        assert detail["metrics"]["equity"] == pytest.approx(1009.58001)
        assert detail["positions"] == []
        assert detail["last_bar_at"] is not None

    async def test_health_snapshot_reports_supervision_and_freshness(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "hs"))
        await h.bar(session, "100")
        assert h.mgr is not None
        snap = await h.mgr.health_snapshot()
        (health,) = snap.sessions
        assert (health.session_id, health.desired, health.supervised) == (
            session.session_id,
            True,
            True,
        )
        assert health.last_bar_age_s is not None and health.last_bar_age_s < 5
        assert health.open_positions == 0
        assert snap.risk_available and not snap.kill_switch
        await h.mgr.stop_session(session.session_id)
        (stopped,) = (await h.mgr.health_snapshot()).sessions
        assert (stopped.desired, stopped.supervised) == (False, False)


async def test_session_error_is_counted(h: Harness, strategies_dir: Path) -> None:
    session = await h.session(write_strategy(strategies_dir, "err"))
    assert h.mgr is not None
    before = metrics._counter("hydra_sessions_errored_total").labels(strategy_id="err")._value.get()
    await h.mgr._on_runtime_failure(session, "drill")
    assert session.status == "error"
    assert session.error_message == "drill"
    assert not session.task_alive
    after = metrics._counter("hydra_sessions_errored_total").labels(strategy_id="err")._value.get()
    assert after - before == 1
