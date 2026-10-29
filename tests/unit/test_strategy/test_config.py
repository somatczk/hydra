"""config_hash, load_all_strategy_configs (bad files skipped and logged, SM-15) and schedule."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hydra.strategy.config import (
    ScheduleConfig,
    StrategyConfig,
    config_hash,
    load_all_strategy_configs,
)

HOUR = timedelta(hours=1)

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


class TestSchedule:
    def _cfg(self, primary: str = "1h", **schedule: object) -> StrategyConfig:
        return StrategyConfig(**{**_BASE, "timeframes": {"primary": primary}, "schedule": schedule})

    def test_default_is_every_hour_every_day(self) -> None:
        sched = ScheduleConfig()
        assert sched.active_hours_utc == list(range(24))
        assert sched.active_days == list(range(7))
        assert all(sched.allows(datetime(2026, 1, 5, tzinfo=UTC) + i * HOUR) for i in range(168))

    def test_open_time_hour_and_monday_zero(self) -> None:
        sched = ScheduleConfig(active_hours_utc=[13], active_days=[0])
        monday_13 = datetime(2026, 1, 5, 13, tzinfo=UTC)  # 2026-01-05 is a Monday
        assert sched.allows(monday_13)
        assert not sched.allows(monday_13 - HOUR)  # 12:00 bar: closes at 13:00, still out
        assert not sched.allows(monday_13 + HOUR)
        assert not sched.allows(monday_13 + 24 * HOUR)  # Tuesday
        assert sched.allows(datetime(2026, 1, 5, 14, tzinfo=timezone(timedelta(hours=1))))

    @pytest.mark.parametrize(
        ("schedule", "message"),
        [
            ({"active_hours_utc": [24]}, "less than or equal to 23"),
            ({"active_hours_utc": [-1]}, "greater than or equal to 0"),
            ({"active_days": [7]}, "less than or equal to 6"),
            ({"active_hours_utc": []}, "at least 1 item"),
            ({"active_days": []}, "at least 1 item"),
            ({"active_hours_utc": [9, 13, 9, 13, 2]}, r"active_hours_utc lists \[9, 13\] more"),
            ({"active_days": [1, 1]}, r"active_days lists \[1\] more than once"),
            ({"hours": [13]}, "Extra inputs are not permitted"),
        ],
    )
    def test_invalid_schedules_are_refused(self, schedule: dict[str, object], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            self._cfg(**schedule)

    def test_hours_no_primary_bar_opens_at_are_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"1d bars open only at hours \[0\] UTC"):
            self._cfg("1d", active_hours_utc=list(range(13, 22)))
        with pytest.raises(ValidationError, match=r"\[0, 4, 8, 12, 16, 20\]"):
            self._cfg("4h", active_hours_utc=[13, 14, 15])
        with pytest.raises(ValidationError, match="1w bars open on Monday"):
            self._cfg("1w", active_days=[1, 2])

    def test_schedules_a_primary_bar_can_match_are_accepted(self) -> None:
        assert self._cfg("4h", active_hours_utc=[13, 16]).schedule is not None
        assert self._cfg("1d", active_hours_utc=[0], active_days=[5, 6]).schedule is not None
        assert self._cfg("15m", active_hours_utc=[13]).schedule is not None

    def test_the_schedule_is_part_of_the_config_hash(self) -> None:
        assert StrategyConfig(**_BASE).schedule is None
        assert config_hash(StrategyConfig(**_BASE)) != config_hash(self._cfg(active_days=[0]))
