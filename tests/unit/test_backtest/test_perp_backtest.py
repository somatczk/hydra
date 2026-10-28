"""Perp backtests: the parity guard extended and the direction/funding story.

The P2-A parity pattern extended to FUTURES (P4-C): the SAME PerpPaperTradingExecutor
module serves the paper session and the backtest, so on identical bars, funding rows and
fees the fills, bar-by-bar equity and account totals must be identical — longs, shorts,
protective pairs and funding accruals included. A standalone run then pins the short
round trip's direction record and funding's effect on the result.
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

import hydra.strategy.engine as engine_module
from hydra.backtest.fills import CommissionConfig
from hydra.backtest.runner import BacktestRunner
from hydra.core.config import HydraConfig
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, MarketType, Symbol, Timeframe
from hydra.data.bars import last_closed_open
from hydra.execution.client_factory import ClientFactory, SharedRestClients
from hydra.execution.perp_trading import PerpPaperTradingExecutor, static_funding_reader
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
RISK = replace(RiskConfig(), max_drawdown_pct=D("1000"))
LEVERAGE = 2
CONFIG: dict[str, Any] = {
    "id": "perp-parity",
    "name": "perp-parity",
    "symbols": ["BTCUSDT"],
    "exchange": {"exchange_id": "binanceusdm", "market_type": "FUTURES", "leverage": LEVERAGE},
    "timeframes": {"primary": "1h"},
    "position_sizing": {"max_position_pct": 10.0},
    "risk": {
        "stop_loss_method": "fixed_pct",
        "stop_loss_value": 2.0,
        "take_profit_method": "fixed_pct",
        "take_profit_value": 3.0,
    },
}


class PerpParityStrategy(BaseStrategy):
    """Longs a rise, shorts a fall, exits fully on the adverse threshold."""

    @property
    def required_history(self) -> int:
        return 20

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        last, prev = self.context.bars(symbol, bar.timeframe, 2)[::-1]
        position = self.context.position(symbol)
        if position is None:
            if last.close > prev.close:
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
            if last.close < prev.close:
                return [
                    EntrySignal(
                        symbol=bar.symbol,
                        direction=Direction.SHORT,
                        strength=D("1"),
                        strategy_id=self.strategy_id,
                        exchange_id="binanceusdm",
                        market_type=MarketType.FUTURES,
                    )
                ]
            return []
        threshold = D("1.012") if position.direction == Direction.LONG else D("0.988")
        hit = (
            last.close >= position.avg_entry_price * threshold
            if position.direction == Direction.LONG
            else last.close <= position.avg_entry_price * threshold
        )
        if hit:
            return [self._exit(bar)]
        return []

    def _exit(self, bar: BarEvent) -> ExitSignal:
        return ExitSignal(
            symbol=bar.symbol, strategy_id=self.strategy_id, exchange_id="binanceusdm"
        )


def _bars(end: datetime) -> list[OHLCV]:
    """Random walk with a 4 % gap every 23rd bar and a wide range every 31st."""
    rng = np.random.default_rng(11)
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


def _funding_rows(bars: list[OHLCV], rate: str = "0.0001") -> dict[str, list[tuple[datetime, D]]]:
    """A settlement every 8 hours across the traded range (03/11/19 UTC cadence)."""
    first, last = bars[0].timestamp, bars[-1].timestamp
    rows: list[tuple[datetime, D]] = []
    probe = first.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    while probe <= last:
        rows.append((probe, D(rate)))
        probe += timedelta(hours=8)
    return {"BTCUSDT": rows}


async def _markets(exchange_id: str) -> list[dict[str, Any]]:
    return list(binance_markets().values())


def _key(f: Any) -> tuple[object, ...]:
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


def _perp_clients(
    warm_rows: list[list[Any]], funding: dict[str, list[tuple[datetime, D]]]
) -> ClientFactory:
    return ClientFactory(
        ws_factory=lambda _eid: FakeWsExchange,
        rest_pool=SharedRestClients(rest_factory=lambda eid, creds: FakeRestExchange(warm_rows)),
        markets_loader=_markets,
        repository_factory=lambda _pool: None,
        funding_reader_factory=lambda _pool, _eid: static_funding_reader(funding),
    )


async def _paper_session(
    bars: list[OHLCV], strategies_dir: Path, config: dict[str, Any]
) -> tuple[PerpPaperTradingExecutor, list[D]]:
    (strategies_dir / "perp-parity.yaml").write_text(yaml.safe_dump(config))
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
    clients = _perp_clients(rows, _funding_rows(bars))
    mgr = SessionManager(
        db_pool=None,
        config=HydraConfig(),
        clients=clients,
        risk=risk,
    )
    await mgr.startup()
    sid = await mgr.start_session("perp-parity", "paper", CAPITAL)
    session = mgr.get_session(sid)
    assert session is not None and session._event_bus is not None and session._runtime
    session._runtime.om._clock = lambda: now[0].timestamp()  # the runner's bar clock
    executor = session._executor
    assert isinstance(executor, PerpPaperTradingExecutor)

    equity: list[D] = []
    for bar in bars[WARM:]:
        now[0] = bar.timestamp + H
        await session._event_bus.publish(
            BarEvent(
                symbol=Symbol("BTCUSDT"),
                timeframe=Timeframe.H1,
                ohlcv=bar,
                exchange_id="binanceusdm",
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        equity.append(await executor.get_equity())
    await mgr.stop_session(sid)
    equity[-1] = await executor.get_equity()
    await mgr.graceful_shutdown()
    return executor, equity


async def _assert_perp_parity(
    bars: list[OHLCV],
    cls: type[BaseStrategy],
    strategies_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PerpPaperTradingExecutor, BacktestRunner, Any]:
    monkeypatch.setattr(
        engine_module, "ALLOWED_CLASS_PREFIX", (engine_module.ALLOWED_CLASS_PREFIX, __name__)
    )
    config = {**CONFIG, "strategy_class": f"{__name__}.{cls.__name__}"}
    paper, paper_equity = await _paper_session(bars, strategies_dir, config)
    funding = _funding_rows(bars)
    assert result_fills(paper)  # the fixture actually trades

    built: list[PerpPaperTradingExecutor] = []
    original_init = PerpPaperTradingExecutor.__init__

    def recording(self: PerpPaperTradingExecutor, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(PerpPaperTradingExecutor, "__init__", recording)
    result = await BacktestRunner().run(
        cls,
        StrategyConfig(**config),
        bars,
        initial_capital=CAPITAL,
        commission=CommissionConfig(),
        warmup_bars=WARM,
        slippage_pct=SLIP,
        risk_config=RiskConfig(),
        funding=funding,
    )

    [backtest] = built
    paper_fills = [_key(f) for f in paper.get_fills()]
    assert [_key(f) for f in backtest.get_fills()] == paper_fills
    assert result.equity_curve == paper_equity
    p, b = paper.get_account_snapshot(), backtest.get_account_snapshot()
    assert (b.wallet_available, b.margin_used, b.base_totals) == (
        p.wallet_available,
        p.margin_used,
        p.base_totals,
    )
    assert b.realized_net == p.realized_net
    assert b.funding_applied == p.funding_applied and b.funding_applied > 0
    assert (
        CAPITAL + sum((t.pnl for t in result.trades), D("0")) + _funding_total(backtest)
        == result.equity_curve[-1]
    )
    return paper, BacktestRunner(), result


def result_fills(paper: PerpPaperTradingExecutor) -> list[Any]:
    return [f for f in paper.get_fills() if f.side != "FUNDING"]


def _funding_total(ex: PerpPaperTradingExecutor) -> D:
    return sum((f.net_pnl for f in ex.get_fills() if f.side == "FUNDING"), D("0"))


async def test_perp_paper_equals_perp_backtest_on_the_same_bars(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _bars(last_closed_open(datetime.now(UTC), Timeframe.H1))
    paper, _, result = await _assert_perp_parity(
        bars, PerpParityStrategy, strategies_dir, monkeypatch
    )
    fills = result_fills(paper)
    sides = {f.side for f in fills}
    assert sides == {"BUY", "SELL"}  # longs and shorts both traded
    directions = {t.direction for t in result.trades}
    assert directions == {"LONG", "SHORT"}
    reasons = {f.reason for f in fills}
    assert "take_profit" in reasons or "stop_loss" in reasons
    assert paper.get_account_snapshot().leverage == LEVERAGE


async def test_a_perp_backtest_records_short_trades_and_funding(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _bars(last_closed_open(datetime.now(UTC), Timeframe.H1))
    _, _, result = await _assert_perp_parity(bars, PerpParityStrategy, strategies_dir, monkeypatch)
    shorts = [t for t in result.trades if t.direction == "SHORT"]
    assert shorts
    for trade in result.trades:
        # the recorded direction matches the PnL's sign: a short gains when it exits
        # below its entry, a long when it exits above
        move = (trade.entry_price - trade.exit_price) * trade.quantity
        if trade.direction == "LONG":
            move = (trade.exit_price - trade.entry_price) * trade.quantity
        assert (move > 0) == (trade.pnl > 0)
    assert result.total_trades == len(result.trades) > 0
