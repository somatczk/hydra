"""M04: Strategy framework, built-in strategies, hot-reload, no-code rule engine."""

from __future__ import annotations

from hydra.strategy.base import BaseStrategy
from hydra.strategy.builtin import (
    BreakoutStrategy,
    CompositeStrategy,
    MeanReversionBBStrategy,
    MLEnsembleStrategy,
    MomentumRSIMACDStrategy,
    RuleBasedStrategy,
    TrendFollowingSupertrend,
)
from hydra.strategy.condition_schema import (
    Comparator,
    Condition,
    ConditionGroup,
    LogicOperator,
    RuleSet,
)
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

__all__ = [
    # Base
    "BaseStrategy",
    # Built-in strategies
    "BreakoutStrategy",
    # Condition schema
    "Comparator",
    "CompositeStrategy",
    "Condition",
    "ConditionGroup",
    "LogicOperator",
    "MLEnsembleStrategy",
    "MeanReversionBBStrategy",
    "MomentumRSIMACDStrategy",
    "RuleBasedStrategy",
    "RuleSet",
    # Config
    "StrategyConfig",
    # Context
    "StrategyContext",
    # Engine
    "StrategyEngine",
    "TrendFollowingSupertrend",
]
