"""Every shipped template loads as a rule-based strategy; every shipped config uses that class."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
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
    assert yaml.safe_load(path.read_text())["strategy_class"] == (
        "hydra.strategy.builtin.rule_based.RuleBasedStrategy"
    )
