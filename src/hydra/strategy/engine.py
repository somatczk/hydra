"""Strategy engine: loads strategies, routes events, aggregates signals."""

from __future__ import annotations

import importlib
import inspect
import logging
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent, Event
from hydra.core.protocols import EventBus
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig, load_all_strategy_configs
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


ALLOWED_CLASS_PREFIX = "hydra.strategy.builtin."


def _import_strategy_class(dotted_path: str) -> type[BaseStrategy]:
    """Import a strategy class from its dotted path (``hydra.strategy.builtin.*`` only).

    Example: ``"hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy"``. The prefix is checked
    before the import, which would otherwise run any module's code.
    """
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        msg = f"Invalid strategy class path: {dotted_path}"
        raise ValueError(msg)
    if not module_path.startswith(ALLOWED_CLASS_PREFIX):
        msg = f"strategy_class {dotted_path!r} must be under {ALLOWED_CLASS_PREFIX}*"
        raise ValueError(msg)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        msg = f"{dotted_path} is not a subclass of BaseStrategy"
        raise TypeError(msg)
    return cls


def validate_strategy_config(data: Mapping[str, Any]) -> StrategyConfig:
    """Strictly validate a strategy mapping: schema, allowlisted class, then a throwaway build.

    Building the class runs the strategy's own checks (rule indicators and parameters).
    Raises ``ValueError`` (``pydantic.ValidationError`` included) or ``TypeError``.
    """
    cfg = StrategyConfig.model_validate(dict(data))
    try:
        cls = _import_strategy_class(cfg.strategy_class)
    except ImportError as exc:
        msg = f"cannot import {cfg.strategy_class}: {exc}"
        raise ValueError(msg) from exc
    if inspect.isabstract(cls):
        msg = f"{cfg.strategy_class} is abstract"
        raise TypeError(msg)
    cls(config=cfg, context=StrategyContext())
    return cfg


def write_strategy_config(path: Path, data: Mapping[str, Any]) -> StrategyConfig:
    """Validate *data*, then write it to *path* atomically (nothing is written when invalid)."""
    cfg = validate_strategy_config(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(yaml.dump(dict(data), default_flow_style=False, sort_keys=False))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return cfg


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
        self.errors = 0
        self.last_error: str | None = None

    # -- Lifecycle -----------------------------------------------------------

    async def load_strategies(self, config_dir: Path) -> None:
        """Load all strategy configs from *config_dir* and instantiate them."""
        configs = load_all_strategy_configs(config_dir)
        for cfg in configs:
            if not cfg.enabled:
                logger.info("Skipping disabled strategy: %s", cfg.id)
                continue
            try:
                await self._instantiate_strategy(cfg)
            except (ImportError, ValueError, TypeError) as exc:
                logger.error("Skipping strategy %s: %s", cfg.id, exc)

    async def load_strategy_from_config(
        self,
        cfg: StrategyConfig,
        cls: type[BaseStrategy] | None = None,
    ) -> None:
        """Load a single strategy from an already-parsed config object (``cls``: skip import)."""
        if not cfg.enabled:
            logger.info("Skipping disabled strategy: %s", cfg.id)
            return
        await self._instantiate_strategy(cfg, cls)

    async def _instantiate_strategy(
        self,
        cfg: StrategyConfig,
        cls: type[BaseStrategy] | None = None,
    ) -> None:
        """Import, instantiate, and register a strategy."""
        if cls is None:
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
        for sid, strategy in self._strategies.items():
            try:
                await strategy.on_stop()
            except Exception as exc:
                logger.exception("Error in strategy %s on_stop", sid)
                self.errors += 1
                self.last_error = f"{sid}: {exc!r}"
        logger.info("Strategy engine stopped")

    # -- Event routing -------------------------------------------------------

    def _tracks(self, symbol: str, timeframe: Timeframe) -> bool:
        """True when any loaded strategy declares this symbol and timeframe."""
        return any(
            symbol in cfg.symbols and timeframe in cfg.all_timeframes()
            for cfg in self._strategy_configs.values()
        )

    async def warm_up(
        self, bars_by_stream: Mapping[tuple[str, Timeframe], Sequence[OHLCV]]
    ) -> None:
        """Seed the context with history, oldest first, without calling ``on_bar``.

        Raises ``ValueError`` when a stream has more bars than the context keeps, which would
        silently leave strategies short of ``required_history``.
        """
        limit = self._context.max_bars
        for (symbol, timeframe), bars in bars_by_stream.items():
            if limit is not None and len(bars) > limit:
                msg = (
                    f"{symbol} {timeframe}: {len(bars)} warm-up bars exceed the context's "
                    f"max_bars={limit}; size it with feed.window_for(required_history)"
                )
                raise ValueError(msg)
        for (symbol, timeframe), bars in bars_by_stream.items():
            for bar in sorted(bars, key=lambda b: b.timestamp):
                self._context.add_bar(symbol, timeframe, bar)
        logger.info(
            "Strategy context warmed up: %s",
            {f"{s}/{tf}": len(b) for (s, tf), b in bars_by_stream.items()},
        )

    async def _on_bar_event(self, event: Event) -> None:
        """Add a closed bar to the context once, then evaluate the strategies it drives.

        Every declared timeframe (primary, confirmation, entry) reaches the context; only
        strategies whose primary timeframe matches are evaluated, and never on a replayed
        catch-up bar.
        """
        if not isinstance(event, BarEvent) or event.ohlcv is None:
            return
        bar = event
        symbol = str(bar.symbol)
        if not self._tracks(symbol, bar.timeframe):
            return
        self._context.add_bar(symbol, bar.timeframe, event.ohlcv)
        if bar.replay:
            return
        for sid, strategy in self._strategies.items():
            cfg = self._strategy_configs[sid]
            if symbol not in cfg.symbols or bar.timeframe != cfg.timeframes.primary:
                continue
            try:
                signals = await strategy.on_bar(bar)
            except Exception as exc:
                logger.exception("Error in strategy %s on_bar", sid)
                self.errors += 1
                self.last_error = f"{sid}: {exc!r}"
                continue
            for signal in signals:
                await self._event_bus.publish(signal)
                try:
                    from hydra.core.metrics import record_signal

                    record_signal(
                        getattr(signal, "strategy_id", sid),
                        signal.event_type,
                    )
                except Exception:
                    pass

    # -- Hot reload ----------------------------------------------------------

    async def replace_strategy(self, cfg: StrategyConfig) -> None:
        """Swap a loaded strategy for a new instance of the same class built from *cfg*.

        The module is not re-imported and the context is shared, so indicator history survives.
        """
        old_cfg = self._strategy_configs.get(cfg.id)
        old = self._strategies.get(cfg.id)
        if old_cfg is None or old is None:
            msg = f"Cannot replace unknown strategy: {cfg.id}"
            raise KeyError(msg)
        if cfg.strategy_class != old_cfg.strategy_class:
            msg = f"Cannot change strategy_class of {cfg.id} in place"
            raise ValueError(msg)
        new = type(old)(config=cfg, context=self._context)
        await old.on_stop()
        self._strategies[cfg.id] = new
        self._strategy_configs[cfg.id] = cfg
        if self._running:
            await new.on_start()
        logger.info("Replaced strategy config: %s", cfg.id)

    # -- Accessors -----------------------------------------------------------

    def get_strategy(self, strategy_id: str) -> BaseStrategy | None:
        """Return a loaded strategy by ID, or ``None``."""
        return self._strategies.get(strategy_id)

    def get_all_strategies(self) -> dict[str, BaseStrategy]:
        """Return all loaded strategies."""
        return dict(self._strategies)

    @property
    def hydra_config(self) -> HydraConfig:
        """The config the engine was built with (consumers read ``ml.models_dir``)."""
        return self._config
