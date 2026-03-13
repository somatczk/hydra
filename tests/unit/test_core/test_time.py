"""Tests for hydra.core.time — UTCClock and BacktestClock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hydra.core.time import BacktestClock, UTCClock


# ---------------------------------------------------------------------------
# UTCClock
# ---------------------------------------------------------------------------


class TestUTCClock:
    def test_returns_utc(self) -> None:
        clock = UTCClock()
        now = clock.now()
        assert now.tzinfo == timezone.utc

    def test_is_not_backtest(self) -> None:
        clock = UTCClock()
        assert clock.is_backtest is False

    def test_time_advances(self) -> None:
        clock = UTCClock()
        t1 = clock.now()
        t2 = clock.now()
        assert t2 >= t1

    def test_close_to_real_time(self) -> None:
        clock = UTCClock()
        now = clock.now()
        real_now = datetime.now(timezone.utc)
        delta = abs((real_now - now).total_seconds())
        assert delta < 1.0  # within 1 second


# ---------------------------------------------------------------------------
# BacktestClock
# ---------------------------------------------------------------------------


class TestBacktestClock:
    def test_default_start(self) -> None:
        clock = BacktestClock()
        assert clock.now() == datetime(2020, 1, 1, tzinfo=timezone.utc)

    def test_custom_start(self) -> None:
        start = datetime(2023, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)
        assert clock.now() == start

    def test_naive_start_gets_utc(self) -> None:
        clock = BacktestClock(start=datetime(2023, 1, 1))
        assert clock.now().tzinfo == timezone.utc

    def test_is_backtest(self) -> None:
        clock = BacktestClock()
        assert clock.is_backtest is True

    def test_advance_to(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)

        t2 = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        clock.advance_to(t2)
        assert clock.now() == t2

    def test_advance_to_same_time(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)
        clock.advance_to(start)  # same time — should be fine
        assert clock.now() == start

    def test_advance_backwards_raises(self) -> None:
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)
        past = datetime(2024, 5, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="Cannot move clock backwards"):
            clock.advance_to(past)

    def test_advance_naive_timestamp_gets_utc(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)
        clock.advance_to(datetime(2024, 1, 2))
        assert clock.now().tzinfo == timezone.utc

    def test_incremental_advances(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)

        times = [start + timedelta(hours=i) for i in range(1, 5)]
        for t in times:
            clock.advance_to(t)
            assert clock.now() == t

    def test_time_does_not_advance_without_call(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(start=start)
        assert clock.now() == start
        assert clock.now() == start  # still the same
