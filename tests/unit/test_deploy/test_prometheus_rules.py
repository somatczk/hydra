"""Producer/rule agreement for deploy/prometheus-rules.yml against hydra.dashboard.metrics."""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from hydra.dashboard import metrics
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.risk.pretrade import PreTradeRiskManager

RULES_PATH = Path(__file__).resolve().parents[3] / "deploy" / "prometheus-rules.yml"
HISTOGRAM_RULE = re.compile(
    r"histogram_quantile\(\s*[\d.]+\s*,.*?(\w+)_bucket\b.*\)\s*>\s*([\d.]+)\s*$", re.S
)


class _RiskStateConn:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def execute(self, query: str, *args: Any) -> None:
        assert query.startswith("UPDATE risk_state"), query
        self._row.update(daily_pnl=args[0], consecutive_losses=args[2], current_drawdown=args[4])

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM risk_state" in query, query
        return dict(self._row) if self._row else None


class _RiskStatePool:
    def __init__(self) -> None:
        self.row: dict[str, Any] = {}

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_RiskStateConn]:
        yield _RiskStateConn(self.row)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


def _rules() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(RULES_PATH.read_text())
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"]}


def _drawdown_threshold() -> float:
    expr = _rules()["HydraDrawdownExceedsThreshold"]["expr"].strip()
    match = re.fullmatch(r"hydra_drawdown_pct\s*>\s*([\d.]+)", expr)
    assert match, expr
    return float(match.group(1))


def _exported_sample(name: str) -> float:
    for family in text_string_to_metric_families(generate_latest().decode()):
        for sample in family.samples:
            if sample.name == name:
                return float(sample.value)
    raise AssertionError(f"{name} not exported")


async def test_drawdown_emitted_as_fraction() -> None:
    pool = _RiskStatePool()
    risk = PreTradeRiskManager(db_pool=pool)
    await risk.record_fill(Decimal("0"), Decimal("10000"))
    await risk.record_fill(Decimal("-1200"), Decimal("8800"))
    assert pool.row["current_drawdown"] == Decimal("0.12")

    sessions = SessionManager(db_pool=None)
    sessions._db_pool = pool
    session = TradingSession(session_id="s1", strategy_id="any", trading_mode="paper")
    state = await sessions._build_portfolio_state(session)

    assert state.current_drawdown == Decimal("0.12")
    exported = _exported_sample("hydra_drawdown_pct")
    assert exported == pytest.approx(0.12)
    assert exported > _drawdown_threshold()


def test_drawdown_rule_threshold_matches_fraction_unit() -> None:
    threshold = _drawdown_threshold()
    assert 0 < threshold < 1
    assert threshold < 0.12
    assert threshold >= 0.08


def test_histogram_rule_thresholds_below_top_bucket() -> None:
    metrics._ensure_initialized()
    top_bucket = {
        c._name: max(b for b in c._upper_bounds if not math.isinf(b))
        for c in metrics._metrics.values()
        if hasattr(c, "_upper_bounds")
    }

    checked = {}
    for alert, rule in _rules().items():
        match = HISTOGRAM_RULE.search(rule["expr"])
        if not match:
            continue
        metric, threshold = match.group(1), float(match.group(2))
        assert metric in top_bucket, f"{alert}: {metric} has no histogram collector"
        assert threshold < top_bucket[metric], (
            f"{alert}: p-quantile capped at top bucket {top_bucket[metric]}, threshold {threshold}"
        )
        checked[alert] = metric

    assert set(checked) == {
        "HydraOrderFillLatencyHigh",
        "HydraEventBusLatencyHigh",
        "HydraExchangeAPILatencyHigh",
        "HydraMLInferenceLatencyHigh",
    }
