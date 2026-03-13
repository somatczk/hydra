"""Protocol (ABC) definitions for all Hydra modules.

These serve as contracts that modules implement. Each module depends only on
``hydra.core`` and programmes against these protocols, enabling loose coupling
and easy testing via mocks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from hydra.core.events import Event, OrderFillEvent
from hydra.core.types import (
    OHLCV,
    Direction,
    ExchangeId,
    MarketType,
    OrderRequest,
    Position,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

type EventCallback = Callable[[Event], Awaitable[None]]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@runtime_checkable
class DataProvider(Protocol):
    """Fetches and streams historical and live market data."""

    async def get_bars(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        exchange_id: ExchangeId = "binance",
    ) -> Sequence[OHLCV]: ...

    async def get_latest_bar(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        exchange_id: ExchangeId = "binance",
    ) -> OHLCV | None: ...

    async def subscribe(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        exchange_id: ExchangeId = "binance",
    ) -> None: ...


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


@runtime_checkable
class Strategy(Protocol):
    """Trading strategy that reacts to market events."""

    @property
    def strategy_id(self) -> str: ...

    @property
    def required_history(self) -> int:
        """Number of historical bars needed before going live."""
        ...

    async def on_bar(self, symbol: Symbol, ohlcv: OHLCV) -> None: ...

    async def on_trade(self, symbol: Symbol, price: Decimal, quantity: Decimal) -> None: ...

    async def on_fill(self, fill: OrderFillEvent) -> None: ...

    async def on_start(self) -> None: ...

    async def on_stop(self) -> None: ...


@runtime_checkable
class StrategyEngine(Protocol):
    """Manages strategy lifecycle and dispatching."""

    async def load_strategies(self, config: dict[str, Any]) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@runtime_checkable
class Executor(Protocol):
    """Submits orders to exchanges."""

    async def submit_order(self, order: OrderRequest) -> str:
        """Submit an order, returns the exchange order ID."""
        ...

    async def cancel_order(self, order_id: str, exchange_id: ExchangeId = "binance") -> bool:
        """Cancel an open order. Returns True on success."""
        ...

    async def get_open_orders(
        self,
        symbol: Symbol | None = None,
        exchange_id: ExchangeId = "binance",
    ) -> Sequence[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


@runtime_checkable
class RiskManager(Protocol):
    """Pre-trade and portfolio-level risk checks."""

    async def check_order(self, order: OrderRequest) -> tuple[bool, str]:
        """Returns (approved, reason)."""
        ...

    async def get_circuit_breaker_status(self) -> dict[str, Any]: ...


@runtime_checkable
class PositionSizer(Protocol):
    """Calculates position sizes based on risk parameters."""

    async def calculate_size(
        self,
        symbol: Symbol,
        direction: Direction,
        entry_price: Decimal,
        stop_price: Decimal | None = None,
        exchange_id: ExchangeId = "binance",
        market_type: MarketType = MarketType.SPOT,
    ) -> Decimal: ...


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


@runtime_checkable
class PortfolioTracker(Protocol):
    """Tracks positions and portfolio value."""

    async def get_position(
        self,
        symbol: Symbol,
        strategy_id: str | None = None,
        exchange_id: ExchangeId = "binance",
    ) -> Position | None: ...

    async def get_all_positions(
        self,
        strategy_id: str | None = None,
        exchange_id: ExchangeId | None = None,
    ) -> Sequence[Position]: ...

    async def get_portfolio_value(self) -> Decimal: ...


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBus(Protocol):
    """Async publish/subscribe event bus."""

    async def publish(self, event: Event) -> None: ...

    async def subscribe(
        self,
        event_type: str,
        callback: EventCallback,
    ) -> None: ...

    async def unsubscribe(
        self,
        event_type: str,
        callback: EventCallback,
    ) -> None: ...


# ---------------------------------------------------------------------------
# ML
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelServer(Protocol):
    """Serves ML model predictions."""

    async def predict(
        self,
        model_name: str,
        features: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def get_model_info(self, model_name: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Time source abstraction for live vs backtest."""

    def now(self) -> datetime: ...

    @property
    def is_backtest(self) -> bool: ...
