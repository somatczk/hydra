"""Shared dashboard dependencies: the HydraConfig loaded once by the app lifespan."""

from __future__ import annotations

from fastapi import FastAPI, Request

from hydra.core.config import HydraConfig

_bound: HydraConfig | None = None


class ConfigNotLoadedError(RuntimeError):
    """A route needed the config before the lifespan loaded it."""


def bind_config(app: FastAPI, cfg: HydraConfig) -> None:
    """Called by the lifespan only: ``app.state.config`` and :func:`current_config`."""
    global _bound
    app.state.config = cfg
    _bound = cfg


def get_config(request: Request) -> HydraConfig:
    """FastAPI dependency: the validated config of the running app."""
    cfg = getattr(request.app.state, "config", None)
    if not isinstance(cfg, HydraConfig):
        raise ConfigNotLoadedError("HydraConfig not loaded; the app lifespan has not run")
    return cfg


def current_config() -> HydraConfig:
    """The config bound by the lifespan, for helpers that have no request."""
    if _bound is None:
        raise ConfigNotLoadedError("HydraConfig not loaded; the app lifespan has not run")
    return _bound
