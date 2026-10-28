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

from hydra.core.config import ConfigError
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
