"""SM-16: the Python builtins decide on indicators over the whole context, not a short window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import talib

from hydra.core.events import BarEvent, EntrySignal
from hydra.core.types import OHLCV, Direction, Symbol, Timeframe
from hydra.indicators.library import supertrend_direction
from hydra.strategy.base import BaseStrategy
from hydra.strategy.builtin.momentum import MomentumRSIMACDStrategy
from hydra.strategy.builtin.trend_following import TrendFollowingSupertrend
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

_TF = Timeframe.H1


def _walk(seed: int, n: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 30000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


def _cfg(cls: type[BaseStrategy], parameters: dict[str, object]) -> StrategyConfig:
    return StrategyConfig(
        id="full-ctx",
        name="Full context",
        strategy_class=f"{cls.__module__}.{cls.__name__}",
        parameters=parameters,
    )


async def _entries(
    cls: type[BaseStrategy], cfg: StrategyConfig, closes: np.ndarray
) -> set[tuple[int, str]]:
    ctx = StrategyContext()
    strategy = cls(config=cfg, context=ctx)
    out: set[tuple[int, str]] = set()
    for t, c in enumerate(closes):
        ohlcv = OHLCV(
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=Decimal(1),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=t),
        )
        ctx.add_bar("BTCUSDT", _TF, ohlcv)
        for s in await strategy.on_bar(
            BarEvent(symbol=Symbol("BTCUSDT"), timeframe=_TF, ohlcv=ohlcv)
        ):
            if isinstance(s, EntrySignal):
                out.add((t, "L" if s.direction == Direction.LONG else "S"))
    return out


def _momentum_reference(closes: np.ndarray, window: int | None) -> set[tuple[int, str]]:
    out: set[tuple[int, str]] = set()
    for t in range(49, len(closes)):
        c = closes[: t + 1] if window is None else closes[t + 1 - window : t + 1]
        rsi = talib.RSI(c, 14)[-1]
        hist = talib.MACD(c, 12, 26, 9)[2]
        if np.isnan(rsi) or np.isnan(hist[-2]):
            continue
        if rsi < 50 and hist[-2] <= 0 < hist[-1]:
            out.add((t, "L"))
        if rsi > 50 and hist[-2] >= 0 > hist[-1]:
            out.add((t, "S"))
    return out


def _trend_reference(closes: np.ndarray, window: int | None) -> set[tuple[int, str]]:
    out: set[tuple[int, str]] = set()
    for t in range(209, len(closes)):
        c = closes[: t + 1] if window is None else closes[t + 1 - window : t + 1]
        d = supertrend_direction(c * 1.01, c * 0.99, c, 10, 3.0)
        fast, slow = talib.EMA(c, 50)[-1], talib.EMA(c, 200)[-1]
        if np.isnan(d[-2]) or np.isnan(slow):
            continue
        if d[-1] == 1.0 and d[-2] != 1.0 and fast > slow:
            out.add((t, "L"))
        if d[-1] == -1.0 and d[-2] == 1.0 and fast < slow:
            out.add((t, "S"))
    return out


async def test_momentum_decides_on_the_full_context() -> None:
    closes = _walk(7, 600)
    full = _momentum_reference(closes, None)
    assert full != _momentum_reference(closes, 50)
    cfg = _cfg(
        MomentumRSIMACDStrategy,
        {"required_history": 50, "rsi_oversold": 50, "rsi_overbought": 50},
    )
    assert await _entries(MomentumRSIMACDStrategy, cfg, closes) == full


async def test_trend_following_decides_on_the_full_context() -> None:
    closes = _walk(9, 600)
    full = _trend_reference(closes, None)
    assert full != _trend_reference(closes, 210)
    cfg = _cfg(TrendFollowingSupertrend, {"required_history": 210})
    assert await _entries(TrendFollowingSupertrend, cfg, closes) == full
