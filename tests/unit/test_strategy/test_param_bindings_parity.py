"""The builder's port of ``param_bindings`` and the engine's must agree on one fixture.

``dashboard/src/components/builder/bindings.test.ts`` asserts the same file from the TypeScript
side, so a resolver change that lands on one side only fails a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hydra.strategy.builtin.rule_based import ambiguous, param_bindings
from hydra.strategy.condition_schema import RuleSet

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
