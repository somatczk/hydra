"""Engine lifespan: config and key errors stop startup before any DB connect; no silent no-pool."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import asyncpg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import ValidationError

from hydra.core.encryption import CredentialKeyError, encrypt
from hydra.dashboard import lifespan


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
