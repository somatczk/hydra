"""The builder's port of ``param_bindings`` and the engine's must agree on one fixture.

``dashboard/src/components/builder/bindings.test.ts`` asserts the same file from the TypeScript
side, so a resolver change that lands on one side only fails a test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from hydra.strategy.builtin.rule_based import ambiguous, param_bindings
from hydra.strategy.condition_schema import RuleSet
from hydra.strategy.context import indicator_params
from hydra.strategy.indicator_registry import get_all_indicators

FIXTURE = Path(__file__).parents[3] / "dashboard" / "src" / "test" / "paramBindings.parity.json"


def test_the_engine_resolver_matches_the_shared_fixture() -> None:
    fixture: dict[str, Any] = json.loads(FIXTURE.read_text())
    table = param_bindings(RuleSet.model_validate(fixture["rules"]))
    bindable = {
        name: [
            {
                "group": s.group,
                "index": s.index,
                "target": s.target,
                "key": s.key,
                "value": s.default,
                "implicit": s.implicit,
            }
            for s in sites
        ]
        for name, sites in table.items()
        if not ambiguous(sites)
    }
    assert bindable == fixture["bindable"]
    assert sorted(name for name, sites in table.items() if ambiguous(sites)) == fixture["ambiguous"]


def test_the_engine_refuses_the_fixture_target_the_builder_flags() -> None:
    """``refused``: the builder flags a param_overrides target from the registry's params
    (``unknownTargets``); the engine raises at the same site at save. The fixture's catalogue
    is the registry's, and the registry lists exactly what the engine's signature takes."""
    refused: dict[str, Any] = json.loads(FIXTURE.read_text())["refused"]
    registry = {info.name: [p.name for p in info.params] for info in get_all_indicators()}
    for name, params in refused["catalogue"].items():
        assert registry[name] == params
        assert list(indicator_params(name, {})) == params
    site = refused["site"]
    condition = refused["rules"][site["group"]]["conditions"][site["index"]]
    message = (
        f"{site['group']}[{site['index']}] param_overrides {site['name']!r}: "
        f"{condition['indicator']} has no parameter {site['key']!r}"
    )
    with pytest.raises(ValueError, match=re.escape(message)):
        param_bindings(RuleSet.model_validate(refused["rules"]))
