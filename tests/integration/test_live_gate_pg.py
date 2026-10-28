"""The live gate on real TimescaleDB: the arm ceremony's rows and the delete gate."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import ccxt
import ccxt.async_support
import pytest
from alembic import command
from cryptography.fernet import Fernet

from hydra.core.encryption import encrypt
from hydra.data.migrations.runner import alembic_config, upgrade_to_head
from hydra.risk.state import fetch_risk_events
from hydra.services.errors import ServiceError
from hydra.services.live_gate import (
    create_live_approval,
    execute_credential_delete,
    live_approval_store,
)

TRADE_CAPABLE = json.dumps(
    {"enable_spot_and_margin_trading": True, "enable_withdrawals": False, "ip_restrict": False}
)


class _Backend:
    """The executors' backend: the kill switch is off, one optional live session."""

    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or []

    async def kill_switch_active(self) -> bool:
        return False

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions]


class FakeBinance:
    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    async def close(self) -> None:
        return None


async def _fake_get_account_api_restrictions(*args: Any) -> dict[str, Any]:
    return {
        "ipRestrict": False,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": True,
    }


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.privateGetAccountApiRestrictions = _fake_get_account_api_restrictions


@pytest.fixture(autouse=True)
def _credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def binance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccxt.async_support, "binance", FakeBinance)


@pytest.fixture()
async def pool(fresh_database: str) -> Any:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    created = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    assert created is not None
    try:
        yield created
    finally:
        await created.close()


async def _store_credentials(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO exchange_credentials "
            "(exchange_id, encrypted_key, encrypted_secret, encrypted_passphrase, updated_at, "
            "validated_at, validated_environment, validated_permissions) "
            "VALUES ('binance', $1, $2, NULL, now(), now(), 'mainnet', $3::jsonb)",
            encrypt("key-1"),
            encrypt("secret-1"),
            TRADE_CAPABLE,
        )


async def _config(pool: asyncpg.Pool, key: str) -> Any:
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT value FROM system_config WHERE key = $1", key)
    return None if raw is None else json.loads(raw)


async def test_the_full_arm_flow_writes_the_rows_with_double_attribution(
    pool: asyncpg.Pool,
) -> None:
    await _store_credentials(pool)
    state = SimpleNamespace(db_pool=pool, trader=_Backend())
    store = live_approval_store(state)

    approval = await create_live_approval(
        store,
        action="live_arming",
        reason="arm live trading",
        params={"live_capital": 500.0, "reason": "arm live trading"},
        requested_by="service:mcp",
    )
    outcome = await store.approve(approval.id, approval.code, approved_by="user:soma", deps=state)
    assert outcome.executed is True
    assert outcome.result["status"] == "armed"
    assert outcome.approval.requested_by == "service:mcp"

    armed = await _config(pool, "live_armed")
    assert armed["armed"] is True
    assert armed["by"] == "service:mcp"
    assert armed["at"]
    assert await _config(pool, "live_capital") == 500.0

    rows = await fetch_risk_events(pool, 10)  # newest first
    assert [r.kind for r in rows] == [
        "live_armed",
        "approval_approved",
        "approval_requested",
    ]
    # double attribution: the executed action is the requester's, the approval the human's
    assert [(r.kind, r.actor) for r in rows] == [
        ("live_armed", "service:mcp"),
        ("approval_approved", "user:soma"),
        ("approval_requested", "service:mcp"),
    ]
    assert rows[0].details == {"live_capital": 500.0}


async def test_credential_delete_gates_on_running_live_sessions(pool: asyncpg.Pool) -> None:
    await _store_credentials(pool)
    state = SimpleNamespace(
        db_pool=pool,
        trader=_Backend(
            sessions=[{"session_id": "s-live", "trading_mode": "live", "status": "running"}]
        ),
    )
    with pytest.raises(ServiceError) as gated:
        await execute_credential_delete(
            state, {"exchange_id": "binance", "reason": "rotate keys"}, "user:soma"
        )
    detail = gated.value.detail
    assert isinstance(detail, dict)
    assert (gated.value.status_code, detail["code"], detail["running_live_sessions"]) == (
        409,
        "credential_delete_gated",
        ["s-live"],
    )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM exchange_credentials") == 1

    # no live session running: the approved delete goes through
    state.trader = _Backend()
    result = await execute_credential_delete(
        state, {"exchange_id": "binance", "reason": "rotate keys"}, "user:soma"
    )
    assert result["deleted"] is True
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM exchange_credentials") == 0


async def test_migration_0010_up_and_down(fresh_database: str) -> None:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    conn = await asyncpg.connect(fresh_database)
    try:
        columns = await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'exchange_credentials' "
            "AND column_name = 'validated_permissions'"
        )
        assert [tuple(c) for c in columns] == [("validated_permissions", "jsonb", "YES")]
    finally:
        await conn.close()

    # pin the target: the permission column reverses onto 0009
    await asyncio.to_thread(command.downgrade, alembic_config(fresh_database), "0009_live_orders")
    conn = await asyncpg.connect(fresh_database)
    try:
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'exchange_credentials' "
            "AND column_name = 'validated_permissions'"
        )
        assert columns == []
    finally:
        await conn.close()
