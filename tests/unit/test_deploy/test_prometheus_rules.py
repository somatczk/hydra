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
from hydra.risk.pretrade import PreTradeRiskManager

RULES_PATH = Path(__file__).resolve().parents[3] / "deploy" / "prometheus-rules.yml"
HISTOGRAM_RULE = re.compile(
    r"histogram_quantile\(\s*[\d.]+\s*,.*?(\w+)_bucket\b.*\)\s*>\s*([\d.]+)\s*$", re.S
)


class _RecordingConn:
    def __init__(self, sink: list[tuple[Any, ...]]) -> None:
        self._sink = sink

    async def execute(self, query: str, *args: Any) -> None:
        self._sink.append(args)


class _RecordingPool:
    def __init__(self) -> None:
        self.executed: list[tuple[Any, ...]] = []

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_RecordingConn]:
        yield _RecordingConn(self.executed)


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
    pool = _RecordingPool()
    mgr = PreTradeRiskManager(db_pool=pool)
    await mgr.record_fill(Decimal("0"), Decimal("10000"))
    await mgr.record_fill(Decimal("-1200"), Decimal("8800"))

    persisted_drawdown = pool.executed[-1][4]
    metrics.update_portfolio(8800.0, float(Decimal(str(persisted_drawdown))), -1200.0)

    assert _exported_sample("hydra_drawdown_pct") == pytest.approx(0.12)


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
