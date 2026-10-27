"""Hydra configuration: one validated model, loaded once at startup.

Precedence (highest first): environment, ``{HYDRA_ENV}.yaml``, ``base.yaml``, model defaults. Any
behaviour key can be overridden from the environment as ``HYDRA_<SECTION>__<KEY>``. Infrastructure
settings come only from the environment: ``DATABASE_URL``, ``REDIS_URL``, ``HYDRA_CREDENTIAL_KEY``,
``HYDRA_API_TOKEN`` and ``HYDRA_MCP_TOKEN``. Unknown keys, unknown or retired ``HYDRA_*`` variables,
unresolved ``${VAR}`` placeholders and infrastructure keys in YAML are startup errors. Validation
errors never echo input values, so a bad secret is named but not printed.
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

import hydra

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
CONFIG_DIR_ENV = "HYDRA_CONFIG_DIR"
CONFIG_ENV_ENV = "HYDRA_ENV"

_PLACEHOLDER = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")

# where a setting comes from instead -> the YAML keys (lower-case) that try to set it
_ENV_ONLY_SOURCES: dict[str, tuple[str, ...]] = {
    "DATABASE_URL": ("database", "database_url"),
    "REDIS_URL": ("redis", "redis_url"),
    "HYDRA_CREDENTIAL_KEY": ("credential_key", "hydra_credential_key"),
    "HYDRA_API_TOKEN": ("api_token", "hydra_api_token"),
    "HYDRA_MCP_TOKEN": ("mcp_token", "hydra_mcp_token"),
    "the Settings page (exchange keys are stored encrypted in the database)": ("exchanges",),
}
_ENV_ONLY_KEYS = {key: source for source, keys in _ENV_ONLY_SOURCES.items() for key in keys}

# HYDRA_* names read outside HydraConfig; the system.py ones go when P1-F-b drops its env reads
_OTHER_HYDRA_ENV = frozenset(
    {
        "HYDRA_ENV",
        "HYDRA_CONFIG_DIR",
        "HYDRA_REQUIRE_INFRA",
        "HYDRA_REDIS_URL",
        "HYDRA_TRADING_MODE",
        "HYDRA_MAX_STRATEGIES",
        "HYDRA_DEFAULT_PAIR",
        "HYDRA_DEFAULT_TIMEFRAME",
    }
)
_OTHER_HYDRA_ENV_PREFIXES = ("HYDRA_DB_",)
_RETIRED_HYDRA_ENV = {
    "HYDRA_TESTNET": "HYDRA_TRADING__TESTNET",
    "HYDRA_PAPER_TRADING": "HYDRA_TRADING__PAPER_TRADING",
    "HYDRA_CORS_ORIGINS": "HYDRA_API__CORS_ORIGINS",
    "HYDRA_AUTH_MODE": "HYDRA_API__AUTH_MODE",
    "HYDRA_TRUSTED_PROXY_CIDRS": "HYDRA_API__TRUSTED_PROXY_CIDRS",
    "HYDRA_LOG_LEVEL": "HYDRA_LOGGING__LEVEL",
    "HYDRA_LOG_FORMAT": "HYDRA_LOGGING__FORMAT",
    "HYDRA_MODELS_DIR": "HYDRA_ML__MODELS_DIR",
    "HYDRA_DATABASE_DSN": "DATABASE_URL",
    "HYDRA_DATABASE_URL": "DATABASE_URL",
}

_yaml_layer: ContextVar[dict[str, Any] | None] = ContextVar("hydra_yaml_layer", default=None)


class ConfigError(ValueError):
    """A config file cannot be turned into a valid HydraConfig."""


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _substitute_env_vars(value: str, source: Path) -> str:
    """Replace ``${VAR}`` / ``${VAR:default}``; an unset variable without a default is an error."""

    def _replacer(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise ConfigError(f"{source}: ${{{var_name}}} is not set and has no default")

    return _PLACEHOLDER.sub(_replacer, value)


def _resolve_env_vars(obj: Any, source: Path) -> Any:
    if isinstance(obj, str):
        return _substitute_env_vars(obj, source)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v, source) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(item, source) for item in obj]
    return obj


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse one config file, reject env-only keys and resolve placeholders."""
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open() as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    for key in data:
        source = _ENV_ONLY_KEYS.get(str(key).lower())
        if source is not None:
            raise ConfigError(f"{path}: '{key}' is not a config file key; set it via {source}")
    resolved: dict[str, Any] = _resolve_env_vars(data, path)
    return resolved


class _YamlLayer(PydanticBaseSettingsSource):
    """The merged YAML tree for the current load_config() call, ranked below the environment."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class TradingConfig(_Section):
    """Trading behaviour settings."""

    testnet: bool = True
    paper_trading: bool = True
    default_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["BTCUSDT"])
    live_poll_interval_s: int = Field(default=5, ge=1)
    live_reconcile_interval_s: int = Field(default=60, ge=1)

    @field_validator("default_symbols", mode="before")
    @classmethod
    def _csv(cls, v: Any) -> Any:
        return _split_csv(v)


class LoggingConfig(_Section):
    """Logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "colored"] = "json"

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


AuthMode = Literal["proxy", "token", "dev"]
ProxyNetwork = IPv4Network | IPv6Network


class ApiConfig(_Section):
    """REST API settings; auth fields are validated here and enforced by the security layer."""

    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    auth_mode: AuthMode = "proxy"
    trusted_proxy_cidrs: Annotated[list[ProxyNetwork], NoDecode] = Field(default_factory=list)

    @field_validator("cors_origins", "trusted_proxy_cidrs", mode="before")
    @classmethod
    def _csv(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("cors_origins")
    @classmethod
    def _explicit_origins(cls, v: list[str]) -> list[str]:
        for origin in v:
            if "*" in origin:
                raise ValueError(f"wildcard CORS origin {origin!r} is not allowed")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin {origin!r} must start with http:// or https://")
        return v

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _no_catch_all(cls, v: list[ProxyNetwork]) -> list[ProxyNetwork]:
        for network in v:
            if network.prefixlen == 0:
                raise ValueError(
                    f"api.trusted_proxy_cidrs: {network} trusts every peer; list the proxy network"
                )
        return v


class McpConfig(_Section):
    """MCP server settings; the token is env-only (``HYDRA_MCP_TOKEN``) and validated at the root.

    ``allowed_hosts`` is the SDK's DNS-rebinding allowlist (the ``Host`` header must match one
    entry); it is deliberately empty by default so a token without hosts is a config error, never
    a server that denies every request.
    """

    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _csv(cls, v: Any) -> Any:
        return _split_csv(v)


class TraderConfig(_Section):
    """The trader/api process split (D5).

    ``url`` points the dashboard API at the trader's internal bearer-gated API
    (``HYDRA_TRADER__URL``): unset is today's single process, set is the two-process layout
    (the API serves the UI and MCP, the trader holds the sessions). ``timeout_s`` bounds
    every remote call.
    """

    url: str | None = None
    timeout_s: float = Field(10.0, gt=0)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("trader.url must be an http:// or https:// URL of the trader process")
        return v


class MlConfig(_Section):
    """Machine learning settings."""

    models_dir: str = "/app/models"
    inference_timeout_ms: int = Field(50, gt=0)


class PlatformConfig(_Section):
    """Platform metadata. The version is the package version, never a config value."""

    name: str = "hydra"

    @property
    def version(self) -> str:
        return hydra.__version__


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class HydraConfig(BaseSettings):
    """Root configuration model; build it with :func:`load_config`."""

    model_config = SettingsConfigDict(
        env_prefix="HYDRA_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
    )

    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    ml: MlConfig = Field(default_factory=MlConfig)
    trader: TraderConfig = Field(default_factory=TraderConfig)

    database_url: SecretStr | None = Field(None, validation_alias="DATABASE_URL")
    redis_url: SecretStr | None = Field(None, validation_alias="REDIS_URL")
    credential_key: SecretStr | None = Field(None, validation_alias="HYDRA_CREDENTIAL_KEY")
    api_token: SecretStr | None = Field(None, validation_alias="HYDRA_API_TOKEN")
    mcp_token: SecretStr | None = Field(None, validation_alias="HYDRA_MCP_TOKEN")

    @field_validator(
        "database_url", "redis_url", "credential_key", "api_token", "mcp_token", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("database_url")
    @classmethod
    def _postgres_dsn(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a postgresql:// DSN")
        return v

    @field_validator("redis_url")
    @classmethod
    def _redis_url(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().startswith(
            ("redis://", "rediss://", "unix://")
        ):
            raise ValueError("REDIS_URL must be a redis:// URL")
        return v

    @field_validator("api_token")
    @classmethod
    def _long_token(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 32:
            raise ValueError("HYDRA_API_TOKEN must be at least 32 characters")
        return v

    @field_validator("mcp_token")
    @classmethod
    def _long_mcp_token(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 32:
            raise ValueError("HYDRA_MCP_TOKEN must be at least 32 characters")
        return v

    @model_validator(mode="after")
    def _trader_url_needs_token(self) -> HydraConfig:
        if self.trader.url is not None and self.api_token is None:
            raise ValueError(
                "HYDRA_TRADER__URL requires HYDRA_API_TOKEN: the trader's internal API is "
                "bearer-gated with the same token"
            )
        return self

    @model_validator(mode="after")
    def _token_mode_has_token(self) -> HydraConfig:
        if self.api.auth_mode == "token" and self.api_token is None:
            raise ValueError("api.auth_mode 'token' requires HYDRA_API_TOKEN")
        return self

    @model_validator(mode="after")
    def _mcp_needs_allowed_hosts(self) -> HydraConfig:
        if self.mcp_token is not None and not self.mcp.allowed_hosts:
            raise ValueError(
                "HYDRA_MCP_TOKEN requires HYDRA_MCP__ALLOWED_HOSTS: the MCP transport's "
                "DNS-rebinding protection denies every request without an allowed host"
            )
        return self

    @model_validator(mode="after")
    def _mcp_token_is_not_api_token(self) -> HydraConfig:
        if (
            self.mcp_token is not None
            and self.api_token is not None
            and self.mcp_token.get_secret_value().strip()
            == self.api_token.get_secret_value().strip()
        ):
            raise ValueError(
                "HYDRA_MCP_TOKEN must differ from HYDRA_API_TOKEN: the API token is a full "
                "service principal over the REST API"
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, _YamlLayer(settings_cls, _yaml_layer.get() or {}))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def config_dir_from_env() -> Path:
    """``HYDRA_CONFIG_DIR`` when set, else the repo/image ``config/`` directory."""
    raw = os.environ.get(CONFIG_DIR_ENV)
    return Path(raw) if raw else _DEFAULT_CONFIG_DIR


def _check_hydra_env_names() -> None:
    """Reject ``HYDRA_*`` variables nothing reads; pydantic-settings would drop them silently."""
    sections = {
        name.upper()
        for name, field in HydraConfig.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, _Section)
    }
    aliases = {
        str(field.validation_alias).upper()
        for field in HydraConfig.model_fields.values()
        if field.validation_alias is not None
    }
    for raw_name in sorted(os.environ):
        name = raw_name.upper()
        if not name.startswith("HYDRA_") or name in aliases or name in _OTHER_HYDRA_ENV:
            continue
        if name.startswith(_OTHER_HYDRA_ENV_PREFIXES):
            continue
        section, nested, _ = name.removeprefix("HYDRA_").partition("__")
        if nested and section in sections:
            continue
        replacement = _RETIRED_HYDRA_ENV.get(name)
        if replacement is not None:
            raise ConfigError(f"{raw_name} is no longer read; set {replacement} instead")
        raise ConfigError(
            f"{raw_name} is not a Hydra setting; overrides are HYDRA_<SECTION>__<KEY> "
            f"(sections: {', '.join(sorted(s.lower() for s in sections))})"
        )


def load_config(env: str | None = None, config_dir: str | Path | None = None) -> HydraConfig:
    """Load and validate the config once: env > ``{env}.yaml`` > ``base.yaml`` > defaults.

    ``env`` defaults to ``HYDRA_ENV`` (or ``"base"``: base.yaml only); ``config_dir`` defaults to
    ``HYDRA_CONFIG_DIR``. A named overlay that does not exist is an error, and so is an unknown or
    retired ``HYDRA_*`` variable.
    """
    _check_hydra_env_names()
    config_path = Path(config_dir) if config_dir else config_dir_from_env()
    overlay = env if env is not None else os.environ.get(CONFIG_ENV_ENV, "base")
    data = _load_yaml(config_path / "base.yaml")
    if overlay != "base":
        data = _deep_merge(data, _load_yaml(config_path / f"{overlay}.yaml"))
    token = _yaml_layer.set(data)
    try:
        return HydraConfig()
    finally:
        _yaml_layer.reset(token)
