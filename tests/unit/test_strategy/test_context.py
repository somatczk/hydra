"""StrategyContext: one bar per timestamp, optional bound, cache invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.context import StrategyContext

_T0 = datetime(2026, 5, 4, tzinfo=UTC)
_TF = Timeframe.H1


def _bar(i: int, close: str) -> OHLCV:
    c = Decimal(close)
    return OHLCV(
        open=c, high=c, low=c, close=c, volume=Decimal(1), timestamp=_T0 + timedelta(hours=i)
    )


def test_same_timestamp_replaces_and_recomputes_indicator() -> None:
    ctx = StrategyContext()
    for i, close in enumerate(["10", "20", "30"]):
        ctx.add_bar("BTCUSDT", _TF, _bar(i, close))
    assert ctx.indicator("sma", "BTCUSDT", _TF, period=3)[-1] == pytest.approx(20.0)

    ctx.add_bar("BTCUSDT", _TF, _bar(2, "60"))

    bars = ctx.bars("BTCUSDT", _TF, 10)
    assert len(bars) == 3
    assert bars[-1].close == Decimal("60")
    assert ctx.indicator("sma", "BTCUSDT", _TF, period=3)[-1] == pytest.approx(30.0)


def test_older_timestamp_is_ignored() -> None:
    ctx = StrategyContext()
    ctx.add_bar("BTCUSDT", _TF, _bar(0, "10"))
    ctx.add_bar("BTCUSDT", _TF, _bar(1, "11"))
    ctx.add_bar("BTCUSDT", _TF, _bar(0, "99"))
    assert [b.close for b in ctx.bars("BTCUSDT", _TF, 10)] == [Decimal("10"), Decimal("11")]


def test_max_bars_keeps_the_newest() -> None:
    ctx = StrategyContext(max_bars=3)
    for i in range(5):
        ctx.add_bar("BTCUSDT", _TF, _bar(i, str(100 + i)))
    stamps = [b.timestamp for b in ctx.bars("BTCUSDT", _TF, 10)]
    assert stamps == [_T0 + timedelta(hours=h) for h in (2, 3, 4)]
    assert ctx.max_bars == 3
    sma = ctx.indicator("sma", "BTCUSDT", _TF, period=3)
    assert len(sma) == 3
    assert sma[-1] == pytest.approx(103.0)


def test_default_is_unbounded() -> None:
    ctx = StrategyContext()
    for i in range(5):
        ctx.add_bar("BTCUSDT", _TF, _bar(i, "1"))
    assert len(ctx.bars("BTCUSDT", _TF, 100)) == 5
    assert ctx.max_bars is None


def test_max_bars_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_bars"):
        StrategyContext(max_bars=0)


def test_timeframes_are_independent() -> None:
    ctx = StrategyContext()
    ctx.add_bar("BTCUSDT", Timeframe.H1, _bar(0, "1"))
    ctx.add_bar("BTCUSDT", Timeframe.H4, _bar(0, "2"))
    assert np.isnan(ctx.indicator("sma", "BTCUSDT", Timeframe.H4, period=2)[-1])
    assert ctx.latest_bar("BTCUSDT", Timeframe.H1) == _bar(0, "1")
    assert ctx.latest_bar("BTCUSDT", Timeframe.H4) == _bar(0, "2")
