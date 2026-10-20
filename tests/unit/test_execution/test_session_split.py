"""Structural guard for the session_manager split and the injected-config seam."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from importlinter.cli import lint_imports

from hydra.core.config import HydraConfig
from hydra.core.events import EntrySignal, OrderFillEvent
from hydra.core.types import OrderRequest, Symbol
from hydra.execution import (
    client_factory,
    exchange_client,
    flatten,
    portfolio_state,
    session_manager,
    session_store,
    session_wiring,
)
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.session_manager import SessionManager
from hydra.risk.pretrade import PreTradeRiskManager

REPO = Path(__file__).resolve().parents[3]

MOVED: dict[Any, list[str]] = {
    session_store: [
        "read_kill_switch",
        "set_kill_switch",
        "persist_session",
        "load_session_trades",
        "load_running",
        "load_recent",
        "mark_recovery_failed",
        "fetch_exchange_credentials",
    ],
    session_wiring: [
        "collect_timeframes",
        "build_engine",
        "attach_order_path",
        "start_session_runtime",
        "execute_order_actions",
        "signal_to_order",
    ],
    flatten: ["flatten_session"],
    client_factory: [
        "build_paper_executor",
        "build_live_executor",
        "load_live_credentials",
        "feed_exchange_factory",
    ],
    portfolio_state: ["build_portfolio_state"],
}

REMOVED_FROM_MANAGER = [
    "_collect_timeframes",
    "_persist_session",
    "_load_session_trades",
    "_execute_order_actions",
    "_signal_to_order",
]

MOVED_SOURCE_MARKERS = [
    "trading_sessions",
    "kill_switch_active",
    "FROM trades",
    "FROM risk_state",
    "exchange_credentials",
    "update_portfolio",
    "ccxt",
    "ExchangeClient(",
    "PaperTradingExecutor(",
    "InMemoryEventBus(",
    "StrategyEngine(",
    "OrderManager(",
    "ExchangeFeedManager(",
    "amount=qty",
    "subscribe(",
]

STRATEGY_YAML = """\
id: {sid}
name: Split guard
strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy
enabled: false
symbols: [BTCUSDT]
exchange:
  exchange_id: binance
  market_type: SPOT
timeframes:
  primary: 1h
"""


@pytest.mark.parametrize(
    ("module", "name"),
    [(m, n) for m, names in MOVED.items() for n in names],
    ids=lambda v: v if isinstance(v, str) else v.__name__.rsplit(".", 1)[-1],
)
def test_moved_function_lives_in_new_module(module: Any, name: str) -> None:
    fn = getattr(module, name)
    assert inspect.iscoroutinefunction(fn) or inspect.isfunction(fn)
    assert fn.__module__ == module.__name__


@pytest.mark.parametrize("name", REMOVED_FROM_MANAGER)
def test_session_manager_no_longer_defines_moved_helper(name: str) -> None:
    assert not hasattr(session_manager, name)
    assert not hasattr(SessionManager, name)


@pytest.mark.parametrize("marker", MOVED_SOURCE_MARKERS)
def test_moved_bodies_are_not_left_in_session_manager(marker: str) -> None:
    assert marker not in inspect.getsource(session_manager)


@pytest.mark.parametrize(
    "name",
    ["InMemoryEventBus", "ExchangeFeedManager", "OrderManager", "PortfolioState", "StrategyEngine"],
)
def test_wiring_classes_are_not_runtime_attributes_of_session_manager(name: str) -> None:
    # A stale patch("hydra.execution.session_manager.<name>") must fail loudly, not no-op.
    assert not hasattr(session_manager, name)
    with pytest.raises(AttributeError), patch(f"hydra.execution.session_manager.{name}"):
        pass


def test_class_map_moved_to_client_factory() -> None:
    assert exchange_client.EXCHANGE_CLASS_MAP is client_factory.EXCHANGE_CLASS_MAP
    assert client_factory.EXCHANGE_CLASS_MAP["binance"] == "binanceusdm"
    assert client_factory.SPOT_EXCHANGE_CLASS_MAP["binance"] == "binance"
    assert not hasattr(exchange_client, "_EXCHANGE_CLASS_MAP")
    assert not hasattr(exchange_client, "_SPOT_EXCHANGE_CLASS_MAP")


def test_manager_keeps_registry_and_public_api() -> None:
    for name in (
        "start_session",
        "stop_session",
        "stop_all",
        "release_kill_switch",
        "list_sessions",
        "get_session",
        "get_session_detail",
        "graceful_shutdown",
        "recover_running_sessions",
        "load_recent_sessions",
        "reload_risk_config",
    ):
        assert getattr(SessionManager, name).__module__ == session_manager.__name__
    assert session_manager.TradingSession.__module__ == session_manager.__name__


def _logger_state() -> dict[str, tuple[bool, bool, int, list[logging.Handler]]]:
    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    return {
        lg.name: (lg.disabled, lg.propagate, lg.level, list(lg.handlers))
        for lg in loggers
        if isinstance(lg, logging.Logger)
    }


def test_import_linter_contracts_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    saved = _logger_state()
    try:
        assert lint_imports(config_filename=str(REPO / "pyproject.toml"), no_cache=True) == 0
    finally:
        # lint_imports disables every existing logger as a side effect.
        for name, (disabled, propagate, level, handlers) in saved.items():
            lg = logging.getLogger(name) if name != "root" else logging.getLogger()
            lg.disabled, lg.propagate, lg.level = disabled, propagate, level
            lg.handlers[:] = handlers


# ---------------------------------------------------------------------------
# Injected HydraConfig
# ---------------------------------------------------------------------------


class _IdleExchange:
    """ccxt.pro stand-in whose streams never yield, so no network is touched."""

    def __init__(self) -> None:
        self._never = asyncio.Event()

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list[float]]:
        await self._never.wait()
        return []

    async def watch_trades(self, symbol: str) -> list[dict[str, Any]]:
        await self._never.wait()
        return []

    async def close(self) -> None:
        return None


class _Conn:
    def __init__(self, running_rows: list[dict[str, Any]]) -> None:
        self.running_rows = running_rows
        self.executed: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self.running_rows if "status = 'running'" in query else []

    async def fetchrow(self, query: str, *args: Any) -> None:
        return None

    async def fetchval(self, query: str, *args: Any) -> bool:
        return False

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append(query)


class _Pool:
    def __init__(self, running_rows: list[dict[str, Any]] | None = None) -> None:
        self.conn = _Conn(running_rows or [])

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        yield self.conn


@pytest.fixture()
def write_strategy(strategies_dir: Path) -> Callable[[str], str]:
    def _write(sid: str) -> str:
        (strategies_dir / f"{sid}.yaml").write_text(STRATEGY_YAML.format(sid=sid))
        return sid

    return _write


@pytest.fixture()
def idle_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_factory, "feed_exchange_factory", lambda _eid: _IdleExchange)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.usefixtures("idle_feed")
async def test_injected_config_reaches_engine_and_load_config_never_called(
    write_strategy: Callable[[str], str],
) -> None:
    cfg = HydraConfig()
    mgr = SessionManager(db_pool=None, config=cfg)
    sid = write_strategy("split-a")

    with patch.object(
        session_manager, "load_config", side_effect=AssertionError("load_config called")
    ) as load:
        session_id = await mgr.start_session(sid, "paper", paper_capital=Decimal("1000"))
        await _settle()
        session = mgr.get_session(session_id)
        assert session is not None
        assert session._engine is not None
        assert session._engine._config is cfg
        assert session.status == "running"
        assert session._task is not None and not session._task.done()
        assert isinstance(session._executor, PaperTradingExecutor)
        assert await session._executor.get_balance() == {"USDT": Decimal("1000")}
        assert session._feed is not None
        assert isinstance(session._feed._exchanges["binance"], _IdleExchange)
        await mgr.stop_session(session_id)

    load.assert_not_called()
    assert session.status == "stopped"
    assert session.positions_at_stop == 0


@pytest.mark.usefixtures("idle_feed")
async def test_injected_config_used_by_recovery(write_strategy: Callable[[str], str]) -> None:
    sid = write_strategy("split-r")
    row = {
        "id": "sess-r",
        "strategy_id": sid,
        "trading_mode": "paper",
        "exchange_id": "binance",
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "paper_capital": Decimal("2500"),
        "started_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    pool = _Pool([row])
    cfg = HydraConfig()
    mgr = SessionManager(db_pool=pool, config=cfg)

    with patch.object(
        session_manager, "load_config", side_effect=AssertionError("load_config called")
    ) as load:
        await mgr.recover_running_sessions()
        await _settle()
        session = mgr.get_session("sess-r")
        assert session is not None
        assert session.status == "running"
        assert session._engine is not None
        assert session._engine._config is cfg
        assert await session._executor.get_balance() == {"USDT": Decimal("2500")}
        assert session._feed is not None
        assert isinstance(session._feed._exchanges["binance"], _IdleExchange)
        await mgr.graceful_shutdown()

    load.assert_not_called()
    assert not any("Recovery failed" in q for q in pool.conn.executed)


@pytest.mark.usefixtures("idle_feed")
async def test_without_config_load_config_runs_once_per_start(
    write_strategy: Callable[[str], str],
) -> None:
    mgr = SessionManager(db_pool=None)
    loaded = HydraConfig()

    with patch.object(session_manager, "load_config", return_value=loaded) as load:
        first = await mgr.start_session(write_strategy("split-b"), "paper", Decimal("100"))
        assert load.call_count == 1
        second = await mgr.start_session(write_strategy("split-c"), "paper", Decimal("100"))
        assert load.call_count == 2
        session = mgr.get_session(first)
        assert session is not None and session._engine is not None
        assert session._engine._config is loaded
        await mgr.stop_session(first)
        await mgr.stop_session(second)


class _RiskStateConn(_Conn):
    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM risk_state" in query:
            return {"daily_pnl": "-50", "consecutive_losses": 2, "current_drawdown": "0.04"}
        return None


@pytest.mark.usefixtures("idle_feed")
async def test_order_manager_state_builder_reads_manager_pool_at_call_time(
    write_strategy: Callable[[str], str],
) -> None:
    mgr = SessionManager(db_pool=None, config=HydraConfig())
    session_id = await mgr.start_session(write_strategy("split-s"), "paper", Decimal("1000"))
    session = mgr.get_session(session_id)
    assert session is not None and session._order_manager is not None

    pool = _Pool()
    pool.conn = _RiskStateConn([])
    mgr._db_pool = pool
    state = await session._order_manager._portfolio_state_builder()

    assert state.current_drawdown == Decimal("0.04")
    assert state.daily_pnl == Decimal("-50")
    assert state.consecutive_losses == 2
    assert state.portfolio_value == Decimal("1000")
    mgr._db_pool = None
    await mgr.stop_session(session_id)


class _RecordingRiskManager(PreTradeRiskManager):
    def __init__(self) -> None:
        super().__init__()
        self.fills: list[tuple[Decimal, Decimal]] = []

    async def record_fill(self, pnl: Decimal, portfolio_value: Decimal) -> None:
        self.fills.append((pnl, portfolio_value))


@pytest.mark.usefixtures("idle_feed")
async def test_entry_sizing_reads_risk_config_after_balance_await(
    write_strategy: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = SessionManager(db_pool=None, config=HydraConfig())
    mgr._risk_config = {"max_risk_per_trade": "0.02"}
    session_id = await mgr.start_session(write_strategy("split-cfg"), "paper", Decimal("1000"))
    await _settle()
    session = mgr.get_session(session_id)
    assert session is not None and session._event_bus is not None

    async def balance_with_reload() -> dict[str, Decimal]:
        await asyncio.sleep(0)
        mgr._risk_config = {"max_risk_per_trade": "0.001"}
        return {"USDT": Decimal("10000")}

    async def last_price(symbol: str) -> Decimal:
        return Decimal("100")

    submitted: list[OrderRequest] = []

    async def record_submit(order: OrderRequest) -> str:
        submitted.append(order)
        return "oid"

    monkeypatch.setattr(session._executor, "get_balance", balance_with_reload)
    monkeypatch.setattr(session._executor, "get_last_price", last_price)
    monkeypatch.setattr(session._order_manager, "submit_order", record_submit)

    await session._event_bus.publish(EntrySignal(symbol=Symbol("BTCUSDT")))

    assert [o.quantity for o in submitted] == [Decimal("0.10000000")]
    await mgr.stop_session(session_id)


@pytest.mark.usefixtures("idle_feed")
async def test_fill_recorded_on_risk_manager_current_after_balance_await(
    write_strategy: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = SessionManager(db_pool=None, config=HydraConfig())
    old, new = _RecordingRiskManager(), _RecordingRiskManager()
    mgr._risk_manager = old
    session_id = await mgr.start_session(write_strategy("split-fill"), "paper", Decimal("1000"))
    await _settle()
    session = mgr.get_session(session_id)
    assert session is not None and session._event_bus is not None

    async def balance_with_reload() -> dict[str, Decimal]:
        await asyncio.sleep(0)
        mgr._risk_manager = new
        return {"USDT": Decimal("7000"), "BTC": Decimal("3")}

    monkeypatch.setattr(session._executor, "get_balance", balance_with_reload)

    await session._event_bus.publish(OrderFillEvent(symbol=Symbol("BTCUSDT")))

    assert old.fills == []
    assert new.fills == [(Decimal("0"), Decimal("7003"))]
    await mgr.stop_session(session_id)
