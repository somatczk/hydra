"""Unit pins for the research service: the walk-forward submit fast-fail predicate."""

from __future__ import annotations

from datetime import UTC, datetime

from hydra.backtest.jobs import WalkForwardRunRequest
from hydra.core.types import Timeframe
from hydra.services.research import _span_covers


def _wfo_body(
    start: str = "2024-01-01", end: str = "2024-12-31", **overrides: object
) -> WalkForwardRunRequest:
    """The review's repro request: 2 folds, train_pct 0.75."""
    body: dict[str, object] = {
        "strategy_id": "saw_d",
        "start_date": start,
        "end_date": end,
        "fold_count": 2,
        "train_pct": 0.75,
        "param_space": [{"name": "sma_period", "type": "int", "low": 4, "high": 5}],
    }
    body.update(overrides)
    return WalkForwardRunRequest.model_validate(body)


class TestSpanCovers:
    """A covered span may fast-fail at submit only when it is also dense: ``window_fits``
    sizes the windows from the stored ``bar_count``, so a sparse interior could still be
    filled in by a fetch and change that sizing — the worker owns that answer.
    """

    def test_sparse_interior_span_defers_even_when_the_ends_cover(self) -> None:
        # the review's repro: 10 stored daily bars spanning all of 2024 — the ends
        # cover the request, but the count misses 356 of the 366 grid slots
        stored = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC), 10)
        assert _span_covers(stored, _wfo_body(), Timeframe.D1) is False

    def test_dense_covered_span_can_fast_fail(self) -> None:
        # 31 daily bars over 31 grid slots: no fetch could change the sizing
        stored = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 31, tzinfo=UTC), 31)
        assert _span_covers(stored, _wfo_body(start="2024-01-01", end="2024-01-31"), Timeframe.D1)

    def test_a_span_short_of_the_request_never_covers(self) -> None:
        stored = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC), 200)
        assert _span_covers(stored, _wfo_body(), Timeframe.D1) is False
