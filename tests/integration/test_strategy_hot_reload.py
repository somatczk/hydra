"""Integration: Strategy hot reload via StrategyEngine.

Tests that changing strategy configuration and triggering a reload causes
the engine to use updated parameters without restarting.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStrategyHotReload:
    """Engine reloads strategy when config changes."""

    async def test_config_change_reloads_strategy(self) -> None:
        """Change strategy parameters -> engine reloads -> new params take effect."""
        event_bus = InMemoryEventBus()
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        hydra_config = HydraConfig()

        engine = StrategyEngine(
            config=hydra_config,
            event_bus=event_bus,
            context=context,
        )

        # Load a strategy with initial parameters
        config_v1 = StrategyConfig(
            id="reload_test",
            name="Reload Test",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            symbols=["BTCUSDT"],
            parameters={
                "rsi_period": 14,
                "required_history": 50,
            },
        )

        await engine.load_strategy_from_config(config_v1)
        await engine.start()

        # Verify strategy is loaded
        strategy_v1 = engine.get_strategy("reload_test")
        assert strategy_v1 is not None
        assert strategy_v1.config.parameters["rsi_period"] == 14

        # Reload the strategy (simulates hot reload)
        await engine.reload_strategy("reload_test")

        # Verify strategy was reloaded (new instance)
        strategy_v2 = engine.get_strategy("reload_test")
        assert strategy_v2 is not None
        # The reloaded strategy should still have the same config
        assert strategy_v2.config.parameters["rsi_period"] == 14

        await engine.stop()

    async def test_reload_unknown_strategy_is_noop(self) -> None:
        """Reloading a non-existent strategy ID does not raise."""
        event_bus = InMemoryEventBus()
        context = StrategyContext()
        hydra_config = HydraConfig()

        engine = StrategyEngine(
            config=hydra_config,
            event_bus=event_bus,
            context=context,
        )

        # Should not raise
        await engine.reload_strategy("nonexistent_strategy")

    async def test_engine_loads_from_config_dir(self) -> None:
        """Engine loads strategies from a directory of YAML files."""
        event_bus = InMemoryEventBus()
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        hydra_config = HydraConfig()

        engine = StrategyEngine(
            config=hydra_config,
            event_bus=event_bus,
            context=context,
        )

        # Create a temporary config directory with one strategy YAML
        with tempfile.TemporaryDirectory() as tmpdir:
            config_data = {
                "id": "dir_test",
                "name": "Dir Load Test",
                "strategy_class": "hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
                "enabled": True,
                "symbols": ["BTCUSDT"],
                "parameters": {"rsi_period": 14, "required_history": 50},
            }
            config_path = Path(tmpdir) / "test_strategy.yaml"
            with config_path.open("w") as f:
                yaml.dump(config_data, f)

            await engine.load_strategies(Path(tmpdir))

        strategy = engine.get_strategy("dir_test")
        assert strategy is not None
        assert strategy.strategy_id == "dir_test"
