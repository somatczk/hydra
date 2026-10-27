"""Producer/rule agreement for deploy/prometheus-rules.yml against hydra.core.metrics."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from hydra.core import metrics
from hydra.dashboard.health import FEED_MESSAGE_MAX_AGE_S, closed_bar_max_age
from tests.unit.test_risk.harness import make_account, permissive

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
RULES_PATH = DEPLOY / "prometheus-rules.yml"
RULE_TESTS_PATH = DEPLOY / "prometheus-rules.test.yml"
HYDRA_NAME = re.compile(r"\bhydra_\w+")
EXTERNAL_SERIES = re.compile(r"^(up|process_\w+)\{")
HISTOGRAM_RULE = re.compile(
    r"histogram_quantile\(\s*[\d.]+\s*,.*?(\w+)_bucket\b.*\)\s*>\s*([\d.]+)\s*$", re.S
)


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
    match = re.fullmatch(r"hydra_drawdown_ratio\s*>\s*([\d.]+)", expr)
    assert match, expr
    return float(match.group(1))


def _exported_sample(name: str) -> float:
    for family in text_string_to_metric_families(generate_latest().decode()):
        for sample in family.samples:
            if sample.name == name:
                return float(sample.value)
    raise AssertionError(f"{name} not exported")


async def test_drawdown_emitted_as_fraction() -> None:
    acct = await make_account(permissive())
    ex = await acct.add_session("s1", "10000")
    await acct.mark("60000")
    await ex.create_order("BTCUSDT", "BUY", "MARKET", Decimal("0.1"))
    await acct.mark("48000")

    assert acct.svc.account().peak_equity == Decimal("10000")
    assert acct.svc.account().equity == Decimal("8800.0")
    exported = _exported_sample("hydra_drawdown_ratio")
    assert exported == pytest.approx(0.12)
    assert exported > _drawdown_threshold()
    assert _exported_sample("hydra_circuit_breaker_active_tier") == 3.0


def test_drawdown_rule_threshold_matches_fraction_unit() -> None:
    threshold = _drawdown_threshold()
    assert 0 < threshold < 1
    assert threshold < 0.12
    assert threshold >= 0.08


def test_histogram_rule_thresholds_below_top_bucket() -> None:
    top_bucket = {
        spec.name: max(b for b in spec.buckets if not math.isinf(b))
        for spec in metrics.CONTRACT
        if spec.kind == "histogram"
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
    }


def test_every_hydra_series_in_rules_is_in_the_contract() -> None:
    exported = {sample for spec in metrics.CONTRACT for sample in spec.samples}
    for alert, rule in _rules().items():
        names = set(HYDRA_NAME.findall(rule["expr"]))
        if not names:
            assert EXTERNAL_SERIES.search(rule["expr"]), f"{alert} reads no known series"
        assert names <= exported, f"{alert}: {sorted(names - exported)} not exported"


def test_rules_without_an_honest_producer_are_gone() -> None:
    rules = _rules()
    assert "HydraReconciliationMismatch" not in rules
    assert "HydraDataGapDetected" not in rules
    exprs = " ".join(r["expr"] for r in rules.values())
    assert "hydra_reconciliation_mismatch" not in exprs
    assert "HydraMLInferenceLatencyHigh" not in rules
    assert "hydra_ml_" not in exprs


def test_feed_staleness_rules_compare_against_prometheus_time() -> None:
    rules = _rules()
    assert rules["HydraFeedStale"]["expr"].startswith("time() - ")
    assert rules["HydraClosedBarOverdue"]["expr"].startswith("time() - ")
    assert "hydra_feed_bar_interval_seconds" in rules["HydraClosedBarOverdue"]["expr"]


def test_feed_thresholds_match_the_readiness_check() -> None:
    rules = _rules()
    stale = re.fullmatch(
        r"time\(\) - hydra_feed_last_message_timestamp_seconds > ([\d.]+)",
        rules["HydraFeedStale"]["expr"].strip(),
    )
    assert stale and float(stale.group(1)) == FEED_MESSAGE_MAX_AGE_S
    overdue = re.fullmatch(
        r"time\(\) - hydra_feed_last_closed_bar_timestamp_seconds"
        r" > ([\d.]+) \* hydra_feed_bar_interval_seconds \+ ([\d.]+)",
        rules["HydraClosedBarOverdue"]["expr"].strip(),
    )
    assert overdue
    bars, grace = float(overdue.group(1)), float(overdue.group(2))
    for interval in (60.0, 3600.0):
        assert bars * interval + grace == closed_bar_max_age(interval)


def test_health_rules_read_the_monitor_gauges() -> None:
    rules = _rules()
    assert rules["HydraNotReady"]["expr"] == "hydra_ready == 0"
    assert rules["HydraSessionNotSupervised"]["expr"] == (
        "hydra_session_desired == 1 unless on (session_id) hydra_session_supervised == 1"
    )
    assert rules["HydraHealthMonitorStalled"]["expr"].startswith(
        "time() - hydra_health_monitor_last_run_timestamp_seconds > "
    )


def test_memory_is_left_to_the_container_limit_rules() -> None:
    rules = _rules()
    assert "HydraHighMemoryUsage" not in rules
    assert not [a for a, r in rules.items() if "memory" in r["expr"]]


def test_every_alert_has_a_firing_and_a_silent_promtool_case() -> None:
    cases = yaml.safe_load(RULE_TESTS_PATH.read_text())["tests"]
    fires: set[str] = set()
    silent: set[str] = set()
    for case in cases:
        for check in case.get("alert_rule_test", []):
            (fires if check["exp_alerts"] else silent).add(check["alertname"])
    alerts = set(_rules())
    assert alerts - fires == set(), "alerts without a firing case"
    assert alerts - silent == set(), "alerts without a non-firing case"


# ---------------------------------------------------------------------------
# The live protective gap (P3-B): rule/producer agreement and the gauge lifecycle
# ---------------------------------------------------------------------------


def _gap(session_id: str) -> float | None:
    return REGISTRY.get_sample_value(
        "hydra_live_protective_gap",
        {
            "session_id": session_id,
            "strategy_id": "saw",
            "exchange_id": "binance",
            "symbol": "BTCUSDT",
        },
    )


def test_live_protective_gap_rule_matches_the_contract() -> None:
    rules = _rules()
    gap = rules["HydraLiveProtectiveGap"]
    assert gap["expr"].strip() == "hydra_live_protective_gap == 1"
    assert gap["for"] == "1m"
    assert gap["labels"] == {"severity": "critical", "team": "trading"}
    assert "dead-man checklist" in gap["annotations"]["description"]

    (spec,) = [s for s in metrics.CONTRACT if s.name == "hydra_live_protective_gap"]
    assert spec.kind == "gauge"
    assert set(spec.labels) == {"session_id", "strategy_id", "exchange_id", "symbol"}
    assert spec.samples == ("hydra_live_protective_gap",)


def test_the_gap_gauge_sets_and_clears() -> None:
    metrics.set_live_protective_gap("s1", "saw", "binance", "BTCUSDT", True)
    assert _gap("s1") == 1.0

    metrics.set_live_protective_gap("s1", "saw", "binance", "BTCUSDT", False)
    assert _gap("s1") is None  # cleared, not zero: nothing restates the series


def test_forget_session_keeps_the_gap_while_a_position_remains() -> None:
    metrics.set_session_position("s1", "saw", "binance", "BTCUSDT", 1.0)
    metrics.set_live_protective_gap("s1", "saw", "binance", "BTCUSDT", True)

    metrics.forget_session("s1", "saw", "live")

    assert _gap("s1") == 1.0  # R13: the unprotected remainder keeps paging


def test_forget_session_clears_the_gap_when_nothing_is_held() -> None:
    metrics.set_session_position("s2", "saw", "binance", "BTCUSDT", 0.0)
    metrics.set_live_protective_gap("s2", "saw", "binance", "BTCUSDT", True)

    metrics.forget_session("s2", "saw", "live")

    assert _gap("s2") is None


def test_forget_session_clears_the_gap_of_a_session_that_never_reported_positions() -> None:
    metrics.set_live_protective_gap("s3", "saw", "binance", "BTCUSDT", True)

    metrics.forget_session("s3", "saw", "live")

    assert _gap("s3") is None


# ---------------------------------------------------------------------------
# The live reconcile state (P3-A2): rule/producer agreement and the gauge lifecycle
# ---------------------------------------------------------------------------


def _reconcile_state(session_id: str) -> float | None:
    return REGISTRY.get_sample_value("hydra_live_reconcile_state", {"session_id": session_id})


def test_live_reconcile_rule_matches_the_contract() -> None:
    rules = _rules()
    alert = rules["HydraLiveReconcileMismatch"]
    assert alert["expr"].strip() == "hydra_live_reconcile_state == 1"
    assert alert["for"] == "2m"
    assert alert["labels"] == {"severity": "critical", "team": "trading"}
    assert "never auto-corrected" in alert["annotations"]["description"]

    (spec,) = [s for s in metrics.CONTRACT if s.name == "hydra_live_reconcile_state"]
    assert spec.kind == "gauge"
    assert set(spec.labels) == {"session_id"}
    assert spec.samples == ("hydra_live_reconcile_state",)


def test_live_reconcile_unknown_rule_matches_the_contract() -> None:
    """A5: value 2 (unknown) gets the consumer it was exported for — the blind alert."""
    rules = _rules()
    alert = rules["HydraLiveReconcileUnknown"]
    assert alert["expr"].strip() == "hydra_live_reconcile_state == 2"
    assert alert["for"] == "10m"
    assert alert["labels"] == {"severity": "warning", "team": "trading"}
    assert "BLIND" in alert["annotations"]["description"]
    assert "cannot be detected" in alert["annotations"]["description"]


def test_the_reconcile_gauge_maps_the_states() -> None:
    metrics.set_live_reconcile_state("s1", "ok")
    assert _reconcile_state("s1") == 0.0
    metrics.set_live_reconcile_state("s1", "mismatch")
    assert _reconcile_state("s1") == 1.0
    metrics.set_live_reconcile_state("s1", "unknown")
    assert _reconcile_state("s1") == 2.0
    metrics.set_live_reconcile_state("s1", "never")
    assert _reconcile_state("s1") == 3.0
    with pytest.raises(ValueError):
        metrics.set_live_reconcile_state("s1", "bogus")


def test_forget_session_clears_the_reconcile_state() -> None:
    metrics.set_live_reconcile_state("s4", "mismatch")

    metrics.forget_session("s4", "saw", "live")

    assert _reconcile_state("s4") is None


def test_forgetting_a_session_that_never_reconciled_is_quiet() -> None:
    metrics.forget_session("s5", "saw", "paper")  # the series was never set

    assert _reconcile_state("s5") is None
