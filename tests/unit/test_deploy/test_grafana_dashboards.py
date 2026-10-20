"""Grafana panels that read hydra_drawdown_pct must treat it as a fraction (0.05 = 5%)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from hydra.risk.circuit_breakers import _TIERS

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARDS = [
    REPO_ROOT / "deploy" / "grafana-dashboard.json",
    REPO_ROOT / "config" / "grafana" / "hydra-overview.json",
]
TIER_EXPR = re.compile(r"hydra_drawdown_pct\s*>=\s*bool\s*([\d.]+)")


def _panels(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text())
    return list(doc.get("dashboard", doc)["panels"])


def _drawdown_panels(path: Path) -> list[dict[str, Any]]:
    return [
        p
        for p in _panels(path)
        if any("hydra_drawdown_pct" in t.get("expr", "") for t in p.get("targets", []))
    ]


def _threshold_values(panel: dict[str, Any]) -> list[float]:
    steps = panel.get("fieldConfig", {}).get("defaults", {}).get("thresholds", {}).get("steps", [])
    return [float(s["value"]) for s in steps if s["value"] is not None]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_drawdown_value_panels_use_fraction_unit(path: Path) -> None:
    value_panels = [p for p in _drawdown_panels(path) if p["type"] != "state-timeline"]
    assert value_panels, f"{path.name}: no panel reads hydra_drawdown_pct"

    for panel in value_panels:
        defaults = panel["fieldConfig"]["defaults"]
        assert defaults.get("unit") == "percentunit", panel["title"]
        thresholds = _threshold_values(panel)
        assert thresholds, panel["title"]
        assert all(0 <= v < 1 for v in thresholds), (panel["title"], thresholds)
        if "max" in defaults:
            assert 0 < defaults["max"] < 1, (panel["title"], defaults["max"])
            assert max(thresholds) < defaults["max"], panel["title"]


def test_circuit_breaker_panel_matches_risk_tiers() -> None:
    (panel,) = [
        p
        for p in _panels(DASHBOARDS[0])
        if p["type"] == "state-timeline" and p["title"] == "Circuit Breaker Status"
    ]
    targets = sorted(panel["targets"], key=lambda t: t["refId"])

    parsed = []
    for target in targets:
        match = TIER_EXPR.fullmatch(target["expr"].strip())
        assert match, target["expr"]
        parsed.append(float(match.group(1)))

    tiers = [float(spec.drawdown_threshold) for spec in _TIERS]
    assert parsed == tiers
    for spec, target in zip(_TIERS, targets, strict=True):
        pct = f"{float(spec.drawdown_threshold) * 100:g}%"
        assert target["legendFormat"] == f"Tier {spec.tier} ({pct})"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_json_formatting_is_stable(path: Path) -> None:
    text = path.read_text()
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert "\t" not in text
