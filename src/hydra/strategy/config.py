"""Strategy configuration models (Pydantic v2).

Supports loading from YAML files in ``config/strategies/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from hydra.core.types import ExchangeId, MarketType, Timeframe

# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


class ExchangeStrategyConfig(BaseModel):
    """Exchange settings scoped to a single strategy."""

    exchange_id: ExchangeId = "binance"
    market_type: MarketType = MarketType.SPOT


class TimeframeConfig(BaseModel):
    """Timeframe configuration for a strategy."""

    primary: Timeframe = Timeframe.H1
    confirmation: Timeframe | None = None
    entry: Timeframe | None = None


class PositionSizingConfig(BaseModel):
    """Position sizing parameters."""

    method: str = "fixed_fraction"
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 10.0
    fixed_quantity: float | None = None


class ScheduleConfig(BaseModel):
    """Optional schedule for strategy activation windows."""

    active_hours_utc: list[int] = Field(default_factory=lambda: list(range(24)))
    active_days: list[int] = Field(default_factory=lambda: list(range(7)))


class MLOverlayConfig(BaseModel):
    """Optional ML overlay configuration."""

    model_name: str = ""
    confidence_threshold: float = 0.6
    features: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Main strategy config
# ---------------------------------------------------------------------------


class StrategyConfig(BaseModel):
    """Configuration for a single strategy instance."""

    id: str
    name: str
    strategy_class: str  # dotted path e.g. "hydra.strategy.builtin.MomentumRSIMACDStrategy"
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    exchange: ExchangeStrategyConfig = Field(default_factory=ExchangeStrategyConfig)
    timeframes: TimeframeConfig = Field(default_factory=TimeframeConfig)
    parameters: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    position_sizing: PositionSizingConfig = Field(default_factory=PositionSizingConfig)
    schedule: ScheduleConfig | None = None
    ml_overlay: MLOverlayConfig | None = None


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------


def load_strategy_config(path: Path) -> StrategyConfig:
    """Load a single strategy config from a YAML file."""
    with path.open() as f:
        data = yaml.safe_load(f)
    if data is None:
        msg = f"Empty strategy config file: {path}"
        raise ValueError(msg)
    return StrategyConfig(**data)


def load_all_strategy_configs(config_dir: Path) -> list[StrategyConfig]:
    """Load all ``*.yaml`` / ``*.yml`` strategy configs from a directory."""
    configs: list[StrategyConfig] = []
    if not config_dir.exists():
        return configs
    for path in sorted(config_dir.iterdir()):
        if path.suffix in (".yaml", ".yml"):
            configs.append(load_strategy_config(path))
    return configs
