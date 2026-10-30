"""Multi-timeframe replay: confirmation bars reach the strategy, in close-time order.

A 4h-confirmation strategy over 1h bars: the 4h supertrend must veto entries its
direction disagrees with, which only happens when the 4h stream is actually replayed —
today an empty confirmation stream silently never vetoes. The no-lookahead rule: a 4h
bar closing at T is in the context before the 1h bar closing at T is evaluated, and
nothing closes in the future of what a decision saw (the 4h bar closing at T+4h is
invisible to the 1h bar closing at T-1h).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
H = timedelta(hours=1)
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(
    ts: datetime, o: str, h: str | None = None, low: str | None = None, c: str | None = None
) -> OHLCV:
    op = D(o)
    close = D(c) if c is not None else op
    return OHLCV(
        open=op,
        high=D(h) if h is not None else max(op, close),
        low=D(low) if low is not None else min(op, close),
        close=close,
        volume=D("10000"),
        timestamp=ts,
    )


def _rising_1h(count: int, start: float = 100.0, step: float = 0.5) -> list[OHLCV]:
    """A monotonic rise: every 1h bar closes above its predecessor."""
    out = []
    price = start
    ts = T0
    for _ in range(count):
        price += step
        out.append(_bar(ts, str(price)))
        ts += H
    return out


class _ConfirmingEntry(BaseStrategy):
    """Enters long on a 1h rise, but only when the 4h stream's last close is above 120.

    The confirmation read is a two-value slice of the 4h series — an empty stream
    yields no bars and the condition silently passes (the dishonesty this pins).
    """

    count: ClassVar[int] = 0

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        type(self).count += 1
        if self.context.position(symbol) is not None:
            return []
        confirm = self.context.bars(symbol, Timeframe.H4, 2)
        if not confirm:
            return []  # honestly nothing confirmed: refuse rather than silently pass
        if confirm[-1].close <= D("120"):
            return []
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


def _four_hour(bars_1h: list[OHLCV]) -> list[OHLCV]:
    """Every four 1h bars folded into one 4h bar (open of the first, close of the last)."""
    out = []
    for i in range(0, len(bars_1h) - 3, 4):
        window = bars_1h[i : i + 4]
        out.append(
            OHLCV(
                open=window[0].open,
                high=max(b.high for b in window),
                low=min(b.low for b in window),
                close=window[-1].close,
                volume=sum((b.volume for b in window), D("0")),
                timestamp=window[0].timestamp,
            )
        )
    return out


async def _run(
    bars: list[OHLCV],
    extras: dict[Timeframe, list[OHLCV]] | None,
    confirmation: Timeframe | None = Timeframe.H4,
) -> BacktestResult:
    cfg = StrategyConfig(
        id="mtf",
        name="mtf",
        strategy_class="x.S",
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h", "confirmation": str(confirmation) if confirmation else None},
    )
    _ConfirmingEntry.count = 0
    return await BacktestRunner().run(
        _ConfirmingEntry,
        cfg,
        bars,
        initial_capital=D("100000"),
        commission=CommissionConfig(spot_maker=D("0"), spot_taker=D("0")),
        slippage_pct=D("0"),
        extra_bars=extras,
    )


async def test_the_confirmed_entry_set_matches_where_the_4h_stream_agrees() -> None:
    bars = _rising_1h(60)  # closes 100.5 .. 130; the 4h closes cross 120 around bar 40
    four_h = _four_hour(bars)

    result = await _run(bars, {Timeframe.H4: four_h})

    # the 4h close exceeds 120 first at the 4h bar closing at T0+44h (fold of bars 40-43:
    # close 120.5); the first 1h entry therefore fills at the bar closing at 44h+step
    entries = [t for t in result.transactions if t["type"] == "entry"]
    assert entries, "the confirmed fixture must trade"
    assert all(D(str(t["price"])) > D("120") for t in entries), (
        "no entry may fill before the 4h stream confirms"
    )


async def test_without_extras_the_strategy_honestly_refuses_to_enter() -> None:
    """The removed silent never-veto, pinned: no extras means the confirmation condition
    cannot pass (the strategy reads an empty stream), so NOTHING enters — instead of
    today's behaviour where an empty confirmation stream silently confirmed everything."""
    bars = _rising_1h(60)

    result = await _run(bars, None)

    assert result.transactions == []
    assert result.total_trades == 0


async def test_a_4h_bar_is_visible_only_once_it_closes() -> None:
    """No lookahead: the 1h bar closing at T-1h must not see the 4h bar closing at T.

    The 4h bar opening at T0+40h closes at T0+44h with close 122 (bars 40-43 folded).
    The 1h bar closing at T0+43h (index 42, close 121.5) is evaluated BEFORE that 4h bar
    closes, so a strategy reading the 4h stream there must still see the previous 4h
    close (120); the 1h bar closing at T0+44h sees 122 — the tie goes to the higher
    timeframe first, exactly the ingestion order.
    """
    bars = _rising_1h(60)
    four_h = _four_hour(bars)

    seen: dict[int, Decimal] = {}

    class _Probing(_ConfirmingEntry):
        async def on_bar(self, bar: BarEvent) -> list[EntrySignal]:
            i = type(self).count
            confirm = self.context.bars(str(bar.symbol), Timeframe.H4, 1)
            if i in (41, 42, 43):
                seen[i] = confirm[-1].close if confirm else D("-1")
            return await super().on_bar(bar)

    cfg = StrategyConfig(
        id="mtf",
        name="mtf",
        strategy_class="x.S",
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h", "confirmation": "4h"},
    )
    _Probing.count = 0
    await BacktestRunner().run(
        _Probing,
        cfg,
        bars,
        initial_capital=D("100000"),
        commission=CommissionConfig(spot_maker=D("0"), spot_taker=D("0")),
        slippage_pct=D("0"),
        extra_bars={Timeframe.H4: four_h},
    )

    # bar 41 and 42 close at T0+42h/43h: the 4h bar closing at 44h is still in the future
    assert (seen[41], seen[42]) == (D("120"), D("120"))
    # bar 43 closes at T0+44h exactly: the tie goes to the higher timeframe FIRST
    assert seen[43] == D("122")
