"""Alembic baseline on real TimescaleDB: fresh, legacy 2.1.10, idempotent, concurrent, atomic."""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import sqlalchemy.exc
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import FastAPI

from hydra.dashboard import lifespan
from hydra.data.migrations import __main__ as migrations_cli
from hydra.data.migrations.runner import (
    MigrationError,
    alembic_config,
    current_revision,
    head_revision,
    heads,
    upgrade_to_head,
)
from tests import infra

REPO = Path(__file__).resolve().parents[2]
LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "schema_2_1_10.sql"

_COLUMNS_SQL = """
    SELECT table_schema, table_name, column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema IN ('public', 'ts') AND table_name <> 'alembic_version'
    ORDER BY 1, 2, 3
"""


async def _upgrade(dsn: str) -> None:
    await asyncio.to_thread(upgrade_to_head, dsn)


async def _fetch(dsn: str, sql: str, *args: object) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


async def _execute(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _columns(dsn: str) -> set[tuple[str, ...]]:
    return {tuple(r) for r in await _fetch(dsn, _COLUMNS_SQL)}


def test_chain_has_one_head() -> None:
    assert heads() == [head_revision()]


async def test_fresh_db_upgrade(fresh_database: str) -> None:
    await _upgrade(fresh_database)

    assert await current_revision(fresh_database) == head_revision()
    hypertables = await _fetch(
        fresh_database,
        "SELECT hypertable_schema || '.' || hypertable_name AS name "
        "FROM timescaledb_information.hypertables",
    )
    assert {r["name"] for r in hypertables} == {"ts.ohlcv_1m", "ts.trades", "ts.funding_rates"}
    risk = await _fetch(
        fresh_database, "SELECT max_position_pct FROM risk_config WHERE scope = 'global'"
    )
    assert [r["max_position_pct"] for r in risk] == [Decimal("0.1000")]
    config = {
        r["key"]: r["value"]
        for r in await _fetch(fresh_database, "SELECT key, value FROM system_config")
    }
    assert config == {
        "trading_mode": '"paper"',
        "default_pair": '"BTC/USDT"',
        "default_timeframe": '"1h"',
        "max_concurrent_strategies": "5",
        "paper_capital": "10000",
    }
    assert Decimal(config["paper_capital"]) == Decimal("10000")
    policies = await _fetch(
        fresh_database,
        "SELECT proc_name FROM timescaledb_information.jobs "
        "WHERE proc_name IN ('policy_retention', 'policy_compression', "
        "'policy_refresh_continuous_aggregate')",
    )
    assert policies == []
    nullable = await _fetch(
        fresh_database,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'backtest_results' AND column_name = 'data_source'",
    )
    assert [r["is_nullable"] for r in nullable] == ["NO"]
    session_cols = await _fetch(
        fresh_database,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'trading_sessions' AND column_name IN "
        "('strategy_config', 'config_hash') ORDER BY column_name",
    )
    assert [tuple(r) for r in session_cols] == [
        ("config_hash", "text", "YES"),
        ("strategy_config", "jsonb", "YES"),
    ]


async def test_downgrade_drops_the_session_config_columns(fresh_database: str) -> None:
    """The session snapshot revision is reversible: its downgrade drops both columns."""
    await _upgrade(fresh_database)

    # pin the target: 0007 reverses onto 0006, so '-1' would stop at 0008's parent
    await asyncio.to_thread(command.downgrade, alembic_config(fresh_database), "0006_backtest_jobs")

    columns = await _fetch(
        fresh_database,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'trading_sessions' "
        "AND column_name IN ('strategy_config', 'config_hash')",
    )
    assert columns == []
    assert await current_revision(fresh_database) == "0006_backtest_jobs"


async def test_0011_upgrade_gives_pre_existing_rows_the_feed_source(fresh_database: str) -> None:
    """Bars stored before 0011 (the 2.6.0 prod shape) all take the immutable 'feed' default."""
    await _upgrade(fresh_database)
    await asyncio.to_thread(
        command.downgrade, alembic_config(fresh_database), "0010_validated_permissions"
    )
    await _execute(
        fresh_database,
        "INSERT INTO ts.ohlcv_1m (exchange, symbol, timeframe, timestamp, open, high, "
        "low, close, volume) VALUES ('binance', 'BTCUSDT', '1m', '2026-01-01T00:00:00Z', "
        "1, 1, 1, 1, 1), ('binance', 'BTCUSDT', '1m', '2026-01-01T00:01:00Z', 1, 1, 1, 1, 1)",
    )

    await _upgrade(fresh_database)

    sources = await _fetch(
        fresh_database, "SELECT source, count(*) FROM ts.ohlcv_1m GROUP BY source"
    )
    assert [tuple(r) for r in sources] == [("feed", 2)]
    column = await _fetch(
        fresh_database,
        "SELECT column_default, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'ts' AND table_name = 'ohlcv_1m' AND column_name = 'source'",
    )
    assert [tuple(r) for r in column] == [("'feed'::text", "NO")]
    assert await current_revision(fresh_database) == head_revision()


async def test_0011_downgrade_drops_the_column_and_the_ledger(fresh_database: str) -> None:
    """The attribution revision is reversible: the column and the ledger both go."""
    await _upgrade(fresh_database)
    await _execute(
        fresh_database,
        "INSERT INTO data_cache_entries (exchange, symbol, timeframe, first_ts, last_ts, rows) "
        "VALUES ('binance', 'BTCUSDT', '1m', now(), now(), 10)",
    )

    await asyncio.to_thread(
        command.downgrade, alembic_config(fresh_database), "0010_validated_permissions"
    )

    leftovers = await _fetch(fresh_database, "SELECT to_regclass('public.data_cache_entries')")
    assert [tuple(r) for r in leftovers] == [(None,)]
    assert (
        await _fetch(
            fresh_database,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'ts' "
            "AND table_name = 'ohlcv_1m' AND column_name = 'source'",
        )
        == []
    )

    await _upgrade(fresh_database)
    assert await current_revision(fresh_database) == head_revision()
    entries = await _fetch(fresh_database, "SELECT count(*) FROM data_cache_entries")
    assert [r["count"] for r in entries] == [0]


async def test_0011_data_cache_entries_is_unique_per_dataset(fresh_database: str) -> None:
    """One ledger row per dataset: UNIQUE(exchange, symbol, timeframe) and NOT NULL bounds."""
    await _upgrade(fresh_database)
    insert = (
        "INSERT INTO data_cache_entries (exchange, symbol, timeframe, first_ts, last_ts, rows) "
        "VALUES ('binance', 'BTCUSDT', '1m', now(), now(), 3)"
    )
    await _execute(fresh_database, insert)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _execute(fresh_database, insert)
    row = await _fetch(
        fresh_database,
        "SELECT status, created_at IS NOT NULL AS created, last_fetched_at IS NOT NULL AS fetched, "
        "last_used_at IS NOT NULL AS used FROM data_cache_entries",
    )
    assert [tuple(r) for r in row] == [("ready", True, True, True)]
    with pytest.raises(asyncpg.NotNullViolationError):
        await _execute(
            fresh_database,
            "INSERT INTO data_cache_entries (exchange, symbol, timeframe, last_ts, rows) "
            "VALUES ('binance', 'ETHUSDT', '1m', now(), 1)",
        )


async def test_legacy_2_1_10_schema_upgrades_in_place(fresh_database: str) -> None:
    await _execute(fresh_database, LEGACY_SCHEMA.read_text())
    await _execute(
        fresh_database,
        """
        INSERT INTO trading_sessions (id, strategy_id, trading_mode, status)
            VALUES ('s-1', 'rsi', 'paper', 'stopped');
        INSERT INTO backtest_results (id, strategy, period) VALUES ('synthetic', 'rsi', '1d');
        INSERT INTO backtest_result_trades
            (backtest_id, entry_time, exit_time, side, entry_price, exit_price, pnl)
            VALUES ('synthetic', now(), now(), 'long', 1, 2, 1);
        UPDATE risk_config SET max_position_pct = 0.0700 WHERE scope = 'global';
        """,
    )

    await _upgrade(fresh_database)

    assert await current_revision(fresh_database) == head_revision()
    sessions = await _fetch(fresh_database, "SELECT id, strategy_id FROM trading_sessions")
    assert [tuple(r) for r in sessions] == [("s-1", "rsi")]
    assert await _fetch(fresh_database, "SELECT id FROM backtest_results") == []
    assert await _fetch(fresh_database, "SELECT id FROM backtest_result_trades") == []
    risk = await _fetch(fresh_database, "SELECT max_position_pct FROM risk_config")
    assert [r["max_position_pct"] for r in risk] == [Decimal("0.0700")]

    reference = await infra.create_database()
    try:
        reference_dsn = infra.dsn(reference)
        await _upgrade(reference_dsn)
        assert await _columns(fresh_database) == await _columns(reference_dsn)
    finally:
        await infra.drop_database(reference)


async def test_upgrade_is_idempotent(fresh_database: str) -> None:
    await _upgrade(fresh_database)
    await _execute(
        fresh_database, "UPDATE system_config SET value = '2500' WHERE key = 'paper_capital'"
    )
    before = await _columns(fresh_database)

    await _upgrade(fresh_database)

    assert await _columns(fresh_database) == before
    versions = await _fetch(fresh_database, "SELECT version_num FROM alembic_version")
    assert [r["version_num"] for r in versions] == [head_revision()]
    capital = await _fetch(
        fresh_database, "SELECT value FROM system_config WHERE key = 'paper_capital'"
    )
    assert [r["value"] for r in capital] == ["2500"]


async def _cli_upgrade(dsn: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "hydra.data.migrations",
        "upgrade",
        env={**os.environ, "DATABASE_URL": dsn},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    return proc.returncode or 0, out.decode()


async def test_concurrent_upgrades_from_two_processes(fresh_database: str) -> None:
    results = await asyncio.gather(_cli_upgrade(fresh_database), _cli_upgrade(fresh_database))

    head = head_revision()
    assert [code for code, _ in results] == [0, 0], results
    assert all(f"current={head} head={head}" in out for _, out in results)
    versions = await _fetch(fresh_database, "SELECT version_num FROM alembic_version")
    assert [r["version_num"] for r in versions] == [head]
    risk = await _fetch(fresh_database, "SELECT scope FROM risk_config")
    assert [r["scope"] for r in risk] == ["global"]


async def test_failed_migration_aborts_startup(
    fresh_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _execute(fresh_database, "CREATE VIEW trading_sessions AS SELECT 1 AS id")
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()

    with pytest.raises(sqlalchemy.exc.DBAPIError, match="trading_sessions"):
        async with lifespan.run(app):
            pytest.fail("startup must not complete")

    assert app.state.session_manager is None
    leftovers = await _fetch(
        fresh_database,
        "SELECT to_regclass('public.alembic_version') AS v, to_regclass('ts.ohlcv_1m') AS o, "
        "to_regclass('public.risk_config') AS r, to_regclass('public.system_config') AS s",
    )
    assert tuple(leftovers[0]) == (None, None, None, None)


def test_cli_has_no_localhost_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert migrations_cli.main(["current"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_alembic_env_has_no_localhost_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HYDRA_DATABASE_DSN", "postgresql://hydra:hydra@localhost:5432/hydra")
    with pytest.raises(MigrationError, match="DATABASE_URL"):
        command.current(Config(str(REPO / "alembic.ini")))
