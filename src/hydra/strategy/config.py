"""Strategy configuration models (Pydantic v2).

Supports loading from YAML files in ``config/strategies/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hydra.core.types import ExchangeId, MarketType, Timeframe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


class ExchangeStrategyConfig(BaseModel):
    """Exchange settings scoped to a single strategy.

    ``leverage`` and ``margin_mode`` are the USD⌂-M perp fields: they apply to
    ``binanceusdm`` FUTURES strategies only (isolated margin is the sole mode in
    2.8.0; the 1-3 range is the pydantic half — session start re-validates
    against the ``risk_config`` cap).
    """

    exchange_id: ExchangeId = "binance"
    market_type: MarketType = MarketType.SPOT
    leverage: int = Field(1, ge=1, le=3)
    margin_mode: Literal["isolated"] = "isolated"

    @model_validator(mode="after")
    def _spot_never_leverages(self) -> ExchangeStrategyConfig:
        if self.market_type == MarketType.SPOT and self.leverage != 1:
            raise ValueError("leverage applies to futures strategies only (spot is 1x)")
        return self


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


StopLossMethod = Literal["fixed_pct", "atr", "trailing_stop"]
TakeProfitMethod = Literal["none", "fixed_pct", "atr"]

DEFAULT_STOP_LOSS_PCT = 5.0


class StrategyRiskConfig(BaseModel):
    """Protective exits placed on every entry fill (decision 17).

    ``fixed_pct`` and ``trailing_stop`` values are percentages (5.0 = 5 %), ``atr`` values are
    multiples of ATR(``atr_period``) on the primary timeframe. A take-profit turns the stop into
    an OCO. A trailing stop cannot be combined with a take-profit (both would sell the full
    position).
    """

    model_config = ConfigDict(extra="forbid")

    stop_loss_method: StopLossMethod = "fixed_pct"
    stop_loss_value: float = Field(DEFAULT_STOP_LOSS_PCT, gt=0)
    take_profit_method: TakeProfitMethod = "none"
    take_profit_value: float = Field(0.0, ge=0)
    atr_period: int = Field(14, ge=2)

    @model_validator(mode="after")
    def _consistent(self) -> StrategyRiskConfig:
        pct_methods = {"fixed_pct", "trailing_stop"}
        if self.stop_loss_method in pct_methods and self.stop_loss_value >= 100:
            raise ValueError("stop_loss_value is a percentage and must be below 100")
        if self.take_profit_method != "none" and self.take_profit_value <= 0:
            raise ValueError("take_profit_value must be > 0 when a take-profit method is set")
        if self.stop_loss_method == "trailing_stop" and self.take_profit_method != "none":
            raise ValueError("a trailing stop cannot be combined with a take-profit")
        return self


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
    risk: StrategyRiskConfig = Field(default_factory=StrategyRiskConfig)
    schedule: ScheduleConfig | None = None
    ml_overlay: MLOverlayConfig | None = None

    def all_timeframes(self) -> list[Timeframe]:
        """Primary, confirmation and entry timeframes, deduplicated, in that order."""
        declared = (self.timeframes.primary, self.timeframes.confirmation, self.timeframes.entry)
        out: list[Timeframe] = []
        for tf in declared:
            if tf is not None and tf not in out:
                out.append(tf)
        return out


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
    """Load all ``*.yaml`` / ``*.yml`` strategy configs; a bad file is skipped and logged."""
    configs: list[StrategyConfig] = []
    if not config_dir.exists():
        return configs
    for path in sorted(config_dir.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        try:
            configs.append(load_strategy_config(path))
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            logger.error("Skipping invalid strategy config %s: %s", path, exc)
    return configs


def config_hash(cfg: StrategyConfig) -> str:
    """Short digest of a config; ``enabled`` and ``name`` are excluded (start forces enabled)."""
    payload = json.dumps(cfg.model_dump(mode="json", exclude={"enabled", "name"}), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
