"""Seam harness for session tests: real SessionManager, SessionRuntime, StrategyEngine,
RuleBasedStrategy, OrderManager, RiskService and PaperTradingExecutor.

Only the ccxt boundary is fake: the feed's WebSocket client never yields (bars are published on
the session bus the way the feed would), the warm-up REST client serves flat history, and the
market-rules loader returns the fixture's spot markets (lot step 0.00001, no minimum notional).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent, PriceEvent
from hydra.core.types import OHLCV, ExchangeId, Symbol, Timeframe
from hydra.data.bars import last_closed_open
from hydra.execution.client_factory import ClientFactory, SharedRestClients
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.risk.pretrade import RiskConfig
from hydra.risk.state import InMemoryRiskStateStore
from tests.fixtures.binance_kline import FakeRestExchange, FakeWsExchange, binance_markets

H = timedelta(hours=1)
D = Decimal


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def history_rows(end: datetime, n: int, close: float = 100.0) -> list[list[Any]]:
    """*n* flat closed hourly bars ending at *end* (all four prices = *close*)."""
    return [[_ms(end - i * H), close, close, close, close, 10.0] for i in range(n)][::-1]


async def markets_loader(exchange_id: str) -> list[dict[str, Any]]:
    return list(binance_markets().values())


def rule(*conditions: tuple[str, float]) -> dict[str, Any]:
    """An AND group of ``close <comparator> value`` conditions."""
    return {
        "operator": "AND",
        "conditions": [
            {"indicator": "close", "params": {}, "comparator": comp, "value": value}
            for comp, value in conditions
        ],
    }


NEVER = rule(("less_than", 0.0))


def write_strategy(
    strategies_dir: Path,
    sid: str,
    *,
    entry_long: dict[str, Any] | None = None,
    exit_long: dict[str, Any] | None = None,
    entry_short: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    exchange_id: str = "binance",
    market_type: str = "SPOT",
    strategy_class: str = "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    symbols: Iterable[str] = ("BTCUSDT",),
    timeframes: dict[str, str] | None = None,
    max_position_pct: float = 10.0,
) -> str:
    data: dict[str, Any] = {
        "id": sid,
        "name": sid,
        "strategy_class": strategy_class,
        "enabled": False,
        "symbols": list(symbols),
        "exchange": {"exchange_id": exchange_id, "market_type": market_type},
        "timeframes": timeframes or {"primary": "1h"},
        "parameters": {
            "required_history": 2,
            "rules": {
                "entry_long": entry_long or NEVER,
                "exit_long": exit_long or NEVER,
                "entry_short": entry_short,
                "exit_short": None,
            },
        },
        "position_sizing": {"max_position_pct": max_position_pct},
    }
    if risk is not None:
        data["risk"] = risk
    (strategies_dir / f"{sid}.yaml").write_text(yaml.safe_dump(data))
    return sid


@dataclass
class Harness:
    """A started SessionManager over the fakes; ``bar()`` closes the next hourly bar."""

    strategies_dir: Path
    capital: Decimal = field(default_factory=lambda: Decimal("1000"))
    risk_config: RiskConfig | None = None
    history_close: float = 100.0
    history_bars: int = 20
    end: datetime = field(default_factory=lambda: last_closed_open(datetime.now(UTC), Timeframe.H1))
    ws: list[FakeWsExchange] = field(default_factory=list)
    mgr: SessionManager | None = None
    store: InMemoryRiskStateStore | None = None
    _next: dict[str, datetime] = field(default_factory=dict)

    def clients(self) -> ClientFactory:
        rows = history_rows(self.end, self.history_bars, self.history_close)

        def ws_factory(exchange_id: str) -> Any:
            def make() -> FakeWsExchange:
                ex = FakeWsExchange()
                self.ws.append(ex)
                return ex

            return make

        return ClientFactory(
            ws_factory=ws_factory,
            rest_pool=SharedRestClients(rest_factory=lambda eid, creds: FakeRestExchange(rows)),
            markets_loader=markets_loader,
            repository_factory=lambda _pool: None,
            history_window=20,
        )

    async def start(self, **kwargs: Any) -> SessionManager:
        self.store = InMemoryRiskStateStore(
            paper_capital=self.capital, config=self.risk_config or RiskConfig()
        )
        self.mgr = SessionManager(
            db_pool=None,
            config=HydraConfig(),
            clients=self.clients(),
            risk_store=self.store,
            **kwargs,
        )
        await self.mgr.startup()
        return self.mgr

    async def session(self, strategy_id: str, capital: str | None = None) -> TradingSession:
        assert self.mgr is not None
        sid = await self.mgr.start_session(
            strategy_id, "paper", D(capital) if capital is not None else None
        )
        session = self.mgr.get_session(sid)
        assert session is not None
        return session

    async def bar(
        self,
        session: TradingSession,
        close: str,
        *,
        open: str | None = None,
        high: str | None = None,
        low: str | None = None,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
    ) -> OHLCV:
        """Publish the next closed bar exactly as the feed does (closed, not replayed)."""
        ts = self._next.get(session.session_id, self.end) + H
        self._next[session.session_id] = ts
        c = D(close)
        o = D(open) if open is not None else c
        bar = OHLCV(
            open=o,
            high=D(high) if high is not None else max(o, c),
            low=D(low) if low is not None else min(o, c),
            close=c,
            volume=D("10"),
            timestamp=ts,
        )
        assert session._event_bus is not None
        await session._event_bus.publish(
            BarEvent(symbol=Symbol(symbol), timeframe=timeframe, ohlcv=bar, exchange_id="binance")
        )
        await settle()
        return bar

    async def price(
        self,
        session: TradingSession,
        price: str,
        *,
        symbol: str = "BTCUSDT",
        exchange_id: ExchangeId = "binance",
    ) -> None:
        """Publish a live price of the forming candle exactly as the feed does."""
        assert session._event_bus is not None
        await session._event_bus.publish(
            PriceEvent(symbol=Symbol(symbol), price=D(price), exchange_id=exchange_id)
        )
        await settle()

    async def close(self) -> None:
        if self.mgr is not None:
            for s in self.mgr.list_sessions():
                if s.status == "running":
                    await self.mgr.stop_session(s.session_id)
            await self.mgr.graceful_shutdown()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def executor(session: TradingSession) -> PaperTradingExecutor:
    ex = session._executor
    assert isinstance(ex, PaperTradingExecutor)
    return ex


async def usdt(session: TradingSession) -> Decimal:
    return (await executor(session).get_balance())["USDT"]


async def held(session: TradingSession, symbol: str = "BTCUSDT") -> Decimal:
    positions = await executor(session).get_positions(symbol)
    return sum((p.quantity for p in positions), D("0"))
