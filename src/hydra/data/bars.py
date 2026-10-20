"""Bar-grid helpers and the closed-bar assembler for streamed candles.

Binance kline pushes carry the full state of the forming candle, and ccxt.pro drops the
exchange's "closed" flag. The only close signal left is a push for a later candle, so
``ClosedBarAssembler`` holds the newest candle and emits it once a later one arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hydra.core.types import OHLCV, Timeframe

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_WEEK_ANCHOR_S = 4 * 86400  # 1970-01-05, a Monday: Binance weekly candles open on Mondays


def timeframe_seconds(timeframe: Timeframe | str) -> int:
    """Length of one bar in seconds."""
    seconds = TIMEFRAME_SECONDS.get(str(timeframe))
    if seconds is None:
        msg = f"Unknown timeframe: {timeframe}"
        raise ValueError(msg)
    return seconds


def _anchor(timeframe: Timeframe | str) -> int:
    return _WEEK_ANCHOR_S if str(timeframe) == "1w" else 0


def floor_to_tf(dt: datetime, timeframe: Timeframe | str) -> datetime:
    """Open time of the bar that contains ``dt``."""
    tf_s = timeframe_seconds(timeframe)
    anchor = _anchor(timeframe)
    seconds = (dt.astimezone(UTC) - _EPOCH) // timedelta(seconds=1)
    return _EPOCH + timedelta(seconds=(seconds - anchor) // tf_s * tf_s + anchor)


def ceil_to_tf(dt: datetime, timeframe: Timeframe | str) -> datetime:
    """``dt`` when it is on the bar grid, else the next bar open after it."""
    floored = floor_to_tf(dt, timeframe)
    if floored == dt.astimezone(UTC):
        return floored
    return floored + timedelta(seconds=timeframe_seconds(timeframe))


def close_time(bar_open: datetime, timeframe: Timeframe | str) -> datetime:
    """Time at which the bar opened at ``bar_open`` closes."""
    return bar_open + timedelta(seconds=timeframe_seconds(timeframe))


def last_closed_open(now: datetime, timeframe: Timeframe | str) -> datetime:
    """Open time of the newest bar that is fully closed at ``now``."""
    return floor_to_tf(now, timeframe) - timedelta(seconds=timeframe_seconds(timeframe))


@dataclass(frozen=True, slots=True)
class AssemblerResult:
    """Output of one ``ClosedBarAssembler.feed`` call."""

    closed: list[OHLCV]
    latest_price: Decimal | None


class ClosedBarAssembler:
    """Turn streamed candle updates for one (symbol, timeframe) into closed bars, once each.

    - A row with the held candle's timestamp replaces it (each push is the full candle).
    - A row with a later timestamp closes the held candle, which is emitted, and is held.
    - A row at or before the last emitted bar is ignored (replay after a reconnect).
    """

    def __init__(self, timeframe: Timeframe, last_emitted: datetime | None = None) -> None:
        self.timeframe = timeframe
        self._held: OHLCV | None = None
        self._last_emitted = last_emitted

    @property
    def held(self) -> OHLCV | None:
        """The forming candle, not yet known to be closed."""
        return self._held

    @property
    def last_emitted(self) -> datetime | None:
        """Open time of the newest bar emitted (or seeded) as closed."""
        return self._last_emitted

    def seed(self, last_emitted: datetime) -> None:
        """Advance the closed-bar watermark (warm-up tail or REST gap fill)."""
        if self._last_emitted is None or last_emitted > self._last_emitted:
            self._last_emitted = last_emitted
        if self._held is not None and self._held.timestamp <= last_emitted:
            self._held = None

    def reset_after_reconnect(self) -> None:
        """Drop the held candle: updates may have been missed, so it may not be final."""
        self._held = None

    def feed(self, rows: list[OHLCV]) -> AssemblerResult:
        """Consume the candle rows of one watch return (oldest first)."""
        closed: list[OHLCV] = []
        latest: Decimal | None = None
        for row in rows:
            if self._last_emitted is not None and row.timestamp <= self._last_emitted:
                continue
            held = self._held
            if held is None or row.timestamp == held.timestamp:
                self._held = row
            elif row.timestamp > held.timestamp:
                closed.append(held)
                self._last_emitted = held.timestamp
                self._held = row
            else:
                continue
            latest = row.close
        return AssemblerResult(closed=closed, latest_price=latest)
