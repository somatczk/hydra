"""Unit pins for the research service: the walk-forward submit fast-fail predicate and 422."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hydra.backtest.jobs import Preflight, WalkForwardRunRequest
from hydra.core.types import Timeframe
from hydra.services import research
from hydra.services.errors import ServiceError
from hydra.services.research import _span_covers
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.config import StrategyConfig


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
    """A covered span may fast-fail at submit only when it is also dense: ``fold_windows``
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


_SMA_RULES = {
    "entry_long": {
        "operator": "AND",
        "conditions": [
            {
                "indicator": "close",
                "params": {},
                "comparator": "crosses_above",
                "value": "sma:period=5",
                "value_ref_overrides": {"sma_period": "period"},
            }
        ],
    }
}


class TestSubmitRangeTooShort:
    """The submit-time 422 on a covered, dense span uses the worker's own window sizing."""

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, days: int) -> list[str]:
        first = datetime(2024, 1, 1, tzinfo=UTC)
        stored = (first, first + timedelta(days=days), days + 1)
        config = StrategyConfig(
            id="saw_d",
            name="Saw",
            strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
            symbols=["BTCUSDT"],
            timeframes={"primary": "1d"},
            parameters={"rules": _SMA_RULES, "sma_period": 5},
        )
        queued: list[str] = []

        async def preflight(*_args: object, **_kwargs: object) -> Preflight:
            return Preflight(
                RuleBasedStrategy, config, "binance", "BTCUSDT", Timeframe.D1, None, stored
            )

        async def enqueue(_pool: object, kind: str, *_args: object) -> str:
            queued.append(kind)
            return "wf-1"

        monkeypatch.setattr(
            research, "_load_backtest_strategy", lambda _sid: (RuleBasedStrategy, config)
        )
        monkeypatch.setattr(research, "_preflight", preflight)
        monkeypatch.setattr(research.jobs, "enqueue", enqueue)
        return queued

    async def test_too_short_for_the_folds_is_the_422_naming_the_days_needed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued = self._stub(monkeypatch, days=149)
        body = _wfo_body(start="2024-01-01", end="2024-05-29")
        with pytest.raises(ServiceError) as exc:
            await research.submit_walkforward(None, body)
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["code"] == "range_too_short"
        assert detail["message"].startswith("range too short for 2 folds: stored bars span ")
        assert "need about 150 days" in detail["message"]
        assert queued == []

    async def test_a_span_that_fits_the_folds_is_queued(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued = self._stub(monkeypatch, days=155)
        body = _wfo_body(start="2024-01-01", end="2024-06-04")
        assert await research.submit_walkforward(None, body) == {
            "task_id": "wf-1",
            "status": "queued",
        }
        assert queued == ["walkforward"]
