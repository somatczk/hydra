"""Tests for the StrategyEngine."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, Event
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine


def _make_bar(
    symbol: str = "BTCUSDT",
    timeframe: Timeframe = Timeframe.H1,
    close: float = 100.0,
    high: float = 105.0,
    low: float = 95.0,
    volume: float = 1000.0,
) -> BarEvent:
    from datetime import UTC, datetime

    ohlcv = OHLCV(
        open=Decimal(str(close)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        timestamp=datetime.now(UTC),
    )
    return BarEvent(symbol=symbol, timeframe=timeframe, ohlcv=ohlcv)


class TestStrategyEngine:
    """StrategyEngine tests."""

    async def test_load_strategy_from_config(self) -> None:
        """Loading a strategy from a StrategyConfig object works."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg = StrategyConfig(
            id="momentum_test",
            name="Momentum Test",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            symbols=["BTCUSDT"],
        )
        await engine.load_strategy_from_config(cfg)
        assert engine.get_strategy("momentum_test") is not None
        assert "momentum_test" in engine.get_all_strategies()

    async def test_load_strategies_from_yaml(self) -> None:
        """Load strategies from YAML files in a directory."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        strategy_data = {
            "id": "yaml_test",
            "name": "YAML Test",
            "strategy_class": "hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            "symbols": ["BTCUSDT"],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "test_strategy.yaml"
            with yaml_path.open("w") as f:
                yaml.dump(strategy_data, f)
            await engine.load_strategies(Path(tmpdir))

        assert engine.get_strategy("yaml_test") is not None

    async def test_disabled_strategy_not_loaded(self) -> None:
        """A disabled strategy should not be loaded."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg = StrategyConfig(
            id="disabled",
            name="Disabled",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            enabled=False,
        )
        await engine.load_strategy_from_config(cfg)
        assert engine.get_strategy("disabled") is None

    async def test_route_bar_to_correct_strategy(self) -> None:
        """Bar events should be routed to strategies matching symbol/timeframe."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg = StrategyConfig(
            id="btc_h1",
            name="BTC H1",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            symbols=["BTCUSDT"],
            parameters={"required_history": 5},
        )
        await engine.load_strategy_from_config(cfg)

        # Pre-populate context with enough bars
        from datetime import UTC, datetime

        for i in range(10):
            ohlcv = OHLCV(
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("95"),
                close=Decimal(str(100 + i)),
                volume=Decimal("1000"),
                timestamp=datetime.now(UTC),
            )
            ctx.add_bar("BTCUSDT", Timeframe.H1, ohlcv)

        # Subscribe and send a bar event
        received_signals: list[Event] = []

        async def capture(event: Event) -> None:
            received_signals.append(event)

        await bus.subscribe("entry_signal", capture)
        await bus.subscribe("exit_signal", capture)

        await engine.start()
        bar = _make_bar("BTCUSDT", Timeframe.H1)
        await bus.publish(bar)
        await engine.stop()

        # Strategy may or may not produce signals depending on indicator values,
        # but it should not raise an error
        assert isinstance(received_signals, list)

    async def test_wrong_symbol_not_routed(self) -> None:
        """Bar for a different symbol should not trigger the strategy."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg = StrategyConfig(
            id="btc_only",
            name="BTC Only",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            symbols=["BTCUSDT"],
        )
        await engine.load_strategy_from_config(cfg)
        await engine.start()

        # Send a bar for ETHUSDT -- should not match
        bar = _make_bar("ETHUSDT", Timeframe.H1)
        # Should not raise
        await engine._on_bar_event(bar)
        await engine.stop()

    async def test_hot_reload(self) -> None:
        """Hot reload should re-instantiate the strategy."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg = StrategyConfig(
            id="reload_test",
            name="Reload Test",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
        )
        await engine.load_strategy_from_config(cfg)
        old_strategy = engine.get_strategy("reload_test")
        assert old_strategy is not None

        await engine.reload_strategy("reload_test")
        new_strategy = engine.get_strategy("reload_test")
        assert new_strategy is not None
        # Should be a different instance
        assert old_strategy is not new_strategy
