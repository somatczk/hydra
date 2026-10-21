"""Every shipped template loads into a real strategy instance."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

TEMPLATES = sorted((Path(__file__).resolve().parents[3] / "config" / "templates").glob("*.yaml"))


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
    assert isinstance(engine.get_strategy(path.stem), BaseStrategy)
