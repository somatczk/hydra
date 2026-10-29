"""Grafana panels read only series hydra.core.metrics exports, with its labels and units."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import REGISTRY
from prometheus_client.utils import floatToGoString

from hydra.core import metrics
from hydra.core.metrics import CONTRACT
from hydra.risk.circuit_breakers import _TIERS
from tests.unit.test_execution.session_harness import Harness, held, rule, write_strategy

REPO_ROOT = Path(__file__).resolve().parents[3]
DRAWDOWN_DASHBOARDS = [
    REPO_ROOT / "deploy" / "grafana-dashboard.json",
    REPO_ROOT / "config" / "grafana" / "hydra-overview.json",
]
DASHBOARDS = [*DRAWDOWN_DASHBOARDS, REPO_ROOT / "config" / "grafana" / "hydra-system.json"]
RULES_PATH = REPO_ROOT / "deploy" / "prometheus-rules.yml"
SRC = REPO_ROOT / "src" / "hydra"
TIER_VALUES = [float(spec.drawdown_threshold) for spec in _TIERS]
HYDRA_NAME = re.compile(r"\bhydra_\w+")
BY_CLAUSE = re.compile(r"\bby\s*\(([^)]*)\)")
LEGEND_LABEL = re.compile(r"\{\{\s*(\w+)\s*\}\}")
LE_MATCHER = re.compile(r"\ble\s*=\s*\"([^\"]+)\"")

SAMPLE_TO_SPEC = {sample: spec for spec in CONTRACT for sample in spec.samples}


def _doc(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text())
    return dict(doc.get("dashboard", doc))


def _panels(path: Path) -> list[dict[str, Any]]:
    return list(_doc(path)["panels"])


def _targets(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for panel in _panels(path):
        for target in panel.get("targets", []):
            yield panel["title"], target


def _drawdown_panels(path: Path) -> list[dict[str, Any]]:
    return [
        p
        for p in _panels(path)
        if any("hydra_drawdown_ratio" in t.get("expr", "") for t in p.get("targets", []))
    ]


def _threshold_values(panel: dict[str, Any]) -> list[float]:
    steps = panel.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps", [])
    return [float(s["value"]) for s in steps if s["value"] is not None]


@pytest.mark.parametrize("path", DRAWDOWN_DASHBOARDS, ids=lambda p: p.name)
def test_drawdown_value_panels_use_fraction_unit(path: Path) -> None:
    value_panels = [p for p in _drawdown_panels(path) if p["type"] != "state-timeline"]
    assert value_panels, f"{path.name}: no panel reads hydra_drawdown_ratio"

    for panel in value_panels:
        defaults = panel["fieldConfig"]["defaults"]
        assert defaults.get("unit") == "percentunit", panel["title"]
        thresholds = _threshold_values(panel)
        assert sorted(thresholds) == TIER_VALUES, (panel["title"], thresholds)
        if "max" in defaults:
            assert 0 < defaults["max"] < 1, (panel["title"], defaults["max"])
            assert max(thresholds) < defaults["max"], panel["title"]


def test_tier_timeline_shows_the_live_breaker_tier() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["title"] == "Circuit Breaker Tier"]
    assert panel["type"] == "state-timeline"
    assert [(t["expr"], t["legendFormat"]) for t in panel["targets"]] == [
        ("max by (scope) (hydra_circuit_breaker_active_tier)", "{{scope}}")
    ]
    texts = {
        value: option["text"]
        for mapping in panel["fieldConfig"]["defaults"]["mappings"]
        for value, option in mapping["options"].items()
    }
    assert texts == {"0": "None"} | {str(t.tier): f"Tier {t.tier}: {t.action}" for t in _TIERS}
    assert not [p for p in _panels(DASHBOARDS[0]) if "drawdown_ratio >= bool" in json.dumps(p)]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_hydra_series_is_exported(path: Path) -> None:
    queries = [t.get("expr", "") for _, t in _targets(path)]
    queries += [v.get("query", "") for v in _doc(path).get("templating", {}).get("list", [])]
    used = {name for q in queries for name in HYDRA_NAME.findall(q)}
    assert used, path.name
    assert used <= set(SAMPLE_TO_SPEC), sorted(used - set(SAMPLE_TO_SPEC))


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_grouping_and_legend_labels_exist_on_the_series(path: Path) -> None:
    for title, target in _targets(path):
        expr = target.get("expr", "")
        specs = [SAMPLE_TO_SPEC[n] for n in HYDRA_NAME.findall(expr) if n in SAMPLE_TO_SPEC]
        if not specs:
            continue
        allowed = {label for spec in specs for label in spec.labels}
        if "_bucket" in expr:
            allowed.add("le")
        grouped = {x.strip() for clause in BY_CLAUSE.findall(expr) for x in clause.split(",")}
        legend = set(LEGEND_LABEL.findall(target.get("legendFormat", "")))
        assert (grouped | legend) - {""} <= allowed, (title, expr)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_bucket_matchers_name_a_real_bucket_bound(path: Path) -> None:
    for title, target in _targets(path):
        expr = target.get("expr", "")
        for value in LE_MATCHER.findall(expr):
            (name,) = {n for n in HYDRA_NAME.findall(expr) if n.endswith("_bucket")}
            bounds = {floatToGoString(b) for b in SAMPLE_TO_SPEC[name].buckets} | {"+Inf"}
            assert value in bounds, (title, value)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_panel_without_an_honest_source(path: Path) -> None:
    titles = [p["title"] for p in _panels(path)]
    for word in ("Sharpe", "Accuracy", "PSI", "ML"):
        assert not [t for t in titles if word in t], (path.name, word)
    assert "hydra_ml_" not in path.read_text(), path.name


def _producers() -> dict[str, set[str]]:
    """Contract metric -> the src/hydra functions outside core/metrics.py that call its setters.

    ``hydra.ml`` is excluded: nothing on the runtime path imports it until P5.
    """
    module = (SRC / "core" / "metrics.py").read_text()
    setters: dict[str, set[str]] = {}
    for node in ast.parse(module).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("forget_"):
            continue
        body = ast.get_source_segment(module, node) or ""
        for spec in CONTRACT:
            if f'"{spec.name}"' in body:
                setters.setdefault(spec.name, set()).add(node.name)
    callers: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "ml" or rel == Path("core", "metrics.py"):
            continue
        text = path.read_text()
        for name, fns in setters.items():
            if any(re.search(rf"\b{fn}\(", text) for fn in fns):
                callers.setdefault(name, set()).add(str(rel))
    for name, fns in setters.items():
        if "_ensure_initialized" in fns:
            callers.setdefault(name, set()).add("core/metrics.py (_ensure_initialized)")
    return callers


def test_every_series_a_rule_or_panel_reads_has_a_runtime_producer() -> None:
    texts = [RULES_PATH.read_text(), *(path.read_text() for path in DASHBOARDS)]
    read = {
        SAMPLE_TO_SPEC[n].name for t in texts for n in HYDRA_NAME.findall(t) if n in SAMPLE_TO_SPEC
    }
    producers = _producers()
    assert read
    assert {name for name in read if not producers.get(name)} == set()
    assert producers["hydra_sessions_errored_total"] == {
        "execution/session_manager.py",
        "execution/session_recovery.py",
    }
    # §8: the trader's startup drift task is the drift gauges' only producer,
    # never anything inside hydra.ml (which _producers already excludes)
    assert producers["hydra_ml_drift_psi"] == {"trader/main.py"}
    assert producers["hydra_ml_drift_nan_ratio"] == {"trader/main.py"}


@pytest.fixture()
def _fresh_registry() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


def _sample(name: str, **labels: str) -> float:
    total = 0.0
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


@pytest.mark.usefixtures("_fresh_registry")
async def test_trade_series_count_each_paper_fill_once(strategies_dir: Path) -> None:
    h = Harness(strategies_dir)
    await h.start()
    try:
        sid = write_strategy(
            strategies_dir,
            "pnl-once",
            entry_long=rule(("less_than", 100.05), ("greater_than", 99.95)),
            exit_long=rule(("greater_than", 100.2)),
        )
        session = await h.session(sid)
        await h.bar(session, "100")
        assert await held(session) == Decimal("1.00000")
        await h.bar(session, "100.3")
        assert await held(session) == Decimal("0")
    finally:
        await h.close()

    assert _sample("hydra_trades_total", side="BUY", strategy_id="pnl-once") == 1.0
    assert _sample("hydra_trades_total", side="SELL", strategy_id="pnl-once") == 1.0
    # gross = 100.1997 - 100.1 = +0.0997; net = gross - 0.1001997 - 0.1001 = -0.1005997 USDT
    assert _sample("hydra_trade_pnl_count", strategy_id="pnl-once") == 1.0
    assert _sample("hydra_trade_pnl_bucket", strategy_id="pnl-once", le="-10.0") == 0.0
    assert _sample("hydra_trade_pnl_bucket", strategy_id="pnl-once", le="0.0") == 1.0


def test_win_rate_counts_break_even_as_a_loss() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["title"] == "Win Rate"]
    assert [t["expr"] for t in panel["targets"]] == [
        '1 - sum(increase(hydra_trade_pnl_bucket{le="0.0"}[$__range]))'
        " / sum(increase(hydra_trade_pnl_count[$__range]))"
    ]
    assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"
    assert "break-even counts as a loss" in panel["description"]


def test_exchange_variable_reads_a_series_every_session_produces() -> None:
    (variable,) = [
        v for v in _doc(DASHBOARDS[0])["templating"]["list"] if v["name"] == "exchange_id"
    ]
    assert variable["query"] == "label_values(hydra_feed_bars_total, exchange_id)"


def test_fill_latency_panel_is_titled_for_what_it_measures() -> None:
    titles = {p["title"]: p for p in _panels(DASHBOARDS[0])}
    assert "Strategy Latency" not in titles
    exprs = [t["expr"] for t in titles["Order Fill Latency"]["targets"]]
    assert exprs == [
        "sum by (le) (rate(hydra_order_fill_latency_seconds_bucket[$__rate_interval]))"
    ]


def test_session_panels_match_the_alert_rules() -> None:
    rules = {
        r["alert"]: r["expr"]
        for g in yaml.safe_load(RULES_PATH.read_text())["groups"]
        for r in g["rules"]
    }
    titles = {p["title"]: p for p in _panels(DASHBOARDS[0])}
    zombies = titles["Sessions: Desired vs Supervised"]["targets"][2]["expr"]
    assert rules["HydraSessionNotSupervised"] in zombies
    assert [t["expr"] for t in titles["Readiness by Check"]["targets"]] == ["hydra_ready"]
    limit = titles["Closed Bar Age vs Limit"]["targets"][1]["expr"]
    assert limit.startswith("2 * hydra_feed_bar_interval_seconds") and limit.endswith(" + 60")
    assert "2 * hydra_feed_bar_interval_seconds + 60" in rules["HydraClosedBarOverdue"]


def test_position_panel_sums_sessions_per_symbol() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["title"] == "Position Exposure by Symbol"]
    assert [t["expr"] for t in panel["targets"]] == ["sum by (symbol) (abs(hydra_position_size))"]


def test_heat_panel_reads_heat_ratio_not_drawdown() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["title"] == "Portfolio Heat"]
    assert [t["expr"] for t in panel["targets"]] == ["hydra_portfolio_heat_ratio"]
    assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"


def test_error_rate_panel_reads_the_error_counter() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["title"] == "Error Rate"]
    exprs = " ".join(t["expr"] for t in panel["targets"])
    assert "hydra_exchange_api_errors_total" in exprs
    assert "hydra_trades_total" not in exprs


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_json_formatting_is_stable(path: Path) -> None:
    text = path.read_text()
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert "\t" not in text
