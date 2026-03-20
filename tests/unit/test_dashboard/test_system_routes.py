"""Tests for system dashboard routes.

Tests cover:
- Config reads from env vars with defaults
- Config update persists to app.state
- Exchanges endpoint reports API key status from env
- Health endpoint pings DB pool and Redis
- Health overall status derivation logic
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.dashboard.routes.system import router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool: object | None = None) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.db_pool = pool
    test_app.state.system_config = None
    return test_app


@pytest.fixture()
def client_no_pool() -> TestClient:
    return TestClient(_make_app(pool=None))


def _make_mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# Config endpoint
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_defaults_without_env(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/system/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "paper"
        assert data["default_pair"] == "BTC/USDT"
        assert data["default_timeframe"] == "1h"
        assert data["max_concurrent_strategies"] == 5

    def test_reads_env_vars(self) -> None:
        env = {
            "HYDRA_TRADING_MODE": "live",
            "HYDRA_DEFAULT_PAIR": "ETH/USDT",
            "HYDRA_DEFAULT_TIMEFRAME": "4h",
            "HYDRA_MAX_STRATEGIES": "10",
        }
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["trading_mode"] == "live"
            assert data["default_pair"] == "ETH/USDT"
            assert data["default_timeframe"] == "4h"
            assert data["max_concurrent_strategies"] == 10

    def test_invalid_max_strategies_uses_default(self) -> None:
        with patch.dict("os.environ", {"HYDRA_MAX_STRATEGIES": "not-a-number"}, clear=False):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/config")
            assert resp.status_code == 200
            assert resp.json()["max_concurrent_strategies"] == 5

    def test_persisted_config_takes_precedence(self) -> None:
        app = _make_app(pool=None)
        app.state.system_config = {
            "trading_mode": "testnet",
            "default_pair": "SOL/USDT",
            "default_timeframe": "15m",
            "max_concurrent_strategies": 3,
        }
        client = TestClient(app)
        resp = client.get("/api/system/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "testnet"
        assert data["default_pair"] == "SOL/USDT"


class TestUpdateConfig:
    def test_partial_update(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put(
            "/api/system/config",
            json={"trading_mode": "live"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "live"
        # Other fields should retain defaults
        assert data["default_pair"] == "BTC/USDT"
        assert data["default_timeframe"] == "1h"

    def test_full_update(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put(
            "/api/system/config",
            json={
                "trading_mode": "testnet",
                "default_pair": "ETH/BTC",
                "default_timeframe": "4h",
                "max_concurrent_strategies": 2,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "testnet"
        assert data["default_pair"] == "ETH/BTC"
        assert data["max_concurrent_strategies"] == 2

    def test_update_persists_to_state(self) -> None:
        """After update, subsequent GET should return updated values."""
        app = _make_app(pool=None)
        client = TestClient(app)

        client.put("/api/system/config", json={"trading_mode": "live"})
        resp = client.get("/api/system/config")
        assert resp.json()["trading_mode"] == "live"

    def test_update_with_no_fields_returns_current(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put("/api/system/config", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["trading_mode"] == "paper"


# ---------------------------------------------------------------------------
# Exchanges endpoint
# ---------------------------------------------------------------------------


class TestExchanges:
    def test_exchanges_no_pool_no_keys(self) -> None:
        """Without pool and without API keys, all exchanges disconnected."""
        env_clear = {
            "BINANCE_API_KEY": "",
            "BYBIT_API_KEY": "",
            "KRAKEN_API_KEY": "",
            "OKX_API_KEY": "",
        }
        with patch.dict("os.environ", env_clear, clear=False):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 4
            for exchange in data:
                assert exchange["connected"] is False
                assert exchange["api_key_set"] is False

    def test_exchanges_api_key_set_but_no_pool(self) -> None:
        """With API keys but no pool, exchanges show key set but not connected."""
        env = {"BINANCE_API_KEY": "secret123"}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            binance = next(e for e in data if e["id"] == "binance")
            assert binance["api_key_set"] is True
            # No pool means db_connected=False, so connected=False
            assert binance["connected"] is False

    def test_exchanges_with_healthy_pool_and_key(self) -> None:
        """With a healthy pool and API key, exchange shows connected."""
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)
        env = {"BINANCE_API_KEY": "secret123"}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            binance = next(e for e in data if e["id"] == "binance")
            assert binance["connected"] is True
            assert binance["api_key_set"] is True
            assert binance["last_sync"] == "Active"

    def test_exchanges_pool_ping_fails(self) -> None:
        """When pool ping fails, exchanges show not connected even with keys."""
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(side_effect=RuntimeError("Ping failed"))
        env = {"BINANCE_API_KEY": "secret123"}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            binance = next(e for e in data if e["id"] == "binance")
            assert binance["connected"] is False

    def test_exchanges_all_four_present(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/system/exchanges")
        assert resp.status_code == 200
        data = resp.json()
        ids = {e["id"] for e in data}
        assert ids == {"binance", "bybit", "kraken", "okx"}


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestSystemHealth:
    def test_health_no_pool_no_redis(self) -> None:
        """Without pool and without REDIS_URL, both services are down."""
        env = {"REDIS_URL": ""}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["overall"] == "degraded"
            assert len(data["services"]) == 2
            db_svc = next(s for s in data["services"] if s["service"] == "TimescaleDB")
            assert db_svc["status"] == "down"
            redis_svc = next(s for s in data["services"] if s["service"] == "Redis")
            assert redis_svc["status"] == "down"

    def test_health_db_healthy(self) -> None:
        """When DB pool ping succeeds, TimescaleDB shows healthy with latency."""
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)
        env = {"REDIS_URL": ""}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            db_svc = next(s for s in data["services"] if s["service"] == "TimescaleDB")
            assert db_svc["status"] == "healthy"
            assert db_svc["latency_ms"] is not None
            assert db_svc["latency_ms"] >= 0

    def test_health_db_exception(self) -> None:
        """When DB pool raises, TimescaleDB shows down."""
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(side_effect=RuntimeError("Connection refused"))
        env = {"REDIS_URL": ""}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            db_svc = next(s for s in data["services"] if s["service"] == "TimescaleDB")
            assert db_svc["status"] == "down"
            assert db_svc["latency_ms"] is None

    def test_health_redis_healthy(self) -> None:
        """When Redis ping succeeds, Redis shows healthy."""
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.close = AsyncMock()

        env = {"REDIS_URL": "redis://localhost:6379"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch("redis.asyncio.from_url", return_value=mock_redis),
        ):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            redis_svc = next(s for s in data["services"] if s["service"] == "Redis")
            assert redis_svc["status"] == "healthy"
            assert redis_svc["latency_ms"] is not None

    def test_health_redis_exception(self) -> None:
        """When Redis ping fails, Redis shows down."""
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))
        mock_redis.close = AsyncMock()

        env = {"REDIS_URL": "redis://localhost:6379"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch("redis.asyncio.from_url", return_value=mock_redis),
        ):
            client = TestClient(_make_app(pool=None))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            redis_svc = next(s for s in data["services"] if s["service"] == "Redis")
            assert redis_svc["status"] == "down"

    def test_health_overall_healthy_when_all_healthy(self) -> None:
        """When all services are healthy, overall is healthy."""
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)

        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.close = AsyncMock()

        env = {"REDIS_URL": "redis://localhost:6379"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch("redis.asyncio.from_url", return_value=mock_redis),
        ):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["overall"] == "healthy"

    def test_health_overall_degraded_when_any_down(self) -> None:
        """When any service is down, overall is degraded."""
        # DB is up, Redis is down (no REDIS_URL)
        pool, conn = _make_mock_pool()
        conn.fetchval = AsyncMock(return_value=1)
        env = {"REDIS_URL": ""}
        with patch.dict("os.environ", env, clear=False):
            client = TestClient(_make_app(pool=pool))
            resp = client.get("/api/system/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["overall"] == "degraded"


# ---------------------------------------------------------------------------
# Exchange connect / disconnect endpoints
# ---------------------------------------------------------------------------


class TestExchangeConnect:
    def test_connect_exchange_success(self) -> None:
        """POST connect stores credentials and returns success."""
        app = _make_app(pool=None)
        client = TestClient(app)
        resp = client.post(
            "/api/system/exchanges/binance/connect",
            json={"api_key": "key123", "api_secret": "secret456"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "binance"
        assert data["name"] == "Binance"
        assert data["connected"] is True
        assert "Successfully connected" in data["message"]

    def test_connect_exchange_with_passphrase(self) -> None:
        """POST connect with optional passphrase."""
        app = _make_app(pool=None)
        client = TestClient(app)
        resp = client.post(
            "/api/system/exchanges/okx/connect",
            json={"api_key": "key", "api_secret": "secret", "passphrase": "pass123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "okx"
        assert data["connected"] is True

    def test_connect_unknown_exchange_returns_404(self) -> None:
        """POST connect with unknown exchange ID returns 404."""
        app = _make_app(pool=None)
        client = TestClient(app)
        resp = client.post(
            "/api/system/exchanges/unknown/connect",
            json={"api_key": "key", "api_secret": "secret"},
        )
        assert resp.status_code == 404

    def test_disconnect_exchange_success(self) -> None:
        """DELETE connect removes credentials."""
        app = _make_app(pool=None)
        client = TestClient(app)
        # First connect
        client.post(
            "/api/system/exchanges/binance/connect",
            json={"api_key": "key", "api_secret": "secret"},
        )
        # Then disconnect
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert "Disconnected" in data["message"]

    def test_disconnect_unknown_exchange_returns_404(self) -> None:
        """DELETE connect with unknown exchange returns 404."""
        app = _make_app(pool=None)
        client = TestClient(app)
        resp = client.delete("/api/system/exchanges/unknown/connect")
        assert resp.status_code == 404

    def test_connect_reflects_in_exchanges_list(self) -> None:
        """After connecting, GET exchanges shows the exchange as connected."""
        env = {
            "BINANCE_API_KEY": "",
            "BYBIT_API_KEY": "",
            "KRAKEN_API_KEY": "",
            "OKX_API_KEY": "",
        }
        with patch.dict("os.environ", env, clear=False):
            app = _make_app(pool=None)
            client = TestClient(app)
            # Connect binance via the endpoint
            client.post(
                "/api/system/exchanges/binance/connect",
                json={"api_key": "key", "api_secret": "secret"},
            )
            # Check exchanges list
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            binance = next(e for e in data if e["id"] == "binance")
            assert binance["api_key_set"] is True
            assert binance["connected"] is True

    def test_disconnect_reflects_in_exchanges_list(self) -> None:
        """After disconnecting, GET exchanges shows the exchange as disconnected."""
        env = {
            "BINANCE_API_KEY": "",
            "BYBIT_API_KEY": "",
            "KRAKEN_API_KEY": "",
            "OKX_API_KEY": "",
        }
        with patch.dict("os.environ", env, clear=False):
            app = _make_app(pool=None)
            client = TestClient(app)
            # Connect then disconnect
            client.post(
                "/api/system/exchanges/binance/connect",
                json={"api_key": "key", "api_secret": "secret"},
            )
            client.delete("/api/system/exchanges/binance/connect")
            # Check exchanges list
            resp = client.get("/api/system/exchanges")
            assert resp.status_code == 200
            data = resp.json()
            binance = next(e for e in data if e["id"] == "binance")
            assert binance["api_key_set"] is False
            assert binance["connected"] is False
