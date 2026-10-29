"""Every shipped template and config builds as a rule-based strategy and trades what it says."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal
from hydra.core.types import OHLCV, Direction, Symbol
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

CONFIG = Path(__file__).resolve().parents[3] / "config"
TEMPLATES = sorted((CONFIG / "templates").glob("*.yaml"))
SHIPPED = sorted((CONFIG / "strategies").glob("*.yaml"))


def test_five_templates_ship() -> None:
    assert [p.stem for p in TEMPLATES] == [
        "breakout_volume",
        "mean_reversion_bb",
        "momentum_rsi",
        "multi_indicator",
        "trend_sma_crossover",
    ]


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
async def test_template_instantiates(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    data.pop("_template_meta")
    cfg = StrategyConfig(**{**data, "id": path.stem, "enabled": True})
    engine = StrategyEngine(HydraConfig(), InMemoryEventBus(), StrategyContext())
    await engine.load_strategy_from_config(cfg)
    assert type(engine.get_strategy(path.stem)) is RuleBasedStrategy


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_shipped_strategy_config_is_rule_based(path: Path) -> None:
    cfg = StrategyConfig(**yaml.safe_load(path.read_text()))
    assert cfg.strategy_class == "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
    RuleBasedStrategy(config=cfg, context=StrategyContext())


def _load(path: Path) -> StrategyConfig:
    data = yaml.safe_load(path.read_text())
    data.pop("_template_meta", None)
    return StrategyConfig(**{"id": path.stem, **data})


async def _entry_bars(cfg: StrategyConfig, closes: list[float]) -> list[int]:
    """Feed closes bar by bar like a session and return the indices that emitted a long entry."""
    ctx = StrategyContext()
    strategy = RuleBasedStrategy(config=cfg, context=ctx)
    tf = cfg.timeframes.primary
    fired: list[int] = []
    for i, c in enumerate(closes):
        price = Decimal(str(c))
        ohlcv = OHLCV(
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(1),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
        )
        ctx.add_bar("BTCUSDT", tf, ohlcv)
        bar = BarEvent(symbol=Symbol("BTCUSDT"), timeframe=tf, ohlcv=ohlcv)
        signals = await strategy.on_bar(bar)
        if any(isinstance(s, EntrySignal) and s.direction == Direction.LONG for s in signals):
            fired.append(i)
    return fired


async def test_donchian_breakout_fires_on_a_new_60_bar_high() -> None:
    cfg = _load(CONFIG / "strategies" / "research_donchian_breakout.yaml")
    closes = [100.0 + (i % 10) for i in range(70)] + [115.0, 114.0]
    assert await _entry_bars(cfg, closes) == [70]


async def test_momentum_extremes_enters_on_a_20_bar_max() -> None:
    cfg = _load(CONFIG / "strategies" / "research_momentum_extremes.yaml")
    # 50 bars of history (its required_history), then the breakout
    closes = [100.0 - 0.5 * i for i in range(50)] + [110.0, 105.0]
    assert await _entry_bars(cfg, closes) == [50]


async def test_bollinger_templates_trade_the_bands() -> None:
    # 90 calm bars cover the RSI(14) template's 86 bars of required history
    calm = [100.0 + (i % 2) for i in range(90)]
    breakout = _load(CONFIG / "templates" / "breakout_volume.yaml")
    assert await _entry_bars(breakout, [*calm, 110.0, 111.0]) == [90]
    bounce = _load(CONFIG / "templates" / "mean_reversion_bb.yaml")
    assert await _entry_bars(bounce, [*calm, 90.0]) == [90]


async def test_sma_crossover_tunes_each_leg_separately() -> None:
    base = _load(CONFIG / "templates" / "trend_sma_crossover.yaml")
    closes = [200.0 - i for i in range(80)] + [120.0 + 3 * i for i in range(60)]

    def tuned(**top: int) -> StrategyConfig:
        return base.model_copy(update={"parameters": {**base.parameters, **top}})

    assert await _entry_bars(tuned(sma_period=30), closes)
    assert await _entry_bars(tuned(sma_period=30, slow_sma_period=30), closes) == []
