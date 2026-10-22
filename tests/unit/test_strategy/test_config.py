"""config_hash and load_all_strategy_configs (bad files are skipped and logged, SM-15)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hydra.strategy.config import StrategyConfig, config_hash, load_all_strategy_configs

_BASE: dict[str, object] = {
    "id": "cfg-1",
    "name": "Cfg One",
    "strategy_class": "hydra.strategy.builtin.rule_based.RuleBasedStrategy",
    "symbols": ["BTCUSDT"],
    "parameters": {"rsi_period": 14},
}


class TestConfigHash:
    def test_stable_for_equal_configs(self) -> None:
        a = StrategyConfig(**_BASE)
        b = StrategyConfig(**_BASE)
        assert config_hash(a) == config_hash(b)

    def test_changes_with_parameters(self) -> None:
        a = StrategyConfig(**_BASE)
        b = a.model_copy(update={"parameters": {"rsi_period": 21}})
        assert config_hash(a) != config_hash(b)

    def test_enabled_is_excluded(self) -> None:
        """``enabled`` is forced True at session start; the hash must not depend on it."""
        a = StrategyConfig(**{**_BASE, "enabled": True})
        b = StrategyConfig(**{**_BASE, "enabled": False})
        assert config_hash(a) == config_hash(b)

    def test_name_is_excluded(self) -> None:
        a = StrategyConfig(**_BASE)
        b = a.model_copy(update={"name": "Renamed"})
        assert config_hash(a) == config_hash(b)

    def test_is_a_short_hex_digest(self) -> None:
        h = config_hash(StrategyConfig(**_BASE))
        assert len(h) == 12
        int(h, 16)  # does not raise


class TestLoadAllStrategyConfigs:
    def test_valid_files_load(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            "id: a\nname: A\nstrategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        )
        configs = load_all_strategy_configs(tmp_path)
        assert [c.id for c in configs] == ["a"]

    def test_bad_file_is_skipped_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "bad.yaml").write_text("id: bad\n")  # missing strategy_class
        (tmp_path / "good.yaml").write_text(
            "id: good\nname: Good\n"
            "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        )
        with caplog.at_level(logging.ERROR, logger="hydra.strategy.config"):
            configs = load_all_strategy_configs(tmp_path)
        assert [c.id for c in configs] == ["good"]
        assert "bad.yaml" in caplog.text

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_all_strategy_configs(tmp_path / "nope") == []
