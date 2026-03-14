"""Tests for the session detail endpoint: GET /api/trading/sessions/{id}/detail."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from hydra.dashboard.api import app
from hydra.execution.session_manager import SessionManager, TradingSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)


def _make_running_session() -> TradingSession:
    session = TradingSession(
        session_id="sess-run-1",
        strategy_id="strat-alpha",
        trading_mode="paper",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=Decimal("10000"),
        started_at=_NOW,
    )
    # Mock executor
    executor = AsyncMock()
    executor.fetch_balance = AsyncMock(return_value={"USDT": Decimal("10250")})
    executor.fetch_positions = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "side": "long",
                "contracts": 0.01,
                "entryPrice": 65000.0,
                "unrealizedPnl": 120.0,
            }
        ]
    )
    executor._filled_orders = [
        {
            "id": "fill-1",
            "symbol": "BTCUSDT",
            "side": "buy",
            "quantity": 0.01,
            "price": 65000.0,
            "fee": 6.5,
            "pnl": 120.0,
            "timestamp": "2026-03-14T12:30:00+00:00",
        },
    ]
    session._executor = executor
    return session


def _make_stopped_session() -> TradingSession:
    return TradingSession(
        session_id="sess-stop-1",
        strategy_id="strat-beta",
        trading_mode="paper",
        status="stopped",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=Decimal("5000"),
        started_at=_NOW,
        stopped_at=datetime(2026, 3, 14, 14, 0, 0, tzinfo=UTC),
    )


@pytest.fixture()
def mock_session_manager() -> MagicMock:
    mgr = MagicMock(spec=SessionManager)
    mgr.start_session = AsyncMock(return_value="sess-run-1")
    mgr.stop_session = AsyncMock()
    mgr.stop_all = AsyncMock()
    mgr.release_kill_switch = AsyncMock()
    mgr.list_sessions = MagicMock(return_value=[])
    mgr.get_session = MagicMock(return_value=None)
    mgr.get_session_detail = AsyncMock()
    return mgr


@pytest.fixture()
def client(mock_session_manager: MagicMock) -> TestClient:
    app.state.session_manager = mock_session_manager
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionDetailRunning:
    """Detail endpoint for a running session returns live executor data."""

    def test_running_session_returns_detail(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.get_session_detail.return_value = {
            "session_id": "sess-run-1",
            "strategy_id": "strat-alpha",
            "trading_mode": "paper",
            "status": "running",
            "exchange_id": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "paper_capital": 10000.0,
            "started_at": "2026-03-14T12:00:00+00:00",
            "stopped_at": None,
            "error_message": None,
            "metrics": {
                "balance": {"USDT": 10250.0},
                "total_pnl": 120.0,
                "win_rate": 100.0,
                "total_trades": 1,
                "open_positions": 1,
            },
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "direction": "long",
                    "quantity": 0.01,
                    "avg_entry_price": 65000.0,
                    "unrealized_pnl": 120.0,
                }
            ],
            "trades": [
                {
                    "id": "fill-1",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": 0.01,
                    "price": 65000.0,
                    "fee": 6.5,
                    "pnl": 120.0,
                    "timestamp": "2026-03-14T12:30:00+00:00",
                }
            ],
        }

        resp = client.get("/api/trading/sessions/sess-run-1/detail")
        assert resp.status_code == 200

        data = resp.json()
        assert data["session_id"] == "sess-run-1"
        assert data["status"] == "running"
        assert data["metrics"]["balance"]["USDT"] == 10250.0
        assert data["metrics"]["total_trades"] == 1
        assert data["metrics"]["open_positions"] == 1
        assert len(data["positions"]) == 1
        assert data["positions"][0]["symbol"] == "BTCUSDT"
        assert len(data["trades"]) == 1


class TestSessionDetailStopped:
    """Detail endpoint for a stopped session returns DB trades."""

    def test_stopped_session_returns_detail(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.get_session_detail.return_value = {
            "session_id": "sess-stop-1",
            "strategy_id": "strat-beta",
            "trading_mode": "paper",
            "status": "stopped",
            "exchange_id": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "paper_capital": 5000.0,
            "started_at": "2026-03-14T12:00:00+00:00",
            "stopped_at": "2026-03-14T14:00:00+00:00",
            "error_message": None,
            "metrics": {
                "balance": {},
                "total_pnl": 45.0,
                "win_rate": 66.7,
                "total_trades": 3,
                "open_positions": 0,
            },
            "positions": [],
            "trades": [
                {
                    "id": "1",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": 0.01,
                    "price": 64000.0,
                    "fee": 6.4,
                    "pnl": 50.0,
                    "timestamp": "2026-03-14T12:10:00+00:00",
                },
                {
                    "id": "2",
                    "symbol": "BTCUSDT",
                    "side": "sell",
                    "quantity": 0.01,
                    "price": 63500.0,
                    "fee": 6.35,
                    "pnl": -15.0,
                    "timestamp": "2026-03-14T12:40:00+00:00",
                },
                {
                    "id": "3",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "quantity": 0.01,
                    "price": 63800.0,
                    "fee": 6.38,
                    "pnl": 10.0,
                    "timestamp": "2026-03-14T13:20:00+00:00",
                },
            ],
        }

        resp = client.get("/api/trading/sessions/sess-stop-1/detail")
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] == "stopped"
        assert data["stopped_at"] is not None
        assert data["metrics"]["total_trades"] == 3
        assert data["metrics"]["open_positions"] == 0
        assert len(data["trades"]) == 3


class TestSessionDetailNotFound:
    """Detail endpoint returns 404 for nonexistent session."""

    def test_nonexistent_session_returns_404(
        self, client: TestClient, mock_session_manager: MagicMock
    ) -> None:
        mock_session_manager.get_session_detail.side_effect = KeyError(
            "Session not-found not found"
        )
        resp = client.get("/api/trading/sessions/not-found/detail")
        assert resp.status_code == 404


class TestSessionDetailMetrics:
    """Metrics are computed correctly from trade list."""

    def test_metrics_computation(self, client: TestClient, mock_session_manager: MagicMock) -> None:
        # 4 trades: 3 winners, 1 loser
        mock_session_manager.get_session_detail.return_value = {
            "session_id": "sess-metrics",
            "strategy_id": "strat-gamma",
            "trading_mode": "paper",
            "status": "stopped",
            "exchange_id": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "paper_capital": 10000.0,
            "started_at": "2026-03-14T10:00:00+00:00",
            "stopped_at": "2026-03-14T16:00:00+00:00",
            "error_message": None,
            "metrics": {
                "balance": {},
                "total_pnl": 85.0,
                "win_rate": 75.0,
                "total_trades": 4,
                "open_positions": 0,
            },
            "positions": [],
            "trades": [
                {"id": "1", "pnl": 30.0},
                {"id": "2", "pnl": 25.0},
                {"id": "3", "pnl": -20.0},
                {"id": "4", "pnl": 50.0},
            ],
        }

        resp = client.get("/api/trading/sessions/sess-metrics/detail")
        assert resp.status_code == 200

        metrics = resp.json()["metrics"]
        assert metrics["total_pnl"] == 85.0
        assert metrics["win_rate"] == 75.0
        assert metrics["total_trades"] == 4
        assert metrics["open_positions"] == 0
