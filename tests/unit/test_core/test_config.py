"""Tests for hydra.core.config — YAML loading, env var substitution, layered config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hydra.core.config import (
    HydraConfig,
    _deep_merge,
    _substitute_env_vars,
    load_config,
)


# ---------------------------------------------------------------------------
# Env var substitution
# ---------------------------------------------------------------------------


class TestEnvVarSubstitution:
    def test_simple_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _substitute_env_vars("${MY_VAR}") == "hello"

    def test_var_with_default(self) -> None:
        # Unset var should fall back to default
        result = _substitute_env_vars("${NONEXISTENT_VAR_12345:fallback}")
        assert result == "fallback"

    def test_var_with_empty_default(self) -> None:
        result = _substitute_env_vars("${NONEXISTENT_VAR_12345:}")
        assert result == ""

    def test_no_default_no_env(self) -> None:
        # Should leave placeholder unchanged
        result = _substitute_env_vars("${NEVER_SET_VAR_XYZ}")
        assert result == "${NEVER_SET_VAR_XYZ}"

    def test_multiple_vars_in_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "5432")
        result = _substitute_env_vars("${HOST}:${PORT}")
        assert result == "localhost:5432"

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "from_env")
        result = _substitute_env_vars("${MY_VAR:default}")
        assert result == "from_env"


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_simple_merge(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": 10}
        override = {"a": {"y": 99, "z": 3}}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 99, "z": 3}, "b": 10}

    def test_override_replaces_non_dict(self) -> None:
        base = {"a": [1, 2]}
        override = {"a": [3]}
        result = _deep_merge(base, override)
        assert result == {"a": [3]}


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_base_config(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            database:
              host: myhost
              port: 5433
            trading:
              testnet: true
        """)
        )

        cfg = load_config(env="base", config_dir=tmp_path)
        assert isinstance(cfg, HydraConfig)
        assert cfg.database.host == "myhost"
        assert cfg.database.port == 5433
        assert cfg.trading.testnet is True

    def test_layered_config(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            database:
              host: basehost
              port: 5432
            trading:
              testnet: true
              paper_trading: true
        """)
        )

        live = tmp_path / "live.yaml"
        live.write_text(
            textwrap.dedent("""\
            trading:
              testnet: false
              paper_trading: false
        """)
        )

        cfg = load_config(env="live", config_dir=tmp_path)
        assert cfg.database.host == "basehost"  # from base
        assert cfg.trading.testnet is False  # overridden by live
        assert cfg.trading.paper_trading is False

    def test_env_var_substitution_in_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_DB_HOST", "envhost")
        monkeypatch.setenv("TEST_DB_PORT", "9999")

        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            database:
              host: ${TEST_DB_HOST}
              port: ${TEST_DB_PORT}
        """)
        )

        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.database.host == "envhost"
        assert cfg.database.port == 9999

    def test_env_var_with_default_in_yaml(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            logging:
              level: ${LOG_LEVEL_NONEXISTENT:WARNING}
              format: ${LOG_FORMAT_NONEXISTENT:colored}
        """)
        )

        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.logging.level == "WARNING"
        assert cfg.logging.format == "colored"

    def test_missing_env_yaml_is_ok(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            trading:
              testnet: true
        """)
        )
        # "staging.yaml" doesn't exist — should not raise
        cfg = load_config(env="staging", config_dir=tmp_path)
        assert cfg.trading.testnet is True

    def test_empty_base_yaml(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("")
        cfg = load_config(env="base", config_dir=tmp_path)
        # Defaults should apply
        assert isinstance(cfg, HydraConfig)
        assert cfg.trading.testnet is True

    def test_no_base_yaml(self, tmp_path: Path) -> None:
        # No files at all — defaults
        cfg = load_config(env="base", config_dir=tmp_path)
        assert isinstance(cfg, HydraConfig)


# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


class TestNestedConfigs:
    def test_database_dsn(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            database:
              host: db.example.com
              port: 5432
              name: hydra
              user: admin
              password: secret
        """)
        )
        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.database.dsn == "postgresql+asyncpg://admin:secret@db.example.com:5432/hydra"

    def test_exchanges_config(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text(
            textwrap.dedent("""\
            exchanges:
              binance:
                api_key: mykey
                api_secret: mysecret
                market_types:
                  - spot
                  - futures
        """)
        )
        cfg = load_config(env="base", config_dir=tmp_path)
        assert "binance" in cfg.exchanges
        assert cfg.exchanges["binance"].api_key == "mykey"
        assert cfg.exchanges["binance"].market_types == ["spot", "futures"]

    def test_all_sections_present(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("")
        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.database is not None
        assert cfg.redis is not None
        assert cfg.trading is not None
        assert cfg.logging is not None
        assert cfg.api is not None
        assert cfg.telegram is not None
        assert cfg.ml is not None

    def test_real_base_config_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the actual repo base.yaml loads with env vars set."""
        monkeypatch.setenv("HYDRA_DB_HOST", "localhost")
        monkeypatch.setenv("HYDRA_DB_PORT", "5432")
        monkeypatch.setenv("HYDRA_DB_NAME", "hydra")
        monkeypatch.setenv("HYDRA_DB_USER", "hydra")
        monkeypatch.setenv("HYDRA_DB_PASSWORD", "test")
        monkeypatch.setenv("HYDRA_REDIS_URL", "redis://localhost:6379")
        cfg = load_config(env="base")
        assert cfg.trading.testnet is True
        assert cfg.trading.default_symbols == ["BTCUSDT"]
