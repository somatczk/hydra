"""Strategy configuration models (Pydantic v2).

Supports loading from YAML files in ``config/strategies/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from hydra.core.types import ExchangeId, MarketType, Timeframe
from hydra.data.bars import timeframe_seconds

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
    """When a strategy may open a position: an entry filter, never an exit filter.

    An entry signal passes only when the primary bar it was evaluated on has its open
    timestamp (``OHLCV.timestamp``, UTC: the stamp on fills, trades and charts) at an hour in
    ``active_hours_utc`` and on a day in ``active_days`` (Python ``weekday()``: Monday=0 ..
    Sunday=6). The entry is decided and filled at that bar's close, so ``active_hours_utc:
    [13, ..., 21]`` on 1h bars fills at 14:00-22:00 UTC. 4h bars open only at hours
    0/4/8/12/16/20 and 1d/1w bars at 00:00 (1w on Mondays), so on daily bars only
    ``active_days`` selects. Exits, protective stops, flatten, the kill switch and manual
    orders are never filtered. The default (every hour, every day) is always active.
    """

    model_config = ConfigDict(extra="forbid")

    active_hours_utc: list[Annotated[int, Field(ge=0, le=23)]] = Field(
        default_factory=lambda: list(range(24)), min_length=1
    )
    active_days: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        default_factory=lambda: list(range(7)), min_length=1
    )

    @field_validator("active_hours_utc", "active_days")
    @classmethod
    def _no_duplicates(cls, value: list[int], info: ValidationInfo) -> list[int]:
        repeated = sorted({v for v in value if value.count(v) > 1})
        if repeated:
            raise ValueError(f"{info.field_name} lists {repeated} more than once")
        return value

    def allows(self, bar_open: datetime) -> bool:
        """True when an entry evaluated on the bar opened at *bar_open* may pass."""
        utc = bar_open.astimezone(UTC)
        return utc.hour in self.active_hours_utc and utc.weekday() in self.active_days


class MLOverlayConfig(BaseModel):
    """The veto-only ML overlay on a strategy's entries (P5-C).

    ``enabled`` turns the gate on: the session start then fail-closes on the
    named model's Production version (never at save), and the signal seam vetoes
    an entry whose confidence for its own direction is below
    ``confidence_threshold`` — at the threshold the entry passes. ``shadow``
    records would-vetoes without suppressing them. The overlay never generates,
    sizes or bypasses: the frozen ``FeatureSpec`` is the feature contract, so
    there is no per-strategy feature list.
    """

    enabled: bool = False
    model_name: str = ""
    confidence_threshold: float = Field(0.6, ge=0.0, le=1.0)
    shadow: bool = False

    @model_validator(mode="after")
    def _enabled_names_a_model(self) -> MLOverlayConfig:
        if self.enabled and not self.model_name.strip():
            raise ValueError("an enabled ml_overlay must name a model")
        return self


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

    @model_validator(mode="after")
    def _schedule_can_match(self) -> StrategyConfig:
        """Refuse a schedule no primary bar can ever satisfy (it would never enter)."""
        if self.schedule is None:
            return self
        primary = self.timeframes.primary
        tf_s = timeframe_seconds(primary)
        opens = {h for h in range(24) if h * 3600 % tf_s == 0}
        if not opens & set(self.schedule.active_hours_utc):
            raise ValueError(
                f"schedule.active_hours_utc never matches a {primary} bar: "
                f"{primary} bars open only at hours {sorted(opens)} UTC"
            )
        if primary == Timeframe.W1 and 0 not in self.schedule.active_days:
            raise ValueError(
                "schedule.active_days never matches a 1w bar: 1w bars open on Monday (0)"
            )
        return self

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
