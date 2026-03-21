"""Abstract base classes for all Hydra strategies.

Two strategy paradigms are supported:

1. **Signal-driven** (``BaseStrategy``): ``on_bar`` returns entry/exit signals.
   The session manager converts signals to orders.  Suitable for rule-based
   and indicator-based strategies.

2. **Order-management** (``OrderManagementStrategy``): ``on_bar`` returns
   explicit order actions (place, cancel, modify).  The strategy directly
   manages its own order lifecycle.  Suitable for DCA bots, grid bots,
   and market-making strategies.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from hydra.core.events import (
    BarEvent,
    EntrySignal,
    ExitSignal,
    OrderFillEvent,
    TradeEvent,
)
from hydra.core.types import OrderType
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order action types for OrderManagementStrategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    """Action: place a new order."""

    symbol: str
    side: str  # "BUY" | "SELL"
    order_type: str = OrderType.MARKET
    quantity: Decimal = Decimal("0")
    price: Decimal | None = None
    stop_price: Decimal | None = None
    params: dict[str, object] | None = None
    tag: str = ""  # Strategy-defined tag for tracking (e.g. "safety_order_3")


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Action: cancel a pending order."""

    order_id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class ModifyOrder:
    """Action: modify a pending order (cancel + replace)."""

    order_id: str
    symbol: str
    new_price: Decimal | None = None
    new_quantity: Decimal | None = None


OrderAction = PlaceOrder | CancelOrder | ModifyOrder


class BaseStrategy(ABC):
    """Abstract base strategy class.

    Concrete strategies must implement :meth:`on_bar` and the
    :attr:`required_history` property.  All other event handlers have
    default no-op implementations.
    """

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        self._config = config
        self._context = context

    # -- Properties ----------------------------------------------------------

    @property
    def strategy_id(self) -> str:
        return self._config.id

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def context(self) -> StrategyContext:
        return self._context

    # -- Abstract methods ----------------------------------------------------

    @abstractmethod
    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        """Process a completed bar and optionally return signals."""
        ...

    @property
    @abstractmethod
    def required_history(self) -> int:
        """Number of bars needed before generating signals."""
        ...

    # -- Default event handlers (overridable) --------------------------------

    async def on_trade(self, trade: TradeEvent) -> list[EntrySignal | ExitSignal]:
        """Handle a trade tick. Default: no signals."""
        return []

    async def on_fill(self, fill: OrderFillEvent) -> None:
        """Handle an order fill notification. Default: no-op."""
        return

    async def on_start(self) -> None:
        """Called when the strategy is started. Default: no-op."""
        return

    async def on_stop(self) -> None:
        """Called when the strategy is stopped. Default: no-op."""
        return


class OrderManagementStrategy(ABC):
    """Base class for strategies that manage their own orders.

    Unlike ``BaseStrategy`` which returns entry/exit signals,
    ``OrderManagementStrategy`` returns explicit order actions
    (place, cancel, modify).  The session manager executes them directly.

    Suitable for DCA bots, grid bots, and market-making strategies.
    """

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        self._config = config
        self._context = context

    @property
    def strategy_id(self) -> str:
        return self._config.id

    @property
    def config(self) -> StrategyConfig:
        return self._config

    @property
    def context(self) -> StrategyContext:
        return self._context

    @abstractmethod
    async def on_bar(self, bar: BarEvent) -> list[OrderAction]:
        """Process a bar and return order actions to execute."""
        ...

    @property
    @abstractmethod
    def required_history(self) -> int:
        """Number of bars needed before generating orders."""
        ...

    async def on_fill(self, fill: OrderFillEvent) -> list[OrderAction]:
        """Handle an order fill. Return follow-up actions (e.g. place TP after entry)."""
        return []

    async def on_start(self) -> list[OrderAction]:
        """Called when the strategy starts. Return initial orders (e.g. grid setup)."""
        return []

    async def on_stop(self) -> list[OrderAction]:
        """Called on stop. Return cleanup orders (e.g. cancel all pending)."""
        return []
