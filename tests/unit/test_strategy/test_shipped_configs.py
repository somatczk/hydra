"""Every shipped strategy and template YAML passes the strict P2-E2 validator and declares
the bar history its rules need."""

from __future__ import annotations

from pathlib import Path

import yaml

from hydra.strategy.builtin.rule_based import RuleBasedStrategy
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import validate_strategy_config
from hydra.strategy.rule_engine import rules_required_history

_REPO = Path(__file__).resolve().parents[3]


def test_every_shipped_yaml_validates() -> None:
    files = [
        path
        for directory in (_REPO / "config" / "strategies", _REPO / "config" / "templates")
        for path in sorted(directory.glob("*.yaml"))
    ]
    assert len(files) == 16  # 11 shipped strategies + 5 templates
    for path in files:
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict), path
        data.pop("_template_meta", None)
        data.setdefault("id", "shipped_template_probe")
        validate_strategy_config(data)


def test_every_shipped_rule_config_declares_at_least_its_derived_history() -> None:
    """A shipped rule strategy or template never gates on fewer bars than its rules need
    (``rules_required_history`` over the rules its own parameters resolve)."""
    short = []
    for directory in (_REPO / "config" / "strategies", _REPO / "config" / "templates"):
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            data.pop("_template_meta", None)
            data.setdefault("id", "shipped_template_probe")
            config = validate_strategy_config(data)
            if config.strategy_class != RuleBasedStrategy.__module__ + ".RuleBasedStrategy":
                continue
            strategy = RuleBasedStrategy(config=config, context=StrategyContext())
            derived = rules_required_history(strategy._rules)
            if config.parameters["required_history"] < derived:
                short.append(f"{path.name}: {config.parameters['required_history']} < {derived}")
    assert short == []
