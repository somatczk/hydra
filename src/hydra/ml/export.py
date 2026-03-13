"""ONNX export utilities for trained ML models.

Converts XGBoost and scikit-learn models to ONNX format and validates
export accuracy.  All ONNX library imports are lazy (inside functions).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy import ndarray


def export_xgboost_to_onnx(
    model: object,
    feature_names: list[str],
    output_path: Path,
) -> Path:
    """Export an XGBoost model to ONNX format.

    Parameters
    ----------
    model:
        A fitted XGBoost model (e.g. ``xgboost.XGBRegressor``).
    feature_names:
        List of input feature names.
    output_path:
        Destination file path for the ``.onnx`` file.

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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    return output_path


def export_sklearn_to_onnx(
    model: object,
    feature_names: list[str],
    output_path: Path,
) -> Path:
    """Export a scikit-learn model to ONNX format.

    Parameters
    ----------
    model:
        A fitted scikit-learn estimator (e.g. ``RandomForestRegressor``).
    feature_names:
        List of input feature names.
    output_path:
        Destination file path for the ``.onnx`` file.

    Returns
    -------
    Path
        The path to the written ONNX file.
    """
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    n_features = len(feature_names)
    initial_type = [("features", FloatTensorType([None, n_features]))]
    onnx_model = convert_sklearn(model, initial_types=initial_type)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    return output_path


def validate_onnx_export(
    original_model: object,
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
    original_preds = np.asarray(original_model.predict(test_input))  # type: ignore[union-attr]

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
