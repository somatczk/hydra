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
    DataConfig,
    EventsConfig,
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
        assert cfg.redis_url is not None
        assert cfg.redis_url.get_secret_value() == "redis://hydra-redis:6379"
        assert cfg.credential_key is not None
        assert cfg.credential_key.get_secret_value() == key
        assert cfg.api.cors_origins == ["https://trading.example.com"]
        assert cfg.trading.paper_trading is True
        assert cfg.trading.testnet is True
        assert cfg.logging.level == "INFO"
        assert cfg.ml.inference_timeout_ms == 5

    def test_secrets_do_not_leak_into_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", ROMULUS_DSN)
        monkeypatch.setenv("REDIS_URL", "redis://:R3disPw@hydra-redis:6379")
        monkeypatch.setenv("HYDRA_API_TOKEN", "t" * 40)
        cfg = load_config()
        for text in (repr(cfg), str(cfg.model_dump())):
            assert "pw@" not in text
            assert "R3disPw" not in text
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
            ("mcp_token: abc\n", "HYDRA_MCP_TOKEN"),
            ("hydra_mcp_token: abc\n", "HYDRA_MCP_TOKEN"),
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

    def test_validation_errors_do_not_print_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_API_TOKEN", "tok_SECRETabcdefghijklmnopqrstu")
        monkeypatch.setenv("DATABASE_URL", "mysql://hydra:S3cretPassw0rd@db:5432/hydra")
        monkeypatch.setenv("REDIS_URL", "http://:R3disPw@redis:6379")
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        text = str(exc_info.value)
        assert {loc for loc, _ in _loc_types(exc_info.value)} >= {
            ("HYDRA_API_TOKEN",),
            ("DATABASE_URL",),
            ("REDIS_URL",),
        }
        for secret in ("SECRETabc", "tPassw0rd", "S3cret", "R3disPw"):
            assert secret not in text

    def test_model_level_error_does_not_print_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("DATABASE_URL", ROMULUS_DSN)
        monkeypatch.setenv("REDIS_URL", "redis://hydra-redis:6379")
        monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", key)
        monkeypatch.setenv("HYDRA_API__AUTH_MODE", "token")
        with pytest.raises(ValidationError, match="HYDRA_API_TOKEN") as exc_info:
            load_config()
        text = str(exc_info.value)
        assert key[-12:] not in text
        assert key[:12] not in text
        assert "pw@" not in text


# ---------------------------------------------------------------------------
# HYDRA_* variables outside the model
# ---------------------------------------------------------------------------


class TestHydraEnvNames:
    @pytest.mark.parametrize(
        ("name", "replacement"),
        [
            ("HYDRA_TESTNET", "HYDRA_TRADING__TESTNET"),
            ("HYDRA_CORS_ORIGINS", "HYDRA_API__CORS_ORIGINS"),
            ("HYDRA_AUTH_MODE", "HYDRA_API__AUTH_MODE"),
            ("HYDRA_TRUSTED_PROXY_CIDRS", "HYDRA_API__TRUSTED_PROXY_CIDRS"),
            ("HYDRA_LOG_LEVEL", "HYDRA_LOGGING__LEVEL"),
            ("HYDRA_DATABASE_DSN", "DATABASE_URL"),
        ],
    )
    def test_retired_name_fails_with_its_replacement(
        self, monkeypatch: pytest.MonkeyPatch, name: str, replacement: str
    ) -> None:
        monkeypatch.setenv(name, "dev")
        with pytest.raises(ConfigError, match=rf"^{name} is no longer read; set {replacement}"):
            load_config()

    @pytest.mark.parametrize(
        "name", ["HYDRA_TRADING_TESTNET", "HYDRA_FOO__BAR", "hydra_typo", "HYDRA_MCP_FOO"]
    )
    def test_unknown_name_fails(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv(name, "x")
        with pytest.raises(ConfigError, match=rf"^{name} is not a Hydra setting"):
            load_config()

    def test_known_names_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in {
            "HYDRA_ENV": "base",
            "HYDRA_CONFIG_DIR": str(_DEFAULT_CONFIG_DIR),
            "HYDRA_REQUIRE_INFRA": "1",
            "HYDRA_DB_HOST": "localhost",
            "HYDRA_REDIS_URL": "redis://localhost:6379",
            "HYDRA_TRADING_MODE": "paper",
            "HYDRA_MAX_STRATEGIES": "5",
            "HYDRA_DEFAULT_PAIR": "BTC/USDT",
            "HYDRA_DEFAULT_TIMEFRAME": "1h",
            "hydra_api__port": "9001",
        }.items():
            monkeypatch.setenv(name, value)
        assert load_config().api.port == 9001


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
        monkeypatch.setenv("HYDRA_TRADING__DEFAULT_SYMBOLS", "BTCUSDT, ETHUSDT")
        cfg = load_config()
        assert cfg.trading.default_symbols == ["BTCUSDT", "ETHUSDT"]
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

    @pytest.mark.parametrize("cidrs", ["0.0.0.0/0", "10.10.16.0/24,::/0"])
    def test_catch_all_cidr_fails(self, monkeypatch: pytest.MonkeyPatch, cidrs: str) -> None:
        monkeypatch.setenv("HYDRA_API__TRUSTED_PROXY_CIDRS", cidrs)
        with pytest.raises(ValidationError, match="trusts every peer") as exc_info:
            load_config()
        assert ("api", "trusted_proxy_cidrs") in {loc for loc, _ in _loc_types(exc_info.value)}

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


# ---------------------------------------------------------------------------
# MCP fields (env-only token; the transport mounts only when it is set)
# ---------------------------------------------------------------------------


class TestMcpFields:
    def test_short_mcp_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_MCP_TOKEN", "short")
        monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "trading.example.com")
        with pytest.raises(ValidationError, match="32 characters"):
            load_config()

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_token_is_unset(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("HYDRA_MCP_TOKEN", value)
        cfg = load_config()
        assert cfg.mcp_token is None
        assert cfg.mcp.allowed_hosts == []

    def test_token_without_allowed_hosts_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_MCP_TOKEN", "x" * 48)
        with pytest.raises(ValidationError, match="HYDRA_MCP__ALLOWED_HOSTS"):
            load_config()

    def test_token_equal_to_api_token_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        token = "y" * 48
        monkeypatch.setenv("HYDRA_API_TOKEN", token)
        monkeypatch.setenv("HYDRA_MCP_TOKEN", token)
        monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "trading.example.com")
        with pytest.raises(ValidationError, match="must differ from HYDRA_API_TOKEN"):
            load_config()

    @pytest.mark.parametrize(
        ("api_value", "mcp_value"),
        [
            pytest.param("y" * 48, " " + "y" * 48, id="mcp-token-padded-left"),
            pytest.param("y" * 48, "y" * 48 + " ", id="mcp-token-padded-right"),
            pytest.param(" " + "y" * 48, "y" * 48, id="api-token-padded-left"),
        ],
    )
    def test_whitespace_padded_tokens_must_differ(
        self, monkeypatch: pytest.MonkeyPatch, api_value: str, mcp_value: str
    ) -> None:
        """Copy-paste padding must not defeat the A7 guard: strip-compare both sides."""
        monkeypatch.setenv("HYDRA_API_TOKEN", api_value)
        monkeypatch.setenv("HYDRA_MCP_TOKEN", mcp_value)
        monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "trading.example.com")
        with pytest.raises(ValidationError, match="must differ from HYDRA_API_TOKEN"):
            load_config()

    def test_valid_token_and_hosts_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_MCP_TOKEN", "z" * 48)
        monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "trading.example.com")
        cfg = load_config()
        assert cfg.mcp_token is not None
        assert cfg.mcp_token.get_secret_value() == "z" * 48
        assert cfg.mcp.allowed_hosts == ["trading.example.com"]

    def test_allowed_hosts_csv_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_MCP_TOKEN", "z" * 48)
        monkeypatch.setenv("HYDRA_MCP__ALLOWED_HOSTS", "trading.example.com, localhost , ,")
        cfg = load_config()
        assert cfg.mcp.allowed_hosts == ["trading.example.com", "localhost"]

    def test_unknown_nested_mcp_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_MCP__FOO", "x")
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        assert (("mcp", "foo"), "extra_forbidden") in _loc_types(exc_info.value)


class TestDataFields:
    """The on-demand fetch section (P6-B1): the folded budget/patience/cap defaults."""

    def test_defaults_are_the_folded_decisions(self) -> None:
        cfg = DataConfig()
        assert cfg.auto_fetch_enabled is True
        assert cfg.fetch_weight_per_min == 900
        assert cfg.fetch_patience_s == 900
        assert cfg.fetch_max_bars == 2_000_000
        assert cfg.cache_max_gb == 10.0
        assert cfg.sweep_interval_s == 3600
        assert cfg.row_bytes_estimate == 205
        assert load_config().data == cfg

    def test_env_overrides_thread_the_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_DATA__AUTO_FETCH_ENABLED", "false")
        monkeypatch.setenv("HYDRA_DATA__FETCH_WEIGHT_PER_MIN", "450")
        monkeypatch.setenv("HYDRA_DATA__FETCH_PATIENCE_S", "300")
        monkeypatch.setenv("HYDRA_DATA__FETCH_MAX_BARS", "100000")
        monkeypatch.setenv("HYDRA_DATA__CACHE_MAX_GB", "0.5")
        monkeypatch.setenv("HYDRA_DATA__SWEEP_INTERVAL_S", "120")
        monkeypatch.setenv("HYDRA_DATA__ROW_BYTES_ESTIMATE", "192")
        cfg = load_config()
        assert cfg.data.auto_fetch_enabled is False
        assert cfg.data.fetch_weight_per_min == 450
        assert cfg.data.fetch_patience_s == 300
        assert cfg.data.fetch_max_bars == 100_000
        assert cfg.data.cache_max_gb == 0.5
        assert cfg.data.sweep_interval_s == 120
        assert cfg.data.row_bytes_estimate == 192

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("fetch_weight_per_min", 0, id="weight-zero"),
            pytest.param("fetch_weight_per_min", 6001, id="weight-over-binance-ip-budget"),
            pytest.param("fetch_patience_s", 0, id="patience-zero"),
            pytest.param("fetch_max_bars", 0, id="max-bars-zero"),
            pytest.param("cache_max_gb", -0.1, id="cache-negative"),
            pytest.param("cache_max_gb", float("inf"), id="cache-infinite"),
            pytest.param("sweep_interval_s", 0, id="sweep-interval-zero"),
            pytest.param("row_bytes_estimate", 0, id="row-bytes-zero"),
        ],
    )
    def test_out_of_range_knobs_fail(self, field: str, value: int | float) -> None:
        with pytest.raises(ValidationError):
            DataConfig(**{field: value})

    def test_unknown_data_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_DATA__NOPE", "x")
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        assert (("data", "nope"), "extra_forbidden") in _loc_types(exc_info.value)


class TestBacktestFields:
    def test_the_job_lease_defaults_to_three_minutes(self) -> None:
        assert load_config().backtest.job_lease_s == 180

    def test_the_job_lease_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_BACKTEST__JOB_LEASE_S", "600")
        assert load_config().backtest.job_lease_s == 600

    def test_a_lease_near_the_10s_tick_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_BACKTEST__JOB_LEASE_S", "30")
        with pytest.raises(ValidationError):
            load_config()


class TestEventsFields:
    """The event fan-out flag (P7-E): opt-in, default memory, redis requires REDIS_URL."""

    def test_the_default_bus_is_memory(self) -> None:
        assert load_config().events.bus == "memory"

    def test_the_env_override_selects_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_EVENTS__BUS", "redis")
        monkeypatch.setenv("REDIS_URL", "redis://hydra-redis:6379")
        cfg = load_config()
        assert cfg.events.bus == "redis"
        assert cfg.redis_url is not None

    def test_the_redis_bus_without_redis_url_names_both(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            HydraConfig(events=EventsConfig(bus="redis"))
        message = str(exc_info.value.errors()[0]["msg"])
        assert "events.bus 'redis' requires REDIS_URL" in message

    def test_an_unknown_bus_value_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_EVENTS__BUS", "kafka")
        with pytest.raises(ValidationError):
            load_config()

    def test_an_unknown_events_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYDRA_EVENTS__NOPE", "x")
        with pytest.raises(ValidationError) as exc_info:
            load_config()
        assert (("events", "nope"), "extra_forbidden") in _loc_types(exc_info.value)


def test_repo_base_yaml_sections_match_model() -> None:
    raw = yaml.safe_load((_DEFAULT_CONFIG_DIR / "base.yaml").read_text())
    assert set(raw) <= set(HydraConfig.model_fields)
    assert "telegram" not in raw
