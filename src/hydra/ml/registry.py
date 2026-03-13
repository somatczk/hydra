"""In-memory model registry for tracking model versions and lifecycle.

Manages model versioning, stage promotion, and retrieval. Can later be backed
by MLflow for persistent storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

from hydra.ml.training import TrainedModel

# ---------------------------------------------------------------------------
# ModelInfo dataclass
# ---------------------------------------------------------------------------


@dataclass
class ModelInfo:
    """Metadata about a registered model."""

    model_id: str
    name: str
    version: str
    stage: str
    metrics: dict[str, float]
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal registry entry
# ---------------------------------------------------------------------------


@dataclass
class _RegistryEntry:
    """Internal storage pairing a trained model with its registry info."""

    model: TrainedModel
    info: ModelInfo = field(repr=False)


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """In-memory model registry tracking versions and lifecycle stages.

    Models progress through stages: ``"none"`` -> ``"staging"`` ->
    ``"production"`` -> ``"archived"``.
    """

    _VALID_STAGES: ClassVar[set[str]] = {"none", "staging", "production", "archived"}

    def __init__(self) -> None:
        self._models: dict[str, list[_RegistryEntry]] = {}

    def register(self, model: TrainedModel, name: str) -> str:
        """Register a trained model under *name* and return its model_id.

        The version is auto-incremented as ``"v1"``, ``"v2"``, etc.
        """
        entries = self._models.setdefault(name, [])
        version = f"v{len(entries) + 1}"
        model_id = f"{name}/{version}"

        info = ModelInfo(
            model_id=model_id,
            name=name,
            version=version,
            stage="none",
            metrics=dict(model.metrics),
            created_at=datetime.now(tz=UTC),
        )
        entries.append(_RegistryEntry(model=model, info=info))
        return model_id

    def get_model(self, name: str, version: str | None = None) -> TrainedModel | None:
        """Retrieve a model by name and optional version.

        If *version* is ``None``, the latest version is returned.
        """
        entries = self._models.get(name)
        if not entries:
            return None

        if version is None:
            return entries[-1].model

        for entry in entries:
            if entry.info.version == version:
                return entry.model
        return None

    def list_models(self, name: str | None = None) -> list[ModelInfo]:
        """List model info records, optionally filtered by *name*."""
        result: list[ModelInfo] = []
        if name is not None:
            for entry in self._models.get(name, []):
                result.append(entry.info)
        else:
            for entries in self._models.values():
                for entry in entries:
                    result.append(entry.info)
        return result

    def promote(self, model_id: str, stage: str) -> None:
        """Promote a model to the given stage.

        Parameters
        ----------
        model_id:
            Model identifier in the form ``"name/version"``.
        stage:
            Target stage: ``"staging"``, ``"production"``, or ``"archived"``.

        Raises
        ------
        ValueError
            If the model_id is not found or the stage is invalid.
        """
        if stage not in self._VALID_STAGES:
            msg = f"Invalid stage '{stage}'. Must be one of {self._VALID_STAGES}"
            raise ValueError(msg)

        entry = self._find_entry(model_id)
        if entry is None:
            msg = f"Model '{model_id}' not found"
            raise ValueError(msg)

        # If promoting to production, demote current production model to archived
        if stage == "production":
            name = entry.info.name
            for e in self._models.get(name, []):
                if e.info.stage == "production" and e.info.model_id != model_id:
                    e.info.stage = "archived"

        entry.info.stage = stage

    def get_production_model(self, name: str) -> TrainedModel | None:
        """Return the model currently in ``"production"`` stage for *name*."""
        for entry in self._models.get(name, []):
            if entry.info.stage == "production":
                return entry.model
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_entry(self, model_id: str) -> _RegistryEntry | None:
        """Locate a registry entry by model_id."""
        for entries in self._models.values():
            for entry in entries:
                if entry.info.model_id == model_id:
                    return entry
        return None
