"""Strategy engine: loads strategies, routes events, aggregates signals."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent, Event
from hydra.core.protocols import EventBus
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig, load_all_strategy_configs
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


def _import_strategy_class(dotted_path: str) -> type[BaseStrategy]:
    """Import a strategy class from its dotted path.

    Example: ``"hydra.strategy.builtin.MomentumRSIMACDStrategy"``
    """
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        msg = f"Invalid strategy class path: {dotted_path}"
        raise ValueError(msg)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        msg = f"{dotted_path} is not a subclass of BaseStrategy"
        raise TypeError(msg)
    return cls


class StrategyEngine:
    """Loads strategies, routes events, aggregates signals.

    The engine subscribes to ``BarEvent`` on the event bus and dispatches
    bars to the correct strategy based on symbol/timeframe matching.
    Collected signals are published back onto the event bus.
    """

    def __init__(
        self,
        config: HydraConfig,
        event_bus: EventBus,
        context: StrategyContext,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._context = context
        self._strategies: dict[str, BaseStrategy] = {}
        self._strategy_configs: dict[str, StrategyConfig] = {}
        self._running = False

    # -- Lifecycle -----------------------------------------------------------

    async def load_strategies(self, config_dir: Path) -> None:
        """Load all strategy configs from *config_dir* and instantiate them."""
        configs = load_all_strategy_configs(config_dir)
        for cfg in configs:
            if not cfg.enabled:
                logger.info("Skipping disabled strategy: %s", cfg.id)
                continue
            await self._instantiate_strategy(cfg)

    async def load_strategy_from_config(self, cfg: StrategyConfig) -> None:
        """Load a single strategy from an already-parsed config object."""
        if not cfg.enabled:
            logger.info("Skipping disabled strategy: %s", cfg.id)
            return
        await self._instantiate_strategy(cfg)

    async def _instantiate_strategy(self, cfg: StrategyConfig) -> None:
        """Import, instantiate, and register a strategy."""
        cls = _import_strategy_class(cfg.strategy_class)
        strategy = cls(config=cfg, context=self._context)
        self._strategies[cfg.id] = strategy
        self._strategy_configs[cfg.id] = cfg
        logger.info("Loaded strategy: %s (%s)", cfg.id, cfg.strategy_class)

    async def start(self) -> None:
        """Start the engine: subscribe to bar events and call on_start."""
        self._running = True
        await self._event_bus.subscribe("bar", self._on_bar_event)
        for strategy in self._strategies.values():
            await strategy.on_start()
        logger.info("Strategy engine started with %d strategies", len(self._strategies))

    async def stop(self) -> None:
        """Stop the engine and all strategies."""
        self._running = False
        await self._event_bus.unsubscribe("bar", self._on_bar_event)
        for strategy in self._strategies.values():
            await strategy.on_stop()
        logger.info("Strategy engine stopped")

    # -- Event routing -------------------------------------------------------

    async def _on_bar_event(self, event: Event) -> None:
        """Route a bar event to matching strategies."""
        if not isinstance(event, BarEvent):
            return
        bar = event
        for sid, strategy in self._strategies.items():
            cfg = self._strategy_configs[sid]
            # Check symbol match
            if str(bar.symbol) not in cfg.symbols:
                continue
            # Check timeframe match
            if bar.timeframe != cfg.timeframes.primary:
                continue
            # Update context with the new bar
            if bar.ohlcv is not None:
                self._context.add_bar(str(bar.symbol), bar.timeframe, bar.ohlcv)
            # Dispatch to strategy
            try:
                signals = await strategy.on_bar(bar)
            except Exception:
                logger.exception("Error in strategy %s on_bar", sid)
                continue
            # Publish signals
            for signal in signals:
                await self._event_bus.publish(signal)

    # -- Hot reload ----------------------------------------------------------

    async def reload_strategy(self, strategy_id: str) -> None:
        """Hot-reload a strategy by re-importing its class and re-instantiating."""
        if strategy_id not in self._strategy_configs:
            logger.warning("Cannot reload unknown strategy: %s", strategy_id)
            return
        cfg = self._strategy_configs[strategy_id]
        old = self._strategies.get(strategy_id)
        if old is not None:
            await old.on_stop()
        # Re-import (force module reload)
        module_path, _, class_name = cfg.strategy_class.rpartition(".")
        module = importlib.import_module(module_path)
        importlib.reload(module)
        cls = getattr(module, class_name)
        new_strategy = cls(config=cfg, context=self._context)
        self._strategies[strategy_id] = new_strategy
        if self._running:
            await new_strategy.on_start()
        logger.info("Reloaded strategy: %s", strategy_id)

    # -- Accessors -----------------------------------------------------------

    def get_strategy(self, strategy_id: str) -> BaseStrategy | None:
        """Return a loaded strategy by ID, or ``None``."""
        return self._strategies.get(strategy_id)

    def get_all_strategies(self) -> dict[str, BaseStrategy]:
        """Return all loaded strategies."""
        return dict(self._strategies)
