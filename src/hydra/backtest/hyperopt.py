"""Hyperparameter optimization for trading strategies.

Supports Bayesian optimization (via Optuna) and grid search over a user-defined
parameter space. Each trial runs a full backtest and records the resulting metrics.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

if TYPE_CHECKING:
    import optuna as _optuna_type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter space definition
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParamDef:
    """Definition of a single hyperparameter.

    For ``int`` and ``float`` types, ``low`` and ``high`` are required.
    For ``categorical`` type, ``choices`` is required.
    """

    name: str
    type: Literal["int", "float", "categorical"]
    low: float | None = None
    high: float | None = None
    choices: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type in ("int", "float"):
            if self.low is None or self.high is None:
                raise ValueError(
                    f"ParamDef '{self.name}': 'low' and 'high' are required for type '{self.type}'"
                )
            if self.low > self.high:
                raise ValueError(
                    f"ParamDef '{self.name}': 'low' ({self.low}) must be <= 'high' ({self.high})"
                )
        elif self.type == "categorical":
            if not self.choices:
                raise ValueError(
                    f"ParamDef '{self.name}': 'choices' must be non-empty for categorical type"
                )


@dataclass(slots=True)
class ParameterSpace:
    """Defines the full parameter search space for a hyperopt run."""

    params: list[ParamDef] = field(default_factory=list)

    def suggest(self, trial: _optuna_type.Trial) -> dict[str, Any]:
        """Ask an Optuna trial to sample one point from the space."""
        values: dict[str, Any] = {}
        for p in self.params:
            if p.type == "int":
                if p.low is None or p.high is None:
                    raise ValueError(f"Param '{p.name}' (int) requires low and high bounds")
                values[p.name] = trial.suggest_int(p.name, int(p.low), int(p.high))
            elif p.type == "float":
                if p.low is None or p.high is None:
                    raise ValueError(f"Param '{p.name}' (float) requires low and high bounds")
                values[p.name] = trial.suggest_float(p.name, p.low, p.high)
            else:
                values[p.name] = trial.suggest_categorical(p.name, p.choices)
        return values

    def grid_combinations(self) -> list[dict[str, Any]]:
        """Enumerate every combination in the grid.

        For float params the midpoint is used as the sole candidate (grid search
        over continuous ranges requires explicit step semantics that belong to the
        caller; this falls back to a single representative value).
        """
        per_param: list[list[Any]] = []
        names: list[str] = []
        for p in self.params:
            names.append(p.name)
            if p.type == "categorical":
                per_param.append(list(p.choices))
            elif p.type == "int":
                if p.low is None or p.high is None:
                    raise ValueError(f"Param '{p.name}' (int) requires low and high bounds")
                per_param.append(list(range(int(p.low), int(p.high) + 1)))
            else:
                # float: use low, midpoint, high as grid candidates
                if p.low is None or p.high is None:
                    raise ValueError(f"Param '{p.name}' (float) requires low and high bounds")
                mid = (p.low + p.high) / 2.0
                per_param.append([p.low, mid, p.high])

        combinations: list[dict[str, Any]] = []
        for combo in itertools.product(*per_param):
            combinations.append(dict(zip(names, combo, strict=True)))
        return combinations


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrialRecord:
    """Metrics captured for a single hyperopt trial."""

    trial_number: int
    params: dict[str, Any]
    sharpe: float
    total_return: float
    max_drawdown: float
    total_trades: int


@dataclass(slots=True)
class HyperoptResult:
    """Aggregated result of a hyperparameter optimization run."""

    best_params: dict[str, Any] = field(default_factory=dict)
    best_metric: float = 0.0  # Best Sharpe ratio achieved
    trials: list[TrialRecord] = field(default_factory=list)
    total_trials: int = 0
    completed_trials: int = 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class HyperoptRunner:
    """Orchestrates hyperparameter optimization over a BacktestRunner.

    Supports two search methods:

    * ``"bayesian"`` — Uses Optuna's TPE sampler to intelligently explore the
      parameter space. Efficient for large, high-dimensional spaces.
    * ``"grid"`` — Exhaustive enumeration of all combinations. Suitable for
      small discrete spaces where coverage matters more than efficiency.
    """

    def __init__(self, backtest_runner: BacktestRunner | None = None) -> None:
        self._runner = backtest_runner or BacktestRunner()

    async def run(
        self,
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        param_space: ParameterSpace,
        method: Literal["bayesian", "grid"] = "bayesian",
        max_trials: int = 50,
        metric: str = "sharpe_ratio",
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
        on_trial_complete: Any = None,
    ) -> HyperoptResult:
        """Run the hyperparameter optimization.

        Parameters
        ----------
        strategy_class:
            Strategy class to evaluate.
        base_config:
            Base StrategyConfig. ``parameters`` dict will be updated with each
            trial's sampled values before the backtest runs.
        bars:
            Chronologically sorted OHLCV bars to replay.
        initial_capital:
            Starting equity for each trial backtest.
        param_space:
            Search space definition.
        method:
            ``"bayesian"`` (Optuna TPE) or ``"grid"`` (exhaustive).
        max_trials:
            Maximum number of trials. For grid search this is capped at the
            true number of combinations.
        metric:
            Objective metric name. Currently only ``"sharpe_ratio"`` is used;
            the field is reserved for future extensibility.
        symbol:
            Trading symbol passed to the backtest runner.
        timeframe:
            Bar timeframe passed to the backtest runner.
        on_trial_complete:
            Optional async callable ``(trial_number, completed, total) -> None``
            invoked after each trial completes. Used to stream progress to callers.
        """
        if not bars:
            return HyperoptResult()

        if method == "bayesian":
            return await self._run_bayesian(
                strategy_class=strategy_class,
                base_config=base_config,
                bars=bars,
                initial_capital=initial_capital,
                param_space=param_space,
                max_trials=max_trials,
                symbol=symbol,
                timeframe=timeframe,
                on_trial_complete=on_trial_complete,
            )

        return await self._run_grid(
            strategy_class=strategy_class,
            base_config=base_config,
            bars=bars,
            initial_capital=initial_capital,
            param_space=param_space,
            max_trials=max_trials,
            symbol=symbol,
            timeframe=timeframe,
            on_trial_complete=on_trial_complete,
        )

    # ------------------------------------------------------------------
    # Private: Bayesian (Optuna)
    # ------------------------------------------------------------------

    async def _run_bayesian(
        self,
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        param_space: ParameterSpace,
        max_trials: int,
        symbol: str,
        timeframe: Timeframe,
        on_trial_complete: Any,
    ) -> HyperoptResult:
        try:
            import optuna
        except ImportError as exc:
            raise ImportError(
                "Bayesian hyperparameter optimisation requires optuna. "
                "Install it with: uv pip install 'hydra[ml-training]'"
            ) from exc

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )

        trial_records: list[TrialRecord] = []

        for trial_num in range(max_trials):
            trial = study.ask()
            sampled_params = param_space.suggest(trial)

            sharpe, record = await self._evaluate_trial(
                trial_number=trial_num,
                params=sampled_params,
                strategy_class=strategy_class,
                base_config=base_config,
                bars=bars,
                initial_capital=initial_capital,
                symbol=symbol,
                timeframe=timeframe,
            )

            study.tell(trial, sharpe)
            trial_records.append(record)

            if on_trial_complete is not None:
                await on_trial_complete(trial_num + 1, trial_num + 1, max_trials, record)

            # Yield to event loop so API stays responsive
            await asyncio.sleep(0)

        return self._build_result(trial_records, total_trials=max_trials)

    # ------------------------------------------------------------------
    # Private: Grid search
    # ------------------------------------------------------------------

    async def _run_grid(
        self,
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        param_space: ParameterSpace,
        max_trials: int,
        symbol: str,
        timeframe: Timeframe,
        on_trial_complete: Any,
    ) -> HyperoptResult:
        combinations = param_space.grid_combinations()
        total = min(len(combinations), max_trials)
        trial_records: list[TrialRecord] = []

        for trial_num, params in enumerate(combinations[:total]):
            _, record = await self._evaluate_trial(
                trial_number=trial_num,
                params=params,
                strategy_class=strategy_class,
                base_config=base_config,
                bars=bars,
                initial_capital=initial_capital,
                symbol=symbol,
                timeframe=timeframe,
            )
            trial_records.append(record)

            if on_trial_complete is not None:
                await on_trial_complete(trial_num + 1, trial_num + 1, total, record)

            await asyncio.sleep(0)

        return self._build_result(trial_records, total_trials=total)

    # ------------------------------------------------------------------
    # Private: single trial evaluation
    # ------------------------------------------------------------------

    async def _evaluate_trial(
        self,
        trial_number: int,
        params: dict[str, Any],
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        symbol: str,
        timeframe: Timeframe,
    ) -> tuple[float, TrialRecord]:
        """Run one backtest with ``params`` merged into ``base_config.parameters``.

        Returns the Sharpe ratio and a TrialRecord for logging.
        """
        # Merge sampled params into a copy of the base parameters dict so we
        # never mutate the shared base_config.
        merged_params = {**base_config.parameters, **params}

        trial_config = StrategyConfig(
            id=base_config.id,
            name=base_config.name,
            strategy_class=base_config.strategy_class,
            symbols=base_config.symbols,
            exchange=base_config.exchange,
            timeframes=base_config.timeframes,
            parameters=merged_params,
        )

        try:
            result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=trial_config,
                bars=bars,
                initial_capital=initial_capital,
                symbol=symbol,
                timeframe=timeframe,
            )
            sharpe = result.sharpe_ratio
            total_return = float(result.total_return)
            max_drawdown = float(result.max_drawdown)
            total_trades = result.total_trades
        except Exception:
            logger.debug("Trial %d failed with params %s", trial_number, params, exc_info=True)
            sharpe = -999.0
            total_return = 0.0
            max_drawdown = 0.0
            total_trades = 0

        record = TrialRecord(
            trial_number=trial_number,
            params=params,
            sharpe=sharpe,
            total_return=total_return,
            max_drawdown=max_drawdown,
            total_trades=total_trades,
        )
        return sharpe, record

    # ------------------------------------------------------------------
    # Private: build final result
    # ------------------------------------------------------------------

    @staticmethod
    def _build_result(
        trial_records: list[TrialRecord],
        total_trials: int,
    ) -> HyperoptResult:
        if not trial_records:
            return HyperoptResult(total_trials=total_trials, completed_trials=0)

        best = max(trial_records, key=lambda r: r.sharpe)

        return HyperoptResult(
            best_params=best.params,
            best_metric=best.sharpe,
            trials=trial_records,
            total_trials=total_trials,
            completed_trials=len(trial_records),
        )
