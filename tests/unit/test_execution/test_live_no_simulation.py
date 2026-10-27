"""The no-simulation pin: a live session never fills locally.

``on_closed_bar`` does the full inherited bookkeeping (marks, peak, persist) but never
evaluates a resting order, and no ``create_order`` ever answers a local fill. The paper/backtest
parity guard itself is ``test_execution_parity.py``, untouched by P3-A.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hydra.core.types import OHLCV
from hydra.execution.live_executor import LiveTradingExecutor
from hydra.risk.market_rules import MarketRules
from tests.fixtures.binance_trading import FakeBinanceTradingClient

D = Decimal
H = timedelta(hours=1)
BASE_TS = datetime(2026, 10, 27, tzinfo=UTC)

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)


def _executor(fake: FakeBinanceTradingClient) -> LiveTradingExecutor:
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": D("10000")},
        db_pool=None,
        strategy_id="strat",
        session_id="no-sim",
        market_rules={"BTCUSDT": RULES},
    )
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


def _bar(close: str, ts: datetime) -> OHLCV:
    c = D(close)
    return OHLCV(open=c, high=c, low=c, close=c, volume=D("1"), timestamp=ts)


async def test_on_closed_bar_never_fills_and_never_mutates_balances() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    # a resting LIMIT BUY that the paper executor would fill on this bar
    await ex.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), price=D("90000"))
    before = ex.get_account_snapshot()

    fills = await ex.on_closed_bar("BTCUSDT", _bar("100000", BASE_TS))

    assert fills == []
    after = ex.get_account_snapshot()
    assert (after.quote_free, after.quote_locked) == (before.quote_free, before.quote_locked)
    assert after.base_totals == before.base_totals
    # the bookkeeping still ran: the mark, the peak and the last-bar stamp
    assert after.marks["BTCUSDT"] == D("100000")
    assert after.peak_equity == before.peak_equity
    assert after.last_bar_ts["BTCUSDT"] == BASE_TS


async def test_a_market_create_order_never_answers_a_local_fill() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)

    resp = await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))

    # the exchange filled instantly; the executor still answers the resting-ack shape
    assert (resp["status"], resp["filled"]) == ("PENDING", False)
    assert ex.get_fills() == []
    assert ex.get_account_snapshot().base_totals == {}
