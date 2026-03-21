"""Built-in strategies (8 total including RuleBasedStrategy and DCAStrategy)."""

from __future__ import annotations

from hydra.strategy.builtin.breakout import BreakoutStrategy
from hydra.strategy.builtin.composite import CompositeStrategy
from hydra.strategy.builtin.dca import DCAStrategy
from hydra.strategy.builtin.mean_reversion import MeanReversionBBStrategy
from hydra.strategy.builtin.ml_ensemble import MLEnsembleStrategy
from hydra.strategy.builtin.momentum import MomentumRSIMACDStrategy
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.builtin.trend_following import TrendFollowingSupertrend

__all__ = [
    "BreakoutStrategy",
    "CompositeStrategy",
    "DCAStrategy",
    "MLEnsembleStrategy",
    "MeanReversionBBStrategy",
    "MomentumRSIMACDStrategy",
    "RuleBasedStrategy",
    "TrendFollowingSupertrend",
]
