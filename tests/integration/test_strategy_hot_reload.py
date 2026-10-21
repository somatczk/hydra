"""Integration: StrategyEngine.replace_strategy swaps a running strategy's parameters.

The engine is real; only the strategy is a test double that records what it saw. Session-level
bar-boundary enforcement (never mid-bar) is covered by test_execution/test_session_wiring.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

pytestmark = pytest.mark.integration


class _RecordingStrategy(BaseStrategy):
    """Records the ``rsi_period`` parameter it saw on every bar."""

    seen: list[int]

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)
        self.seen = []

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        self.seen.append(self._config.parameters["rsi_period"])
        return []


def _bar(i: int) -> BarEvent:
    c = Decimal(100 + i)
    ts = datetime(2026, 1, 1 + i, tzinfo=UTC)
    ohlcv = OHLCV(open=c, high=c, low=c, close=c, volume=Decimal(1), timestamp=ts)
    return BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv)


def _cfg(rsi_period: int) -> StrategyConfig:
    return StrategyConfig(
        id="reload_test",
        name="Reload Test",
        strategy_class=f"{__name__}._RecordingStrategy",
        symbols=["BTCUSDT"],
        parameters={"rsi_period": rsi_period},
    )


async def test_replace_strategy_applies_between_bars() -> None:
    """A config change between bars takes effect on the next bar, not the current one."""
    bus = InMemoryEventBus()
    ctx = StrategyContext()
    engine = StrategyEngine(config=HydraConfig(), event_bus=bus, context=ctx)

    await engine.load_strategy_from_config(_cfg(14), cls=_RecordingStrategy)
    await engine.start()

    await bus.publish(_bar(0))
    strategy = engine.get_strategy("reload_test")
    assert isinstance(strategy, _RecordingStrategy)
    assert strategy.seen == [14]

    await engine.replace_strategy(_cfg(21))

    await bus.publish(_bar(1))
    new_strategy = engine.get_strategy("reload_test")
    assert isinstance(new_strategy, _RecordingStrategy)
    assert new_strategy is not strategy
    assert strategy.seen == [14]
    assert new_strategy.seen == [21]

    await engine.stop()
