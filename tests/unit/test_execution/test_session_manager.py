"""Tests for SessionManager: session lifecycle, paper vs live, kill switch."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hydra.execution.session_manager import SessionManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db_pool() -> MagicMock:
    """Create a mock asyncpg pool with acquire() returning async context manager."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)  # kill_switch_active = False
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.fixture()
def manager(mock_db_pool: MagicMock) -> SessionManager:
    return SessionManager(db_pool=mock_db_pool)


@pytest.fixture()
def manager_no_db() -> SessionManager:
    return SessionManager(db_pool=None)


# ---------------------------------------------------------------------------
# Strategy config fixture (mock YAML loading)
# ---------------------------------------------------------------------------


def _mock_strategy_config(strategy_id: str = "test-strategy") -> MagicMock:
    from hydra.strategy.config import (
        ExchangeStrategyConfig,
        StrategyConfig,
        TimeframeConfig,
    )

    return StrategyConfig(
        id=strategy_id,
        name="Test Strategy",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        enabled=True,
        symbols=["BTCUSDT"],
        exchange=ExchangeStrategyConfig(exchange_id="binance"),
        timeframes=TimeframeConfig(),
    )


def _mock_hydra_config() -> MagicMock:
    """Return a mock HydraConfig that doesn't need real YAML files."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Session lifecycle tests
# ---------------------------------------------------------------------------


class TestStartSession:
    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_start_session_creates_running_session(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        session_id = await manager.start_session("test-strategy", "paper")

        assert session_id is not None
        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].status == "running"
        assert sessions[0].strategy_id == "test-strategy"
        assert sessions[0].trading_mode == "paper"

    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_start_session_returns_session_id(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        session_id = await manager.start_session("test-strategy", "paper")
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_start_paper_session_uses_paper_executor(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        session_id = await manager.start_session(
            "test-strategy", "paper", paper_capital=Decimal("50000")
        )
        session = manager.get_session(session_id)
        assert session is not None
        assert session.trading_mode == "paper"
        assert session.paper_capital == Decimal("50000")
        from hydra.execution.paper_trading import PaperTradingExecutor

        assert isinstance(session._executor, PaperTradingExecutor)


class TestStopSession:
    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_stop_session_sets_status_stopped(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        session_id = await manager.start_session("test-strategy", "paper")
        await manager.stop_session(session_id)

        session = manager.get_session(session_id)
        assert session is not None
        assert session.status == "stopped"
        assert session.stopped_at is not None

    async def test_stop_nonexistent_session_raises(self, manager: SessionManager) -> None:
        with pytest.raises(KeyError, match="not found"):
            await manager.stop_session("nonexistent-id")


class TestStopAll:
    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_stop_all_stops_all_running(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        await manager.start_session("test-strategy", "paper")
        # Reset mock to allow second session with different ID
        mock_find.return_value = _mock_strategy_config("test-strategy-2")
        await manager.start_session("test-strategy-2", "paper")

        await manager.stop_all()

        for s in manager.list_sessions():
            assert s.status == "stopped"


class TestDuplicateSession:
    @patch("hydra.execution.session_manager.load_config", return_value=_mock_hydra_config())
    @patch.object(SessionManager, "_find_strategy_config", return_value=_mock_strategy_config())
    @patch("hydra.execution.session_manager.StrategyEngine")
    async def test_duplicate_session_raises(
        self,
        mock_engine_cls: MagicMock,
        mock_find: MagicMock,
        mock_load_config: MagicMock,
        manager: SessionManager,
    ) -> None:
        engine = AsyncMock()
        mock_engine_cls.return_value = engine

        await manager.start_session("test-strategy", "paper")

        with pytest.raises(ValueError, match="already has a running session"):
            await manager.start_session("test-strategy", "paper")


class TestKillSwitch:
    async def test_kill_switch_blocks_new_sessions(self, mock_db_pool: MagicMock) -> None:
        # Mock kill_switch_active = True
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_db_pool.acquire = MagicMock(return_value=ctx)

        mgr = SessionManager(db_pool=mock_db_pool)

        with pytest.raises(RuntimeError, match="Kill switch is active"):
            await mgr.start_session("test-strategy", "paper")


class TestStrategyNotFound:
    async def test_unknown_strategy_raises(self, manager: SessionManager) -> None:
        with pytest.raises((ValueError, FileNotFoundError)):
            await manager.start_session("nonexistent-strategy", "paper")
