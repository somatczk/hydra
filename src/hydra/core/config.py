"""Pydantic-based configuration with YAML layered loading and env substitution.

Configuration is loaded in layers: base.yaml <- {env}.yaml <- environment variables.
YAML values support ``${VAR}`` and ``${VAR:default}`` environment variable
substitution.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Default config directory — can be overridden via HYDRA_CONFIG_DIR
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# Pattern for ${VAR} and ${VAR:default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def _substitute_env_vars(value: str) -> str:
    """Replace ``${VAR}`` / ``${VAR:default}`` placeholders with env values."""

    def _replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        return match.group(0)  # leave unchanged if no env and no default

    return _ENV_VAR_PATTERN.sub(_replacer, value)


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively resolve ``${VAR}`` placeholders in a parsed YAML tree."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
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
    """Load a YAML file and resolve env vars."""
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    return _resolve_env_vars(data)


# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


class DatabaseConfig(BaseModel):
    """Database connection settings."""

    host: str = "localhost"
    port: int = 5432
    name: str = "hydra"
    user: str = "hydra"
    password: str = ""

    @property
    def dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        )


class RedisConfig(BaseModel):
    """Redis connection settings."""

    url: str = "redis://localhost:6379"


class ExchangeConfig(BaseModel):
    """Single exchange configuration."""

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    market_types: list[str] = Field(default_factory=lambda: ["spot"])


class TradingConfig(BaseModel):
    """Trading behaviour settings."""

    testnet: bool = True
    paper_trading: bool = True
    default_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: str = "INFO"
    format: str = "json"


class ApiConfig(BaseModel):
    """REST API settings."""

    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


class TelegramConfig(BaseModel):
    """Telegram bot settings."""

    bot_token: str = ""
    chat_id: str = ""


class MlConfig(BaseModel):
    """Machine learning settings."""

    models_dir: str = "/app/models"
    inference_timeout_ms: int = 5


class PlatformConfig(BaseModel):
    """Platform metadata."""

    name: str = "hydra"
    version: str = "0.1.0"


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class HydraConfig(BaseSettings):
    """Root configuration model.

    Loads from: base.yaml <- {env}.yaml <- environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="HYDRA_",
        env_nested_delimiter="__",
    )

    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    exchanges: dict[str, ExchangeConfig] = Field(default_factory=dict)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    ml: MlConfig = Field(default_factory=MlConfig)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(
    env: str = "base",
    config_dir: str | Path | None = None,
) -> HydraConfig:
    """Load layered YAML config: base.yaml <- {env}.yaml <- env vars.

    Parameters
    ----------
    env:
        Environment name (e.g. ``"live"``, ``"backtest"``). ``"base"``
        means only the base config is loaded.
    config_dir:
        Path to the config directory.  Defaults to ``<repo>/config/``.
    """
    config_path = Path(config_dir) if config_dir else _DEFAULT_CONFIG_DIR

    # Layer 1: base.yaml
    data = _load_yaml(config_path / "base.yaml")

    # Layer 2: {env}.yaml (skip if env == "base")
    if env != "base":
        overlay = _load_yaml(config_path / f"{env}.yaml")
        data = _deep_merge(data, overlay)

    return HydraConfig(**data)
