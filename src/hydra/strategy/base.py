"""Abstract base class for all Hydra strategies."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from hydra.core.events import (
    BarEvent,
    EntrySignal,
    ExitSignal,
    OrderFillEvent,
    TradeEvent,
)
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


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
