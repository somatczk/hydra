"""PAPER SMOKE: the P1 acceptance test (CI job "Paper Smoke", ``make smoke``).

The real engine (``hydra.dashboard.api.app`` with its lifespan: config, credential key, pool,
Alembic, RiskService, SessionManager recovery, HealthMonitor) runs on a real TimescaleDB with the
env romulus gives it (DATABASE_URL, REDIS_URL, HYDRA_CREDENTIAL_KEY, HYDRA_API_TOKEN, token auth)
and is driven over HTTP with the bearer token. Only the exchange is replaced, through the
``app.state.session_clients`` seam: committed Binance BTCUSDT 1m bars replayed over a
ccxt.pro-shaped WebSocket fake (with synthetic intra-bar updates), REST warm-up over the same bars
and Binance spot market rules. ``ts.ohlcv_1m`` is seeded with the warm-up history. No test calls
Binance.

Expected trades come from a reference evaluation of the strategy (close crosses SMA(5)) over the
fixture's final closes, and expected prices and balances from the paper fill model: market fills at
the closed-bar close +/- 0.1 % slippage, 0.1 % taker fee in USDT, entry notional = 10 % of the
sleeve floored to the 0.00001 BTC lot.

Live bars are dated from the current minute onwards, so a healthy ``/readyz`` cannot fail on
closed-bar age; that threshold is covered by ``tests/unit/test_dashboard/test_health_routes.py``
and here by the monitor clock. The feed's own staleness is real: the replay stalls once its
released frames run out, and the feed watchdog flags ``ws_silence`` after 60 s on the wall clock.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi import FastAPI
from prometheus_client.parser import text_string_to_metric_families

from hydra.core import metrics
from hydra.core.events import BarEvent, Event
from hydra.core.types import Timeframe
from hydra.dashboard import api, lifespan
from hydra.dashboard.health import HealthMonitor
from hydra.data.bars import last_closed_open
from hydra.data.storage import MarketDataRepository
from hydra.execution.session_manager import SessionManager
from tests.e2e.replay_exchange import ReplayMarket
from tests.fixtures.universe import offline_refresher_factory

WARMUP = 30
WS_SILENCE_WAIT_S = 120.0
CAPITAL = Decimal("1000")
FRACTION = Decimal("0.10")
FEE = Decimal("0.001")
SLIPPAGE = Decimal("0.001")
FEE_BUFFER = Decimal("0.002")
LOT = Decimal("0.00001")
STOP_FRACTION = Decimal("0.05")
CENT8 = Decimal("0.00000001")
STRATEGY_ID = "smoke-sma5"


# ---------------------------------------------------------------------------
# Reference model
# ---------------------------------------------------------------------------


def _sma5(closes: list[float], k: int) -> float:
    return sum(closes[k - 4 : k + 1]) / 5


def _crosses(closes: list[float], k: int, *, above: bool) -> bool:
    prev, cur = closes[k - 1], closes[k]
    prev_sma, cur_sma = _sma5(closes, k - 1), _sma5(closes, k)
    if above:
        return prev <= prev_sma and cur > cur_sma
    return prev >= prev_sma and cur < cur_sma


@dataclass
class Reference:
    """Signals of the smoke strategy over the fixture: indexes into the live bars."""

    market: ReplayMarket

    @property
    def closes(self) -> list[float]:
        return [float(b.close) for b in self.market.bars]

    def entries_exits(self, upto: int, *, flat_until: int = -1) -> list[tuple[str, int]]:
        """('BUY'|'SELL', live index) up to ``upto``; entries before ``flat_until`` are refused."""
        closes = self.closes
        events: list[tuple[str, int]] = []
        holding = False
        stop = Decimal(0)
        for j, bar in enumerate(self.market.live[: upto + 1]):
            k = WARMUP + j
            if holding:
                assert bar.low > stop, f"protective stop would trigger at live bar {j}"
                if _crosses(closes, k, above=False):
                    events.append(("SELL", j))
                    holding = False
            elif _crosses(closes, k, above=True) and j >= flat_until:
                events.append(("BUY", j))
                holding = True
                stop = bar.close * (1 + SLIPPAGE) * (1 - STOP_FRACTION)
        return events

    def entry_signals(self, upto: int) -> list[int]:
        """Live bars where the strategy signals an entry while flat (no fills at all)."""
        closes = self.closes
        return [j for j in range(upto + 1) if _crosses(closes, WARMUP + j, above=True)]

    def trap(self, before: int) -> int:
        """A live bar whose intra-bar update (close = high) crosses SMA(5) but whose final close
        does not: a feed that acted on forming candles would trade there."""
        for j in range(before):
            k = WARMUP + j
            partial = [*self.closes[:k], float(self.market.live[j].high)]
            if _crosses(partial, k, above=True) and not _crosses(self.closes, k, above=True):
                return j
        raise AssertionError("fixture has no intra-bar trap before the first entry")


@dataclass(frozen=True)
class Fill:
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal


def entry_fill(close: Decimal, sleeve: Decimal = CAPITAL) -> Fill:
    budget = min(sleeve * FRACTION, sleeve / (1 + FEE_BUFFER))
    quantity = (budget / close).quantize(LOT, rounding=ROUND_DOWN)
    price = close * (1 + SLIPPAGE)
    return Fill("BUY", quantity, price, quantity * price * FEE)


def exit_fill(close: Decimal, quantity: Decimal) -> Fill:
    price = close * (1 - SLIPPAGE)
    return Fill("SELL", quantity, price, quantity * price * FEE)


def db8(value: Decimal) -> Decimal:
    """NUMERIC(24,8) as Postgres rounds it (half away from zero)."""
    return value.quantize(CENT8, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Engine harness
# ---------------------------------------------------------------------------


@dataclass
class MonitorClock:
    offset: float = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class BarWatch:
    """Subscribed last on a session bus: a bar it sees has been fully processed by the session."""

    def __init__(self) -> None:
        self.seen: list[datetime] = []
        self._changed = asyncio.Event()

    async def __call__(self, event: Event) -> None:
        if isinstance(event, BarEvent) and not event.replay and event.ohlcv is not None:
            self.seen.append(event.ohlcv.timestamp)
            self._changed.set()

    async def wait_for(self, ts: datetime, timeout: float = 30.0) -> None:
        async with asyncio.timeout(timeout):
            while ts not in self.seen:
                self._changed.clear()
                await self._changed.wait()


@dataclass
class Engine:
    app: FastAPI
    client: httpx.AsyncClient
    market: ReplayMarket
    clock: MonitorClock
    dsn: str
    watches: dict[str, BarWatch] = field(default_factory=dict)

    @property
    def mgr(self) -> SessionManager:
        mgr = self.app.state.session_manager
        assert isinstance(mgr, SessionManager)
        return mgr

    async def watch(self, session_id: str) -> BarWatch:
        session = self.mgr.get_session(session_id)
        assert session is not None and session._event_bus is not None
        watch = BarWatch()
        await session._event_bus.subscribe("bar", watch)
        self.watches[session_id] = watch
        return watch

    async def replay_through(self, session_id: str, live_index: int) -> None:
        self.market.release_through(live_index)
        await self.watches[session_id].wait_for(self.market.live[live_index].open_time)

    async def start(self, strategy_id: str = STRATEGY_ID) -> str:
        resp = await self.client.post(
            "/api/trading/sessions",
            json={"strategy_id": strategy_id, "trading_mode": "paper", "paper_capital": "1000"},
        )
        assert resp.status_code == 201, resp.json()
        session_id = str(resp.json()["session_id"])
        await self.watch(session_id)
        return session_id

    async def metric(self, name: str, **labels: str) -> float | None:
        text = (await self.client.get("/metrics")).text
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                if sample.name == name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return float(sample.value)
        return None

    async def readyz(self) -> httpx.Response:
        """/readyz after one background monitor run, which also writes the gauges."""
        monitor = self.app.state.health_monitor
        assert isinstance(monitor, HealthMonitor)
        await monitor.run_once()
        return await self.client.get("/readyz")

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(self.dsn)
        try:
            return list(await conn.fetch(sql, *args))
        finally:
            await conn.close()


def write_strategy(directory: Path, sid: str, *, exchange_id: str = "binance") -> None:
    def cross(comparator: str) -> dict[str, Any]:
        condition = {
            "indicator": "close",
            "params": {},
            "comparator": comparator,
            "value": "sma:period=5",
        }
        return {"operator": "AND", "conditions": [condition]}

    doc = {
        "id": sid,
        "name": sid,
        "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        "enabled": False,
        "symbols": ["BTCUSDT"],
        "exchange": {"exchange_id": exchange_id, "market_type": "SPOT"},
        "timeframes": {"primary": "1m"},
        "parameters": {
            "required_history": 10,
            "rules": {
                "entry_long": cross("crosses_above"),
                "exit_long": cross("crosses_below"),
                "entry_short": None,
                "exit_short": None,
            },
        },
        "position_sizing": {"max_position_pct": 10.0},
    }
    (directory / f"{sid}.yaml").write_text(yaml.safe_dump(doc))


@pytest.fixture()
def romulus_env(fresh_database: str, redis_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    for name in ("HYDRA_ENV", "HYDRA_CONFIG_DIR", "HYDRA_API__TRUSTED_PROXY_CIDRS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", fresh_database)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("HYDRA_API_TOKEN", secrets.token_urlsafe(32))
    monkeypatch.setenv("HYDRA_API__AUTH_MODE", "token")
    return fresh_database


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


@asynccontextmanager
async def engine(dsn: str, market: ReplayMarket, clock: MonitorClock) -> AsyncIterator[Engine]:
    """One engine process: the real lifespan with the replay seams and the monitor clock."""
    app = api.app

    def monitor(target: FastAPI) -> HealthMonitor:
        return HealthMonitor(target, clock=clock)

    app.state.session_clients = market.clients(history_window=WARMUP)
    app.state.health_monitor_factory = monitor
    app.state.universe_refresher_factory = offline_refresher_factory
    try:
        async with lifespan.run(app):
            token = app.state.config.api_token
            assert token is not None
            headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://engine", headers=headers
            ) as client:
                yield Engine(app, client, market, clock, dsn)
    finally:
        app.state.session_clients = None
        app.state.health_monitor_factory = None
        app.state.universe_refresher_factory = None


async def anchored_market(app: FastAPI, market: ReplayMarket) -> None:
    """Rebase the fixture to the newest closed minute and seed the warm-up bars into the DB."""
    now = datetime.now(UTC)
    if now.second >= 57:
        await asyncio.sleep(61 - now.second)
    market.anchor(last_closed_open(datetime.now(UTC), Timeframe.M1))
    repo = MarketDataRepository.from_pool(app.state.db_pool)
    await repo.upsert_closed_bars(
        [("binance", "BTCUSDT", Timeframe.M1, bar.ohlcv()) for bar in market.history]
    )


def state_free(state: str | dict[str, Any], asset: str) -> Decimal:
    doc = json.loads(state) if isinstance(state, str) else state
    return Decimal(doc["free"].get(asset, "0"))


async def trades(eng: Engine, session_id: str) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    rows = await eng.fetch(
        "SELECT side, quantity, price, fee FROM trades WHERE session_id = $1 ORDER BY fill_seq",
        session_id,
    )
    return [(r["side"], r["quantity"], r["price"], r["fee"]) for r in rows]


def as_row(fill: Fill) -> tuple[str, Decimal, Decimal, Decimal]:
    return (fill.side, db8(fill.quantity), db8(fill.price), db8(fill.fee))


async def open_lots(eng: Engine, session_id: str) -> int:
    rows = await eng.fetch(
        "SELECT count(*) AS n FROM positions WHERE session_id = $1 AND quantity > 0", session_id
    )
    return int(rows[0]["n"])


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_round_trip_on_closed_bars_then_a_stalled_feed_fails_readiness(
    romulus_env: str, strategies_dir: Path
) -> None:
    write_strategy(strategies_dir, STRATEGY_ID)
    market = ReplayMarket(WARMUP)
    clock = MonitorClock()
    async with engine(romulus_env, market, clock) as eng:
        await anchored_market(eng.app, market)
        ref = Reference(market)
        (_, i), (_, j) = ref.entries_exits(len(market.live) - 1)[:2]
        trap = ref.trap(before=i)
        sid = await eng.start()

        await eng.replay_through(sid, i)
        entry = entry_fill(market.live[i].close)
        assert await trades(eng, sid) == [as_row(entry)]
        session = eng.mgr.get_session(sid)
        assert session is not None
        stops = await session._executor.fetch_open_orders("BTCUSDT")
        assert [(o["type"], Decimal(str(o["amount"]))) for o in stops] == [
            ("STOP_MARKET", entry.quantity)
        ]
        assert Decimal(str(stops[0]["stop_price"])) == entry.price * (1 - STOP_FRACTION)

        await eng.replay_through(sid, j)
        exit_ = exit_fill(market.live[j].close, entry.quantity)
        net = exit_.quantity * exit_.price - entry.quantity * entry.price - exit_.fee - entry.fee
        rows = await eng.fetch(
            "SELECT side, net_pnl, timestamp FROM trades WHERE session_id = $1 ORDER BY fill_seq",
            sid,
        )
        assert await trades(eng, sid) == [as_row(entry), as_row(exit_)]
        assert [r["timestamp"] for r in rows] == [
            market.live[i].open_time,
            market.live[j].open_time,
        ]
        assert market.live[trap].open_time not in [r["timestamp"] for r in rows]
        assert rows[1]["net_pnl"] == db8(net)
        [account] = await eng.fetch("SELECT state FROM paper_accounts WHERE session_id = $1", sid)
        assert state_free(account["state"], "USDT") == (
            CAPITAL
            - entry.quantity * entry.price
            - entry.fee
            + exit_.quantity * exit_.price
            - exit_.fee
        )
        assert state_free(account["state"], "BTC") == 0
        assert await open_lots(eng, sid) == 0
        assert await session._executor.fetch_open_orders("BTCUSDT") == []
        assert await eng.metric("hydra_trades_total", strategy_id=STRATEGY_ID, side="BUY") == 1
        assert await eng.metric("hydra_trades_total", strategy_id=STRATEGY_ID, side="SELL") == 1
        assert await eng.metric("hydra_trade_pnl_count", strategy_id=STRATEGY_ID) == 1
        loss = 1.0 if net <= 0 else 0.0
        pnl_le0 = await eng.metric("hydra_trade_pnl_bucket", strategy_id=STRATEGY_ID, le="0.0")
        assert pnl_le0 == loss
        feed = {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1m"}
        bar_ts = market.live[j].open_time.timestamp()
        assert await eng.metric("hydra_feed_last_closed_bar_timestamp_seconds", **feed) == bar_ts

        ready = await eng.readyz()
        assert ready.status_code == 200, ready.json()
        [stream] = ready.json()["checks"]["feeds"]["streams"]
        assert (stream["session_id"], stream["live"], stream["stale_reasons"]) == (sid, True, [])
        assert market.served == market.allowed

        async with asyncio.timeout(WS_SILENCE_WAIT_S):
            while (await eng.mgr.health_snapshot()).sessions[0].feed_live:
                await asyncio.sleep(1)
        silent = await eng.readyz()
        assert silent.status_code == 503
        [stream] = silent.json()["checks"]["feeds"]["streams"]
        assert (stream["session_id"], stream["live"], stream["ok"]) == (sid, False, False)
        assert "ws_silence" in stream["stale_reasons"]
        assert not {"message_age", "closed_bar_age"} & set(stream["stale_reasons"])
        assert silent.json()["checks"]["sessions"]["ok"] is True
        assert await eng.metric("hydra_ready", check="feeds") == 0.0

        message_ts = await eng.metric("hydra_feed_last_message_timestamp_seconds", **feed)
        assert message_ts is not None
        clock.offset = max(message_ts + 121, bar_ts + 181) - time.time()
        stalled = await eng.client.get("/readyz")
        assert stalled.status_code == 503
        [stream] = stalled.json()["checks"]["feeds"]["streams"]
        assert {"ws_silence", "message_age", "closed_bar_age"} <= set(stream["stale_reasons"])
        assert (await eng.client.get("/livez")).status_code == 200


async def test_risk_rejection_then_the_kill_switch_flattens_and_latches(
    romulus_env: str, strategies_dir: Path
) -> None:
    write_strategy(strategies_dir, STRATEGY_ID)
    market = ReplayMarket(WARMUP)
    async with engine(romulus_env, market, MonitorClock()) as eng:
        await anchored_market(eng.app, market)
        ref = Reference(market)
        refused, accepted = ref.entry_signals(len(market.live) - 1)[:2]
        assert ref.entries_exits(accepted, flat_until=accepted) == [("BUY", accepted)]
        put = await eng.client.put("/api/risk/config", json={"max_portfolio_heat": 0.0001})
        assert put.status_code == 200, put.json()
        sid = await eng.start()

        await eng.replay_through(sid, refused)
        assert await trades(eng, sid) == []
        rejected = {"check": "portfolio_heat", "intent": "open"}
        assert await eng.metric("hydra_risk_rejections_total", **rejected) == 1
        # live bars are a minute apart; OrderManager refuses an identical entry within 1 s
        await asyncio.sleep(1.1)

        put = await eng.client.put("/api/risk/config", json={"max_portfolio_heat": 0.30})
        assert put.status_code == 200, put.json()
        await eng.replay_through(sid, accepted)
        entry = entry_fill(market.live[accepted].close)
        assert await trades(eng, sid) == [as_row(entry)]
        assert await eng.metric("hydra_risk_rejections_total", **rejected) == 1
        assert await eng.metric("hydra_risk_rejections_total", check="duplicate") is None

        kill = await eng.client.post("/api/trading/kill-switch")
        assert kill.status_code == 200, kill.json()
        assert (kill.json()["sessions_stopped"], kill.json()["positions_remaining"]) == (1, 0)
        assert kill.json()["persisted"] is True
        flatten = exit_fill(market.live[accepted].close, entry.quantity)
        assert await trades(eng, sid) == [as_row(entry), as_row(flatten)]
        assert await open_lots(eng, sid) == 0
        [row] = await eng.fetch("SELECT status FROM trading_sessions WHERE id = $1", sid)
        assert row["status"] == "stopped"
        [risk] = await eng.fetch("SELECT kill_switch_active FROM risk_config")
        assert risk["kill_switch_active"] is True
        assert await eng.metric("hydra_kill_switch_active") == 1.0

        again = await eng.client.post(
            "/api/trading/sessions",
            json={"strategy_id": STRATEGY_ID, "trading_mode": "paper", "paper_capital": "100"},
        )
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "kill_switch_active"


async def test_restart_recovers_the_position_without_a_zombie_and_stop_flattens(
    romulus_env: str, strategies_dir: Path
) -> None:
    write_strategy(strategies_dir, STRATEGY_ID)
    market = ReplayMarket(WARMUP)
    clock = MonitorClock()
    async with engine(romulus_env, market, clock) as eng:
        await anchored_market(eng.app, market)
        (_, i) = Reference(market).entries_exits(len(market.live) - 1)[0]
        sid = await eng.start()
        await eng.replay_through(sid, i)
        entry = entry_fill(market.live[i].close)
        assert await trades(eng, sid) == [as_row(entry)]

    async with engine(romulus_env, market, clock) as eng:
        session = eng.mgr.get_session(sid)
        assert session is not None
        assert (session.status, session.task_alive) == ("running", True)
        detail = (await eng.client.get(f"/api/trading/sessions/{sid}/detail")).json()
        assert [(p["symbol"], Decimal(str(p["quantity"]))) for p in detail["positions"]] == [
            ("BTCUSDT", entry.quantity)
        ]
        stops = await session._executor.fetch_open_orders("BTCUSDT")
        assert [(o["type"], Decimal(str(o["amount"]))) for o in stops] == [
            ("STOP_MARKET", entry.quantity)
        ]
        assert Decimal(str(stops[0]["stop_price"])) == entry.price * (1 - STOP_FRACTION)
        assert session._runtime is not None
        armed = session._runtime.protect.armed("BTCUSDT")
        assert armed is not None and armed.order_id == stops[0]["id"]
        ready = await eng.readyz()
        assert ready.status_code == 200, ready.json()
        sessions = ready.json()["checks"]["sessions"]
        assert sessions["zombies"] == []
        assert sessions["sessions"] == [
            {
                "session_id": sid,
                "strategy_id": STRATEGY_ID,
                "mode": "paper",
                "desired": True,
                "supervised": True,
            }
        ]
        labels = {"session_id": sid, "strategy_id": STRATEGY_ID, "mode": "paper"}
        assert await eng.metric("hydra_session_supervised", **labels) == 1.0
        assert await eng.metric("hydra_session_desired", **labels) == 1.0

        stop = await eng.client.delete(f"/api/trading/sessions/{sid}")
        assert stop.status_code == 200, stop.json()
        assert (stop.json()["status"], stop.json()["positions_at_stop"]) == ("stopped", 0)
        flatten = exit_fill(market.live[i].close, entry.quantity)
        assert await trades(eng, sid) == [as_row(entry), as_row(flatten)]
        [row] = await eng.fetch(
            "SELECT status, positions_at_stop FROM trading_sessions WHERE id = $1", sid
        )
        assert (row["status"], row["positions_at_stop"]) == ("stopped", 0)
        assert await open_lots(eng, sid) == 0
        assert (await eng.readyz()).json()["checks"]["sessions"]["sessions"] == []


async def test_only_binance_paper_sessions_start(romulus_env: str, strategies_dir: Path) -> None:
    write_strategy(strategies_dir, STRATEGY_ID)
    write_strategy(strategies_dir, "smoke-bybit", exchange_id="bybit")
    async with engine(romulus_env, ReplayMarket(WARMUP), MonitorClock()) as eng:
        bybit = await eng.client.post(
            "/api/trading/sessions", json={"strategy_id": "smoke-bybit", "trading_mode": "paper"}
        )
        assert bybit.status_code == 400
        assert bybit.json()["detail"]["code"] == "unsupported_venue"
        live = await eng.client.post(
            "/api/trading/sessions", json={"strategy_id": STRATEGY_ID, "trading_mode": "live"}
        )
        assert live.status_code == 409
        assert live.json()["detail"]["code"] == "live_gate_required"
        assert await eng.fetch("SELECT id FROM trading_sessions") == []
        assert (await eng.client.get("/readyz")).status_code == 200
