"""Grafana panels read only series hydra.core.metrics exports, with its labels and units."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.utils import floatToGoString

from hydra.core.metrics import CONTRACT
from hydra.risk.circuit_breakers import _TIERS

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARDS = [
    REPO_ROOT / "deploy" / "grafana-dashboard.json",
    REPO_ROOT / "config" / "grafana" / "hydra-overview.json",
]
TIER_EXPR = re.compile(r"hydra_drawdown_ratio\s*>=\s*bool\s*([\d.]+)")
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


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
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


def test_tier_timeline_matches_risk_tiers() -> None:
    (panel,) = [p for p in _panels(DASHBOARDS[0]) if p["type"] == "state-timeline"]
    assert panel["title"] == "Drawdown vs Breaker Tiers"
    texts = {
        value: option["text"]
        for mapping in panel["fieldConfig"]["defaults"]["mappings"]
        for value, option in mapping["options"].items()
    }
    assert texts == {"0": "Below", "1": "At/above threshold"}

    targets = sorted(panel["targets"], key=lambda t: t["refId"])

    parsed = []
    for target in targets:
        match = TIER_EXPR.fullmatch(target["expr"].strip())
        assert match, target["expr"]
        parsed.append(float(match.group(1)))

    assert parsed == TIER_VALUES
    for spec, target in zip(_TIERS, targets, strict=True):
        pct = f"{float(spec.drawdown_threshold) * 100:g}%"
        assert target["legendFormat"] == f"Tier {spec.tier} ({pct})"


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
    for word in ("Sharpe", "Accuracy", "PSI"):
        assert not [t for t in titles if word in t], (path.name, word)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_panel_reads_trade_series_until_the_paper_double_count_is_fixed(path: Path) -> None:
    skewed = {"hydra_trades_total", "hydra_trade_pnl"}
    for title, target in _targets(path):
        families = {SAMPLE_TO_SPEC[n].name for n in HYDRA_NAME.findall(target.get("expr", ""))}
        assert not families & skewed, title
    titles = {p["title"] for p in _panels(path)}
    assert not titles & {"Win Rate", "Fill Rate", "Trades by Strategy", "Total Trades (24h)"}


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
