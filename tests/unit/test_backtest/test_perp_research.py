"""Perp hyperopt and walk-forward: funding reaches every run and accrues only inside its bars.

A long-only strategy on a constant positive funding rate (longs pay) must end strictly
worse than the same run with no funding rows, in the hyperopt trials and holdout and in
every walk-forward fold. Each fold's out-of-sample run must equal a plain backtest of that
window with the same params, whether it gets the full-window funding mapping or only the
rows inside its own bars (no cross-window leakage or double counting).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.backtest.hyperopt import HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.walkforward import WalkForwardAnalyzer
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType, Timeframe
from hydra.execution.perp_trading import FundingRow
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2024, 1, 1, tzinfo=UTC)
CAPITAL = D("10000")


class _LongHoldStrategy(BaseStrategy):
    """Longs at cycle position 0 of a 10-bar cycle and exits ``hold`` bars later."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        pos = int((bar.ohlcv.timestamp - T0) / DAY) % 10
        has_position = self.context.position(str(bar.symbol)) is not None
        if pos == 0 and not has_position:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binanceusdm",
                    market_type=MarketType.FUTURES,
                )
            ]
        if pos == int(self.config.parameters["hold"]) and has_position:
            return [
                ExitSignal(
                    symbol=bar.symbol, strategy_id=self.strategy_id, exchange_id="binanceusdm"
                )
            ]
        return []


def _config() -> StrategyConfig:
    return StrategyConfig(
        id="perp-research",
        name="Perp research",
        strategy_class="test._LongHoldStrategy",
        symbols=["BTCUSDT"],
        exchange={"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": 2},
        timeframes={"primary": "1d"},
        parameters={"hold": 3},
    )


def _bars(count: int) -> list[OHLCV]:
    """A drifting sawtooth: finite Sharpe, so trials are eligible to win."""
    out: list[OHLCV] = []
    for i in range(count):
        price = D("100") + D("0.3") * (i % 10) + D("0.02") * i
        out.append(
            OHLCV(
                open=price,
                high=price + D("0.05"),
                low=price - D("0.05"),
                close=price,
                volume=D("1000"),
                timestamp=T0 + i * DAY,
            )
        )
    return out


def _funding(count: int, rate: str = "0.001") -> dict[str, list[FundingRow]]:
    """Three settlements a day (03/11/19 UTC) across the whole window, longs pay."""
    rows = [(T0 + d * DAY + timedelta(hours=h), D(rate)) for d in range(count) for h in (3, 11, 19)]
    return {"BTCUSDT": rows}


def _space() -> ParameterSpace:
    return ParameterSpace(params=[ParamDef(name="hold", type="categorical", choices=[3, 5])])


async def test_hyperopt_trials_and_holdout_accrue_funding() -> None:
    bars = _bars(100)
    funding = _funding(100)

    async def run(rows: dict[str, list[FundingRow]] | None) -> Any:
        return await HyperoptRunner(BacktestRunner()).run(
            strategy_class=_LongHoldStrategy,
            base_config=_config(),
            bars=bars,
            initial_capital=CAPITAL,
            param_space=_space(),
            method="grid",
            max_trials=2,
            timeframe=Timeframe.D1,
            funding=rows,
        )

    plain, funded = await run(None), await run(funding)
    assert plain.holdout is not None and funded.holdout is not None
    assert funded.holdout.trades
    assert funded.holdout.equity_curve[-1] < plain.holdout.equity_curve[-1]
    assert all(
        f.total_return < p.total_return
        for f, p in zip(funded.trials, plain.trials, strict=True)
        if p.status == "ok"
    )


async def test_walkforward_folds_accrue_funding_and_match_a_plain_window_backtest() -> None:
    bars = _bars(240)
    funding = _funding(240)

    async def run(rows: dict[str, list[FundingRow]] | None) -> Any:
        return await WalkForwardAnalyzer(initial_capital=CAPITAL).run_walkforward(
            bars=bars,
            strategy_class=_LongHoldStrategy,
            strategy_config=_config(),
            param_space=_space(),
            fold_count=3,
            train_pct=0.5,
            method="grid",
            timeframe=Timeframe.D1,
            funding=rows,
        )

    plain, funded = await run(None), await run(funding)
    assert len(funded.folds) == 3
    for fold, plain_fold in zip(funded.folds, plain.folds, strict=True):
        oos = fold.out_of_sample_metrics
        assert oos.trades
        assert oos.equity_curve[-1] < plain_fold.out_of_sample_metrics.equity_curve[-1]

        # a plain backtest of the same window (1 warm-up bar, same params)
        is_end = 60 * (fold.fold_index + 1)
        window = bars[is_end - 1 : is_end + 60]
        config = _config().model_copy(update={"parameters": fold.selected_params})
        first, last = window[1].timestamp, window[-1].timestamp
        inside = {"BTCUSDT": [r for r in funding["BTCUSDT"] if first < r[0] <= last]}
        for rows in (funding, inside):
            expected = await BacktestRunner().run(
                strategy_class=_LongHoldStrategy,
                strategy_config=config,
                bars=window,
                initial_capital=CAPITAL,
                timeframe=Timeframe.D1,
                warmup_bars=1,
                funding=rows,
            )
            assert oos.equity_curve == expected.equity_curve
