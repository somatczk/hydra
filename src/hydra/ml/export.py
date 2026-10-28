"""ONNX export for the trained XGBoost classifier, spec metadata embedded.

``export_xgboost_to_onnx`` writes the feature version and the spec metadata
into the ONNX ``custom_metadata_map`` so serving can tell which feature set an
artifact was trained on (SM-11: an export without a spec carries
``feature_version=0``, which serving distrusts). ``validate_onnx_export`` gates
an export on prediction parity with the original model. All ONNX library
imports are lazy (inside functions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.ml.features import FeatureSpec
from hydra.ml.training import SupportsPredict


def export_xgboost_to_onnx(
    model: object,
    feature_names: list[str],
    output_path: Path,
    spec: FeatureSpec | None = None,
) -> Path:
    """Export a fitted XGBoost model to ONNX with the feature-spec metadata embedded.

    Parameters
    ----------
    model:
        A fitted XGBoost model (e.g. ``xgboost.XGBClassifier``).
    feature_names:
        List of input feature names.
    output_path:
        Destination file path for the ``.onnx`` file.
    spec:
        The spec the model was trained with; its version becomes the artifact's
        ``feature_version``. Without a spec the export is marked
        ``feature_version=0`` (untrusted by serving).

    Returns
    -------
    Path
        The path to the written ONNX file.
    """
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType

    n_features = len(feature_names)
    initial_type = [("features", FloatTensorType([None, n_features]))]
    onnx_model = convert_xgboost(model, initial_types=initial_type)
    _write_metadata(onnx_model, feature_names, spec)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    return output_path


def _write_metadata(
    onnx_model: Any,
    feature_names: list[str],
    spec: FeatureSpec | None,
) -> None:
    """Write the custom_metadata_map entries serving reads."""
    metadata: dict[str, str] = {
        "feature_version": str(spec.spec_version if spec is not None else 0),
        "feature_names": json.dumps(feature_names),
    }
    if spec is not None:
        metadata["spec_version"] = str(spec.spec_version)
        metadata["horizon"] = str(spec.horizon)
    for key, value in metadata.items():
        entry = onnx_model.metadata_props.add()
        entry.key = key
        entry.value = value


def validate_onnx_export(
    original_model: SupportsPredict,
    onnx_path: Path,
    test_input: ndarray,
    tolerance: float = 1e-5,
) -> bool:
    """Validate that ONNX export produces predictions matching the original model.

    Parameters
    ----------
    original_model:
        The original fitted model with a ``.predict()`` method.
    onnx_path:
        Path to the exported ONNX file.
    test_input:
        Test input array for comparison.
    tolerance:
        Maximum allowed absolute difference between predictions.

    Returns
    -------
    bool
        ``True`` if predictions match within tolerance.
    """
    import onnxruntime as ort

    # Get original predictions
    original_preds = np.asarray(original_model.predict(test_input))

    # Get ONNX predictions
    session = ort.InferenceSession(
        str(onnx_path),
        providers=["CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name
    input_data = test_input.astype(np.float32)
    if input_data.ndim == 1:
        input_data = input_data.reshape(1, -1)

    onnx_results = session.run(None, {input_name: input_data})
    onnx_preds = np.asarray(onnx_results[0]).flatten()
    original_flat = original_preds.flatten()

    return bool(np.allclose(original_flat, onnx_preds, atol=tolerance))
