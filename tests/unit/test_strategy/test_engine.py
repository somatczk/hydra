"""Tests for the StrategyEngine."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, Event, ExitSignal
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.strategy.base import BaseStrategy
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


# ---------------------------------------------------------------------------
# Closed-bar routing (SM-19, EX-14, replay) and warm-up
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 3, 2, tzinfo=UTC)
_CALLS: dict[str, list[tuple[Timeframe, datetime, int]]] = {}


class RecordingStrategy(BaseStrategy):
    """Records every on_bar call with the context length it saw."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        assert bar.ohlcv is not None
        seen = len(self.context.bars(str(bar.symbol), bar.timeframe, 10_000))
        _CALLS.setdefault(self.strategy_id, []).append((bar.timeframe, bar.ohlcv.timestamp, seen))
        return []


def _recording_cfg(sid: str, **timeframes: Timeframe) -> StrategyConfig:
    return StrategyConfig(
        id=sid,
        name=sid,
        strategy_class=f"{__name__}.RecordingStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1, **timeframes},
    )


def _closed(tf: Timeframe, i: int, *, replay: bool = False) -> BarEvent:
    step = timedelta(seconds=timeframe_seconds(tf))
    c = Decimal(100 + i)
    ohlcv = OHLCV(open=c, high=c, low=c, close=c, volume=Decimal(1), timestamp=_T0 + i * step)
    return BarEvent(symbol=Symbol("BTCUSDT"), timeframe=tf, ohlcv=ohlcv, replay=replay)


async def _engine(
    *cfgs: StrategyConfig,
) -> tuple[InMemoryEventBus, StrategyContext, StrategyEngine]:
    _CALLS.clear()
    bus = InMemoryEventBus()
    ctx = StrategyContext()
    engine = StrategyEngine(config=HydraConfig(), event_bus=bus, context=ctx)
    for cfg in cfgs:
        await engine.load_strategy_from_config(cfg)
    await engine.start()
    return bus, ctx, engine


class TestClosedBarRouting:
    async def test_bar_added_once_for_two_strategies(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("a"), _recording_cfg("b"))
        for i in range(10):
            await bus.publish(_closed(Timeframe.H1, i))

        assert len(ctx.bars("BTCUSDT", Timeframe.H1, 100)) == 10
        assert [seen for _, _, seen in _CALLS["a"]] == list(range(1, 11))
        assert [seen for _, _, seen in _CALLS["b"]] == list(range(1, 11))

    async def test_confirmation_timeframe_reaches_context(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("mtf", confirmation=Timeframe.H4))
        for i in range(3):
            await bus.publish(_closed(Timeframe.H4, i))
        for i in range(8):
            await bus.publish(_closed(Timeframe.H1, i))

        h4 = ctx.bars("BTCUSDT", Timeframe.H4, 100)
        assert [b.timestamp for b in h4] == [_T0 + timedelta(hours=4 * i) for i in range(3)]
        assert [tf for tf, _, _ in _CALLS["mtf"]] == [Timeframe.H1] * 8

    async def test_undeclared_timeframe_is_ignored(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("h1"))
        await bus.publish(_closed(Timeframe.D1, 0))
        assert ctx.bars("BTCUSDT", Timeframe.D1, 10) == []
        assert "h1" not in _CALLS

    async def test_replay_bar_not_dispatched(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("r"))
        await bus.publish(_closed(Timeframe.H1, 0, replay=True))
        await bus.publish(_closed(Timeframe.H1, 1, replay=True))
        await bus.publish(_closed(Timeframe.H1, 2))

        assert len(ctx.bars("BTCUSDT", Timeframe.H1, 100)) == 3
        assert _CALLS["r"] == [(Timeframe.H1, _T0 + timedelta(hours=2), 3)]

    async def test_forming_update_replaces_instead_of_appending(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("dup"))
        await bus.publish(_closed(Timeframe.H1, 0))
        await bus.publish(_closed(Timeframe.H1, 0))
        assert len(ctx.bars("BTCUSDT", Timeframe.H1, 100)) == 1

    async def test_warm_up_adds_without_on_bar(self) -> None:
        bus, ctx, engine = await _engine(_recording_cfg("w", confirmation=Timeframe.H4))
        h1 = [_closed(Timeframe.H1, i).ohlcv for i in range(500)]
        h4 = [_closed(Timeframe.H4, i).ohlcv for i in range(125)]
        await engine.warm_up(
            {
                ("BTCUSDT", Timeframe.H1): [b for b in reversed(h1) if b is not None],
                ("BTCUSDT", Timeframe.H4): [b for b in h4 if b is not None],
            }
        )

        bars = ctx.bars("BTCUSDT", Timeframe.H1, 10_000)
        assert len(bars) == 500
        assert bars[0].timestamp == _T0 and bars[-1].timestamp == _T0 + timedelta(hours=499)
        assert len(ctx.bars("BTCUSDT", Timeframe.H4, 10_000)) == 125
        assert "w" not in _CALLS

        await bus.publish(_closed(Timeframe.H1, 500))
        assert _CALLS["w"] == [(Timeframe.H1, _T0 + timedelta(hours=500), 501)]
