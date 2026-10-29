"""ONNX Runtime model inference for real-time prediction.

Two surfaces. :class:`ModelInference` is the raw session wrapper the older
serving code used. The P5-C bridge — ``attach_production`` / ``attach_artifact``
— is what every model consumer (the veto overlay at the signal seam and the
ML-native strategy) goes through: registry name → Production version → artifact
on the models mount → onnxruntime session, every step fail-closed and loud
(no model, no artifact, unreadable bytes or a feature-spec mismatch all
refuse). ``predict_up`` answers P(up) from the classifier's probabilities
output, never a silent fallback.

All onnxruntime imports are lazy (inside methods).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.registry import ModelRegistry

# ---------------------------------------------------------------------------
# Internal model wrapper
# ---------------------------------------------------------------------------


@dataclass
class _LoadedModel:
    """Internal wrapper for a loaded ONNX model session."""

    session: Any  # onnxruntime.InferenceSession (ships no type stubs)
    model_path: Path
    model_name: str
    input_name: str
    loaded_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ModelInference
# ---------------------------------------------------------------------------


class ModelInference:
    """ONNX Runtime inference engine for loaded models.

    Parameters
    ----------
    models_dir:
        Directory to watch for ONNX model files.
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._models: dict[str, _LoadedModel] = {}

    def load_model(self, model_path: Path, model_name: str) -> None:
        """Load an ONNX model from *model_path* under *model_name*.

        Replaces any previously loaded model with the same name.
        """
        import onnxruntime as ort

        sess = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        input_name = sess.get_inputs()[0].name

        self._models[model_name] = _LoadedModel(
            session=sess,
            model_path=model_path,
            model_name=model_name,
            input_name=input_name,
        )

        # Check for forward-look bias in older models
        import logging as _logging

        _logger = _logging.getLogger(__name__)
        try:
            meta = sess.get_modelmeta()
            custom = meta.custom_metadata_map if meta else {}
            feature_version = int(custom.get("feature_version", "0"))
            if feature_version < 2:
                _logger.warning(
                    "Model '%s' (feature_version=%d) was trained with "
                    "forward-look bias in multi-TF features. Retrain recommended.",
                    model_name,
                    feature_version,
                )
        except Exception:
            _logger.warning(
                "Model '%s' has no feature_version metadata. "
                "May have been trained with forward-look bias. Retrain recommended.",
                model_name,
            )

    def predict(self, model_name: str, features: ndarray) -> ndarray | None:
        """Run inference on the named model.

        Parameters
        ----------
        model_name:
            Name of a previously loaded model.
        features:
            Input features as a numpy array.  Will be cast to float32.

        Returns
        -------
        ndarray | None
            Prediction array, or ``None`` if the model is missing or fails.
        """
        loaded = self._models.get(model_name)
        if loaded is None:
            return None

        try:
            session = loaded.session
            input_data = features.astype(np.float32)
            if input_data.ndim == 1:
                input_data = input_data.reshape(1, -1)

            t0 = time.monotonic()
            results = session.run(None, {loaded.input_name: input_data})
            try:
                from hydra.core.metrics import observe_ml_inference_latency

                observe_ml_inference_latency(model_name, time.monotonic() - t0)
            except Exception:
                pass
            return np.asarray(results[0])
        except Exception:
            # Fallback: strategy continues without ML
            return None

    def _run(self, model_name: str, features: ndarray) -> list[ndarray]:
        """Run the named model on *features*, raising on any failure."""
        loaded = self._models.get(model_name)
        if loaded is None:
            raise KeyError(f"model '{model_name}' is not loaded")

        session = loaded.session
        input_data = features.astype(np.float32)
        if input_data.ndim == 1:
            input_data = input_data.reshape(1, -1)

        t0 = time.monotonic()
        results = session.run(None, {loaded.input_name: input_data})
        try:
            from hydra.core.metrics import observe_ml_inference_latency

            observe_ml_inference_latency(model_name, time.monotonic() - t0)
        except Exception:
            pass
        return [np.asarray(r) for r in results]

    def predict_outputs(self, model_name: str, features: ndarray) -> list[ndarray]:
        """Every output the named model answers, in declared order.

        Unlike :meth:`predict` this raises on failure: the P5-C consumers
        treat a model that cannot answer as a refusal (fail-closed), never a
        missing prediction.
        """
        return self._run(model_name, features)

    def get_model_info(self, model_name: str) -> dict[str, Any] | None:
        """Return metadata for a loaded model, or ``None`` if not found."""
        loaded = self._models.get(model_name)
        if loaded is None:
            return None

        return {
            "model_name": loaded.model_name,
            "model_path": str(loaded.model_path),
            "input_name": loaded.input_name,
            "loaded_at": loaded.loaded_at,
        }

    def reload_model(self, model_name: str) -> None:
        """Hot-reload a model from disk.

        Re-reads the ONNX file at the path originally used for loading.
        If the model was not previously loaded, this is a no-op.
        """
        loaded = self._models.get(model_name)
        if loaded is None:
            return
        self.load_model(loaded.model_path, model_name)


# ---------------------------------------------------------------------------
# The P5-C serving bridge: registry name -> attached Production model
# ---------------------------------------------------------------------------


class MlServingError(Exception):
    """A fail-closed serving refusal; ``kind`` names which leg refused.

    ``not_promoted`` — the name has no Production version in the registry.
    ``artifact_missing`` — the Production artifact is not on the models mount
    (the deploy risk the §7 battery pins: a missing mount refuses loudly,
    it never turns the overlay into a silent pass-through).
    ``invalid_model`` — the artifact cannot be loaded or does not answer the
    classifier output contract. ``spec_mismatch`` — the artifact's embedded
    feature spec is absent or does not match the frozen spec (SM-11).
    ``unavailable`` — the registry cannot be reached at all.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


_SAFE_ARTIFACT_FILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.onnx$")
_PROBABILITIES_OUTPUT = "probabilities"

# (name, registry version) -> the loaded model; the process attach cache
_ATTACHED: dict[tuple[str, int], AttachedModel] = {}


def _artifact_path(models_dir: Path, artifact_file: str) -> Path:
    """The artifact inside *models_dir*; a stored name that escapes it refuses."""
    if not _SAFE_ARTIFACT_FILE.match(artifact_file) or ".." in artifact_file:
        raise MlServingError("invalid_model", f"stored artifact name {artifact_file!r} is unsafe")
    path = (models_dir / artifact_file).resolve()
    if not path.is_file():
        raise MlServingError(
            "artifact_missing",
            f"the Production artifact {artifact_file} of model on mount "
            f"{models_dir} is missing: the models directory is not mounted or "
            "the artifact was removed — serving refuses rather than trade "
            "without the model",
        )
    return path


def _check_artifact_sha(path: Path, expected: str) -> None:
    """The bytes on the mount must hash to the sha256 the registry pinned."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise MlServingError(
            "invalid_model",
            f"artifact {path.name} hashes to {actual} but the registry pinned "
            f"{expected}: the models mount serves different bytes than the "
            "registered version",
        )


def _check_feature_spec(metadata: dict[str, str]) -> int:
    """The artifact's embedded spec must match the frozen spec exactly (SM-11)."""
    spec = DEFAULT_FEATURE_SPEC
    try:
        names = json.loads(metadata.get("feature_names", ""))
        version = int(metadata.get("spec_version", ""))
    except (TypeError, ValueError) as exc:
        raise MlServingError(
            "spec_mismatch",
            f"the artifact carries an unreadable feature spec ({metadata!r}); "
            f"serving needs one pinned to the frozen spec v{spec.spec_version}",
        ) from exc
    if names != list(spec.feature_names) or version != spec.spec_version:
        raise MlServingError(
            "spec_mismatch",
            f"the artifact's embedded feature spec (v{version}, {len(names)} "
            f"features) does not match the frozen spec v{spec.spec_version} "
            f"({len(spec.feature_names)} features): retrain or promote a "
            "version trained on the current spec",
        )
    return version


@dataclass(frozen=True)
class AttachedModel:
    """One loaded Production artifact and the P(up) contract to read it.

    ``predict_up`` runs the live vector and answers the model's confidence in
    the up class, raising :class:`MlServingError` on any failure — the overlay
    turns that refusal into a veto, never a silent pass.
    """

    name: str
    version: int
    artifact_file: str
    spec_version: int
    inference: ModelInference
    output_index: int
    """Which declared output carries the confidence: the ``probabilities``
    output when the model names one (a real xgboost export answers
    ``[label, probabilities]`` — the confidence lives in the SECOND), else 0."""

    def predict_up(self, vector: ndarray) -> float:
        """P(up) for one live feature vector (the classifier's class-1 column)."""
        outputs = self.inference.predict_outputs(self.name, vector)
        values = np.asarray(outputs[self.output_index], dtype=np.float64)
        if values.shape[-1:] == (2,):
            # a two-column row [P(down), P(up)]: the up column is the confidence
            row = values.reshape(-1, 2)[-1]
            return float(row[1])
        scalar = values.reshape(-1)
        if scalar.size != 1:
            raise MlServingError(
                "invalid_model",
                f"model '{self.name}' answered {scalar.size} values where the "
                "confidence contract needs exactly one (or a two-class "
                "probabilities row)",
            )
        return float(scalar[0])


def _load_attached(path: Path, name: str, version: int) -> AttachedModel:
    """Load and check one artifact synchronously (called off the event loop)."""
    import onnxruntime as ort

    try:
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise MlServingError(
            "invalid_model", f"artifact {path.name} cannot be loaded by onnxruntime: {exc!r}"
        ) from exc
    meta = session.get_modelmeta()
    metadata = dict(meta.custom_metadata_map) if meta is not None else {}
    spec_version = _check_feature_spec(metadata)
    outputs = session.get_outputs()
    output_index = next((i for i, o in enumerate(outputs) if o.name == _PROBABILITIES_OUTPUT), None)
    if output_index is None and len(outputs) != 1:
        raise MlServingError(
            "invalid_model",
            f"model '{name}' answers {len(outputs)} outputs with no "
            f"'{_PROBABILITIES_OUTPUT}' among them; serving reads either the "
            "classifier's probabilities output or exactly one confidence value",
        )
    inference = ModelInference(path.parent)
    inference.load_model(path, name)
    return AttachedModel(
        name=name,
        version=version,
        artifact_file=path.name,
        spec_version=spec_version,
        inference=inference,
        output_index=0 if output_index is None else output_index,
    )


async def _attach(
    models_dir: Path,
    name: str,
    artifact_file: str,
    version: int,
    sha256: str | None,
) -> AttachedModel:
    """The shared attach pipeline: safe path, pinned bytes, load, spec check."""
    path = _artifact_path(models_dir, artifact_file)
    if sha256 is not None:
        await asyncio.to_thread(_check_artifact_sha, path, sha256)
    attached = await asyncio.to_thread(_load_attached, path, name, version)
    _ATTACHED[(name, version)] = attached
    return attached


async def attach_production(pool: Any, models_dir: Path, name: str) -> AttachedModel:
    """Attach the named model's Production version (idempotent, cached).

    The cache is keyed by (name, registry version): a warm attach answers
    without the registry, and a mid-session promote never swaps the model a
    running consumer pinned. Cold attach is the full fail-closed pipeline:
    registry lookup → artifact on the mount → pinned sha256 → onnxruntime →
    frozen-spec metadata check.
    """
    if pool is None:
        for (attached_name, _), attached in _ATTACHED.items():
            if attached_name == name:
                return attached
        raise MlServingError(
            "unavailable",
            f"the model registry is unreachable (no database pool); model "
            f"'{name}' cannot be attached and serving refuses to guess",
        )
    version = await ModelRegistry(pool).get_production(name)
    if version is None:
        raise MlServingError(
            "not_promoted",
            f"model '{name}' has no Production version: promote a version in "
            "the registry before a session can serve it",
        )
    cached = _ATTACHED.get((name, version.version))
    if cached is not None:
        return cached
    return await _attach(
        models_dir, name, version.artifact_file, version.version, version.artifact_sha256
    )


async def attach_artifact(
    models_dir: Path,
    artifact_file: str,
    name: str,
    version: int = 1,
) -> AttachedModel:
    """Attach an artifact by file, skipping the registry (the fixture seam).

    The same safe-path, load and frozen-spec checks run; only the
    name→Production resolution (a database read) is skipped, so backtest and
    test harnesses can serve a prepared fixture model without a pool.
    """
    cached = _ATTACHED.get((name, version))
    if cached is not None:
        return cached
    return await _attach(models_dir, name, artifact_file, version, None)


def _reset_attached() -> None:
    """Test isolation: drop every attached model."""
    _ATTACHED.clear()


__all__ = [
    "AttachedModel",
    "MlServingError",
    "ModelInference",
    "attach_artifact",
    "attach_production",
]
