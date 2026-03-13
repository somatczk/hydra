"""Tests for BaseStrategy ABC."""

from __future__ import annotations

import pytest

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext


class TestBaseStrategy:
    """BaseStrategy ABC tests."""

    def test_cannot_instantiate_directly(self) -> None:
        """BaseStrategy is abstract and cannot be instantiated."""
        cfg = StrategyConfig(
            id="test",
            name="Test",
            strategy_class="hydra.strategy.base.BaseStrategy",
        )
        ctx = StrategyContext()
        with pytest.raises(TypeError):
            BaseStrategy(config=cfg, context=ctx)  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        """A concrete subclass that implements required methods can be instantiated."""

        class ConcreteStrategy(BaseStrategy):
            @property
            def required_history(self) -> int:
                return 10

            async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
                return []

        cfg = StrategyConfig(
            id="concrete",
            name="Concrete",
            strategy_class="tests.test_base.ConcreteStrategy",
        )
        ctx = StrategyContext()
        strategy = ConcreteStrategy(config=cfg, context=ctx)
        assert strategy.strategy_id == "concrete"
        assert strategy.required_history == 10
        assert strategy.config is cfg
        assert strategy.context is ctx

    async def test_default_handlers_return_empty(self) -> None:
        """Default on_trade should return empty list, on_fill/on_start/on_stop are no-ops."""

        class ConcreteStrategy(BaseStrategy):
            @property
            def required_history(self) -> int:
                return 5

            async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
                return []

        cfg = StrategyConfig(
            id="concrete2",
            name="Concrete2",
            strategy_class="test",
        )
        ctx = StrategyContext()
        strategy = ConcreteStrategy(config=cfg, context=ctx)

        from hydra.core.events import TradeEvent

        result = await strategy.on_trade(TradeEvent())
        assert result == []

        # on_fill, on_start, on_stop should not raise
        from hydra.core.events import OrderFillEvent

        await strategy.on_fill(OrderFillEvent())
        await strategy.on_start()
        await strategy.on_stop()
