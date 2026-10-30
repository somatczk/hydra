"""System routes: persisted platform config, exchange key checks, health.

The database is the in-memory ``FakeSystemDb`` (the DB boundary); the exchange is a fake ccxt
``binance`` class (the network boundary). Everything between them is the real code: the routes,
Fernet encryption, P1-E's ``build_readonly_client`` and ``ExchangeClient``, and a real
``RiskService`` for the paper_capital hand-off.
"""

from __future__ import annotations

import json
import secrets
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
import ccxt.async_support
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hydra.core.encryption import decrypt
from hydra.dashboard.routes import system
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.system import router
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore, LoadedConfig, RiskStateUnavailableError
from hydra.services.trader import LocalTrader
from tests.fixtures.universe import OFFLINE_RANKED_AT
from tests.unit.test_risk.harness import Clock, rules_cache

from .system_db import NOW, FakeSystemDb

HOOK_SECRET = secrets.token_urlsafe(12)
API_KEY = "key-" + secrets.token_hex(6)
API_SECRET = secrets.token_hex(12)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(pool: object | None = None) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    install_error_handlers(test_app)
    test_app.state.db_pool = pool
    test_app.state.system_config = None
    test_app.state.session_manager = None
    test_app.state.trader = None
    test_app.state.exchange_credentials = {}
    test_app.state.exchange_credential_errors = {}
    return test_app


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def db() -> FakeSystemDb:
    return FakeSystemDb()


@pytest.fixture()
def app(db: FakeSystemDb) -> FastAPI:
    return _make_app(pool=db)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


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


def _detail(resp: Any) -> dict[str, Any]:
    detail: dict[str, Any] = resp.json()["detail"]
    return detail


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_reads_the_seeded_rows(self, client: TestClient) -> None:
        resp = client.get("/api/system/config")
        assert resp.status_code == 200
        assert resp.json() == {
            "trading_mode": "paper",
            "default_pair": "BTC/USDT",
            "default_timeframe": "1h",
            "max_concurrent_strategies": 5,
            "paper_capital": 10000.0,
            "paper_capital_pending": None,
            "webhook_secret_set": False,
            "discord_webhook_url_set": False,
            "slack_webhook_url_set": False,
            "telegram_bot_token_set": False,
            "telegram_chat_id_set": False,
        }

    def test_stored_values_win(self, db: FakeSystemDb, client: TestClient) -> None:
        db.config["trading_mode"]["value"] = '"live"'
        db.config["paper_capital"]["value"] = "2500.5"
        data = client.get("/api/system/config").json()
        assert data["trading_mode"] == "live"
        assert data["paper_capital"] == 2500.5

    def test_no_pool_is_503(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.get("/api/system/config")
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "database_unavailable"

    def test_missing_paper_capital_row_is_503_not_10000(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        del db.config["paper_capital"]
        resp = client.get("/api/system/config")
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "system_config_missing"
        assert _detail(resp)["missing"] == ["paper_capital"]
        assert "10000" not in resp.text

    def test_invalid_trading_mode_is_503_not_paper(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        db.config["trading_mode"]["value"] = '"testnet"'
        resp = client.get("/api/system/config")
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "system_config_invalid"
        assert _detail(resp)["fields"] == ["trading_mode"]

    def test_db_error_is_503(self, db: FakeSystemDb, client: TestClient) -> None:
        db.fail = OSError("db went away")
        resp = client.get("/api/system/config")
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "database_unavailable"

    def test_env_vars_are_not_a_source(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYDRA_DEFAULT_PAIR", "ETH/USDT")
        del db.config["default_pair"]
        assert client.get("/api/system/config").status_code == 503


# ---------------------------------------------------------------------------
# PUT /config
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def test_partial_update_is_persisted_with_actor(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        resp = client.put("/api/system/config", json={"default_pair": "ETH/USDT"})
        assert resp.status_code == 200
        assert resp.json()["default_pair"] == "ETH/USDT"
        assert db.value("default_pair") == "ETH/USDT"
        assert db.config["default_pair"]["updated_by"] == "anonymous"
        assert db.value("default_timeframe") == "1h"
        assert app.state.system_config["default_pair"] == "ETH/USDT"

    def test_full_update_round_trips(self, db: FakeSystemDb, client: TestClient) -> None:
        payload = {
            "default_pair": "SOL/USDT",
            "default_timeframe": "4h",
            "max_concurrent_strategies": 7,
            "paper_capital": 2500.0,
            "live_capital": 5000.0,
        }
        assert client.put("/api/system/config", json=payload).status_code == 200
        assert {k: db.value(k) for k in payload} == payload
        fresh = TestClient(_make_app(pool=db)).get("/api/system/config").json()
        assert {k: fresh[k] for k in payload if k != "live_capital"} == {
            k: v for k, v in payload.items() if k != "live_capital"
        }
        assert db.value("live_capital") == 5000.0

    def test_trading_mode_is_ignored_and_stays_paper(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        """Must-fix #3: the legacy row is unwritable via the API (the armed row is the
        single live gate) — a PUT carrying trading_mode writes nothing at all."""
        resp = client.put("/api/system/config", json={"trading_mode": "live"})
        assert resp.status_code == 200
        assert resp.json()["trading_mode"] == "paper"
        assert db.value("trading_mode") == "paper"
        assert not any(sql.startswith("INSERT") for sql in db.statements)

    def test_empty_update_returns_current_without_writing(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        resp = client.put("/api/system/config", json={})
        assert resp.status_code == 200
        assert resp.json()["paper_capital"] == 10000.0
        assert not any(sql.startswith("INSERT") for sql in db.statements)

    def test_secret_is_encrypted_at_rest_and_write_only(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        resp = client.put("/api/system/config", json={"webhook_secret": HOOK_SECRET})
        assert resp.status_code == 200
        assert resp.json()["webhook_secret_set"] is True
        assert HOOK_SECRET not in resp.text
        raw = db.value("webhook_secret")
        assert raw != HOOK_SECRET
        assert decrypt(raw) == HOOK_SECRET
        assert app.state.system_config["webhook_secret"] == HOOK_SECRET
        assert HOOK_SECRET not in client.get("/api/system/config").text

    def test_empty_string_clears_a_secret(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        client.put("/api/system/config", json={"webhook_secret": HOOK_SECRET})
        resp = client.put("/api/system/config", json={"webhook_secret": ""})
        assert resp.json()["webhook_secret_set"] is False
        assert "webhook_secret" not in db.config
        assert "webhook_secret" not in app.state.system_config

    def test_undecryptable_secret_reads_as_unset(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.put("/api/system/config", json={"webhook_secret": HOOK_SECRET})
        monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
        resp = client.get("/api/system/config")
        assert resp.status_code == 200
        assert resp.json()["webhook_secret_set"] is False

    def test_save_failure_is_503_and_memory_unchanged(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.fail, db.fail_on = OSError("db went away"), "INSERT INTO system_config"
        resp = client.put("/api/system/config", json={"default_pair": "ETH/USDT"})
        assert resp.status_code == 503
        assert db.value("default_pair") == "BTC/USDT"
        assert app.state.system_config is None

    def test_no_pool_is_503(self, client_no_pool: TestClient) -> None:
        resp = client_no_pool.put("/api/system/config", json={"default_pair": "ETH/USDT"})
        assert resp.status_code == 503

    def test_default_pair_outside_the_universe_is_422_and_writes_nothing(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        """The write gate: a non-member default_pair is refused, nothing is persisted and
        the stored universe row is only ever read."""
        resp = client.put("/api/system/config", json={"default_pair": "FOOBAR"})
        assert resp.status_code == 422
        assert _detail(resp) == {
            "code": "symbol_not_in_universe",
            "message": (
                "FOOBAR is not in the current top-100 universe "
                f"(ranked at {OFFLINE_RANKED_AT.isoformat()}); running sessions and stored "
                "strategies are unaffected"
            ),
            "state": "invalid",
            "symbol": "FOOBAR",
            "universe_ranked_at": OFFLINE_RANKED_AT.isoformat(),
            "universe_count": 3,
        }
        assert db.value("default_pair") == "BTC/USDT"
        # the gate reads the row; an INSERT would have stamped updated_by on it
        assert db.config["universe"]["updated_by"] is None
        assert client.get("/api/system/config").json()["default_pair"] == "BTC/USDT"

    def test_default_pair_slash_form_normalizes_and_saves(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        resp = client.put("/api/system/config", json={"default_pair": "BTC/USDT"})
        assert resp.status_code == 200
        assert resp.json()["default_pair"] == "BTC/USDT"
        assert db.value("default_pair") == "BTC/USDT"
        assert db.config["universe"]["updated_by"] is None

    def test_default_pair_with_no_universe_row_fails_closed(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        """Before the first refresh lands, no default_pair is writable — never a guess."""
        del db.config["universe"]
        resp = client.put("/api/system/config", json={"default_pair": "ETH/USDT"})
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "universe_unavailable"
        assert db.value("default_pair") == "BTC/USDT"


class _SystemConfigStore(InMemoryRiskStateStore):
    """The RiskService store, reading paper_capital from the same system_config rows."""

    def __init__(self, db: FakeSystemDb) -> None:
        super().__init__(paper_capital=Decimal("10000"))
        self.db = db
        self.fail: BaseException | None = None

    async def load_config(self) -> LoadedConfig:
        if self.fail is not None:
            raise self.fail
        self.paper_capital = Decimal(str(self.db.value("paper_capital")))
        return await super().load_config()


def _manager(risk: RiskService) -> SimpleNamespace:
    """P1-E's SessionManager as the trader backend sees it: only ``.risk`` (no reload hook)."""
    return SimpleNamespace(risk=risk)


async def _risk(db: FakeSystemDb, *, recovered: bool) -> tuple[RiskService, _SystemConfigStore]:
    clock = Clock()
    store = _SystemConfigStore(db)
    svc = RiskService(store, clock=clock, market_rules=rules_cache(clock))
    await svc.load()
    if recovered:
        await svc.recovery_complete()
    return svc, store


class TestPaperCapitalChange:
    async def test_change_goes_through_the_audited_paper_reset(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 25000})
        assert resp.status_code == 200
        assert resp.json()["paper_capital"] == 25000.0
        assert resp.json()["paper_capital_pending"] is None
        assert svc.paper_capital == Decimal("25000")
        assert [(e.kind, e.actor) for e in store.events] == [("paper_reset", "anonymous")]
        assert store.events[0].details["old_capital"] == Decimal("10000")

    async def test_change_while_recovering_is_reported_pending(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        svc, store = await _risk(db, recovered=False)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 25000})
        assert resp.status_code == 200
        assert resp.json()["paper_capital"] == 25000.0
        assert resp.json()["paper_capital_pending"] == 25000.0
        assert svc.paper_capital == Decimal("10000")
        assert store.events == []

    async def test_same_capital_does_not_reset(self, db: FakeSystemDb, app: FastAPI) -> None:
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 10000})
        assert resp.json()["paper_capital_pending"] is None
        assert store.events == []

    async def test_reload_failure_is_503_after_the_save(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        store.fail = RiskStateUnavailableError("store down")
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 25000})
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "risk_reload_failed"
        assert db.value("paper_capital") == 25000
        assert svc.paper_capital == Decimal("10000")

    async def test_unexpected_reload_error_is_the_503_envelope(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        store.fail = AttributeError("contract drift")
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 25000})
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "risk_reload_failed"

    async def test_retry_after_a_failed_reload_applies_the_reset(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        client = TestClient(app)
        store.fail = RiskStateUnavailableError("store down")
        assert client.put("/api/system/config", json={"paper_capital": 25000}).status_code == 503
        between = client.get("/api/system/config").json()
        assert between["paper_capital"] == 25000.0
        assert between["paper_capital_pending"] == 25000.0
        store.fail = None
        resp = client.put("/api/system/config", json={"paper_capital": 25000})
        assert resp.status_code == 200
        assert resp.json()["paper_capital_pending"] is None
        assert svc.paper_capital == Decimal("25000")
        assert [e.kind for e in store.events] == ["paper_reset"]
        assert store.events[0].details["new_capital"] == Decimal("25000")

    async def test_capital_above_the_bound_is_422_and_writes_nothing(
        self, db: FakeSystemDb, app: FastAPI
    ) -> None:
        """The save bound: far above any real capital, orders below the cents-quantize limit."""
        svc, store = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 1e16})
        assert resp.status_code == 422
        detail = _detail(resp)
        assert detail["code"] == "validation_error"
        assert "paper_capital" in detail["message"]
        assert "1000000000000000" in detail["message"]
        assert db.value("paper_capital") == 10000
        assert svc.paper_capital == Decimal("10000")
        assert store.events == []

    async def test_capital_within_the_bound_saves(self, db: FakeSystemDb, app: FastAPI) -> None:
        svc, _ = await _risk(db, recovered=True)
        app.state.trader = LocalTrader(app.state.db_pool, _manager(svc))
        resp = TestClient(app).put("/api/system/config", json={"paper_capital": 1_000_000_000})
        assert resp.status_code == 200
        assert resp.json()["paper_capital"] == 1_000_000_000
        assert db.value("paper_capital") == 1_000_000_000
        assert svc.paper_capital == Decimal("1000000000")


async def test_get_paper_capital_has_no_fallback(db: FakeSystemDb) -> None:
    assert await system.get_paper_capital(db) == 10000.0
    del db.config["paper_capital"]
    with pytest.raises(Exception) as excinfo:
        await system.get_paper_capital(db)
    assert getattr(excinfo.value, "status_code", None) == 503


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
# Exchanges: state, key check, connect / disconnect
# ---------------------------------------------------------------------------


class FakeBinance:
    """Stand-in for ``ccxt.async_support.binance``: the network boundary."""

    error: ClassVar[BaseException | None] = None
    restrictions: ClassVar[dict[str, Any] | BaseException | None] = {
        "ipRestrict": False,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": True,
    }
    instances: ClassVar[list[FakeBinance]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        self.balance_calls = 0
        self.restriction_calls = 0
        self.order_symbols: list[str] = []
        FakeBinance.instances.append(self)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol is None:
            raise ccxt.ExchangeError("binance fetchOpenOrders() WARNING: no symbol")
        if FakeBinance.error is not None:
            raise FakeBinance.error
        self.order_symbols.append(symbol)
        return [{"id": f"o-{symbol}", "symbol": symbol}]

    async def fetch_balance(self) -> dict[str, Any]:
        self.balance_calls += 1
        if FakeBinance.error is not None:
            raise FakeBinance.error
        return {"free": {"USDT": 12.5, "BTC": 0.0}}

    async def close(self) -> None:
        self.closed = True


async def _fake_get_account_api_restrictions(fake: FakeBinance, *args: Any) -> dict[str, Any]:
    fake.restriction_calls += 1
    if FakeBinance.error is not None:
        raise FakeBinance.error
    restrictions = FakeBinance.restrictions
    if isinstance(restrictions, BaseException):
        raise restrictions
    return dict(restrictions)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions


BYBIT_KEY_INFO: dict[str, Any] = {
    "retCode": 0,
    "retMsg": "",
    "result": {
        "id": "2208369",
        "note": "testnet",
        "readOnly": 0,
        "permissions": {
            "ContractTrade": [],
            "Spot": ["SpotTrade"],
            "Wallet": ["AccountTransfer"],
            "Options": [],
        },
        "ips": ["91.0.0.1"],
    },
}


class FakeBybit:
    """Stand-in for ``ccxt.async_support.bybit``: the Settings-connect network boundary.

    ``permissions`` answers the GET /v5/user/query-api document (the Bybit apikey-info
    permission model) — spot trading is 'SpotTrade' in permissions['Spot'] and a
    withdraw-capable key carries 'Withdraw' in permissions['Wallet']."""

    error: ClassVar[BaseException | None] = None
    key_info: ClassVar[dict[str, Any]] = dict(BYBIT_KEY_INFO)
    instances: ClassVar[list[FakeBybit]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.closed = False
        self.balance_calls = 0
        self.query_calls = 0
        FakeBybit.instances.append(self)

    async def fetch_balance(self) -> dict[str, Any]:
        self.balance_calls += 1
        if FakeBybit.error is not None:
            raise FakeBybit.error
        return {"free": {"USDT": 12.5, "BTC": 0.0}}

    async def close(self) -> None:
        self.closed = True


async def _fake_query_api(fake: FakeBybit, *args: Any) -> dict[str, Any]:
    fake.query_calls += 1
    if FakeBybit.error is not None:
        raise FakeBybit.error
    return dict(FakeBybit.key_info)


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBybit.privateGetV5UserQueryApi = _fake_query_api


@pytest.fixture()
def bybit(monkeypatch: pytest.MonkeyPatch) -> type[FakeBybit]:
    """The bybit venue's client stand-in; a trade-only spot key by default (P7-F)."""
    FakeBybit.error = None
    FakeBybit.key_info = dict(BYBIT_KEY_INFO)
    FakeBybit.instances = []
    monkeypatch.setattr(ccxt.async_support, "bybit", FakeBybit)
    return FakeBybit


@pytest.fixture()
def binance(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinance]:
    FakeBinance.error = None
    FakeBinance.restrictions = {
        "ipRestrict": False,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": True,
    }
    FakeBinance.instances = []
    monkeypatch.setattr(ccxt.async_support, "binance", FakeBinance)
    return FakeBinance


@pytest.fixture()
def binanceusdm(monkeypatch: pytest.MonkeyPatch) -> type[FakeBinance]:
    """The USD⌂-M perps venue's client stand-in; futures-enabled by default (mainnet)."""
    FakeBinance.error = None
    FakeBinance.restrictions = {
        "ipRestrict": False,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": False,
        "enableFutures": True,
    }
    FakeBinance.instances = []
    monkeypatch.setattr(ccxt.async_support, "binanceusdm", FakeBinance)
    return FakeBinance


def _connect(client: TestClient, exchange: str = "binance", **extra: str) -> Any:
    return client.post(
        f"/api/system/exchanges/{exchange}/connect",
        json={"api_key": API_KEY, "api_secret": API_SECRET, **extra},
    )


class _Trader:
    """The disconnect gate's session read: a minimal TraderBackend stand-in."""

    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or []

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]


def _live_session(session_id: str = "s-live") -> dict[str, Any]:
    return {"session_id": session_id, "trading_mode": "live", "status": "running"}


class TestExchanges:
    def test_states_without_stored_keys(self, client: TestClient) -> None:
        data = client.get("/api/system/exchanges").json()
        assert [e["id"] for e in data] == ["binance", "binanceusdm", "bybit", "kraken", "okx"]
        assert [e["supported"] for e in data] == [True, True, True, False, False]
        for entry in data:
            assert entry["credential_state"] == "missing"
            assert entry["api_key_set"] is False
            assert entry["connected"] is False
            assert entry["last_sync"] is None
            assert entry["environment"] == "testnet"
        assert data[1]["name"] == "Binance USD⌂-M"

    def test_env_keys_are_not_a_badge(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BINANCE_API_KEY", "from-env")
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["api_key_set"] is False
        assert binance["connected"] is False

    def test_validated_keys_are_connected_with_their_check_time(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        db.credentials["binance"] = {"validated_at": NOW, "validated_environment": "testnet"}
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["credential_state"] == "ok"
        assert binance["connected"] is True
        assert binance["last_sync"] == "2026-10-20T12:00:00+00:00"
        assert binance["validated_environment"] == "testnet"

    def test_keys_checked_on_testnet_are_not_connected_on_mainnet(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db.credentials["binance"] = {"validated_at": NOW, "validated_environment": "testnet"}
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["environment"] == "mainnet"
        assert binance["validated_environment"] == "testnet"
        assert binance["connected"] is False

    def test_never_validated_keys_are_not_connected(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        db.credentials["binance"] = {"validated_at": None}
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["credential_state"] == "ok"
        assert binance["api_key_set"] is True
        assert binance["connected"] is False
        assert binance["last_sync"] is None

    def test_undecryptable_keys_are_reported(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.credentials["bybit"] = {"validated_at": NOW, "validated_environment": "testnet"}
        app.state.exchange_credential_errors = {"bybit": "undecryptable"}
        bybit = client.get("/api/system/exchanges").json()[2]
        assert bybit["credential_state"] == "undecryptable"
        assert bybit["connected"] is False

    def test_db_error_is_503(self, db: FakeSystemDb, client: TestClient) -> None:
        db.fail = OSError("db went away")
        resp = client.get("/api/system/exchanges")
        assert resp.status_code == 503

    def test_no_pool_is_503(self, client_no_pool: TestClient) -> None:
        assert client_no_pool.get("/api/system/exchanges").status_code == 503


@pytest.mark.usefixtures("binance")
class TestExchangeConnect:
    def test_valid_keys_are_checked_then_stored_encrypted(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        resp = _connect(client)
        assert resp.status_code == 200
        assert resp.json() == {
            "id": "binance",
            "name": "Binance",
            "connected": True,
            "message": "Binance testnet accepted these keys; they are stored",
            "validated_at": "2026-10-20T12:00:00+00:00",
            "environment": "testnet",
            "trade_enabled": None,
            "ip_restrict": None,
        }
        [probe] = FakeBinance.instances
        assert probe.balance_calls == 1
        assert probe.restriction_calls == 0  # the apiRestrictions read is mainnet-only
        assert probe.closed is True
        assert probe.opts["apiKey"] == API_KEY
        assert probe.opts["secret"] == API_SECRET
        assert probe.opts["sandbox"] is True
        stored = db.credentials["binance"]
        assert stored["encrypted_key"] != API_KEY
        assert decrypt(stored["encrypted_key"]) == API_KEY
        assert stored["validated_at"] == NOW
        assert stored["validated_environment"] == "testnet"
        assert stored["validated_permissions"] is None
        assert app.state.exchange_credentials["binance"]["api_key"] == API_KEY

    def test_mainnet_when_testnet_is_off(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)
        resp = _connect(client)
        assert resp.status_code == 200
        assert resp.json()["environment"] == "mainnet"
        assert resp.json()["message"].startswith("Binance mainnet accepted")
        assert (resp.json()["trade_enabled"], resp.json()["ip_restrict"]) == (True, False)
        assert "sandbox" not in FakeBinance.instances[0].opts
        [probe] = FakeBinance.instances
        assert probe.restriction_calls == 1
        assert json.loads(db.credentials["binance"]["validated_permissions"]) == {
            "enable_spot_and_margin_trading": True,
            "enable_withdrawals": False,
            "ip_restrict": False,
        }

    def test_mainnet_withdrawal_keys_are_refused_and_nothing_is_stored(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)
        FakeBinance.restrictions = {
            "ipRestrict": True,
            "enableWithdrawals": True,
            "enableSpotAndMarginTrading": True,
        }
        resp = _connect(client)
        assert resp.status_code == 422
        assert _detail(resp)["code"] == "credentials_can_withdraw"
        assert _detail(resp)["state"] == "invalid"
        assert "can withdraw" in _detail(resp)["message"]
        assert db.credentials == {}
        assert not any(sql.startswith("INSERT") for sql in db.statements)
        assert app.state.exchange_credentials == {}
        assert FakeBinance.instances[0].closed is True

    def test_mainnet_read_only_keys_connect_with_the_record(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)
        FakeBinance.restrictions = {
            "ipRestrict": True,
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": False,
        }
        resp = _connect(client)
        assert resp.status_code == 200
        assert (resp.json()["trade_enabled"], resp.json()["ip_restrict"]) == (False, True)
        assert json.loads(db.credentials["binance"]["validated_permissions"]) == {
            "enable_spot_and_margin_trading": False,
            "enable_withdrawals": False,
            "ip_restrict": True,
        }

    @pytest.mark.parametrize(
        "error", [ccxt.AuthenticationError("bad key"), ccxt.PermissionDenied("no read")]
    )
    def test_rejected_keys_are_422_and_nothing_is_stored(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient, error: BaseException
    ) -> None:
        FakeBinance.error = error
        resp = _connect(client)
        assert resp.status_code == 422
        assert _detail(resp)["code"] == "credentials_rejected"
        assert _detail(resp)["state"] == "invalid"
        assert _detail(resp)["message"].startswith("Binance testnet rejected these API keys")
        assert str(error) not in resp.text
        assert db.credentials == {}
        assert not any(sql.startswith("INSERT") for sql in db.statements)
        assert app.state.exchange_credentials == {}
        assert FakeBinance.instances[0].closed is True

    @pytest.mark.parametrize(
        "error",
        [ccxt.NetworkError("dns"), ccxt.ExchangeNotAvailable("maintenance"), TimeoutError()],
    )
    def test_unreachable_exchange_is_502_and_nothing_is_stored(
        self, db: FakeSystemDb, client: TestClient, error: BaseException
    ) -> None:
        FakeBinance.error = error
        resp = _connect(client)
        assert resp.status_code == 502
        assert _detail(resp)["code"] == "exchange_unreachable"
        assert db.credentials == {}

    def test_slow_exchange_times_out_as_unreachable(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def hang(self: FakeBinance) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {}

        monkeypatch.setattr(FakeBinance, "fetch_balance", hang)
        monkeypatch.setattr(system, "CONNECT_TIMEOUT_S", 0.01)
        resp = _connect(client)
        assert resp.status_code == 502
        assert db.credentials == {}

    @pytest.mark.parametrize("exchange", ["kraken", "okx"])
    def test_other_exchanges_are_400_before_any_call(
        self, db: FakeSystemDb, client: TestClient, exchange: str
    ) -> None:
        resp = _connect(client, exchange, passphrase=API_SECRET)
        assert resp.status_code == 400
        assert _detail(resp)["code"] == "unsupported_exchange"
        assert "not scheduled" in _detail(resp)["message"]
        assert FakeBinance.instances == []
        assert db.credentials == {}

    def test_unknown_exchange_is_404(self, client: TestClient) -> None:
        resp = _connect(client, "unknown")
        assert resp.status_code == 404
        assert _detail(resp)["state"] == "not_found"

    def test_no_db_is_503_before_the_exchange_call(self, client_no_pool: TestClient) -> None:
        resp = _connect(client_no_pool)
        assert resp.status_code == 503
        assert FakeBinance.instances == []

    def test_persist_failure_is_503_and_keeps_nothing(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.fail, db.fail_on = ConnectionResetError("db went away"), "INSERT"
        resp = _connect(client)
        assert resp.status_code == 503
        assert "verified" not in resp.text
        assert app.state.exchange_credentials == {}

    def test_reconnect_evicts_the_cached_client(self, app: FastAPI, client: TestClient) -> None:
        assert _connect(client).status_code == 200
        assert client.get("/api/system/exchanges/binance/balance").json() == {
            "exchange_id": "binance",
            "balances": {"USDT": 12.5},
        }
        cached = app.state._exchange_clients["binance"]
        client.post(
            "/api/system/exchanges/binance/connect",
            json={"api_key": "new-key", "api_secret": "new-secret"},
        )
        assert "binance" not in app.state._exchange_clients
        assert client.get("/api/system/exchanges/binance/balance").status_code == 200
        assert app.state._exchange_clients["binance"] is not cached
        assert FakeBinance.instances[-1].opts["apiKey"] == "new-key"

    def test_connect_clears_an_undecryptable_report(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        app.state.exchange_credential_errors = {"binance": "undecryptable"}
        assert _connect(client).status_code == 200
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["credential_state"] == "ok"
        assert binance["connected"] is True

    def test_disconnect_removes_keys(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        _connect(client)
        app.state.trader = _Trader()
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert resp.status_code == 200
        assert resp.json()["connected"] is False
        assert db.credentials == {}
        assert "binance" not in app.state.exchange_credentials
        binance = client.get("/api/system/exchanges").json()[0]
        assert binance["credential_state"] == "missing"

    def test_disconnect_delete_failure_is_503_and_keeps_keys(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        _connect(client)
        db.fail, db.fail_on = OSError("db went away"), "DELETE"
        app.state.trader = _Trader()
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert resp.status_code == 503
        assert "binance" in app.state.exchange_credentials

    def test_disconnect_trade_capable_keys_need_the_ceremony(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.credentials["binance"] = {
            "validated_at": NOW,
            "validated_environment": "mainnet",
            "validated_permissions": json.dumps(
                {
                    "enable_spot_and_margin_trading": True,
                    "enable_withdrawals": False,
                    "ip_restrict": False,
                }
            ),
        }
        app.state.trader = _Trader()
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert (resp.status_code, _detail(resp)["code"]) == (409, "credential_delete_gated")
        assert "POST /api/live/credential-delete" in _detail(resp)["message"]
        assert "binance" in db.credentials  # nothing was deleted

    def test_disconnect_null_permission_keys_under_a_running_live_session_are_gated(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        """R31 A1: a pre-P3-C null-permission row must never delete under live money."""
        db.credentials["binance"] = {
            "validated_at": NOW,
            "validated_environment": "mainnet",
            "validated_permissions": None,
        }
        app.state.trader = _Trader([_live_session()])
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert (resp.status_code, _detail(resp)["code"]) == (409, "credential_delete_gated")
        assert _detail(resp)["running_live_sessions"] == ["s-live"]
        assert "binance" in db.credentials  # nothing was deleted

    def test_disconnect_read_only_keys_with_no_live_session_delete_directly(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.credentials["binance"] = {
            "validated_at": NOW,
            "validated_environment": "mainnet",
            "validated_permissions": json.dumps(
                {
                    "enable_spot_and_margin_trading": False,
                    "enable_withdrawals": False,
                    "ip_restrict": True,
                }
            ),
        }
        app.state.trader = _Trader()
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert resp.status_code == 200
        assert resp.json()["connected"] is False
        assert db.credentials == {}

    def test_disconnect_fails_closed_when_sessions_are_unreadable(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        """No bound trader means the session list is unreadable: refuse, never delete."""
        db.credentials["binance"] = {
            "validated_at": NOW,
            "validated_environment": "testnet",
            "validated_permissions": None,
        }
        resp = client.delete("/api/system/exchanges/binance/connect")
        assert (resp.status_code, _detail(resp)["code"]) == (503, "engine_not_ready")
        assert "binance" in db.credentials

    def test_disconnect_unknown_exchange_is_404(self, client: TestClient) -> None:
        assert client.delete("/api/system/exchanges/unknown/connect").status_code == 404

    def test_balance_for_unsupported_exchange_is_400(self, client: TestClient) -> None:
        resp = client.get("/api/system/exchanges/kraken/balance")
        assert resp.status_code == 400

    def test_balance_without_keys_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/system/exchanges/binance/balance")
        assert resp.status_code == 404
        assert _detail(resp)["code"] == "credentials_not_found"


@pytest.mark.usefixtures("binanceusdm")
class TestConnectBinanceUsdm:
    """P4-F closes the futures-keys connect gap: binanceusdm connects through Settings,
    with the fail-closed futures-permission validation the perps arm re-fetches live."""

    def _mainnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)

    def test_testnet_keys_are_checked_then_stored_for_the_perps_venue(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        resp = _connect(client, "binanceusdm")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "binanceusdm"
        assert body["name"] == "Binance USD⌂-M"
        assert body["message"] == "Binance USD⌂-M testnet accepted these keys; they are stored"
        [probe] = FakeBinance.instances
        assert probe.balance_calls == 1
        assert probe.restriction_calls == 0  # the apiRestrictions read is mainnet-only
        assert probe.opts["sandbox"] is True
        assert probe.closed is True
        stored = db.credentials["binanceusdm"]
        assert decrypt(stored["encrypted_key"]) == API_KEY
        assert stored["validated_environment"] == "testnet"
        assert stored["validated_permissions"] is None
        assert app.state.exchange_credentials["binanceusdm"]["api_key"] == API_KEY

    def test_mainnet_futures_keys_connect_with_the_enable_futures_record(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mainnet(monkeypatch)
        resp = _connect(client, "binanceusdm")
        assert resp.status_code == 200
        assert resp.json()["trade_enabled"] is True  # the futures flag, spot off
        assert json.loads(db.credentials["binanceusdm"]["validated_permissions"]) == {
            "enable_spot_and_margin_trading": False,
            "enable_withdrawals": False,
            "ip_restrict": False,
            "enable_futures": True,
        }

    def test_mainnet_keys_without_futures_are_refused_and_nothing_is_stored(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mainnet(monkeypatch)
        FakeBinance.restrictions = {
            "ipRestrict": False,
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": False,
            "enableFutures": False,
        }
        resp = _connect(client, "binanceusdm")
        assert resp.status_code == 422
        assert _detail(resp)["code"] == "credentials_not_futures_capable"
        assert _detail(resp)["state"] == "invalid"
        assert "cannot trade futures" in _detail(resp)["message"]
        assert db.credentials == {}
        assert not any(sql.startswith("INSERT") for sql in db.statements)
        assert app.state.exchange_credentials == {}
        assert FakeBinance.instances[0].closed is True

    def test_mainnet_withdrawal_keys_are_refused_on_the_perps_venue_too(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mainnet(monkeypatch)
        FakeBinance.restrictions = {
            "ipRestrict": False,
            "enableWithdrawals": True,
            "enableSpotAndMarginTrading": False,
            "enableFutures": True,
        }
        resp = _connect(client, "binanceusdm")
        assert resp.status_code == 422
        assert _detail(resp)["code"] == "credentials_can_withdraw"
        assert db.credentials == {}

    def test_a_futures_trade_key_needs_the_ceremony_to_delete(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient
    ) -> None:
        db.credentials["binanceusdm"] = {
            "validated_at": NOW,
            "validated_environment": "mainnet",
            "validated_permissions": json.dumps(
                {
                    "enable_spot_and_margin_trading": False,
                    "enable_withdrawals": False,
                    "ip_restrict": False,
                    "enable_futures": True,
                }
            ),
        }
        app.state.trader = _Trader()
        resp = client.delete("/api/system/exchanges/binanceusdm/connect")
        assert (resp.status_code, _detail(resp)["code"]) == (409, "credential_delete_gated")
        assert "binanceusdm" in db.credentials  # nothing was deleted


@pytest.mark.usefixtures("bybit")
class TestConnectBybit:
    """P7-F: bybit connects through Settings — the mainnet query-api read stores the
    same canonical record, a withdraw-capable key is refused storing nothing, and
    testnet stores no permission record at all."""

    def _mainnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = system.current_config().model_copy(deep=True)
        cfg.trading.testnet = False
        monkeypatch.setattr(system, "current_config", lambda: cfg)

    def test_testnet_keys_are_checked_then_stored(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        resp = _connect(client, "bybit")
        assert resp.status_code == 200
        body = resp.json()
        assert (body["id"], body["name"]) == ("bybit", "Bybit")
        assert body["message"] == "Bybit testnet accepted these keys; they are stored"
        [probe] = FakeBybit.instances
        assert probe.balance_calls == 1
        assert probe.query_calls == 0  # the query-api read is mainnet-only
        assert probe.opts["sandbox"] is True
        assert probe.closed is True
        stored = db.credentials["bybit"]
        assert decrypt(stored["encrypted_key"]) == API_KEY
        assert stored["validated_environment"] == "testnet"
        assert stored["validated_permissions"] is None

    def test_mainnet_keys_connect_with_the_canonical_record(
        self, db: FakeSystemDb, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mainnet(monkeypatch)
        resp = _connect(client, "bybit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trade_enabled"] is True
        assert body["ip_restrict"] is True
        assert json.loads(db.credentials["bybit"]["validated_permissions"]) == {
            "enable_spot_and_margin_trading": True,
            "enable_withdrawals": False,
            "ip_restrict": True,
        }
        [probe] = FakeBybit.instances
        assert probe.query_calls == 1  # the v5 query-api read, not a Binance sapi call
        assert probe.closed is True

    def test_mainnet_withdrawal_keys_are_refused_and_nothing_is_stored(
        self, db: FakeSystemDb, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mainnet(monkeypatch)
        FakeBybit.key_info = {
            "retCode": 0,
            "result": {
                "permissions": {"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer", "Withdraw"]},
                "ips": [],
            },
        }
        resp = _connect(client, "bybit")
        assert resp.status_code == 422
        assert _detail(resp)["code"] == "credentials_can_withdraw"
        assert "Bybit mainnet reports these keys can withdraw" in _detail(resp)["message"]
        assert db.credentials == {}
        assert not any(sql.startswith("INSERT") for sql in db.statements)
        assert app.state.exchange_credentials == {}
        assert FakeBybit.instances[0].closed is True


@pytest.mark.usefixtures("binance")
class TestExchangeReads:
    def test_orders_are_fetched_per_symbol(self, db: FakeSystemDb, client: TestClient) -> None:
        assert _connect(client).status_code == 200
        # the load_running query answers running rows only (the SQL filters)
        db.running_sessions = [
            {
                "id": "s-run",
                "strategy_id": "saw",
                "trading_mode": "paper",
                "status": "running",
                "symbols": ["BTCUSDT", "ETHUSDT"],
            }
        ]
        resp = client.get("/api/system/exchanges/binance/orders")
        assert resp.status_code == 200
        assert resp.json()["symbols"] == ["BTC/USDT", "ETHUSDT"]
        assert [o["id"] for o in resp.json()["orders"]] == ["o-BTC/USDT", "o-ETHUSDT"]
        assert FakeBinance.instances[-1].order_symbols == ["BTC/USDT", "ETHUSDT"]

    def test_running_sessions_read_failure_is_a_503_envelope(
        self, db: FakeSystemDb, client: TestClient
    ) -> None:
        assert _connect(client).status_code == 200
        db.fail, db.fail_on = OSError("db went away"), "FROM trading_sessions"
        resp = client.get("/api/system/exchanges/binance/orders")
        assert resp.status_code == 503
        assert _detail(resp)["code"] == "database_unavailable"
        assert FakeBinance.instances[-1].order_symbols == []

    def test_failed_read_is_502_and_closes_the_cached_client(
        self, app: FastAPI, client: TestClient
    ) -> None:
        assert _connect(client).status_code == 200
        assert client.get("/api/system/exchanges/binance/balance").status_code == 200
        cached = FakeBinance.instances[-1]
        FakeBinance.error = ccxt.NetworkError("dns")
        resp = client.get("/api/system/exchanges/binance/orders")
        assert resp.status_code == 502
        assert _detail(resp)["message"] == "Request to Binance testnet failed"
        assert cached.closed is True
        assert "binance" not in app.state._exchange_clients

    def test_positions_on_a_spot_account_are_501_without_a_call(self, client: TestClient) -> None:
        assert _connect(client).status_code == 200
        calls = len(FakeBinance.instances)
        resp = client.get("/api/system/exchanges/binance/positions")
        assert resp.status_code == 501
        assert _detail(resp)["code"] == "spot_no_positions"
        assert len(FakeBinance.instances) == calls
