"""``StrategyConfig.schedule`` end to end: the backtest runtime vetoes entries, never exits.

``BacktestRunner`` builds the same ``SessionRuntime`` a paper session runs, so these are real
fills: an entry signalled on a bar whose open time is outside the schedule never fills, while
strategy exits and protective stops on off-schedule bars still do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import ScheduleConfig, StrategyConfig

T0 = datetime(2026, 1, 1, tzinfo=UTC)  # a Thursday
H = timedelta(hours=1)
WARM = 20
TRADED = 30


class EnterThenExit(BaseStrategy):
    """Enters LONG when flat, exits on the next bar while held: an entry every other bar."""

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if self.context.position(str(bar.symbol)) is not None:
            if self.config.parameters.get("hold"):
                return []
            return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]
        return [
            EntrySignal(
                symbol=bar.symbol,
                direction=Direction.LONG,
                strength=Decimal("1"),
                strategy_id=self.strategy_id,
            )
        ]


def _bars(gap_at: int | None = None) -> list[OHLCV]:
    out: list[OHLCV] = []
    for i in range(WARM + TRADED):
        price = Decimal("90") if gap_at is not None and i >= gap_at else Decimal("100")
        out.append(OHLCV(price, price, price, price, Decimal("10"), T0 + i * H))
    return out


async def _run(
    schedule: ScheduleConfig | None, bars: list[OHLCV], **parameters: Any
) -> BacktestResult:
    cfg = StrategyConfig(
        id="sched",
        name="sched",
        strategy_class="unused.direct.class.passed",
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h"},
        parameters=parameters,
        schedule=schedule,
    )
    return await BacktestRunner().run(
        EnterThenExit,
        cfg,
        bars,
        initial_capital=Decimal("10000"),
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        warmup_bars=WARM,
    )


async def test_off_schedule_entries_never_fill_and_the_off_schedule_exit_does() -> None:
    bars = _bars()
    first = bars[WARM].timestamp  # Thursday 20:00
    schedule = ScheduleConfig(active_hours_utc=[first.hour], active_days=[first.weekday()])
    result = await _run(schedule, bars)
    # unfiltered, the same bars re-enter every other bar (until the loss-streak pause)
    assert len((await _run(None, bars)).trades) == 12
    [trade] = result.trades
    assert trade.entry_time == first
    assert trade.exit_time == first + H
    assert not schedule.allows(trade.exit_time)


async def test_a_schedule_excluding_every_signal_bar_trades_nothing() -> None:
    bars = _bars()
    traded_days = {b.timestamp.weekday() for b in bars[WARM:]}
    assert traded_days == {3, 4, 5}  # Thursday 20:00 to Saturday 01:00
    schedule = ScheduleConfig(active_days=[d for d in range(7) if d not in traded_days])
    result = await _run(schedule, bars)
    assert result.trades == []
    assert result.equity_curve[-1] == Decimal("10000")


async def test_the_protective_stop_fires_outside_the_schedule() -> None:
    gap = WARM + 5
    bars = _bars(gap_at=gap)
    first = bars[WARM].timestamp
    schedule = ScheduleConfig(active_hours_utc=[first.hour], active_days=[first.weekday()])
    [trade] = (await _run(schedule, bars, hold=True)).trades
    assert trade.entry_time == first
    assert trade.exit_time == bars[gap].timestamp
    assert trade.exit_price < Decimal("95")  # the 5 % default stop, gapped through at 90
    assert not schedule.allows(trade.exit_time)


async def test_the_default_schedule_changes_nothing() -> None:
    bars = _bars()
    unscheduled = await _run(None, bars)
    always = await _run(ScheduleConfig(), bars)
    assert len(unscheduled.trades) == 12
    assert always.trades == unscheduled.trades
    assert always.equity_curve == unscheduled.equity_curve
