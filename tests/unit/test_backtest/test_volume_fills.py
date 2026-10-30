"""The volume model at the runner level: capped entries, sliced exits, ONE round trip.

Zero fees and slippage, so prices are exact. The entry test sizes a capital-100000 run
to 100 units at a close of 100 on a bar that traded 100 — the 10 % volume cap fills 10
and cancels 90. The armed stop then fights five thin bars (volume 20, cap 2/bar) down
94..90 and the run's ``trades`` aggregate those five slices into ONE trade: quantity 10,
volume-weighted exit price, summed PnL. An uncapped run (``max_participation=1``) buys
the whole sized entry in one piece.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(i: int, o: str, volume: str) -> OHLCV:
    op = D(o)
    return OHLCV(
        open=op,
        high=op,
        low=op,
        close=op,
        volume=D(volume),
        timestamp=T0 + timedelta(hours=i),
    )


class _EnterBar0(BaseStrategy):
    """Enters on bar 0; the armed default stop (5 %) exits into the falling thin bars."""

    count: ClassVar[int] = 0

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        i = type(self).count
        type(self).count += 1
        if i == 0:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    market_type=MarketType.SPOT,
                )
            ]
        return []


async def _run(bars: list[OHLCV], sizing_pct: float, capital: str = "10000") -> BacktestResult:
    cfg = StrategyConfig(
        id="volume",
        name="volume",
        strategy_class="x.S",
        symbols=["BTCUSDT"],
        position_sizing={"max_position_pct": sizing_pct},
    )
    _EnterBar0.count = 0
    return await BacktestRunner().run(
        _EnterBar0,
        cfg,
        bars,
        initial_capital=D(capital),
        commission=CommissionConfig(spot_maker=D("0"), spot_taker=D("0")),
        slippage_pct=D("0"),
    )


async def test_the_entry_cap_fills_10_of_100_and_the_stop_slices_five_times_two() -> None:
    # sizing asks for 100 (min(30 % strategy, 10 % global) of 100000 at 100); the bar
    # traded 100, so the 10 % volume cap fills 10 and cancels 90
    bars = [_bar(0, "100", "100")]
    # the stop arms at 95 (5 % below the 100 entry); each thin bar gaps under it
    bars += [_bar(i, op, "20") for i, op in enumerate(("94", "93", "92", "91", "90"), 1)]

    result = await _run(bars, sizing_pct=30.0, capital="100000")

    # the entry transaction is the FILLED slice, not the 100 requested
    (entry, *_rest) = result.transactions
    exits = [t for t in result.transactions if t["type"] == "exit"]
    assert entry["quantity"] == 10.0
    # five exit slices of exactly 2 each, at each thin bar's open (worse of stop and open)
    assert [t["quantity"] for t in exits] == [2.0] * 5
    assert [t["price"] for t in exits] == [94.0, 93.0, 92.0, 91.0, 90.0]

    # one logical exit's slices aggregate into ONE round trip
    [trade] = result.trades
    assert (trade.quantity, trade.entry_price, trade.exit_price) == (
        D("10"),
        D("100"),
        D("92"),  # the volume-weighted price of 2 x (94+93+92+91+90) / 10
    )
    # 10 x (92 - 100): the summed slice PnL, fees zero
    assert trade.pnl == D("-80")
    assert trade.fees == D("0")
    assert trade.exit_time == bars[-1].timestamp


async def test_a_full_volume_bar_still_fills_whole_orders() -> None:
    bars = [_bar(0, "100", "1000")]  # cap 100: the sized entry is never volume-capped
    bars += [_bar(i, op, "1000") for i, op in enumerate(("94", "93", "92"), 1)]

    result = await _run(bars, sizing_pct=15.0)

    # sizing takes min(strategy 15 %, global 10 %) of equity / (mark x fee buffer) = 10,
    # and a full bar absorbs all of it: one entry, one exit, no slices
    (entry, *_rest) = result.transactions
    assert (entry["type"], entry["quantity"]) == ("entry", 10.0)
    [trade] = result.trades
    assert (trade.quantity, trade.entry_price, trade.exit_price) == (
        D("10.00000"),
        D("100"),
        D("94"),  # the full stop fires on the first falling bar
    )
    assert len(result.trades) == 1


async def test_the_runner_passes_a_custom_max_participation_to_both_executors() -> None:
    # an uncapped run (max_participation=1) buys the whole sized entry even on the thin book
    bars = [_bar(0, "100", "100")]
    bars += [_bar(i, op, "20") for i, op in enumerate(("94", "93", "92", "91", "90"), 1)]

    cfg = StrategyConfig(
        id="volume",
        name="volume",
        strategy_class="x.S",
        symbols=["BTCUSDT"],
        position_sizing={"max_position_pct": 15.0},
    )
    _EnterBar0.count = 0
    result = await BacktestRunner().run(
        _EnterBar0,
        cfg,
        bars,
        initial_capital=D("10000"),
        commission=CommissionConfig(spot_maker=D("0"), spot_taker=D("0")),
        slippage_pct=D("0"),
        max_participation=D("1"),
    )

    (entry, *_rest) = result.transactions
    exits = [t for t in result.transactions if t["type"] == "exit"]
    # the uncapped entry buys the whole sized quantity: 10 units (the global 10 % cap)
    assert entry["quantity"] == 10.0
    assert [t["quantity"] for t in exits] == [10.0]
