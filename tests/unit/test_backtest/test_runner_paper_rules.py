"""Decision 18: the backtester applies the paper session's rules.

No pyramiding, no short entries on spot, a protective exit from the strategy's ``risk`` block on
every entry (default 5 % stop), gap-through stops at the worse of stop and open (``fill_rules``),
and the protective order cancelled by an exit. Zero slippage, so prices are exact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from hydra.backtest.fills import CommissionConfig, SlippageModel
from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType
from hydra.execution import fill_rules
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(
    i: int, o: str, h: str | None = None, lo: str | None = None, c: str | None = None
) -> OHLCV:
    close = D(c if c is not None else o)
    op = D(o)
    return OHLCV(
        open=op,
        high=D(h) if h is not None else max(op, close),
        low=D(lo) if lo is not None else min(op, close),
        close=close,
        volume=D("1000"),
        timestamp=T0 + timedelta(hours=i),
    )


def _seq(*specs: tuple[str, ...]) -> list[OHLCV]:
    """Consecutive hourly bars from (open[, high, low, close]) tuples."""
    return [_bar(i, *spec) for i, spec in enumerate(specs)]


def _flat(n: int, price: str = "100") -> list[tuple[str, ...]]:
    return [(price,)] * n


class _Scripted(BaseStrategy):
    """Signals by bar index: ``entries`` long (or short) entries, ``exits`` exits."""

    entries: ClassVar[set[int]] = set()
    exits: ClassVar[set[int]] = set()
    direction = Direction.LONG
    count = 0

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        i = type(self).count
        type(self).count += 1
        out: list[EntrySignal | ExitSignal] = []
        if i in self.entries:
            out.append(
                EntrySignal(
                    symbol=bar.symbol,
                    direction=self.direction,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    market_type=MarketType.SPOT,
                )
            )
        if i in self.exits:
            out.append(
                ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id, exchange_id="binance")
            )
        return out


def _strategy(entries: set[int], exits: set[int] = frozenset(), short: bool = False) -> type:
    return type(
        "S",
        (_Scripted,),
        {
            "entries": set(entries),
            "exits": set(exits),
            "direction": Direction.SHORT if short else Direction.LONG,
            "count": 0,
        },
    )


async def _run(cls: type, bars: list[OHLCV], risk: dict[str, Any] | None = None) -> BacktestResult:
    cfg = StrategyConfig(
        id="rules",
        name="rules",
        strategy_class="x.S",
        symbols=["BTCUSDT"],
        **({"risk": risk} if risk is not None else {}),
    )
    runner = BacktestRunner(
        slippage_model=SlippageModel(spread_factor=D("0"), volume_impact_factor=D("0"))
    )
    commission = CommissionConfig(spot_maker=D("0"), spot_taker=D("0"))
    return await runner.run(cls, cfg, bars, initial_capital=D("10000"), commission=commission)


def _kinds(result: BacktestResult) -> list[tuple[str, str, float]]:
    return [(t["type"], t["side"], t["price"]) for t in result.transactions]


async def test_repeated_entries_do_not_pyramid() -> None:
    result = await _run(_strategy(set(range(50))), _seq(*_flat(50)))
    entries = [t for t in result.transactions if t["type"] == "entry"]
    assert len(entries) == 1
    assert entries[0]["quantity"] == 10.0  # 10 % of 10000 at 100


async def test_short_entries_on_spot_are_dropped() -> None:
    result = await _run(_strategy({0, 1, 2}, short=True), _seq(*_flat(10)))
    assert result.transactions == []
    assert result.total_trades == 0


async def test_default_stop_gaps_at_the_open_like_paper() -> None:
    # entry signal on bar 0 -> market fill at bar 2's open (100); default stop 95
    bars = _seq(*_flat(4), ("90", "91", "89", "90.5"), *_flat(3, "90"))
    result = await _run(_strategy({0}), bars)
    assert _kinds(result) == [("entry", "Buy", 100.0), ("exit", "Sell", 90.0)]
    paper = fill_rules.stop_market_fill("SELL", D("95.00"), bars[4], D("0"))
    assert paper is not None and float(paper.price) == 90.0


async def test_risk_block_stop_and_take_profit_as_oco() -> None:
    risk = {
        "stop_loss_method": "fixed_pct",
        "stop_loss_value": 2.0,
        "take_profit_method": "fixed_pct",
        "take_profit_value": 5.0,
    }
    bars = _seq(*_flat(4), ("100", "105.5", "99.5", "104"), *_flat(2, "104"))
    result = await _run(_strategy({0}), bars, risk)
    assert _kinds(result) == [("entry", "Buy", 100.0), ("exit", "Sell", 105.0)]
    assert result.total_trades == 1


async def test_strategy_exit_cancels_the_stop_and_no_short_follows() -> None:
    bars = _seq(*_flat(6), ("100", "100", "50", "60"), *_flat(3, "60"))
    result = await _run(_strategy({0}, exits={3}), bars)
    # exit signalled on bar 3 fills at bar 5's open; the crash on bar 6 finds no position
    assert _kinds(result) == [("entry", "Buy", 100.0), ("exit", "Sell", 100.0)]
    assert result.total_trades == 1


async def test_trailing_stop_from_the_risk_block() -> None:
    risk = {"stop_loss_method": "trailing_stop", "stop_loss_value": 5.0}
    bars = _seq(*_flat(3), ("100", "120", "100", "120"), ("120", "120", "110", "111"))
    result = await _run(_strategy({0}), bars, risk)
    assert _kinds(result) == [("entry", "Buy", 100.0), ("exit", "Sell", 114.0)]
