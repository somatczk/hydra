"""ONNX Runtime model inference for real-time prediction.

Loads ONNX models and runs CPU inference targeting <5 ms latency.
All onnxruntime imports are lazy (inside methods).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy import ndarray

# ---------------------------------------------------------------------------
# Internal model wrapper
# ---------------------------------------------------------------------------


@dataclass
class _LoadedModel:
    """Internal wrapper for a loaded ONNX model session."""

    session: object  # onnxruntime.InferenceSession
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

            results = session.run(None, {loaded.input_name: input_data})  # type: ignore[union-attr]
            return np.asarray(results[0])
        except Exception:
            # Fallback: strategy continues without ML
            return None

    def get_model_info(self, model_name: str) -> dict | None:
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
