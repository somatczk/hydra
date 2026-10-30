"""Hyperparameter optimisation for trading strategies.

Supports Bayesian optimisation (Optuna TPE, seeded) and grid search over a user-defined
parameter space. Each trial runs a full backtest on the given bars and records its
metrics. A trial that raises, never trades or has a non-finite Sharpe ratio is
ineligible: it is recorded with a status and error string, told to Optuna as FAIL, and
can never win. Identical parameter sets are evaluated once.

``run`` carves a fixed final 20 % holdout that no optimisation trial ever sees and
re-runs the best config on it once, warmed up with the tail of the train window;
``optimise`` runs the trial loop over the bars it is given (the walk-forward analyser
calls it once per fold). Param-space names the strategy cannot read are rejected up
front: a typo would otherwise optimise a no-op dimension. The result records the trial
count, failed trials, the seed, the windows, the DSR and the CSCV PBO of the eligible
trial matrix.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.validation import (
    daily_returns,
    deflated_sharpe,
    pbo_cscv,
    per_period_sharpe,
    probabilistic_sharpe,
)
from hydra.core.types import OHLCV, Timeframe
from hydra.execution.perp_trading import FundingRow
from hydra.strategy.base import BaseStrategy
from hydra.strategy.builtin.ml_ensemble import STRATEGY_CLASS as ML_NATIVE_CLASS
from hydra.strategy.builtin.rule_based import RuleBasedStrategy, bindable_params, param_bindings
from hydra.strategy.condition_schema import RuleSet
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

if TYPE_CHECKING:
    import optuna as _optuna_type

logger = logging.getLogger(__name__)

SEED = 42
HOLDOUT_FRACTION = 0.2


class NoEligibleTrialError(ValueError):
    """No parameter set completed a backtest with at least one trade."""


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
# Parameter-name validation
# ---------------------------------------------------------------------------


def unknown_param_names(
    param_space: ParameterSpace, base_config: StrategyConfig, strategy_class: type[BaseStrategy]
) -> list[str]:
    """Param-space names the strategy cannot read, sorted; a typo optimises a no-op.

    A rule-based strategy reads ``required_history`` and the names its conditions bind
    (``bindable_params``, the resolver ``RuleBasedStrategy`` applies; an ambiguous implicit
    name binds nothing). Any other strategy reads its declared ``parameters``.
    """
    if issubclass(strategy_class, RuleBasedStrategy):
        rules = RuleSet.model_validate(base_config.parameters.get("rules") or {})
        readable = {"required_history", *bindable_params(rules)}
    else:
        readable = set(base_config.parameters)
    return sorted({p.name for p in param_space.params if p.name not in readable})


def param_space_problems(
    param_space: ParameterSpace, base_config: StrategyConfig, strategy_class: type[BaseStrategy]
) -> list[str]:
    """Why the strategy cannot read each ``unknown_param_names`` name; empty when all bind.

    A rule-based name matching conditions that hold different values (``param_bindings``)
    is named with its sites; any other unread name is listed once. Raises ValueError for
    rules that fail to bind.
    """
    unknown = unknown_param_names(param_space, base_config, strategy_class)
    sites = (
        param_bindings(RuleSet.model_validate(base_config.parameters.get("rules") or {}))
        if unknown and issubclass(strategy_class, RuleBasedStrategy)
        else {}
    )
    problems = [
        f"param_space name {name!r} matches conditions with different values by name "
        f"({', '.join(map(str, sites[name]))}), so it binds nothing; bind the one it is "
        "meant for with param_overrides or value_ref_overrides"
        for name in unknown
        if name in sites
    ]
    unread = [name for name in unknown if name not in sites]
    if unread:
        problems.append(
            f"param_space names the strategy never reads: {', '.join(unread)} "
            "(strategy_params lists the names a rule-based strategy binds)"
        )
    untunable = untunable_param_problem(param_space, base_config)
    if untunable:
        problems.append(untunable)
    return problems


def untunable_param_problem(param_space: ParameterSpace, base_config: StrategyConfig) -> str | None:
    """Why a trial cannot vary the ML-native ``model_name``; None when the space does not.

    The name is readable, but a job attaches its model once, before the run, so a trial
    naming another model would hit one nothing bound. An ``ml_overlay`` name sits outside
    ``parameters`` and is already unread.
    """
    if base_config.strategy_class != ML_NATIVE_CLASS:
        return None
    if all(p.name != "model_name" for p in param_space.params):
        return None
    return (
        "param_space varies model_name, but a run serves only the model attached before "
        "it starts; pin one registry version with model_version_id instead"
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrialRecord:
    """Metrics captured for a single hyperopt trial.

    Ineligible trials (``failed``: the backtest raised or the Sharpe is non-finite;
    ``no_trades``: the config never traded) keep ``sharpe`` 0.0 for the API's number
    type, carry the error string and are never selectable as the best trial.
    """

    trial_number: int
    params: dict[str, Any]
    sharpe: float
    total_return: float
    max_drawdown: float
    total_trades: int
    status: Literal["ok", "failed", "no_trades"] = "ok"
    error: str | None = None


@dataclass(slots=True)
class _CachedTrial:
    """Everything one parameter set produced; cached so repeats skip the backtest."""

    status: Literal["ok", "failed", "no_trades"]
    error: str | None
    sharpe: float
    total_return: float
    max_drawdown: float
    total_trades: int
    returns: np.ndarray


@dataclass(slots=True)
class HyperoptResult:
    """Aggregated result of a hyperparameter optimisation run."""

    best_params: dict[str, Any] = field(default_factory=dict)
    best_metric: float = 0.0  # Best train-window Sharpe ratio achieved
    trials: list[TrialRecord] = field(default_factory=list)
    total_trials: int = 0
    completed_trials: int = 0
    # distinct eligible configs (the DSR's N and the PBO matrix's column count)
    n_trials: int = 0
    failed_trials: int = 0
    seed: int = SEED
    train_start: datetime | None = None
    train_end: datetime | None = None
    holdout_start: datetime | None = None
    holdout_end: datetime | None = None
    # the best config re-run once on the holdout window, warmed up from the train tail
    holdout: BacktestResult | None = None
    holdout_psr: float | None = None
    # probabilistic Sharpe of the best trial against the expected max of n_trials trials
    dsr: float | None = None
    # CSCV probability of backtest overfitting over the eligible trial matrix
    pbo: float | None = None
    # the best trial's own backtest result (the walk-forward analyser reports it per fold)
    best_result: BacktestResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON payload stored on the job row; ids and provenance are added by the caller."""
        holdout = None
        if self.holdout is not None:
            holdout = {
                "sharpe": self.holdout.sharpe_ratio,
                "total_return": float(self.holdout.total_return),
                "max_drawdown": float(self.holdout.max_drawdown),
                "total_trades": self.holdout.total_trades,
            }
        return {
            "best_params": self.best_params,
            "best_metric": self.best_metric,
            "trials": [
                {
                    "trial_number": r.trial_number,
                    "params": r.params,
                    "sharpe": r.sharpe,
                    "total_return": r.total_return,
                    "max_drawdown": r.max_drawdown,
                    "total_trades": r.total_trades,
                    "status": r.status,
                    "error": r.error,
                }
                for r in self.trials
            ],
            "total_trials": self.total_trials,
            "completed_trials": self.completed_trials,
            "n_trials": self.n_trials,
            "failed_trials": self.failed_trials,
            "seed": self.seed,
            "train_start": _iso(self.train_start),
            "train_end": _iso(self.train_end),
            "holdout_start": _iso(self.holdout_start),
            "holdout_end": _iso(self.holdout_end),
            "holdout": holdout,
            "holdout_psr": self.holdout_psr,
            "dsr": self.dsr,
            "pbo": self.pbo,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class HyperoptRunner:
    """Orchestrates hyperparameter optimisation over a BacktestRunner.

    ``commission`` prices every trial and the holdout run alike, so a caller with
    non-default fees never selects under one fee regime and evaluates under another.

    Supports two search methods:

    * ``"bayesian"`` — Optuna's seeded TPE sampler. Ineligible trials are told as FAIL,
      so the sampler is never steered toward crashing or non-trading regions.
    * ``"grid"`` — Exhaustive enumeration of all combinations. Suitable for
      small discrete spaces where coverage matters more than efficiency.
    """

    def __init__(
        self,
        backtest_runner: BacktestRunner | None = None,
        commission: CommissionConfig | None = None,
    ) -> None:
        self._runner = backtest_runner or BacktestRunner()
        self._commission = commission or CommissionConfig()

    async def run(
        self,
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        param_space: ParameterSpace,
        method: Literal["bayesian", "grid"] = "bayesian",
        max_trials: int = 50,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
        on_trial_complete: Any = None,
        funding: Mapping[str, Sequence[FundingRow]] | None = None,
    ) -> HyperoptResult:
        """Optimise on the first 80 % of ``bars`` and re-run the best config on the rest.

        The final ``HOLDOUT_FRACTION`` of the bars is never passed to any optimisation
        trial. The holdout run is warmed up with ``required_history`` bars from the end
        of the train window, so the first holdout bar trades with a full context.
        """
        if not bars:
            return HyperoptResult()
        split = int(len(bars) * (1.0 - HOLDOUT_FRACTION))
        if split <= 0 or split >= len(bars):
            msg = f"{len(bars)} bars cannot carve a {HOLDOUT_FRACTION:.0%} holdout"
            raise ValueError(msg)
        train_bars = bars[:split]
        holdout_bars = bars[split:]
        result = await self.optimise(
            strategy_class=strategy_class,
            base_config=base_config,
            bars=train_bars,
            initial_capital=initial_capital,
            param_space=param_space,
            method=method,
            max_trials=max_trials,
            symbol=symbol,
            timeframe=timeframe,
            on_trial_complete=on_trial_complete,
            funding=funding,
        )
        result.train_start = train_bars[0].timestamp
        result.train_end = train_bars[-1].timestamp
        result.holdout_start = holdout_bars[0].timestamp
        result.holdout_end = holdout_bars[-1].timestamp

        best_config = base_config.model_copy(
            update={"parameters": {**base_config.parameters, **result.best_params}}
        )
        required_history = int(
            strategy_class(config=best_config, context=StrategyContext()).required_history
        )
        warm = train_bars[-required_history:] if required_history > 0 else []
        result.holdout = await self._runner.run(
            strategy_class=strategy_class,
            strategy_config=best_config,
            bars=[*warm, *holdout_bars],
            initial_capital=initial_capital,
            commission=self._commission,
            symbol=symbol,
            timeframe=timeframe,
            warmup_bars=len(warm),
            funding=funding,
        )
        result.holdout_psr = probabilistic_sharpe(
            daily_returns(result.holdout.equity_curve, result.holdout.timestamps)
        )
        return result

    async def optimise(
        self,
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        param_space: ParameterSpace,
        method: Literal["bayesian", "grid"] = "bayesian",
        max_trials: int = 50,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
        on_trial_complete: Any = None,
        funding: Mapping[str, Sequence[FundingRow]] | None = None,
    ) -> HyperoptResult:
        """Run the trial loop over ``bars`` (no holdout split).

        The walk-forward analyser calls this once per fold on the fold's in-sample
        window. Raises NoEligibleTrialError when no trial completes with a trade.
        """
        if not bars:
            return HyperoptResult()

        problems = param_space_problems(param_space, base_config, strategy_class)
        if problems:
            raise ValueError("; ".join(problems))

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
                funding=funding,
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
            funding=funding,
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
        funding: Mapping[str, Sequence[FundingRow]] | None,
    ) -> HyperoptResult:
        try:
            import optuna
        except ImportError as exc:
            raise ImportError(
                "Bayesian hyperparameter optimisation requires optuna, a core dependency"
            ) from exc

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=SEED),
        )

        trial_records: list[TrialRecord] = []
        cache: dict[str, _CachedTrial] = {}
        best: TrialRecord | None = None
        best_result: BacktestResult | None = None

        for trial_num in range(max_trials):
            trial = study.ask()
            sampled_params = param_space.suggest(trial)

            outcome, result = await self._run_params(
                params=sampled_params,
                strategy_class=strategy_class,
                base_config=base_config,
                bars=bars,
                initial_capital=initial_capital,
                symbol=symbol,
                timeframe=timeframe,
                cache=cache,
                funding=funding,
            )
            if outcome.status == "ok":
                study.tell(trial, outcome.sharpe)
            else:
                study.tell(trial, state=optuna.trial.TrialState.FAIL)

            record = _record(trial_num, sampled_params, outcome)
            trial_records.append(record)
            if (
                outcome.status == "ok"
                and result is not None
                and (best is None or outcome.sharpe > best.sharpe)
            ):
                best = record
                best_result = result

            if on_trial_complete is not None:
                await on_trial_complete(trial_num + 1, trial_num + 1, max_trials, record)

            # Yield to event loop so the worker stays responsive
            await asyncio.sleep(0)

        return _build_result(
            trial_records=trial_records,
            cache=cache,
            best=best,
            best_result=best_result,
            total_trials=max_trials,
        )

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
        funding: Mapping[str, Sequence[FundingRow]] | None,
    ) -> HyperoptResult:
        combinations = param_space.grid_combinations()
        total = min(len(combinations), max_trials)
        trial_records: list[TrialRecord] = []
        cache: dict[str, _CachedTrial] = {}
        best: TrialRecord | None = None
        best_result: BacktestResult | None = None

        for trial_num, params in enumerate(combinations[:total]):
            outcome, result = await self._run_params(
                params=params,
                strategy_class=strategy_class,
                base_config=base_config,
                bars=bars,
                initial_capital=initial_capital,
                symbol=symbol,
                timeframe=timeframe,
                cache=cache,
                funding=funding,
            )
            record = _record(trial_num, params, outcome)
            trial_records.append(record)
            if (
                outcome.status == "ok"
                and result is not None
                and (best is None or outcome.sharpe > best.sharpe)
            ):
                best = record
                best_result = result

            if on_trial_complete is not None:
                await on_trial_complete(trial_num + 1, trial_num + 1, total, record)

            await asyncio.sleep(0)

        return _build_result(
            trial_records=trial_records,
            cache=cache,
            best=best,
            best_result=best_result,
            total_trials=total,
        )

    # ------------------------------------------------------------------
    # Private: single trial evaluation
    # ------------------------------------------------------------------

    async def _run_params(
        self,
        params: dict[str, Any],
        strategy_class: type[BaseStrategy],
        base_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal,
        symbol: str,
        timeframe: Timeframe,
        cache: dict[str, _CachedTrial],
        funding: Mapping[str, Sequence[FundingRow]] | None,
    ) -> tuple[_CachedTrial, BacktestResult | None]:
        """Backtest one parameter set (or reuse the cached outcome for a repeat).

        The trial config is a full copy of the base config with only ``parameters``
        merged, so position_sizing, filters, risk, schedule, ml_overlay and enabled
        stay exactly as the operator wrote them (BT-8).
        """
        key = json.dumps(params, sort_keys=True, default=str)
        cached = cache.get(key)
        if cached is not None:
            return cached, None

        merged_params = {**base_config.parameters, **params}
        trial_config = base_config.model_copy(update={"parameters": merged_params})

        try:
            result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=trial_config,
                bars=bars,
                initial_capital=initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
                funding=funding,
            )
        except Exception as exc:
            logger.debug("Trial with params %s failed", params, exc_info=True)
            outcome = _CachedTrial(
                status="failed",
                error=str(exc),
                sharpe=0.0,
                total_return=0.0,
                max_drawdown=0.0,
                total_trades=0,
                returns=np.array([], dtype=np.float64),
            )
            cache[key] = outcome
            return outcome, None

        sharpe = result.sharpe_ratio
        if result.total_trades < 1:
            outcome = _CachedTrial(
                status="no_trades",
                error="strategy made no trades",
                sharpe=0.0,
                total_return=float(result.total_return),
                max_drawdown=float(result.max_drawdown),
                total_trades=0,
                returns=np.array([], dtype=np.float64),
            )
        elif not math.isfinite(sharpe):
            outcome = _CachedTrial(
                status="failed",
                error=f"non-finite Sharpe ratio: {sharpe}",
                sharpe=0.0,
                total_return=float(result.total_return),
                max_drawdown=float(result.max_drawdown),
                total_trades=result.total_trades,
                returns=np.array([], dtype=np.float64),
            )
        else:
            outcome = _CachedTrial(
                status="ok",
                error=None,
                sharpe=sharpe,
                total_return=float(result.total_return),
                max_drawdown=float(result.max_drawdown),
                total_trades=result.total_trades,
                returns=daily_returns(result.equity_curve, result.timestamps),
            )
        cache[key] = outcome
        return outcome, result


def _record(trial_number: int, params: dict[str, Any], outcome: _CachedTrial) -> TrialRecord:
    return TrialRecord(
        trial_number=trial_number,
        params=params,
        sharpe=outcome.sharpe,
        total_return=outcome.total_return,
        max_drawdown=outcome.max_drawdown,
        total_trades=outcome.total_trades,
        status=outcome.status,
        error=outcome.error,
    )


def _build_result(
    trial_records: list[TrialRecord],
    cache: dict[str, _CachedTrial],
    best: TrialRecord | None,
    best_result: BacktestResult | None,
    total_trials: int,
) -> HyperoptResult:
    if best is None:
        raise NoEligibleTrialError("no parameter set completed a backtest with a trade")

    distinct_keys: list[str] = []
    for record in trial_records:
        if record.status != "ok":
            continue
        key = json.dumps(record.params, sort_keys=True, default=str)
        if key not in distinct_keys:
            distinct_keys.append(key)
    distinct_returns = [cache[key].returns for key in distinct_keys]
    n_trials = len(distinct_keys)

    if n_trials >= 2:
        trial_srs = np.array([per_period_sharpe(returns) for returns in distinct_returns])
        trial_sr_var = float(np.var(trial_srs, ddof=1))
    else:
        trial_sr_var = 0.0
    best_returns = cache[json.dumps(best.params, sort_keys=True, default=str)].returns
    dsr = deflated_sharpe(best_returns, n_trials, trial_sr_var)

    pbo: float | None = None
    if n_trials >= 2:
        # shortcut: zero-padding a margin-call-truncated run extends it with flat days
        days = max((len(returns) for returns in distinct_returns), default=0)
        matrix = np.zeros((days, n_trials))
        for column, returns in enumerate(distinct_returns):
            matrix[: len(returns), column] = returns
        pbo = pbo_cscv(matrix)

    return HyperoptResult(
        best_params=best.params,
        best_metric=best.sharpe,
        trials=trial_records,
        total_trials=total_trials,
        completed_trials=len(trial_records),
        n_trials=n_trials,
        failed_trials=sum(1 for r in trial_records if r.status == "failed"),
        seed=SEED,
        dsr=dsr,
        pbo=pbo,
        best_result=best_result,
    )
