"""Tests for trading session API routes: start/stop/list/kill-switch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from hydra.dashboard.api import app
from hydra.execution.session_manager import SessionManager, TradingSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_session_manager() -> MagicMock:
    mgr = MagicMock(spec=SessionManager)
    mgr.start_session = AsyncMock(return_value="sess-123")
    mgr.stop_session = AsyncMock()
    mgr.stop_all = AsyncMock()
    mgr.release_kill_switch = AsyncMock()
    mgr.list_sessions = MagicMock(return_value=[])
    mgr.get_session = MagicMock(
        return_value=TradingSession(
            session_id="sess-123",
            strategy_id="test-strategy",
            trading_mode="paper",
            status="running",
            exchange_id="binance",
            symbols=["BTCUSDT"],
            timeframe="1h",
        )
    )
    return mgr


@pytest.fixture()
def client(mock_session_manager: MagicMock) -> TestClient:
    app.state.session_manager = mock_session_manager
    return TestClient(app)


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


class TestStartSession:
    def test_start_session_returns_201(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trading/sessions",
            json={"strategy_id": "test-strategy", "trading_mode": "paper"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"] == "sess-123"
        assert data["strategy_id"] == "test-strategy"
        assert data["trading_mode"] == "paper"

    def test_start_session_invalid_mode(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trading/sessions",
            json={"strategy_id": "test-strategy", "trading_mode": "invalid"},
        )
        assert resp.status_code == 400

    def test_start_session_with_capital(self, client: TestClient) -> None:
        resp = client.post(
            "/api/trading/sessions",
            json={
                "strategy_id": "test-strategy",
                "trading_mode": "paper",
                "paper_capital": 50000.0,
            },
        )
        assert resp.status_code == 201


class TestStopSession:
    def test_stop_session_returns_200(self, client: TestClient) -> None:
        # Override get_session to return stopped
        client.app.state.session_manager.get_session.return_value = TradingSession(
            session_id="sess-123",
            strategy_id="test-strategy",
            trading_mode="paper",
            status="stopped",
        )
        resp = client.delete("/api/trading/sessions/sess-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"

    def test_stop_nonexistent_session_returns_404(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.stop_session.side_effect = KeyError("Session not-found not found")
        resp = client.delete("/api/trading/sessions/not-found")
        assert resp.status_code == 404


class TestListSessions:
    def test_list_sessions_returns_list(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.list_sessions.return_value = [
            TradingSession(
                session_id="sess-1",
                strategy_id="strat-a",
                trading_mode="paper",
                status="running",
            ),
            TradingSession(
                session_id="sess-2",
                strategy_id="strat-b",
                trading_mode="paper",
                status="stopped",
            ),
        ]
        resp = client.get("/api/trading/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["status"] == "running"
        assert data[1]["status"] == "stopped"


class TestKillSwitch:
    def test_activate_kill_switch(self, client: TestClient) -> None:
        resp = client.post("/api/trading/kill-switch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert "Kill switch activated" in data["message"]

    def test_release_kill_switch(self, client: TestClient) -> None:
        resp = client.delete("/api/trading/kill-switch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert "released" in data["message"]

    def test_kill_switch_blocks_new_sessions(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.start_session.side_effect = RuntimeError("Kill switch is active")
        resp = client.post(
            "/api/trading/sessions",
            json={"strategy_id": "test-strategy", "trading_mode": "paper"},
        )
        assert resp.status_code == 409
        assert "Kill switch" in resp.json()["detail"]
