"""hydra.core.config: one model, env > overlay > base > defaults, fail-fast on bad files."""

from __future__ import annotations

import os
import textwrap
from ipaddress import IPv4Network
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from pydantic import ValidationError

from hydra.core.config import (
    _DEFAULT_CONFIG_DIR,
    ConfigError,
    HydraConfig,
    _deep_merge,
    load_config,
)

ROMULUS_DSN = "postgresql://hydra:pw@hydra-timescaledb:5432/hydra"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("HYDRA_") or name in {"DATABASE_URL", "REDIS_URL"}:
            monkeypatch.delenv(name)


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body))
    return path


def _loc_types(exc: ValidationError) -> set[tuple[tuple[str | int, ...], str]]:
    return {(tuple(e["loc"]), e["type"]) for e in exc.errors()}


# ---------------------------------------------------------------------------
# The repo config and the romulus engine env
# ---------------------------------------------------------------------------


class TestRepoConfig:
    def test_romulus_engine_env_loads_repo_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("DATABASE_URL", ROMULUS_DSN)
        monkeypatch.setenv("REDIS_URL", "redis://hydra-redis:6379")
        monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", key)
        monkeypatch.setenv("HYDRA_API__CORS_ORIGINS", "https://trading.example.com")
        monkeypatch.setenv("TZ", "Europe/London")
        monkeypatch.setenv("BINANCE_API_KEY", "x")

        cfg = load_config()

        assert cfg.database_url is not None
        assert cfg.database_url.get_secret_value() == ROMULUS_DSN
        assert cfg.redis_url == "redis://hydra-redis:6379"
        assert cfg.credential_key is not None
        assert cfg.credential_key.get_secret_value() == key
        assert cfg.api.cors_origins == ["https://trading.example.com"]
        assert cfg.trading.paper_trading is True
        assert cfg.trading.testnet is True
        assert cfg.logging.level == "INFO"
        assert cfg.ml.inference_timeout_ms == 5

    def test_secrets_do_not_leak_into_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", ROMULUS_DSN)
        monkeypatch.setenv("HYDRA_API_TOKEN", "t" * 40)
        text = repr(load_config())
        assert "pw@" not in text
        assert "t" * 40 not in text

    @pytest.mark.parametrize(
        "path", sorted(_DEFAULT_CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name
    )
    def test_every_repo_yaml_loads_as_overlay(self, path: Path) -> None:
        assert isinstance(load_config(env=path.stem), HydraConfig)

    def test_repo_config_dir_holds_only_base_yaml(self) -> None:
        assert sorted(p.name for p in _DEFAULT_CONFIG_DIR.glob("*.yaml")) == ["base.yaml"]

    def test_repo_base_yaml_has_no_placeholders(self) -> None:
        assert "${" not in (_DEFAULT_CONFIG_DIR / "base.yaml").read_text()

    def test_hydra_config_dir_and_env_select_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "base.yaml", "trading:\n  testnet: true\n")
        _write(tmp_path, "staging.yaml", "trading:\n  testnet: false\n")
        monkeypatch.setenv("HYDRA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("HYDRA_ENV", "staging")
        assert load_config().trading.testnet is False


# ---------------------------------------------------------------------------
# Precedence: env > overlay > base > defaults
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_env_beats_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(
            tmp_path,
            "base.yaml",
            """\
            trading:
              testnet: true
              default_symbols: [ETHUSDT]
            ml:
              models_dir: /a
            """,
        )
        monkeypatch.setenv("HYDRA_TRADING__TESTNET", "false")
        monkeypatch.setenv("HYDRA_ML__MODELS_DIR", "/b")
        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.trading.testnet is False
        assert cfg.ml.models_dir == "/b"
        assert cfg.trading.default_symbols == ["ETHUSDT"]

    def test_overlay_beats_base_and_keeps_siblings(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "trading:\n  testnet: true\n  paper_trading: true\n")
        _write(tmp_path, "live.yaml", "trading:\n  testnet: false\n")
        cfg = load_config(env="live", config_dir=tmp_path)
        assert cfg.trading.testnet is False
        assert cfg.trading.paper_trading is True

    def test_env_beats_overlay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path, "base.yaml", "logging:\n  level: INFO\n")
        _write(tmp_path, "dev.yaml", "logging:\n  level: DEBUG\n")
        monkeypatch.setenv("HYDRA_LOGGING__LEVEL", "warning")
        assert load_config(env="dev", config_dir=tmp_path).logging.level == "WARNING"

    def test_defaults_fill_an_empty_base(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "")
        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.trading.testnet is True
        assert cfg.api.port == 8000
        assert cfg.database_url is None

    def test_deep_merge(self) -> None:
        merged = _deep_merge({"a": {"x": 1, "y": 2}, "b": [1, 2]}, {"a": {"y": 9}, "b": [3]})
        assert merged == {"a": {"x": 1, "y": 9}, "b": [3]}


# ---------------------------------------------------------------------------
# Fail-fast: unknown keys, placeholders, retired and env-only keys
# ---------------------------------------------------------------------------


class TestFailFast:
    def test_nested_unknown_key_fails(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "trading:\n  tesnet: false\n")
        with pytest.raises(ValidationError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert (("trading", "tesnet"), "extra_forbidden") in _loc_types(exc_info.value)

    def test_platform_version_key_fails(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "platform:\n  name: hydra\n  version: '0.1.0'\n")
        with pytest.raises(ValidationError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert (("platform", "version"), "extra_forbidden") in _loc_types(exc_info.value)

    def test_telegram_key_now_fails(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "telegram:\n  bot_token: x\n")
        with pytest.raises(ValidationError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert (("telegram",), "extra_forbidden") in _loc_types(exc_info.value)

    @pytest.mark.parametrize(
        ("body", "env_var"),
        [
            ("database:\n  host: x\n", "DATABASE_URL"),
            ("redis:\n  url: redis://x\n", "REDIS_URL"),
            ("database_url: postgresql://x/y\n", "DATABASE_URL"),
            ("DATABASE_URL: postgresql://x/y\n", "DATABASE_URL"),
            ("credential_key: abc\n", "HYDRA_CREDENTIAL_KEY"),
            ("api_token: abc\n", "HYDRA_API_TOKEN"),
            ("exchanges: {}\n", "Settings page"),
        ],
    )
    def test_infra_keys_rejected_in_yaml(self, tmp_path: Path, body: str, env_var: str) -> None:
        path = _write(tmp_path, "base.yaml", body)
        with pytest.raises(ConfigError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert env_var in str(exc_info.value)
        assert str(path) in str(exc_info.value)

    def test_unresolved_placeholder_is_an_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "base.yaml", "api:\n  host: ${NOPE_X}\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert "NOPE_X" in str(exc_info.value)
        assert str(path) in str(exc_info.value)

    def test_placeholder_resolves_from_env_or_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "base.yaml", "api:\n  host: ${API_HOST_X}\n  port: ${API_PORT_X:8123}\n")
        monkeypatch.setenv("API_HOST_X", "127.0.0.2")
        cfg = load_config(env="base", config_dir=tmp_path)
        assert cfg.api.host == "127.0.0.2"
        assert cfg.api.port == 8123

    def test_missing_named_overlay_is_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "base.yaml", "")
        with pytest.raises(ConfigError, match=r"staging\.yaml"):
            load_config(env="staging", config_dir=tmp_path)

    def test_missing_base_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"base\.yaml"):
            load_config(env="base", config_dir=tmp_path)

    def test_bad_env_value_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path, "base.yaml", "")
        monkeypatch.setenv("HYDRA_API__PORT", "not-a-port")
        with pytest.raises(ValidationError) as exc_info:
            load_config(env="base", config_dir=tmp_path)
        assert ("api", "port") in {loc for loc, _ in _loc_types(exc_info.value)}

    def test_database_url_must_be_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "mysql://x/y")
        with pytest.raises(ValidationError, match="postgresql"):
            load_config()


# ---------------------------------------------------------------------------
# API auth fields (validated now, enforced by the API security layer)
# ---------------------------------------------------------------------------


class TestApiAuthFields:
    def test_defaults(self) -> None:
        cfg = load_config()
        assert cfg.api.auth_mode == "proxy"
        assert cfg.api.trusted_proxy_cidrs == []
        assert cfg.api.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]
        assert cfg.api_token is None

    def test_env_lists_are_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API__CORS_ORIGINS", "https://a.example, https://b.example")
        monkeypatch.setenv("HYDRA_API__TRUSTED_PROXY_CIDRS", "10.10.16.0/24,fd00::/64")
        cfg = load_config()
        assert cfg.api.cors_origins == ["https://a.example", "https://b.example"]
        assert cfg.api.trusted_proxy_cidrs[0] == IPv4Network("10.10.16.0/24")
        assert str(cfg.api.trusted_proxy_cidrs[1]) == "fd00::/64"

    @pytest.mark.parametrize("origin", ["*", "https://*.example.com", "trading.example.com"])
    def test_bad_cors_origin_fails(self, monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
        monkeypatch.setenv("HYDRA_API__CORS_ORIGINS", origin)
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        assert ("api", "cors_origins") in {loc for loc, _ in _loc_types(exc_info.value)}

    def test_invalid_cidr_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API__TRUSTED_PROXY_CIDRS", "10.0.0.1/33")
        with pytest.raises(ValidationError):
            load_config()

    def test_unknown_auth_mode_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API__AUTH_MODE", "none")
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        assert (("api", "auth_mode"), "literal_error") in _loc_types(exc_info.value)

    def test_short_api_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API_TOKEN", "short")
        with pytest.raises(ValidationError, match="32 characters"):
            load_config()

    def test_token_mode_needs_a_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API__AUTH_MODE", "token")
        with pytest.raises(ValidationError, match="HYDRA_API_TOKEN"):
            load_config()
        monkeypatch.setenv("HYDRA_API_TOKEN", "x" * 32)
        cfg = load_config()
        assert cfg.api.auth_mode == "token"
        assert cfg.api_token is not None
        assert cfg.api_token.get_secret_value() == "x" * 32


def test_repo_base_yaml_sections_match_model() -> None:
    raw = yaml.safe_load((_DEFAULT_CONFIG_DIR / "base.yaml").read_text())
    assert set(raw) <= set(HydraConfig.model_fields)
    assert "telegram" not in raw
