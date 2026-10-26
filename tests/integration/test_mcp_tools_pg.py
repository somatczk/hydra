"""The research MCP tools against a real TimescaleDB, through the mounted transport.

The tools resolve the engine app's state exactly as in the engine process, so these
tests bind a real pool (a fresh migrated database) and drive JSON-RPC tools/call over
the real mount: data_coverage reads back exactly what was stored, a backtest on an
empty range is the honest 422 no-market-data envelope, and a backtest on stored bars
queues a durable job whose task_id is readable via backtest_status.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from structlog.testing import capture_logs

from hydra.core.config import load_config
from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, Timeframe
from hydra.dashboard import security
from hydra.dashboard.api import app as engine_app
from hydra.dashboard.mcp import mount_mcp
from hydra.dashboard.security import install_security
from hydra.data.migrations.runner import upgrade_to_head
from hydra.data.storage import MarketDataRepository

pytestmark = pytest.mark.integration

TEST_API_TOKEN = secrets.token_urlsafe(32)
TEST_MCP_TOKEN = "m" + secrets.token_urlsafe(48)
ACCEPT_BOTH = "application/json, text/event-stream"

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)

_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": "sma:period=5",
            }
        ],
    },
    "exit_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_below",
                "value": "sma:period=5",
            }
        ],
    },
}


def _write_strategy(directory: Path, strategy_id: str) -> None:
    data = {
        "id": strategy_id,
        "name": f"Strategy {strategy_id}",
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": "binance", "market_type": "SPOT"},
        "timeframes": {"primary": "1h"},
        "parameters": {"rules": _RULES, "required_history": 10},
    }
    (directory / f"{strategy_id}.yaml").write_text(yaml.safe_dump(data))


def _bar(i: int) -> OHLCV:
    close = Decimal(100 + i)
    return OHLCV(
        open=Decimal(100),
        high=Decimal(str(100 + i + 1)),
        low=Decimal("99"),
        close=close,
        volume=Decimal("1"),
        timestamp=T0 + i * H,
    )


def _sse_json(response: Response) -> Any:
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"no data frame in SSE body: {response.text!r}")


class _McpClient:
    """One initialized MCP session over the mounted transport."""

    def __init__(self, client: TestClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._id = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {
            "Authorization": f"Bearer {TEST_MCP_TOKEN}",
            "Accept": ACCEPT_BOTH,
            "Mcp-Session-Id": self._session_id,
        }
        return _sse_json(self._client.post("/mcp/", json=payload, headers=headers))["result"]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[bool, str]:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        return bool(result.get("isError")), content[0]["text"]


@pytest.fixture()
async def pool(fresh_database: str) -> AsyncIterator[Any]:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    db_pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        yield db_pool
    finally:
        await db_pool.close()


@pytest.fixture()
async def mcp(
    pool: Any,
    monkeypatch: pytest.MonkeyPatch,
    strategies_dir: Path,
) -> AsyncIterator[_McpClient]:
    """The mounted MCP app with the engine state bound to the real pool."""
    _write_strategy(strategies_dir, "saw")
    monkeypatch.setattr(engine_app.state, "db_pool", pool)
    monkeypatch.setattr(engine_app.state, "session_manager", None)
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR", "DATABASE_URL", "REDIS_URL", "HYDRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HYDRA_API_TOKEN", TEST_API_TOKEN)
    monkeypatch.setenv("HYDRA_MCP_TOKEN", TEST_MCP_TOKEN)
    monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "testserver")
    cfg = load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        mount = mount_mcp(app, cfg)
        async with mount.session_manager.run():
            yield

    mounted = FastAPI(lifespan=lifespan)
    install_security(mounted)
    mounted.state.config = cfg
    with TestClient(mounted) as client:
        initialize = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "hydra-tests", "version": "0.0.0"},
                },
            },
            headers={
                "Authorization": f"Bearer {TEST_MCP_TOKEN}",
                "Accept": ACCEPT_BOTH,
            },
        )
        session_id = initialize.headers["mcp-session-id"]
        client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Authorization": f"Bearer {TEST_MCP_TOKEN}",
                "Accept": ACCEPT_BOTH,
                "Mcp-Session-Id": session_id,
            },
        )
        yield _McpClient(client, session_id)


async def test_data_coverage_returns_the_exact_stored_range(mcp: _McpClient, pool: Any) -> None:
    repo = MarketDataRepository.from_pool(pool)
    stored = await repo.store_bars(
        [("binance", "BTCUSDT", Timeframe.H1, _bar(i)) for i in range(5)]
    )
    assert stored == 5
    is_error, text = mcp.call(
        "data_coverage", {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h"}
    )
    assert not is_error
    assert json.loads(text) == {
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-01T04:00:00+00:00",
        "bar_count": 5,
    }


async def test_backtest_run_on_an_empty_range_is_the_honest_no_data_envelope(
    mcp: _McpClient,
) -> None:
    is_error, text = mcp.call(
        "backtest_run",
        {"strategy_id": "saw", "start_date": "2026-03-01", "end_date": "2026-03-02"},
    )
    assert is_error
    envelope = json.loads(text.removeprefix("Error executing tool backtest_run: "))
    assert envelope["status"] == 422
    assert envelope["detail"]["code"] == "no_market_data"
    assert envelope["detail"]["symbol"] == "BTCUSDT"
    assert envelope["detail"]["timeframe"] == "1h"
    assert "No market data for binance BTCUSDT 1h" in envelope["detail"]["message"]


async def test_backtest_run_on_stored_bars_queues_a_durable_job(
    mcp: _McpClient, pool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = MarketDataRepository.from_pool(pool)
    stored = await repo.store_bars(
        [("binance", "BTCUSDT", Timeframe.H1, _bar(i)) for i in range(30)]
    )
    assert stored == 30
    with capture_logs() as logs:
        # a logger cached by an earlier audit() call would bypass capture_logs
        monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
        is_error, text = mcp.call(
            "backtest_run",
            {"strategy_id": "saw", "start_date": "2026-01-01", "end_date": "2026-01-02"},
        )
    assert not is_error
    queued = json.loads(text)
    assert queued["status"] == "queued"
    task_id = queued["task_id"]
    assert task_id.startswith("task-")

    audit_lines = [event for event in logs if event.get("event") == "audit"]
    assert [
        (line["action"], line["principal"], line["principal_kind"]) for line in audit_lines
    ] == [("backtest_run", "mcp", "service")]
    assert audit_lines[0]["strategy_id"] == "saw"
    assert audit_lines[0]["task_id"] == task_id

    is_error, text = mcp.call("backtest_status", {"task_id": task_id})
    assert not is_error
    status = json.loads(text)
    assert status["task_id"] == task_id
    assert status["status"] in ("queued", "running")  # no worker runs in this test
    assert status["result_id"] is None
