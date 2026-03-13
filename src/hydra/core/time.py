"""Clock implementations for live trading and backtesting.

Both ``UTCClock`` and ``BacktestClock`` satisfy the ``Clock`` protocol
defined in ``hydra.core.protocols``.
"""

from __future__ import annotations

from datetime import UTC, datetime


class UTCClock:
    """Real-time UTC clock for live / paper trading."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)

    @property
    def is_backtest(self) -> bool:
        return False


class BacktestClock:
    """Simulated clock for deterministic backtesting.

    Time only advances when ``advance_to`` is called, guaranteeing
    reproducible event ordering.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is not None and start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._current: datetime = start or datetime(2020, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        """Return the simulated current time."""
        return self._current

    def advance_to(self, timestamp: datetime) -> None:
        """Advance the clock to *timestamp*.

        Raises ``ValueError`` if *timestamp* is in the past relative to
        the current simulated time.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp < self._current:
            msg = (
                f"Cannot move clock backwards: {timestamp.isoformat()} "
                f"< {self._current.isoformat()}"
            )
            raise ValueError(msg)
        self._current = timestamp

    @property
    def is_backtest(self) -> bool:
        return True
