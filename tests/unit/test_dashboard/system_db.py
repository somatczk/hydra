"""In-memory stand-in for the two tables the system routes use (the DB boundary only).

It answers exactly the statements ``hydra.dashboard.routes.system`` issues, stores JSONB values as
the JSON text asyncpg returns, and rolls a transaction back when its body raises. The real SQL
runs against TimescaleDB in ``tests/integration/test_system_config_persistence.py``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

SEEDED: dict[str, Any] = {
    "trading_mode": "paper",
    "default_pair": "BTC/USDT",
    "default_timeframe": "1h",
    "max_concurrent_strategies": 5,
    "paper_capital": 10000,
}

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FakeSystemDb:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, dict[str, Any]] = {}
        for key, value in (SEEDED if config is None else config).items():
            self.config[key] = {"value": json.dumps(value), "updated_by": None}
        self.credentials: dict[str, dict[str, Any]] = {}
        self.running_sessions: list[dict[str, Any]] = []
        self.fail: BaseException | None = None
        self.fail_on: str | None = None
        self.statements: list[str] = []

    # -- pool ---------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeSystemDb]:
        if self.fail is not None and self.fail_on is None:
            raise self.fail
        yield self

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        saved = copy.deepcopy((self.config, self.credentials))
        try:
            yield
        except BaseException:
            self.config, self.credentials = saved
            raise

    def value(self, key: str) -> Any:
        return json.loads(self.config[key]["value"])

    # -- connection -----------------------------------------------------------

    def _check(self, sql: str) -> None:
        self.statements.append(sql)
        if self.fail is not None and self.fail_on is not None and self.fail_on in sql:
            raise self.fail

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if sql == "SELECT key, value FROM system_config":
            return [{"key": k, "value": row["value"]} for k, row in self.config.items()]
        if sql == (
            "SELECT exchange_id, validated_at, validated_environment, validated_permissions "
            "FROM exchange_credentials"
        ):
            return [
                {
                    "exchange_id": eid,
                    "validated_at": row["validated_at"],
                    "validated_environment": row.get("validated_environment"),
                    "validated_permissions": row.get("validated_permissions"),
                }
                for eid, row in self.credentials.items()
            ]
        if sql == "SELECT * FROM trading_sessions WHERE status = 'running' ORDER BY created_at":
            return [dict(row) for row in self.running_sessions]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        if sql == "SELECT value FROM system_config WHERE key = 'paper_capital'":
            row = self.config.get("paper_capital")
            return None if row is None else row["value"]
        if sql == "SELECT 1":
            return 1
        if sql.startswith("INSERT INTO exchange_credentials"):
            exchange_id, key, secret, passphrase, environment, permissions = args
            self.credentials[exchange_id] = {
                "encrypted_key": key,
                "encrypted_secret": secret,
                "encrypted_passphrase": passphrase,
                "validated_at": NOW,
                "validated_environment": environment,
                "validated_permissions": permissions,
            }
            return NOW
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._check(sql)
        if sql == ("SELECT validated_permissions FROM exchange_credentials WHERE exchange_id = $1"):
            row = self.credentials.get(args[0])
            return (
                None if row is None else {"validated_permissions": row.get("validated_permissions")}
            )
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self._check(sql)
        if sql.startswith("INSERT INTO system_config"):
            key, value, actor = args
            json.loads(value)
            self.config[key] = {"value": value, "updated_by": actor}
            return "INSERT 0 1"
        if sql == "DELETE FROM system_config WHERE key = $1":
            self.config.pop(args[0], None)
            return "DELETE 1"
        if sql == "DELETE FROM exchange_credentials WHERE exchange_id = $1":
            self.credentials.pop(args[0], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {sql}")
