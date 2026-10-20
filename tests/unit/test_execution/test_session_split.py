"""Structural guards for the session modules: one client factory, no legacy risk path, layers."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from importlinter.cli import lint_imports

from hydra.execution import (
    client_factory,
    flatten,
    protective_orders,
    session_manager,
    session_store,
    session_wiring,
)
from hydra.execution.session_manager import SessionManager
from tests.unit.test_execution.session_harness import Harness, write_strategy

REPO = Path(__file__).resolve().parents[3]
EXECUTION = REPO / "src" / "hydra" / "execution"
SESSION_MODULES = [session_manager, session_wiring, session_store, flatten, protective_orders]


def test_only_client_factory_constructs_exchange_clients() -> None:
    offenders = []
    for path in sorted(EXECUTION.glob("*.py")):
        if path.name in ("client_factory.py", "exchange_client.py"):
            continue
        text = path.read_text()
        for marker in ("ExchangeClient(", "getattr(ccxt", "ccxt.pro.", "ccxt_async."):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == []


@pytest.mark.parametrize("module", SESSION_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_session_path_has_no_legacy_risk_or_fake_reconcile(module: ModuleType) -> None:
    source = inspect.getsource(module)
    for marker in ("PreTradeRiskManager", "build_portfolio_state", "mismatch=False", "amount="):
        assert marker not in source


def test_paper_executor_shims_are_gone() -> None:
    from hydra.execution.paper_trading import PaperTradingExecutor

    assert not hasattr(PaperTradingExecutor, "set_market_price")
    assert not hasattr(PaperTradingExecutor, "check_pending_orders")


@pytest.mark.parametrize("marker", ["trading_sessions", "risk_config", "INSERT", "UPDATE"])
def test_sql_lives_in_session_store(marker: str) -> None:
    assert marker not in inspect.getsource(session_manager)
    assert marker not in inspect.getsource(session_wiring)


@pytest.mark.parametrize(
    "name",
    ["InMemoryEventBus", "ExchangeFeedManager", "OrderManager", "PortfolioState", "StrategyEngine"],
)
def test_wiring_classes_are_not_runtime_attributes_of_session_manager(name: str) -> None:
    # A stale patch("hydra.execution.session_manager.<name>") must fail loudly, not no-op.
    assert not hasattr(session_manager, name)
    with pytest.raises(AttributeError), patch(f"hydra.execution.session_manager.{name}"):
        pass


def test_public_api() -> None:
    for name in (
        "startup",
        "start_session",
        "stop_session",
        "stop_all",
        "release_kill_switch",
        "list_sessions",
        "get_session",
        "get_session_detail",
        "submit_manual_order",
        "health_snapshot",
        "graceful_shutdown",
    ):
        assert getattr(SessionManager, name).__module__ == session_manager.__name__
    for gone in ("recover_running_sessions", "load_recent_sessions", "_reconciliation_loop"):
        assert not hasattr(SessionManager, gone)
    assert callable(client_factory.validate_venue)


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


async def test_injected_config_reaches_the_engine_and_load_config_is_never_called(
    strategies_dir: Path,
) -> None:
    assert "load_config" not in Path(session_manager.__file__).read_text()
    config = inspect.signature(SessionManager).parameters["config"]
    assert (config.kind, config.default) == (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.empty,
    )
    h = Harness(strategies_dir)
    mgr = await h.start()
    session = await h.session(write_strategy(strategies_dir, "cfg"))
    assert session._engine is not None and session._engine._config is mgr._config
    await h.close()
