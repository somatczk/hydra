"""Real-infrastructure fixtures, registered for every test through tests/conftest.py.

Database fixtures read HYDRA_DB_HOST/PORT/NAME/USER/PASSWORD, Redis reads HYDRA_REDIS_URL. Unset env
skips the test (a failure under HYDRA_REQUIRE_INFRA=1); set env with an unreachable server fails it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import quote

import asyncpg
import pytest
import redis

# ---------------------------------------------------------------------------
# TimescaleDB
# ---------------------------------------------------------------------------


def dsn(database: str) -> str:
    user = quote(os.environ.get("HYDRA_DB_USER", "hydra"), safe="")
    password = quote(os.environ.get("HYDRA_DB_PASSWORD", ""), safe="")
    host = os.environ["HYDRA_DB_HOST"]
    port = os.environ.get("HYDRA_DB_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(dsn(os.environ.get("HYDRA_DB_NAME", "postgres")), timeout=10)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def create_database() -> str:
    """Create an empty database on the configured server; fail (not skip) if unreachable."""
    if not os.environ.get("HYDRA_DB_HOST"):
        pytest.skip("HYDRA_DB_HOST not set; integration database not configured")
    name = f"hydra_it_{uuid.uuid4().hex[:10]}"
    try:
        await _admin(f'CREATE DATABASE "{name}"')
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.fail(f"HYDRA_DB_* is set but the database server is unreachable: {exc!r}")
    return name


async def drop_database(name: str) -> None:
    await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
async def fresh_database() -> AsyncIterator[str]:
    """DSN of an empty database created for this test and dropped after it."""
    name = await create_database()
    try:
        yield dsn(name)
    finally:
        await drop_database(name)


@pytest.fixture(scope="module")
def database_dsn() -> Iterator[str]:
    """DSN of an empty database shared by one test module."""
    name = asyncio.run(create_database())
    try:
        yield dsn(name)
    finally:
        asyncio.run(drop_database(name))


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


@pytest.fixture()
def redis_url() -> str:
    """HYDRA_REDIS_URL after a successful PING."""
    url = os.environ.get("HYDRA_REDIS_URL")
    if not url:
        pytest.skip("HYDRA_REDIS_URL not set; integration Redis not configured")
    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.fail(f"HYDRA_REDIS_URL is set but Redis is unreachable: {exc!r}")
    finally:
        client.close()
    return url
