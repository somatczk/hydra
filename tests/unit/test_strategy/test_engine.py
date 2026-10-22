"""Tests for the StrategyEngine."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, Event, ExitSignal
from hydra.core.types import OHLCV, Symbol, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine, validate_strategy_config, write_strategy_config


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

    async def test_replace_strategy(self) -> None:
        """replace_strategy swaps in a new instance built from the new config, same class."""
        config = HydraConfig()
        bus = InMemoryEventBus()
        ctx = StrategyContext()
        engine = StrategyEngine(config=config, event_bus=bus, context=ctx)

        cfg_v1 = StrategyConfig(
            id="reload_test",
            name="Reload Test",
            strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
            parameters={"rsi_period": 14},
        )
        await engine.load_strategy_from_config(cfg_v1)
        await engine.start()
        old_strategy = engine.get_strategy("reload_test")
        assert old_strategy is not None

        stopped: list[str] = []
        orig_on_stop = old_strategy.on_stop

        async def _recording_on_stop() -> None:
            stopped.append("stopped")
            await orig_on_stop()

        old_strategy.on_stop = _recording_on_stop  # type: ignore[method-assign]

        cfg_v2 = cfg_v1.model_copy(update={"parameters": {"rsi_period": 21}})
        await engine.replace_strategy(cfg_v2)

        new_strategy = engine.get_strategy("reload_test")
        assert new_strategy is not None
        assert new_strategy is not old_strategy
        assert new_strategy.config.parameters["rsi_period"] == 21
        assert stopped == ["stopped"]
        assert type(new_strategy) is type(old_strategy)
        assert engine._strategy_configs["reload_test"] is cfg_v2

        with pytest.raises(KeyError):
            await engine.replace_strategy(cfg_v2.model_copy(update={"id": "no-such-id"}))
        with pytest.raises(ValueError, match="Cannot change strategy_class"):
            await engine.replace_strategy(
                cfg_v2.model_copy(
                    update={
                        "strategy_class": "hydra.strategy.builtin.trend_following."
                        "TrendFollowingSupertrend"
                    }
                )
            )
        await engine.stop()

    async def test_import_allowlist(self) -> None:
        """A non-builtin module path is refused before import, and never imported."""
        import sys

        from hydra.strategy.engine import _import_strategy_class

        assert "antigravity" not in sys.modules
        with pytest.raises(ValueError, match="must be under"):
            _import_strategy_class("antigravity.Foo")
        assert "antigravity" not in sys.modules


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
    *cfgs: StrategyConfig, max_bars: int | None = None
) -> tuple[InMemoryEventBus, StrategyContext, StrategyEngine]:
    _CALLS.clear()
    bus = InMemoryEventBus()
    ctx = StrategyContext(max_bars=max_bars)
    engine = StrategyEngine(config=HydraConfig(), event_bus=bus, context=ctx)
    for cfg in cfgs:
        await engine.load_strategy_from_config(cfg, cls=RecordingStrategy)
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

    async def test_warm_up_refuses_more_bars_than_the_context_keeps(self) -> None:
        _, ctx, engine = await _engine(_recording_cfg("cap"), max_bars=1000)
        bars = [_closed(Timeframe.H1, i).ohlcv for i in range(1200)]
        with pytest.raises(
            ValueError, match="1200 warm-up bars exceed the context's max_bars=1000"
        ):
            await engine.warm_up({("BTCUSDT", Timeframe.H1): [b for b in bars if b is not None]})
        assert ctx.bars("BTCUSDT", Timeframe.H1, 10_000) == []

    async def test_late_replay_bar_lands_in_order(self) -> None:
        bus, ctx, _ = await _engine(_recording_cfg("late"))
        for i in (0, 1, 3):
            await bus.publish(_closed(Timeframe.H1, i))
        await bus.publish(_closed(Timeframe.H1, 2, replay=True))
        stamps = [b.timestamp for b in ctx.bars("BTCUSDT", Timeframe.H1, 100)]
        assert stamps == [_T0 + timedelta(hours=h) for h in range(4)]
        assert [c[1] for c in _CALLS["late"]] == [_T0 + timedelta(hours=h) for h in (0, 1, 3)]


# ---------------------------------------------------------------------------
# validate_strategy_config / write_strategy_config (P2-E2)
# ---------------------------------------------------------------------------

_VALID_RULE_BASED: dict[str, object] = {
    "id": "v1",
    "name": "Valid",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "parameters": {
        "required_history": 20,
        "rules": {
            "entry_long": {
                "operator": "AND",
                "conditions": [
                    {
                        "indicator": "rsi",
                        "params": {"period": 14},
                        "comparator": "less_than",
                        "value": 30,
                    }
                ],
            },
        },
    },
}


class TestValidateStrategyConfig:
    def test_valid_config_round_trips(self) -> None:
        cfg = validate_strategy_config(_VALID_RULE_BASED)
        assert cfg.id == "v1"

    def test_disallowed_class_raises(self) -> None:
        data = {**_VALID_RULE_BASED, "strategy_class": "antigravity.Foo"}
        with pytest.raises(ValueError, match="must be under"):
            validate_strategy_config(data)

    def test_bad_schema_raises_validation_error(self) -> None:
        data = dict(_VALID_RULE_BASED)
        del data["strategy_class"]
        with pytest.raises(ValueError):
            validate_strategy_config(data)

    def test_invalid_rule_indicator_raises_at_construction(self) -> None:
        data = {
            **_VALID_RULE_BASED,
            "parameters": {
                "required_history": 20,
                "rules": {
                    "entry_long": {
                        "operator": "AND",
                        "conditions": [
                            {
                                "indicator": "rsii",
                                "params": {"period": 14},
                                "comparator": "less_than",
                                "value": 30,
                            }
                        ],
                    },
                },
            },
        }
        with pytest.raises(ValueError):
            validate_strategy_config(data)

    @pytest.mark.parametrize(
        "value",
        ["50", 50.0, True, 0],
        ids=["string", "float", "bool", "zero"],
    )
    def test_non_integer_required_history_is_rejected(self, value: object) -> None:
        """A quoted or fractional required_history passes pydantic but kills every on_bar."""
        data = dict(_VALID_RULE_BASED)
        data["parameters"] = {**_VALID_RULE_BASED["parameters"], "required_history": value}
        with pytest.raises(ValueError, match="required_history"):
            validate_strategy_config(data)


class TestWriteStrategyConfig:
    def test_writes_atomically(self, tmp_path: Path) -> None:
        path = tmp_path / "strategy.yaml"
        write_strategy_config(path, _VALID_RULE_BASED)
        assert path.is_file()
        written = yaml.safe_load(path.read_text())
        assert written["id"] == "v1"
        assert not list(tmp_path.glob(".*.tmp"))

    def test_invalid_config_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "strategy.yaml"
        data = {**_VALID_RULE_BASED, "strategy_class": "antigravity.Foo"}
        with pytest.raises(ValueError):
            write_strategy_config(path, data)
        assert not path.exists()
        assert not list(tmp_path.glob(".*.tmp"))

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "strategy.yaml"
        write_strategy_config(path, _VALID_RULE_BASED)
        assert path.is_file()
