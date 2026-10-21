"""StrategyContext: one bar per timestamp, optional bound, cache invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
import talib

from hydra.core.types import OHLCV, Timeframe
from hydra.indicators import library as ind_lib
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


def test_older_timestamp_replaces_or_inserts_in_order() -> None:
    ctx = StrategyContext()
    for i, close in ((0, "10"), (1, "11"), (3, "13")):
        ctx.add_bar("BTCUSDT", _TF, _bar(i, close))
    assert ctx.indicator("sma", "BTCUSDT", _TF, period=3)[-1] == pytest.approx(34 / 3)

    ctx.add_bar("BTCUSDT", _TF, _bar(0, "99"))
    ctx.add_bar("BTCUSDT", _TF, _bar(2, "12"))

    bars = ctx.bars("BTCUSDT", _TF, 10)
    assert [b.timestamp for b in bars] == [_T0 + timedelta(hours=h) for h in range(4)]
    assert [b.close for b in bars] == [Decimal(c) for c in ("99", "11", "12", "13")]
    assert ctx.indicator("sma", "BTCUSDT", _TF, period=3)[-1] == pytest.approx(12.0)


def test_bar_older_than_a_full_window_is_ignored() -> None:
    ctx = StrategyContext(max_bars=3)
    for i in range(2, 5):
        ctx.add_bar("BTCUSDT", _TF, _bar(i, str(100 + i)))
    ctx.add_bar("BTCUSDT", _TF, _bar(0, "1"))
    ctx.add_bar("BTCUSDT", _TF, _bar(3, "7"))
    assert [b.close for b in ctx.bars("BTCUSDT", _TF, 10)] == [
        Decimal("102"),
        Decimal("7"),
        Decimal("104"),
    ]


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


def _walk_ctx(n: int) -> tuple[StrategyContext, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(4)
    close = 30000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high, low = close * 1.004, close * 0.996
    volume = rng.uniform(1, 100, n)
    ctx = StrategyContext()
    for i in range(n):
        ctx.add_bar(
            "BTCUSDT",
            _TF,
            OHLCV(
                open=Decimal(str(close[i])),
                high=Decimal(str(high[i])),
                low=Decimal(str(low[i])),
                close=Decimal(str(close[i])),
                volume=Decimal(str(volume[i])),
                timestamp=_T0 + timedelta(hours=i),
            ),
        )
    return ctx, high, low, close, volume


def test_named_outputs_reachable() -> None:
    ctx, high, low, close, _ = _walk_ctx(600)
    hist = ctx.indicator("macd_hist", "BTCUSDT", _TF, fast=12, slow=26, signal=9)
    assert np.array_equal(hist, talib.MACD(close, 12, 26, 9)[2], equal_nan=True)
    direction = ctx.indicator("supertrend_direction", "BTCUSDT", _TF, period=10, multiplier=3.0)
    assert np.array_equal(
        direction, ind_lib.supertrend(high, low, close, 10, 3.0)[1], equal_nan=True
    )


def test_dispatch_follows_the_signature() -> None:
    ctx, high, low, close, volume = _walk_ctx(60)
    pairs = [
        (
            ctx.indicator("mfi", "BTCUSDT", _TF, period=14),
            ind_lib.mfi(high, low, close, volume, 14),
        ),
        (ctx.indicator("obv", "BTCUSDT", _TF), ind_lib.obv(close, volume)),
        (
            ctx.indicator("williams_r", "BTCUSDT", _TF, period=14),
            ind_lib.williams_r(high, low, close, 14),
        ),
        (ctx.indicator("atr", "BTCUSDT", _TF, period=14), talib.ATR(high, low, close, 14)),
    ]
    for got, want in pairs:
        assert np.array_equal(got, want, equal_nan=True)


@pytest.mark.parametrize(
    ("name", "params", "match"),
    [
        ("bollinger_bands", {"period": 20}, "bollinger_lower"),
        ("keltner_channels", {}, "keltner_upper"),
        ("ichimoku", {}, "future bars"),
        ("rsii", {"period": 14}, "Unknown indicator"),
        ("np", {}, "Unknown indicator"),
        ("_DATA", {}, "Unknown indicator"),
        ("rsi", {"period": "abc"}, "must be a number"),
        ("rsi", {"period": True}, "must be a number"),
        ("rsi", {"periodd": 14}, "periodd"),
        ("sma", {}, "period"),
    ],
)
def test_ambiguous_and_invalid_rejected(name: str, params: dict[str, object], match: str) -> None:
    ctx, *_ = _walk_ctx(5)
    with pytest.raises(ValueError, match=match):
        ctx.indicator(name, "BTCUSDT", _TF, **params)
