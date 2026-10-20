"""Shared builders for RiskService tests: real paper executors, in-memory store, injected rules."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.core.events import Event, OrderFillEvent
from hydra.core.types import MarketType, OrderRequest, OrderType, Position, Side, Symbol
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.pretrade import RiskConfig
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore, RiskState

BTC = "BTCUSDT"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

BINANCE_MARKETS: list[dict[str, Any]] = [
    {
        "id": sym,
        "symbol": f"{sym[:-4]}/USDT",
        "spot": True,
        "precision": {"amount": 1e-05, "price": 0.01},
        "limits": {
            "amount": {"min": 1e-05, "max": 9000.0},
            "market": {"min": 0.0, "max": 120.0},
            "cost": {"min": 5.0, "max": None},
        },
    }
    for sym in ("BTCUSDT", "ETHUSDT")
]


class YieldingPaperExecutor(PaperTradingExecutor):
    """Paper executor whose calls yield to the loop, as DB-backed and exchange calls do."""

    async def get_balance(self) -> dict[str, Decimal]:
        await asyncio.sleep(0)
        return await super().get_balance()

    async def get_positions(self, symbol: str | None = None) -> list[Position]:
        await asyncio.sleep(0)
        return await super().get_positions(symbol)

    async def create_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return await super().create_order(*args, **kwargs)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


async def _markets(exchange_id: str) -> Iterable[Mapping[str, Any]]:
    return BINANCE_MARKETS


def rules_cache(clock: Clock) -> MarketRulesCache:
    return MarketRulesCache(loader=_markets, clock=clock)


def permissive(**overrides: Any) -> RiskConfig:
    base: dict[str, Any] = {
        "max_position_pct": Decimal("1"),
        "max_portfolio_heat": Decimal("1"),
        "max_orders_per_minute": 1000,
    }
    base.update(overrides)
    return RiskConfig(**base)


@dataclass
class FillBus:
    """Event bus stand-in that forwards fills to the RiskService, as the session wiring does."""

    risk: RiskService | None
    session_id: str
    events: list[Event] = field(default_factory=list)

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        if self.risk is not None and isinstance(event, OrderFillEvent):
            await self.risk.on_fill(self.session_id, event)

    @property
    def fills(self) -> list[OrderFillEvent]:
        return [e for e in self.events if isinstance(e, OrderFillEvent)]


@dataclass
class Account:
    svc: RiskService
    store: InMemoryRiskStateStore
    clock: Clock
    executors: dict[str, PaperTradingExecutor] = field(default_factory=dict)
    managers: dict[str, OrderManager] = field(default_factory=dict)
    buses: dict[str, FillBus] = field(default_factory=dict)
    bar: int = 0

    async def add_session(self, sid: str, capital: str) -> PaperTradingExecutor:
        ex = YieldingPaperExecutor(
            exchange_id="binance",
            initial_balances={"USDT": Decimal(capital)},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
            session_id=sid,
        )
        for sym in (BTC, "ETHUSDT"):
            price = self.svc.mark("binance", sym)
            if price is not None:
                ex.set_market_price(sym, price)
        await self.svc.register_session(
            sid, ex, allocated=Decimal(capital), symbols=[BTC], exchange_id="binance"
        )
        bus = FillBus(self.svc, sid)
        self.executors[sid] = ex
        self.buses[sid] = bus
        self.managers[sid] = OrderManager(
            executor=ex, event_bus=bus, dedup_window=0.0, risk=self.svc, session_id=sid
        )
        return ex

    async def mark(self, price: str, symbol: str = BTC) -> None:
        self.bar += 1
        for ex in self.executors.values():
            ex.set_market_price(symbol, Decimal(price))
        await self.svc.on_mark("binance", symbol, Decimal(price), T0 + timedelta(hours=self.bar))

    async def submit(
        self, sid: str, side: Side, qty: str, *, reduce_only: bool = False
    ) -> str | None:
        return await self.managers[sid].submit_order(order(side, qty, reduce_only=reduce_only))


def order(side: Side, qty: str, *, reduce_only: bool = False, symbol: str = BTC) -> OrderRequest:
    return OrderRequest(
        symbol=Symbol(symbol),
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        strategy_id="s",
        exchange_id="binance",
        market_type=MarketType.SPOT,
        reduce_only=reduce_only,
    )


async def make_account(
    config: RiskConfig | None = None,
    *,
    capital: str = "10000",
    state: RiskState | None = None,
    kill_switch: bool = False,
    clock: Clock | None = None,
    **kw: Any,
) -> Account:
    clock = clock or Clock()
    store = InMemoryRiskStateStore(
        paper_capital=Decimal(capital),
        config=config or permissive(),
        kill_switch=kill_switch,
        state=state,
    )
    svc = RiskService(store, clock=clock, market_rules=rules_cache(clock), **kw)
    await svc.load()
    return Account(svc=svc, store=store, clock=clock)
