"""Execution parity: a real paper session and BacktestRunner on the same bars agree exactly.

The paper side is the real SessionManager (no DB) with its RiskService and OrderManager on bar time,
the fake REST client and fed closed bars on its session bus exactly as the feed publishes them.
The backtest side is ``BacktestRunner.run`` on the same bars with the same risk config. Fills,
bar-by-bar equity, balances and realized PnL must be identical, and the fixture must hit a
take-profit, a gap-through stop, a bar touching both OCO legs, a partial and a full strategy exit.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

import hydra.backtest.runner as runner_module
import hydra.strategy.engine as engine_module
from hydra.backtest.fills import CommissionConfig
from hydra.backtest.runner import BacktestRunner
from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType, Symbol, Timeframe
from hydra.data.bars import last_closed_open
from hydra.execution.client_factory import ClientFactory
from hydra.execution.paper_trading import PaperFill, PaperTradingExecutor
from hydra.execution.session_manager import SessionManager
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.pretrade import RiskConfig
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from tests.fixtures.binance_kline import FakeRestExchange, FakeWsExchange, binance_markets

D = Decimal
H = timedelta(hours=1)
WARM = 30
TRADED = 270
CAPITAL = D("10000")
SLIP = D("0.001")
# the runner's breaker policy (decision 2), applied to the paper side too
RISK = replace(RiskConfig(), max_drawdown_pct=runner_module._NO_DRAWDOWN_TIERS)
CONFIG: dict[str, Any] = {
    "id": "parity",
    "name": "parity",
    "strategy_class": f"{__name__}.ParityStrategy",
    "symbols": ["BTCUSDT"],
    "timeframes": {"primary": "1h"},
    "position_sizing": {"max_position_pct": 10.0},
    "risk": {
        "stop_loss_method": "fixed_pct",
        "stop_loss_value": 2.0,
        "take_profit_method": "fixed_pct",
        "take_profit_value": 3.0,
    },
}


class ParityStrategy(BaseStrategy):
    """Enters while the close rises (level-triggered), sells half at +1.2 %, all below -0.8 %."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._halved = False

    @property
    def required_history(self) -> int:
        return 20

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        last, prev = self.context.bars(symbol, bar.timeframe, 2)[::-1]
        position = self.context.position(symbol)
        if position is None:
            self._halved = False
            if last.close <= prev.close:
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
        if not self._halved and last.close >= position.avg_entry_price * D("1.012"):
            self._halved = True
            return [self._exit(bar, 0.5)]
        if last.close < position.avg_entry_price * D("0.992"):
            return [self._exit(bar, 1.0)]
        return []

    def _exit(self, bar: BarEvent, close_pct: float) -> ExitSignal:
        return ExitSignal(
            symbol=bar.symbol,
            strategy_id=self.strategy_id,
            exchange_id="binance",
            close_pct=close_pct,
        )


class FlipStrategy(BaseStrategy):
    """Buys when flat and sells everything when holding: same-size orders two bars apart."""

    @property
    def required_history(self) -> int:
        return 20

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if self.context.position(str(bar.symbol)) is not None:
            return [
                ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id, exchange_id="binance")
            ]
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


def _flat_bars(end: datetime, count: int) -> list[OHLCV]:
    p = D("99950")
    start = end - (WARM - 1) * H
    return [OHLCV(p, p, p, p, D("100"), start + i * H) for i in range(count)]


def _bars(end: datetime) -> list[OHLCV]:
    """Random walk with a 4 % gap down every 23rd bar and a wide range every 31st."""
    rng = np.random.default_rng(7)
    start = end - (WARM - 1) * H
    out: list[OHLCV] = []
    close = 100.0
    for i in range(WARM + TRADED):
        open_ = close * (0.96 if i % 23 == 22 else 1.0)
        close = open_ * (1 + rng.normal(0, 0.008))
        wick = 0.045 if i % 31 == 30 else abs(rng.normal(0, 0.004))
        o, c = round(open_, 2), round(close, 2)
        out.append(
            OHLCV(
                open=D(str(o)),
                high=D(str(round(max(o, c) * (1 + wick), 2))),
                low=D(str(round(min(o, c) * (1 - wick), 2))),
                close=D(str(c)),
                volume=D("100"),
                timestamp=start + i * H,
            )
        )
        close = c
    return out


async def _markets(exchange_id: str) -> list[dict[str, Any]]:
    return list(binance_markets().values())


def _key(f: PaperFill) -> tuple[object, ...]:
    return (
        f.timestamp,
        f.side,
        f.order_type,
        f.quantity,
        f.price,
        f.fee,
        f.reason,
        f.gross_pnl,
        f.net_pnl,
        f.status,
        f.mark_source,
    )


async def _paper(
    bars: list[OHLCV], strategies_dir: Path, config: dict[str, Any]
) -> tuple[PaperTradingExecutor, list[D]]:
    (strategies_dir / "parity.yaml").write_text(yaml.safe_dump(config))
    rows = [
        [
            int(b.timestamp.timestamp() * 1000),
            *(float(x) for x in (b.open, b.high, b.low, b.close, b.volume)),
        ]
        for b in bars[:WARM]
    ]
    now = [bars[WARM].timestamp]
    risk = RiskService(
        InMemoryRiskStateStore(paper_capital=CAPITAL, config=RISK),
        market_rules=MarketRulesCache(loader=_markets),
        clock=lambda: now[0],
    )
    mgr = SessionManager(
        db_pool=None,
        config=HydraConfig(),
        clients=ClientFactory(
            ws_factory=lambda _eid: FakeWsExchange,
            rest_factory=lambda _eid: FakeRestExchange(rows),
            markets_loader=_markets,
            repository_factory=lambda _pool: None,
        ),
        risk=risk,
    )
    await mgr.startup()
    sid = await mgr.start_session("parity", "paper", CAPITAL)
    session = mgr.get_session(sid)
    assert session is not None and session._event_bus is not None
    assert session._runtime is not None
    session._runtime.om._clock = lambda: now[0].timestamp()  # the runner's bar clock
    warm = session._runtime.context.bars("BTCUSDT", Timeframe.H1, 1000)
    assert warm == bars[:WARM]
    executor = session._executor
    assert isinstance(executor, PaperTradingExecutor)

    equity: list[D] = []
    for bar in bars[WARM:]:
        now[0] = bar.timestamp + H
        await session._event_bus.publish(
            BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=bar)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        equity.append(await executor.get_equity())
    await mgr.stop_session(sid)
    equity[-1] = await executor.get_equity()
    await mgr.graceful_shutdown()
    return executor, equity


async def _assert_parity(
    bars: list[OHLCV],
    cls: type[BaseStrategy],
    strategies_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[PaperFill]:
    # P2-E2's class allowlist refuses anything outside hydra.strategy.builtin.*; this fixture's
    # test-double strategies are the one legitimate exception (real parity harness, not user input).
    monkeypatch.setattr(
        engine_module, "ALLOWED_CLASS_PREFIX", (engine_module.ALLOWED_CLASS_PREFIX, __name__)
    )
    config = {**CONFIG, "strategy_class": f"{__name__}.{cls.__name__}"}
    paper, paper_equity = await _paper(bars, strategies_dir, config)

    built: list[PaperTradingExecutor] = []

    class Recording(PaperTradingExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(runner_module, "PaperTradingExecutor", Recording)
    result = await BacktestRunner().run(
        cls,
        StrategyConfig(**config),
        bars,
        initial_capital=CAPITAL,
        commission=CommissionConfig(),
        warmup_bars=WARM,
        slippage_pct=SLIP,
        risk_config=RiskConfig(),
    )
    [backtest] = built

    paper_fills = [_key(f) for f in paper.get_fills()]
    assert [_key(f) for f in backtest.get_fills()] == paper_fills
    assert result.equity_curve == paper_equity
    p, b = paper.get_account_snapshot(), backtest.get_account_snapshot()
    assert (b.quote_free, b.quote_locked, b.base_totals) == (
        p.quote_free,
        p.quote_locked,
        p.base_totals,
    )
    assert b.realized_net == p.realized_net
    assert CAPITAL + sum((t.pnl for t in result.trades), D("0")) == result.equity_curve[-1]
    return paper.get_fills()


async def test_backtest_equals_paper_session_on_the_same_bars(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _bars(last_closed_open(datetime.now(UTC), Timeframe.H1))
    fills = await _assert_parity(bars, ParityStrategy, strategies_dir, monkeypatch)
    _assert_fixture_hits_every_case(fills, {b.timestamp: b for b in bars})


async def test_same_size_reentries_are_not_deduplicated_on_either_side(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _flat_bars(last_closed_open(datetime.now(UTC), Timeframe.H1), WARM + 12)
    fills = await _assert_parity(bars, FlipStrategy, strategies_dir, monkeypatch)
    # the fifth losing round trip trips the loss-streak pause
    assert [f.quantity for f in fills if f.side == "BUY"] == [
        D("0.01000"),
        D("0.01000"),
        D("0.00999"),
        D("0.00999"),
        D("0.00998"),
    ]


def _assert_fixture_hits_every_case(fills: list[PaperFill], by_ts: dict[datetime, OHLCV]) -> None:
    entry = D("0")
    held = D("0")
    seen: set[str] = set()
    for f in fills:
        bar = by_ts[f.timestamp]
        if f.side == "BUY":
            entry, held = f.price, held + f.quantity
            continue
        stop, take = entry * D("0.98"), entry * D("1.03")
        if f.reason == "take_profit":
            seen.add("take_profit")
        if f.reason == "stop_loss" and bar.open < stop and f.price == bar.open * (1 - SLIP):
            seen.add("gap_stop")
        if f.reason in ("take_profit", "stop_loss") and bar.high >= take and bar.low <= stop:
            seen.add("both_legs")
        if f.reason == "market" and f.quantity < held:
            seen.add("partial_exit")
        if f.reason == "market" and f.quantity == held and f.timestamp != fills[-1].timestamp:
            seen.add("strategy_exit")
        held -= f.quantity
    assert seen == {"take_profit", "gap_stop", "both_legs", "partial_exit", "strategy_exit"}
