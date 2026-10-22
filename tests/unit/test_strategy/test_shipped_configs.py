"""Every shipped strategy and template YAML passes the strict P2-E2 validator."""

from __future__ import annotations

from pathlib import Path

import yaml

from hydra.strategy.engine import validate_strategy_config

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
