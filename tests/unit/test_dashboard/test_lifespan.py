"""Engine lifespan: config and key errors stop startup before any DB connect; no silent no-pool."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import ValidationError

from hydra.core.config import ConfigError, HydraConfig
from hydra.core.encryption import CredentialKeyError, encrypt
from hydra.dashboard import lifespan

MCP_TEST_TOKEN = "t" * 48
MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "hydra-tests", "version": "0.0.0"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _closed_port_dsn() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"postgresql://hydra:pw@127.0.0.1:{port}/hydra"


@pytest.fixture()
def pool_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every pool creation while still calling the real asyncpg."""
    calls: list[str] = []
    real = asyncpg.create_pool

    def spy(dsn: str, **kwargs: Any) -> Any:
        calls.append(dsn)
        return real(dsn, **kwargs)

    monkeypatch.setattr(asyncpg, "create_pool", spy)
    return calls


async def test_bad_config_aborts_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_calls: list[str]
) -> None:
    (tmp_path / "base.yaml").write_text("trading:\n  tesnet: false\n")
    monkeypatch.setenv("HYDRA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", _closed_port_dsn())
    app = FastAPI()
    with pytest.raises(ValidationError) as exc_info:
        async with lifespan.run(app):
            pytest.fail("startup must not complete")
    assert ("trading", "tesnet") in {tuple(e["loc"]) for e in exc_info.value.errors()}
    assert pool_calls == []


async def test_retired_env_name_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, pool_calls: list[str]
) -> None:
    monkeypatch.setenv("HYDRA_CORS_ORIGINS", "https://trading.example.com")
    monkeypatch.setenv("DATABASE_URL", _closed_port_dsn())
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    app = FastAPI()
    with pytest.raises(ConfigError, match="HYDRA_API__CORS_ORIGINS"):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")
    assert pool_calls == []


async def test_db_without_key_aborts_before_connect(
    monkeypatch: pytest.MonkeyPatch, pool_calls: list[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", _closed_port_dsn())
    monkeypatch.delenv("HYDRA_CREDENTIAL_KEY", raising=False)
    with pytest.raises(CredentialKeyError, match="HYDRA_CREDENTIAL_KEY"):
        async with lifespan.run(FastAPI()):
            pytest.fail("startup must not complete")
    assert pool_calls == []


async def test_malformed_key_aborts_before_connect(
    monkeypatch: pytest.MonkeyPatch, pool_calls: list[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", _closed_port_dsn())
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", "not-a-key")
    with pytest.raises(CredentialKeyError):
        async with lifespan.run(FastAPI()):
            pytest.fail("startup must not complete")
    assert pool_calls == []


async def test_pool_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch, pool_calls: list[str]
) -> None:
    dsn = _closed_port_dsn()
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(lifespan, "DB_CONNECT_BUDGET_S", 1.2)
    app = FastAPI()
    with pytest.raises(OSError):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")
    assert len(pool_calls) >= 2
    assert set(pool_calls) == {dsn}
    assert app.state.db_pool is None
    assert app.state.session_manager is None


class _Rows:
    """Minimal pool whose single query returns fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        conn = MagicMock()

        async def fetch(_sql: str) -> list[dict[str, Any]]:
            return rows

        conn.fetch = fetch
        ctx = MagicMock()

        async def enter(*_: Any) -> Any:
            return conn

        async def leave(*_: Any) -> bool:
            return False

        ctx.__aenter__ = enter
        ctx.__aexit__ = leave
        self.acquire = MagicMock(return_value=ctx)


async def test_undecryptable_credentials_are_reported_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_a, key_b = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", key_a)
    old_row = {
        "exchange_id": "binance",
        "encrypted_key": encrypt("old-key"),
        "encrypted_secret": encrypt("old-secret"),
        "encrypted_passphrase": None,
    }
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", key_b)
    new_row = {
        "exchange_id": "okx",
        "encrypted_key": encrypt("k"),
        "encrypted_secret": encrypt("s"),
        "encrypted_passphrase": encrypt("p"),
    }
    app = FastAPI()
    app.state.db_pool = _Rows([old_row, new_row])

    await lifespan._load_exchange_credentials(app)

    assert app.state.exchange_credential_errors == {"binance": "undecryptable"}
    assert app.state.exchange_credentials == {
        "okx": {"api_key": "k", "api_secret": "s", "passphrase": "p"}
    }


async def test_system_config_values_are_json_decoded() -> None:
    app = FastAPI()
    app.state.db_pool = _Rows(
        [
            {"key": "paper_capital", "value": "2500"},
            {"key": "trading_mode", "value": '"paper"'},
        ]
    )
    await lifespan._load_system_config(app)
    assert app.state.system_config == {"paper_capital": 2500, "trading_mode": "paper"}


# ---------------------------------------------------------------------------
# The MCP mount: no token, no surface; with one, the transport is served and shut down
# ---------------------------------------------------------------------------


def _mcp_mounts(app: FastAPI) -> list[Any]:
    """The routes the lifespan attached at ``/mcp`` (only the mount ever matches)."""
    return [r for r in app.router.routes if getattr(r, "path", "") == "/mcp"]


async def test_no_mcp_token_mounts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    app = FastAPI()
    async with lifespan.run(app):
        assert getattr(app.state, "mcp", None) is None
        assert not [r for r in app.router.routes if getattr(r, "path", "") == "/mcp"]


async def test_mcp_token_mounts_the_transport(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", MCP_TEST_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    app = FastAPI()
    with caplog.at_level(logging.ERROR, logger="hydra.dashboard.mcp"):
        async with lifespan.run(app):
            mount = app.state.mcp
            assert mount is not None
            assert mount.path == "/mcp"
            assert [r for r in app.router.routes if getattr(r, "path", "") == "/mcp"]
            # a request flows through the FastAPI app into the mounted transport
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/mcp/",
                    headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TEST_TOKEN}"},
                    json=MCP_INITIALIZE,
                )
                assert response.status_code == 200
                assert response.headers["mcp-session-id"]
            # leaving the block exits the single-use session manager; nothing may raise
    assert "Task group is not initialized" not in caplog.text


async def test_mcp_mount_is_unmounted_after_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the lifespan exits the surface is gone: no route, no state, /mcp 404s."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", MCP_TEST_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    app = FastAPI()
    async with lifespan.run(app):
        assert len(_mcp_mounts(app)) == 1
    assert _mcp_mounts(app) == []
    assert getattr(app.state, "mcp", None) is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/mcp/",
            headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TEST_TOKEN}"},
            json=MCP_INITIALIZE,
        )
        assert response.status_code == 404


async def test_mcp_lifespan_reentry_serves_a_fresh_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() is re-enterable on one app: the closed mount must not shadow the new one."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", MCP_TEST_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    app = FastAPI()
    for _ in range(2):
        async with lifespan.run(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/mcp/",
                    headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TEST_TOKEN}"},
                    json=MCP_INITIALIZE,
                )
                assert response.status_code == 200
                assert response.headers["mcp-session-id"]
            assert len(_mcp_mounts(app)) == 1
    assert _mcp_mounts(app) == []
    assert getattr(app.state, "mcp", None) is None


async def test_startup_failure_after_mount_still_unmounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup error after the mount must not leave a dead /mcp surface behind."""

    class BrokenMonitor:
        async def start(self) -> None:
            raise RuntimeError("monitor exploded")

        async def stop(self) -> None:
            pass

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", MCP_TEST_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    app = FastAPI()
    app.state.health_monitor_factory = lambda _app: BrokenMonitor()
    with pytest.raises(RuntimeError, match="monitor exploded"):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")
    assert _mcp_mounts(app) == []
    assert getattr(app.state, "mcp", None) is None


# ---------------------------------------------------------------------------
# The universe refresher: started last with a pool, cancelled first on shutdown
# ---------------------------------------------------------------------------


class _FakeUniverseRefresher:
    """The ``universe_refresher_factory`` seam's test double."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def test_universe_refresher_starts_with_a_pool_and_stops_on_shutdown() -> None:
    app = FastAPI()
    app.state.db_pool = object()
    fake = _FakeUniverseRefresher()
    app.state.universe_refresher_factory = lambda _app: fake
    await lifespan._start_universe_refresher(app)
    assert fake.started
    assert app.state.universe_refresher is fake
    await lifespan._stop_universe_refresher(app)
    assert fake.stopped


async def test_without_a_pool_no_universe_refresher_starts() -> None:
    app = FastAPI()
    app.state.db_pool = None
    calls: list[FastAPI] = []

    def factory(target: FastAPI) -> _FakeUniverseRefresher:
        calls.append(target)
        return _FakeUniverseRefresher()

    app.state.universe_refresher_factory = factory
    await lifespan._start_universe_refresher(app)
    assert calls == []
    assert getattr(app.state, "universe_refresher", None) is None


async def test_a_raising_refresh_never_fails_startup() -> None:
    """The refresh runs in the background loop: its failure is a warning there,
    never a startup error, and shutdown still cancels it cleanly."""
    from hydra.data.universe import UniverseRefresher

    attempts: list[int] = []

    async def refresh() -> None:
        attempts.append(1)
        raise RuntimeError("binance unreachable")

    app = FastAPI()
    app.state.db_pool = object()
    refresher = UniverseRefresher(refresh, retry_s=3600.0)
    app.state.universe_refresher_factory = lambda _app: refresher
    await lifespan._start_universe_refresher(app)
    await asyncio.sleep(0)  # the first refresh runs (and fails) in the background
    assert attempts == [1]
    assert app.state.universe_refresher is refresher
    await lifespan._stop_universe_refresher(app)


# ---------------------------------------------------------------------------
# The opt-in Redis fan-out (P7-E): flag on makes Redis tier-1 at startup
# ---------------------------------------------------------------------------


def _closed_redis_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"redis://127.0.0.1:{probe.getsockname()[1]}"


async def test_the_fanout_flag_off_never_pings_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(url: str, *, budget_s: float = 30.0) -> None:
        raise AssertionError("the flag is off: Redis must not be pinged")

    monkeypatch.setattr(lifespan, "require_redis", explode)
    await lifespan._require_redis(HydraConfig())


async def test_the_fanout_flag_on_refuses_startup_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    monkeypatch.setenv("HYDRA_EVENTS__BUS", "redis")
    monkeypatch.setenv("REDIS_URL", _closed_redis_url())
    monkeypatch.setattr(lifespan, "REDIS_CONNECT_BUDGET_S", 0.2)
    app = FastAPI()
    with pytest.raises(redis.exceptions.ConnectionError):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")
    assert app.state.session_manager is None


async def test_a_reachable_redis_lets_startup_through_the_fanout_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path of the ping step: budget spent, startup proceeds (dev mode)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    calls: list[str] = []

    async def fake_require_redis(url: str, *, budget_s: float = 30.0) -> None:
        calls.append(url)

    monkeypatch.setattr(lifespan, "require_redis", fake_require_redis)
    monkeypatch.setenv("HYDRA_EVENTS__BUS", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://hydra-redis:6379")
    app = FastAPI()
    async with lifespan.run(app):
        assert calls == ["redis://hydra-redis:6379"]


async def test_the_redis_bus_without_redis_url_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    monkeypatch.setenv("HYDRA_EVENTS__BUS", "redis")
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = FastAPI()
    with pytest.raises(ValidationError, match="REDIS_URL"):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")


# ---------------------------------------------------------------------------
# The D5 process split: HYDRA_TRADER__URL set binds a RemoteTrader, no engine
# ---------------------------------------------------------------------------


async def test_remote_mode_binds_a_remote_trader_and_never_builds_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the trader url set the API process skips the engine entirely: no
    SessionManager is constructed, a RemoteTrader is bound and its client is closed."""
    from hydra.dashboard.health import SPLIT_CHECKS
    from hydra.dashboard.trader_client import RemoteTrader
    from hydra.execution import session_manager as session_manager_mod

    def no_engine(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a SessionManager was constructed in the API process")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    monkeypatch.setenv("HYDRA_TRADER__URL", "http://trader:8000")
    monkeypatch.setenv("HYDRA_API_TOKEN", "t" * 48)
    monkeypatch.setattr(session_manager_mod, "SessionManager", no_engine)
    app = FastAPI()
    async with lifespan.run(app):
        assert app.state.session_manager is None
        assert isinstance(app.state.trader, RemoteTrader)
        assert not app.state.trader._client.is_closed
        monitor = app.state.health_monitor
        assert monitor is not None and monitor._checks == SPLIT_CHECKS
    assert app.state.trader._client.is_closed


async def test_single_process_mode_still_builds_the_local_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the trader url the startup path is today's: the engine and LocalTrader."""
    from hydra.dashboard.health import REQUIRED_CHECKS
    from hydra.execution.session_manager import SessionManager
    from hydra.services.trader import LocalTrader

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    monkeypatch.delenv("HYDRA_TRADER__URL", raising=False)
    app = FastAPI()
    async with lifespan.run(app):
        assert isinstance(app.state.session_manager, SessionManager)
        assert isinstance(app.state.trader, LocalTrader)
        assert app.state.trader.session_manager is app.state.session_manager
        monitor = app.state.health_monitor
        assert monitor is not None and monitor._checks == REQUIRED_CHECKS


# ---------------------------------------------------------------------------
# The data-cache sweeper: after the refresher, cancelled before it on shutdown
# ---------------------------------------------------------------------------


class _FakeSweeper:
    """The ``data_cache_sweeper_factory`` seam's test double."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def test_cache_sweeper_starts_with_a_pool_and_stops_on_shutdown() -> None:
    app = FastAPI()
    app.state.db_pool = object()
    fake = _FakeSweeper()
    app.state.data_cache_sweeper_factory = lambda _app: fake
    await lifespan._start_data_cache_sweeper(app)
    assert fake.started
    assert app.state.data_cache_sweeper is fake
    await lifespan._stop_data_cache_sweeper(app)
    assert fake.stopped


async def test_without_a_pool_no_cache_sweeper_starts() -> None:
    app = FastAPI()
    app.state.db_pool = None
    calls: list[FastAPI] = []

    def factory(target: FastAPI) -> _FakeSweeper:
        calls.append(target)
        return _FakeSweeper()

    app.state.data_cache_sweeper_factory = factory
    await lifespan._start_data_cache_sweeper(app)
    assert calls == []
    assert getattr(app.state, "data_cache_sweeper", None) is None


async def test_the_default_factory_builds_the_real_sweeper_from_the_data_config() -> None:
    from hydra.core.config import load_config
    from hydra.data.sweeper import DataCacheSweeper

    app = FastAPI()
    app.state.db_pool = object()
    app.state.config = load_config()
    sweeper = lifespan._default_data_cache_sweeper(app)
    assert isinstance(sweeper, DataCacheSweeper)
    await sweeper.stop()  # never started: stopping is safe


async def test_the_sweeper_starts_after_the_refresher_and_stops_before_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R27(5) slot: start last of the background loops, stop before _close_db_pool."""
    order: list[str] = []
    for name, label in (
        ("_start_health_monitor", "start-monitor"),
        ("_start_universe_refresher", "start-refresher"),
        ("_start_data_cache_sweeper", "start-sweeper"),
        ("_start_funding_refresher", "start-funding"),
    ):
        monkeypatch.setattr(lifespan, name, _recorder(order, label))
    for name, label in (
        ("_stop_funding_refresher", "stop-funding"),
        ("_stop_universe_refresher", "stop-refresher"),
        ("_stop_data_cache_sweeper", "stop-sweeper"),
        ("_close_db_pool", "close-pool"),
    ):
        monkeypatch.setattr(lifespan, name, _recorder(order, label))

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    app = FastAPI()
    async with lifespan.run(app):
        assert order == ["start-monitor", "start-refresher", "start-sweeper", "start-funding"]
    assert order == [
        "start-monitor",
        "start-refresher",
        "start-sweeper",
        "start-funding",
        "stop-funding",
        "stop-refresher",
        "stop-sweeper",
        "close-pool",
    ]


# ---------------------------------------------------------------------------
# The funding-rate refresher: after the sweeper, cancelled first on shutdown
# ---------------------------------------------------------------------------


class _FakeFundingRefresher:
    """The ``funding_refresher_factory`` seam's test double."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def test_funding_refresher_starts_with_a_pool_and_stops_on_shutdown() -> None:
    app = FastAPI()
    app.state.db_pool = object()
    fake = _FakeFundingRefresher()
    app.state.funding_refresher_factory = lambda _app: fake
    await lifespan._start_funding_refresher(app)
    assert fake.started
    assert app.state.funding_refresher is fake
    await lifespan._stop_funding_refresher(app)
    assert fake.stopped


async def test_without_a_pool_no_funding_refresher_starts() -> None:
    app = FastAPI()
    app.state.db_pool = None
    calls: list[FastAPI] = []

    def factory(target: FastAPI) -> _FakeFundingRefresher:
        calls.append(target)
        return _FakeFundingRefresher()

    app.state.funding_refresher_factory = factory
    await lifespan._start_funding_refresher(app)
    assert calls == []
    assert getattr(app.state, "funding_refresher", None) is None


async def test_the_default_factory_builds_the_real_funding_refresher() -> None:
    from hydra.data.funding import FundingRefresher

    app = FastAPI()
    app.state.db_pool = object()
    refresher = lifespan._default_funding_refresher(app)
    assert isinstance(refresher, FundingRefresher)
    await refresher.stop()  # never started: stopping is safe


async def test_a_running_lifespan_leaves_no_funding_refresher_without_a_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev mode (no DATABASE_URL) starts no funding task; state stays None."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    app = FastAPI()
    async with lifespan.run(app):
        assert getattr(app.state, "funding_refresher", None) is None


# ---------------------------------------------------------------------------
# The risk relay (P7-D): started at app startup, stopped first on shutdown
# ---------------------------------------------------------------------------


class _FakeRiskRelay:
    """The ``risk_relay_factory`` seam's test double."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def ensure_started(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def test_risk_relay_starts_with_a_pool_and_stops_on_shutdown() -> None:
    app = FastAPI()
    app.state.db_pool = object()
    fake = _FakeRiskRelay()
    app.state.risk_relay_factory = lambda _app: fake
    await lifespan._start_risk_relay(app)
    assert fake.started
    assert app.state.risk_relay is fake
    await lifespan._stop_risk_relay(app)
    assert fake.stopped


async def test_without_a_pool_no_risk_relay_starts() -> None:
    app = FastAPI()
    app.state.db_pool = None
    calls: list[FastAPI] = []

    def factory(target: FastAPI) -> _FakeRiskRelay:
        calls.append(target)
        return _FakeRiskRelay()

    app.state.risk_relay_factory = factory
    await lifespan._start_risk_relay(app)
    assert calls == []
    assert getattr(app.state, "risk_relay", None) is None


async def test_the_default_factory_builds_the_real_risk_relay() -> None:
    from hydra.dashboard.risk_relay import RiskRelay

    app = FastAPI()
    app.state.db_pool = object()
    relay = lifespan._default_risk_relay(app)
    assert isinstance(relay, RiskRelay)
    await relay.stop()  # never started: stopping is safe


async def test_a_running_lifespan_starts_the_risk_relay_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole lifespan: the relay runs with a pool and does not outlive shutdown."""

    class _PoolStub:
        closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    app = FastAPI()
    fake = _FakeRiskRelay()
    app.state.risk_relay_factory = lambda _app: fake
    pool = _PoolStub()
    async with lifespan.run(app):
        app.state.db_pool = pool  # a pool arriving after startup still gets a relay
        await lifespan._start_risk_relay(app)
        assert fake.started and not fake.stopped
    assert fake.stopped
    assert pool.closed


async def test_a_running_lifespan_leaves_no_risk_relay_without_a_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev mode (no DATABASE_URL) starts no risk relay; state stays None."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HYDRA_MCP_TOKEN", raising=False)
    app = FastAPI()
    async with lifespan.run(app):
        assert getattr(app.state, "risk_relay", None) is None


def _recorder(order: list[str], label: str) -> Any:
    """An async stand-in for a lifespan step that records its call order."""

    async def step(_app: FastAPI) -> None:
        order.append(label)

    return step
