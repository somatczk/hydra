"""BacktestRunner on the paper session core: exact fills, equity, sizing, errors and isolation.

Expected numbers are derived by hand from the paper rules: a MARKET order fills at the signal
bar's close x (1 +/- 0.001), fees are 0.1 % of notional in USDT, a gap-through stop fills at the
worse of stop and open, slipped; sizing is ``risk.sizing.size_entry`` floored to the fixture's
0.00001 lot step.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from prometheus_client import REGISTRY

import hydra.backtest.runner as runner_module
from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult
from hydra.backtest.runner import BacktestRunner, StrategyError
from hydra.core.events import BarEvent, EntrySignal, ExitSignal, OrderFillEvent
from hydra.core.types import OHLCV, Direction, MarketType, Position, Side
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.paper_trading import PaperFill, PaperTradingExecutor
from hydra.risk.pretrade import RiskConfig
from hydra.strategy.base import BaseStrategy
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
RULE_CLASS = "hydra.strategy.builtin.rule_based.RuleBasedStrategy"


def _bar(i: int, o: str, h: str, lo: str, c: str, volume: str = "1000") -> OHLCV:
    return OHLCV(
        open=D(o), high=D(h), low=D(lo), close=D(c), volume=D(volume), timestamp=T0 + i * H
    )


def _flat(i: int, price: str = "100") -> OHLCV:
    return _bar(i, price, price, price, price)


# warm-up 100, 100; bar1 closes at 106 (entry), bar2 rises, bar3 gaps through the stop
BARS = [
    _flat(0),
    _flat(1),
    _bar(2, "106", "106", "106", "106"),
    _bar(3, "110", "111", "109", "110"),
    _bar(4, "100", "101", "99", "100"),
]


def _rule(comparator: str, value: float) -> dict[str, Any]:
    cond = {"indicator": "close", "params": {}, "comparator": comparator, "value": value}
    return {"operator": "AND", "conditions": [cond]}


def _config(**overrides: Any) -> StrategyConfig:
    data: dict[str, Any] = {
        "id": "rules",
        "name": "rules",
        "strategy_class": RULE_CLASS,
        "parameters": {
            "required_history": 2,
            "rules": {
                "entry_long": _rule("greater_than", 105),
                "exit_long": _rule("greater_than", 200),
            },
        },
        "position_sizing": {"max_position_pct": 10.0},
        "risk": {"stop_loss_method": "fixed_pct", "stop_loss_value": 5.0},
    }
    data.update(overrides)
    return StrategyConfig(**data)


async def _run(
    bars: list[OHLCV] = BARS,
    *,
    cls: type[BaseStrategy] = RuleBasedStrategy,
    config: StrategyConfig | None = None,
    warmup_bars: int = 2,
    **kwargs: Any,
) -> BacktestResult:
    return await BacktestRunner().run(
        cls,
        config or _config(),
        bars,
        initial_capital=D("10000"),
        commission=CommissionConfig(spot_maker=D("0.001"), spot_taker=D("0.001")),
        warmup_bars=warmup_bars,
        **kwargs,
    )


def _fills(result: BacktestResult) -> list[tuple[str, str, float, float, float]]:
    return [
        (t["time"], t["side"], t["quantity"], t["price"], t["fee"]) for t in result.transactions
    ]


async def test_signal_close_fill_gap_stop_and_fees() -> None:
    result = await _run()

    assert _fills(result) == [
        (BARS[2].timestamp.isoformat(), "Buy", 9.43396, 106.106, 1.00099975976),
        (BARS[4].timestamp.isoformat(), "Sell", 9.43396, 99.9, 0.942452604),
    ]
    assert result.equity_curve == [
        D("9997.99900048024"),
        D("10035.73484048024"),
        D("9939.50939187624"),
    ]
    [trade] = result.trades
    assert trade.entry_price == D("106.106")
    assert trade.exit_price == D("99.900")
    assert trade.pnl == D("-60.49060812376")
    assert trade.fees == D("1.94345236376")
    assert trade.entry_time == BARS[2].timestamp
    assert trade.exit_time == BARS[4].timestamp
    assert D("10000") + sum((t.pnl for t in result.trades), D("0")) == result.equity_curve[-1]


class _Recorder(BaseStrategy):
    """Enters on the first bar after warm-up and records what it can see on every bar."""

    seen: ClassVar[list[tuple[datetime, datetime, Position | None]]] = []

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        assert bar.ohlcv is not None
        newest = self.context.bars(str(bar.symbol), bar.timeframe, 1)[-1].timestamp
        type(self).seen.append((bar.ohlcv.timestamp, newest, self.context.position("BTCUSDT")))
        if len(type(self).seen) > 1:
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


async def test_no_lookahead() -> None:
    _Recorder.seen = []
    result = await _run(cls=_Recorder)

    assert [(bar_ts, newest) for bar_ts, newest, _ in _Recorder.seen] == [
        (b.timestamp, b.timestamp) for b in BARS[2:]
    ]
    assert _Recorder.seen[0][2] is None
    position = _Recorder.seen[1][2]
    assert position is not None and position.avg_entry_price == D("106.106")
    assert result.equity_curve[0] == D("9997.99900048024")  # marked at bar 1's close


async def test_no_future_volume() -> None:
    later = [_bar(5 + i, "100", "100", "100", "100", volume="1000000") for i in range(1000)]
    base = await _run()
    extended = await _run(BARS + later)
    assert _fills(extended)[:2] == _fills(base)


class _Crashes(BaseStrategy):
    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        assert bar.ohlcv is not None
        if bar.ohlcv.timestamp == BARS[3].timestamp:
            raise ZeroDivisionError("division by zero")
        return []


async def test_strategy_exception_fails_the_run() -> None:
    with pytest.raises(StrategyError, match="rules: ZeroDivisionError"):
        await _run(cls=_Crashes)


class _FillCrashes(_Recorder):
    async def on_fill(self, fill: OrderFillEvent) -> None:
        raise ValueError("bad fill handler")


async def test_on_fill_exception_fails_the_run() -> None:
    _Recorder.seen = []
    with pytest.raises(StrategyError, match="rules: ValueError"):
        await _run(cls=_FillCrashes)


class _HookCrashes(_Recorder):
    hook: ClassVar[str] = ""

    async def on_start(self) -> None:
        if self.hook == "start":
            raise RuntimeError("boom")

    async def on_fill(self, fill: OrderFillEvent) -> None:
        if self.hook == "close_out" and fill.side == Side.SELL:
            raise ValueError("bad close-out fill")

    async def on_stop(self) -> None:
        if self.hook == "stop":
            raise KeyError("boom")


@pytest.mark.parametrize(
    ("hook", "error"),
    [("start", "RuntimeError"), ("close_out", "ValueError"), ("stop", "KeyError")],
)
async def test_exception_in_any_strategy_hook_fails_the_run(hook: str, error: str) -> None:
    _Recorder.seen = []
    _HookCrashes.hook = hook
    with pytest.raises(StrategyError, match=f"rules: {error}"):
        await _run(BARS[:3], cls=_HookCrashes)


class _AlwaysLong(BaseStrategy):
    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if self.context.position(str(bar.symbol)) is not None:
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


async def test_daily_loss_halt_runs_on_bar_time_and_stays_out_of_live_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    bars = [_flat(0), _bar(1, "90", "90", "90", "90")] + [_flat(i, "90") for i in range(2, 30)]
    result = await _run(
        bars,
        cls=_AlwaysLong,
        config=_config(position_sizing={"max_position_pct": 90.0}),
        warmup_bars=0,
        risk_config=RiskConfig(
            max_position_pct=D("0.9"), max_risk_per_trade=D("0.5"), max_portfolio_heat=D("5")
        ),
    )

    # a ~9 % gap-stop loss at 01:00 halts entries until the bar whose close is the next 00:00 UTC
    sides = [(t["time"], t["side"]) for t in result.transactions]
    assert sides[:3] == [
        (bars[0].timestamp.isoformat(), "Buy"),
        (bars[1].timestamp.isoformat(), "Sell"),
        (bars[23].timestamp.isoformat(), "Buy"),
    ]
    assert result.trades[0].pnl < D("-300")
    assert not [r for r in caplog.records if r.name == "hydra.audit"]
    assert not [
        r for r in caplog.records if r.name.startswith("hydra.") and r.levelno == logging.WARNING
    ]


async def test_session_failure_stops_the_run_and_flattens(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailsOnBar3(PaperTradingExecutor):
        async def on_closed_bar(self, symbol: str, bar: OHLCV) -> list[PaperFill]:
            if bar.timestamp == BARS[3].timestamp:
                raise OSError("disk gone")
            return await super().on_closed_bar(symbol, bar)

    monkeypatch.setattr(runner_module, "PaperTradingExecutor", _FailsOnBar3)
    result = await _run()

    assert result.stopped_reason == "paper executor failed on a closed bar: disk gone"
    assert result.timestamps == [BARS[2].timestamp, BARS[3].timestamp]
    assert len(result.equity_curve) == len(result.benchmark_equity) == 2
    assert [t["side"] for t in result.transactions] == ["Buy", "Sell"]
    assert D("10000") + sum((t.pnl for t in result.trades), D("0")) == result.equity_curve[-1]


async def test_entry_on_last_bar_is_closed_out_with_fees() -> None:
    result = await _run(BARS[:3])
    assert _fills(result) == [
        (BARS[2].timestamp.isoformat(), "Buy", 9.43396, 106.106, 1.00099975976),
        (BARS[2].timestamp.isoformat(), "Sell", 9.43396, 105.894, 0.99899976024),
    ]
    assert result.equity_curve == [D("9996.00000096000")]
    assert [t.pnl for t in result.trades] == [D("-3.99999904000")]


async def test_futures_config_is_rejected() -> None:
    config = _config(exchange={"exchange_id": "binance", "market_type": "FUTURES"})
    with pytest.raises(UnsupportedVenueError):
        await _run(config=config)


async def test_position_sizing_is_honoured() -> None:
    small = await _run(config=_config(position_sizing={"max_position_pct": 5.0}))
    assert small.transactions[0]["quantity"] == 4.71698

    capped = await _run(risk_config=RiskConfig(max_position_pct=D("0.03")))
    assert capped.transactions[0]["quantity"] == 2.83018


async def test_warmup_bars_only_seed_the_context() -> None:
    result = await _run()
    assert len(result.equity_curve) == len(BARS) - 2
    assert result.timestamps == [b.timestamp for b in BARS[2:]]
    assert len(result.benchmark_equity) == len(result.equity_curve)
    cold = await _run(warmup_bars=0)
    assert len(cold.equity_curve) == len(BARS)
    assert _fills(cold) == _fills(result)


async def test_empty_and_warmup_only_runs() -> None:
    for bars, warm in (([], 0), (BARS[:2], 2)):
        result = await _run(bars, warmup_bars=warm)
        assert result.equity_curve == [D("10000")]
        assert result.total_trades == 0


async def test_run_yields_to_the_event_loop() -> None:
    ticks = 0
    progress: list[float] = []

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    bars = [_flat(i) for i in range(500)]
    await _run(bars, warmup_bars=0, on_progress=progress.append)
    task.cancel()
    assert progress == [0.0, 0.2, 0.4, 0.6, 0.8]
    assert ticks >= 5


def _sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


async def test_backtest_does_not_touch_live_metrics() -> None:
    crash = [_bar(5 + i, "60", "60", "60", "60") for i in range(3)]
    watched = [
        ("hydra_drawdown_ratio", {}),
        ("hydra_portfolio_value", {}),
        ("hydra_circuit_breaker_active_tier", {"scope": "backtest"}),
        ("hydra_kill_switch_active", {}),
        (
            "hydra_trades_total",
            {"symbol": "BTCUSDT", "side": "BUY", "strategy_id": "rules", "exchange_id": "binance"},
        ),
    ]
    before = [_sample(n, **lb) for n, lb in watched]

    await _run(BARS + crash)
    await asyncio.to_thread(asyncio.run, _run(BARS + crash))

    assert [_sample(n, **lb) for n, lb in watched] == before
    position_ids = {
        s.labels.get("session_id", "")
        for metric in REGISTRY.collect()
        if metric.name == "hydra_position_size"
        for s in metric.samples
    }
    assert not any(sid.startswith("backtest-") for sid in position_ids)


def _regime_bars() -> list[OHLCV]:
    """30 drifting zigzag bars (low ADX), 10 bars up 1.5 each, 8 bars down 1 each."""
    closes = [100 + 0.2 * t + 2 * (-1) ** t for t in range(30)]
    closes += [closes[-1] + 1.5 * (k + 1) for k in range(10)]
    closes += [closes[-1] - (k + 1) for k in range(8)]
    return [
        _bar(i, f"{c:.2f}", f"{c + 0.5:.2f}", f"{c - 0.5:.2f}", f"{c:.2f}")
        for i, c in enumerate(closes)
    ]


async def test_adx_filter_and_lag_momentum_trade_end_to_end() -> None:
    """``close > lag:3`` alone buys the chop at bar 10; ``adx(5) > 20`` holds it off to bar 34."""
    bars = _regime_bars()
    above_lag = {"indicator": "close", "comparator": "greater_than", "value": "lag:period=3"}
    below_lag = {"indicator": "close", "comparator": "less_than", "value": "lag:period=3"}
    trending = {
        "indicator": "adx",
        "params": {"period": 5},
        "comparator": "greater_than",
        "value": 20,
    }

    def config(*entry: dict[str, Any]) -> StrategyConfig:
        rules = {
            "entry_long": {"operator": "AND", "conditions": list(entry)},
            "exit_long": {"operator": "AND", "conditions": [below_lag]},
        }
        return _config(parameters={"required_history": 10, "rules": rules})

    filtered = await _run(bars, config=config(trending, above_lag), warmup_bars=0)
    unfiltered = await _run(bars, config=config(above_lag), warmup_bars=0)

    assert [(t, side) for t, side, *_ in _fills(filtered)] == [
        (bars[34].timestamp.isoformat(), "Buy"),
        (bars[41].timestamp.isoformat(), "Sell"),
    ]
    assert _fills(unfiltered)[0][:2] == (bars[10].timestamp.isoformat(), "Buy")
