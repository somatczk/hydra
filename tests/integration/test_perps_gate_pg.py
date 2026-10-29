"""The perps gate on real TimescaleDB: the arm ceremony's system_config rows.

P4-E needs no migration: ``perps_armed`` and ``perps_capital`` ride the existing
``system_config`` table beside the spot gate's rows. This file runs the full ceremony
round-trip against a real database — the challenge, the code, the arm executor with its
fail-closed futures-permission re-fetch (faked only at the ccxt seam), the rows it
writes, the audit trail's double attribution — and the disarm.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import ccxt
import ccxt.async_support
import pytest
from cryptography.fernet import Fernet

from hydra.core.encryption import encrypt
from hydra.data.migrations.runner import current_revision, head_revision, upgrade_to_head
from hydra.risk.state import fetch_risk_events
from hydra.services.errors import ServiceError
from hydra.services.live_gate import create_live_approval, live_approval_store
from hydra.services.perps_gate import (
    execute_perps_arming,
    positively_perps_disarmed,
    read_perps_capital,
    read_perps_state,
    require_perps_gate,
    write_perps_armed_transition,
)

FUTURES_CAPABLE = json.dumps(
    {"enable_spot_and_margin_trading": True, "enable_withdrawals": False, "ip_restrict": False}
)


class _Backend:
    """The executor's backend: the kill switch is off, no sessions."""

    async def kill_switch_active(self) -> bool:
        return False

    async def list_sessions(self) -> list[dict[str, Any]]:
        return []


class FakeBinance:
    restriction_calls = 0

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts

    async def close(self) -> None:
        return None


async def _fake_get_account_api_restrictions(*args: Any) -> dict[str, Any]:
    FakeBinance.restriction_calls += 1
    return {
        "ipRestrict": False,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": True,
        "enableFutures": True,
    }


# ccxt's camelCase implicit API name, bound as an attribute (pep8-naming flags the def)
FakeBinance.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions


@pytest.fixture(autouse=True)
def _credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def binance(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBinance.restriction_calls = 0
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


async def _store_credentials(pool: asyncpg.Pool, environment: str = "mainnet") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO exchange_credentials "
            "(exchange_id, encrypted_key, encrypted_secret, encrypted_passphrase, updated_at, "
            "validated_at, validated_environment, validated_permissions) "
            "VALUES ('binance', $1, $2, NULL, now(), now(), $3, $4::jsonb)",
            encrypt("key-1"),
            encrypt("secret-1"),
            environment,
            FUTURES_CAPABLE,
        )


async def _config(pool: asyncpg.Pool, key: str) -> Any:
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT value FROM system_config WHERE key = $1", key)
    return None if raw is None else json.loads(raw)


async def test_the_full_perps_arm_flow_writes_the_system_config_rows(pool: asyncpg.Pool) -> None:
    """The ceremony round-trip on real PG: the challenge, the code, the audited arm —
    the rows ride system_config (no migration), with double attribution."""
    await _store_credentials(pool)
    state = SimpleNamespace(db_pool=pool, trader=_Backend())
    store = live_approval_store(state)

    assert await read_perps_state(pool) == {
        "armed": False,
        "armed_by": None,
        "armed_at": None,
    }
    with pytest.raises(ServiceError) as refused:
        await require_perps_gate(pool)
    assert isinstance(refused.value.detail, dict)
    assert refused.value.detail["code"] == "perps_not_armed"

    approval = await create_live_approval(
        store,
        action="live_perps_arming",
        reason="arm perps trading",
        params={"perps_capital": 500.0, "reason": "arm perps trading"},
        requested_by="service:mcp",
    )
    outcome = await store.approve(approval.id, approval.code, approved_by="user:soma", deps=state)
    assert outcome.executed is True
    assert outcome.result["status"] == "armed"
    assert outcome.result["perps_capital"] == 500.0
    assert outcome.approval.requested_by == "service:mcp"
    assert FakeBinance.restriction_calls == 1  # the arm re-fetched the permission live

    armed = await _config(pool, "perps_armed")
    assert armed["armed"] is True
    assert armed["by"] == "service:mcp"
    assert armed["at"]
    assert await _config(pool, "perps_capital") == 500.0
    assert await read_perps_capital(pool) == 500.0
    # the spot gate's rows are untouched by the perps ceremony
    assert await _config(pool, "live_armed") is None
    assert await _config(pool, "live_capital") is None

    rows = await fetch_risk_events(pool, 10)  # newest first
    assert [(r.kind, r.actor) for r in rows] == [
        ("perps_armed", "service:mcp"),
        ("approval_approved", "user:soma"),
        ("approval_requested", "service:mcp"),
    ]
    assert rows[0].details == {"perps_capital": 500.0}

    # the positive precondition now reads armed: P4-D's executor may consume it
    state = await require_perps_gate(pool)
    assert state["armed_by"] == "service:mcp"


async def test_the_perps_rows_ride_system_config_no_migration(
    pool: asyncpg.Pool, fresh_database: str
) -> None:
    """No alembic revision belongs to P4-E: the migrated database sits at the repo's
    single head, the perps state is two plain system_config keys, and no perps-specific
    table or column exists anywhere in the schema."""
    assert await current_revision(fresh_database) == head_revision()
    async with pool.acquire() as conn:
        perp_columns = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE column_name LIKE 'perps%'"
        )
        keys = await conn.fetch(
            "SELECT key FROM system_config WHERE key LIKE 'perps%' ORDER BY key"
        )
    assert perp_columns == 0
    assert [k["key"] for k in keys] == []
    await _store_credentials(pool)
    await execute_perps_arming(
        SimpleNamespace(db_pool=pool, trader=_Backend()),
        {"perps_capital": 250.0, "reason": "arm"},
        "user:soma",
    )
    async with pool.acquire() as conn:
        keys = await conn.fetch(
            "SELECT key FROM system_config WHERE key LIKE 'perps%' ORDER BY key"
        )
        value_type = await conn.fetchval(
            "SELECT jsonb_typeof(value) FROM system_config WHERE key = 'perps_capital'"
        )
    assert [k["key"] for k in keys] == ["perps_armed", "perps_capital"]
    assert value_type == "number"


async def test_disarm_flips_the_row_and_the_event(pool: asyncpg.Pool) -> None:
    await _store_credentials(pool)
    state = SimpleNamespace(db_pool=pool, trader=_Backend())
    await execute_perps_arming(state, {"perps_capital": 500.0, "reason": "arm"}, "user:soma")
    assert await positively_perps_disarmed(pool) is False

    await write_perps_armed_transition(pool, armed=False, actor="user:soma", reason="off")
    assert (await read_perps_state(pool))["armed"] is False
    assert await positively_perps_disarmed(pool) is True
    # disarm keeps the capital row: only re-arming changes it
    assert await read_perps_capital(pool) == 500.0
    rows = await fetch_risk_events(pool, 10)
    assert [r.kind for r in rows][:2] == ["perps_disarmed", "perps_armed"]


async def test_arm_refuses_when_the_key_cannot_trade_futures(pool: asyncpg.Pool) -> None:
    """The fail-closed heart on real PG: no enableFutures in the exchange's answer means
    nothing is written — not the armed row, not the capital, not an event."""
    await _store_credentials(pool)

    async def _refuse(*args: Any) -> dict[str, Any]:
        return {"ipRestrict": False, "enableWithdrawals": False, "enableSpotAndMarginTrading": True}

    FakeBinance.sapiGetAccountApiRestrictions = _refuse
    try:
        with pytest.raises(ServiceError) as refused:
            await execute_perps_arming(
                SimpleNamespace(db_pool=pool, trader=_Backend()),
                {"perps_capital": 500.0, "reason": "arm"},
                "user:soma",
            )
        assert isinstance(refused.value.detail, dict)
        assert refused.value.detail["code"] == "credentials_not_futures_capable"
    finally:
        FakeBinance.sapiGetAccountApiRestrictions = _fake_get_account_api_restrictions
    assert await _config(pool, "perps_armed") is None
    assert await _config(pool, "perps_capital") is None
    assert await fetch_risk_events(pool, 10) == []
