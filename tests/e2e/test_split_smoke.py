"""THE SPLIT SMOKE (D5): both processes on one loop, real HTTP and one real WebSocket.

The trader is the real ``hydra.trader`` startup against one TimescaleDB — the pool with its
retry budget, the schema wait (the API owns migrations), ``SessionManager.startup()`` on the
replay exchange, its own app with the full-check HealthMonitor — served by real uvicorn;
the API is the real ``hydra.dashboard.api`` app under its real lifespan with
``HYDRA_TRADER__URL`` pointed at the trader, so it binds a ``RemoteTrader`` and never
constructs a SessionManager. Start → detail → the WS fill tick flow through the real HTTP
boundary, then the trader disappears: readiness fails on the trader check, the kill switch
latches in the database with the honest 503 (R7), the release through the dead trader stays
latched, and the trader's cleanup tick applies the latch exactly once to the still-running
session. The single-process mode (``HYDRA_TRADER__URL`` unset) is ``test_paper_smoke.py``,
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from pydantic import SecretStr
from websockets.asyncio.client import connect

from hydra.core import metrics
from hydra.core.config import HydraConfig, TraderConfig
from hydra.core.types import Timeframe
from hydra.dashboard.api import app
from hydra.dashboard.health import HealthMonitor
from hydra.data.bars import last_closed_open
from hydra.data.migrations.runner import current_revision, head_revision
from hydra.data.storage import MarketDataRepository
from hydra.execution.session_manager import SessionManager
from hydra.trader import main as trader_main
from hydra.trader.server import build_app
from tests.e2e.replay_exchange import ReplayMarket
from tests.e2e.test_paper_smoke import WARMUP, BarWatch, Reference, write_strategy

STRATEGY_ID = "split-sma5"
POLL_TIMEOUT_S = 30.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Trader:
    """The trader process in-process: its engine, monitor, pool and its own uvicorn."""

    def __init__(self, cfg: HydraConfig, pool: asyncpg.Pool, mgr: SessionManager) -> None:
        self.cfg = cfg
        self.pool = pool
        self.mgr = mgr
        self.app = build_app(cfg, pool, mgr)
        self.monitor = HealthMonitor(self.app)
        self.app.state.health_monitor = self.monitor
        self.port = 0
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None

    async def serve(self, port: int) -> None:
        assert self.server is None, "the trader is already served"
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=port, log_config=None, proxy_headers=False
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)

    async def disappear(self) -> None:
        """The trader process goes away; its engine (sessions, monitor) keeps running."""
        server, task = self.server, self.task
        assert server is not None and task is not None
        self.server = self.task = None
        server.should_exit = True
        await task

    async def close(self) -> None:
        if self.server is not None:
            await self.disappear()
        await self.monitor.stop()
        await self.mgr.graceful_shutdown()
        await self.pool.close()


@dataclass
class Split:
    client: httpx.AsyncClient
    trader: Trader
    api_port: int
    token: str


@pytest.fixture(autouse=True)
def _fresh_registry() -> AsyncIterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def test_the_split_serves_the_ui_while_the_trader_holds_the_sessions(
    fresh_database: str,
    redis_url: str,
    strategies_dir: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR", "HYDRA_API__TRUSTED_PROXY_CIDRS"):
        monkeypatch.delenv(name, raising=False)
    token = secrets.token_urlsafe(32)
    trader_port, api_port = _free_port(), _free_port()
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("HYDRA_API_TOKEN", token)
    monkeypatch.setenv("HYDRA_API__AUTH_MODE", "token")
    monkeypatch.setenv("HYDRA_TRADER__URL", f"http://127.0.0.1:{trader_port}")

    app.state.health_monitor_factory = lambda target: HealthMonitor(target, interval_s=1.0)
    try:
        async with _served(app, api_port):
            write_strategy(strategies_dir, STRATEGY_ID)
            market = ReplayMarket(WARMUP)
            trader = await _start_trader(fresh_database, token, trader_port, market)
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{api_port}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                ) as client:
                    await _the_split_story(Split(client, trader, api_port, token), market)
            finally:
                await trader.close()
    finally:
        app.state.health_monitor_factory = None


@asynccontextmanager
async def _served(app_to_serve: Any, port: int) -> AsyncIterator[uvicorn.Server]:
    server = uvicorn.Server(
        uvicorn.Config(
            app_to_serve, host="127.0.0.1", port=port, log_config=None, proxy_headers=False
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield server
    finally:
        server.should_exit = True
        await task


async def _start_trader(dsn: str, token: str, port: int, market: ReplayMarket) -> Trader:
    """The trader's real startup order, composed like ``hydra.trader.main``."""
    cfg = HydraConfig(
        DATABASE_URL=SecretStr(dsn),
        HYDRA_CREDENTIAL_KEY=SecretStr(Fernet.generate_key().decode()),
        HYDRA_API_TOKEN=SecretStr(token),
        trader=TraderConfig(),  # the trader never points at itself: full-check monitor
    )
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    try:
        assert await current_revision(dsn) == head_revision()  # the API migrated first
        await trader_main.wait_for_schema(dsn)
        mgr = SessionManager(
            db_pool=pool, config=cfg, clients=market.clients(history_window=WARMUP)
        )
        await mgr.startup()
        trader = Trader(cfg, pool, mgr)
        await trader.serve(port)
        return trader
    except BaseException:
        await pool.close()
        raise


async def _the_split_story(split: Split, market: ReplayMarket) -> None:
    client, trader = split.client, split.trader

    # both processes are up: the API's readiness embeds the trader's own body
    await _poll(lambda: client.get("/livez"), lambda r: r.status_code == 200)
    await _poll(lambda: client.get("/readyz"), lambda r: r.status_code == 200)
    ready = (await client.get("/readyz")).json()
    assert set(ready["checks"]) == {"db", "migrations", "trader"}
    assert ready["checks"]["trader"]["detail"]["checks"]["risk"]["ok"] is True

    # the session starts through the boundary and runs in the trader process only
    sid = await _start_session_through_the_api(split, market)
    session = trader.mgr.get_session(sid)
    assert session is not None and session.status == "running"
    assert app.state.session_manager is None

    # the browser connects before any fill (the session page's own order): the relay
    # anchors its cursor at the current MAX(id), ready for the fill to come
    ws = await connect(
        f"ws://127.0.0.1:{split.api_port}/ws/trades?session_id={sid}",
        additional_headers=[("Authorization", f"Bearer {split.token}")],
    )
    await _poll(_relay_anchored, lambda anchored: anchored)

    # one entry fill lands in the trader's engine and its trades row crosses the boundary
    (_, entry) = Reference(market).entries_exits(len(market.live) - 1)[0]
    watch = BarWatch()
    assert session._event_bus is not None
    await session._event_bus.subscribe("bar", watch)
    market.release_through(entry)
    await watch.wait_for(market.live[entry].open_time)
    detail = (await client.get(f"/api/trading/sessions/{sid}/detail")).json()
    assert len(detail["trades"]) == 1
    [row] = await _fetch(
        trader.pool, "SELECT id FROM trades WHERE session_id = $1 ORDER BY id", sid
    )

    # the R6 relay tick reaches the real WebSocket client through the API process
    try:
        tick = None
        async with asyncio.timeout(POLL_TIMEOUT_S):
            while tick is None:
                message = json.loads(await ws.recv())
                if set(message) == {"event", "session_id", "trade_id"}:
                    tick = message
        assert tick == {
            "event": "order_fill",
            "session_id": sid,
            "trade_id": int(row["id"]),
        }
    finally:
        await ws.close()

    # the trader disappears: readiness fails on the trader check, the switch latches (R7)
    await trader.disappear()
    await _poll(
        lambda: client.get("/readyz"),
        lambda r: r.status_code == 503 and "error" in r.json()["checks"]["trader"],
    )
    latch = await client.post("/api/trading/kill-switch")
    assert latch.status_code == 503
    body = latch.json()["detail"]
    assert body["code"] == "engine_unreachable"
    assert (body["latched"], body["apply_window_s"]) == (True, 60)
    [row] = await _fetch(
        trader.pool, "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
    )
    assert row["kill_switch_active"] is True
    [event] = await _fetch(
        trader.pool,
        "SELECT details FROM risk_events WHERE kind = 'kill_switch_on' AND scope = 'global'",
    )
    details = json.loads(event["details"])
    assert details["source"] == "db_latch"

    # the release through the dead trader stays latched: no release fallback exists
    release = await client.request(
        "DELETE", "/api/trading/kill-switch", json={"confirm": "RELEASE", "reason": "drill"}
    )
    assert release.status_code == 503
    assert release.json()["detail"]["code"] == "engine_unreachable"

    # the trader's cleanup tick applies the latch exactly once, to the running session
    await trader.mgr._apply_db_kill_switch_latch()
    assert trader.mgr.kill_switch_active() is True
    assert (session.status, session.positions_at_stop) == ("stopped", 0)
    rows = await _fetch(
        trader.pool,
        "SELECT scope FROM risk_events WHERE kind = 'kill_switch_on' AND actor = 'db-latch'",
    )
    assert [r["scope"] for r in rows] == ["paper", "live"]  # both scope services (Q1)
    await trader.mgr._apply_db_kill_switch_latch()
    assert (
        len(
            await _fetch(
                trader.pool,
                "SELECT 1 FROM risk_events WHERE kind = 'kill_switch_on' AND actor = 'db-latch'",
            )
        )
        == 2
    )

    # the trader returns and the release flows through the backend again
    await trader.serve(trader.port)
    await _poll(lambda: client.get("/readyz"), lambda r: r.status_code == 200)
    released = await client.request(
        "DELETE", "/api/trading/kill-switch", json={"confirm": "RELEASE", "reason": "drill"}
    )
    assert released.status_code == 200, released.json()
    assert released.json()["active"] is False
    [row] = await _fetch(
        trader.pool, "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
    )
    assert row["kill_switch_active"] is False

    # the latch's stop_all flattened the held position: the exit fill landed too
    trades = await _fetch(
        trader.pool, "SELECT side FROM trades WHERE session_id = $1 ORDER BY id", sid
    )
    assert [t["side"] for t in trades] == ["BUY", "SELL"]


async def _start_session_through_the_api(split: Split, market: ReplayMarket) -> str:
    """Anchor the replay, seed the warm-up bars, start the session through the API."""
    client, trader = split.client, split.trader
    now = datetime.now(UTC)
    if now.second >= 57:
        await asyncio.sleep(61 - now.second)
    market.anchor(last_closed_open(datetime.now(UTC), Timeframe.M1))
    repo = MarketDataRepository.from_pool(trader.pool)
    await repo.upsert_closed_bars(
        [("binance", "BTCUSDT", Timeframe.M1, bar.ohlcv()) for bar in market.history]
    )
    started = await client.post(
        "/api/trading/sessions",
        json={"strategy_id": STRATEGY_ID, "trading_mode": "paper", "paper_capital": "1000"},
    )
    assert started.status_code == 201, started.json()
    return str(started.json()["session_id"])


async def _relay_anchored() -> bool:
    """The API app's relay has started and anchored its cursor (its first poll ran)."""
    relay = getattr(app.state, "trades_relay", None)
    return relay is not None and relay.running and relay._cursor is not None


async def _poll(
    request: Callable[[], Awaitable[httpx.Response]],
    ok: Callable[[httpx.Response], bool],
    timeout: float = POLL_TIMEOUT_S,
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    while time.monotonic() < deadline:
        last = await request()
        assert last is not None
        if ok(last):
            return last
        await asyncio.sleep(0.2)
    assert last is not None
    raise AssertionError(f"condition not met within {timeout}s: {last.status_code} {last.text}")


async def _fetch(pool: asyncpg.Pool, sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return list(await conn.fetch(sql, *args))
