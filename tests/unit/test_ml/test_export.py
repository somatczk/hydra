"""Tests for hydra.ml.export -- ONNX export utilities.

All ONNX libraries (onnxmltools, skl2onnx, onnxruntime) are mocked so tests
run without them installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from hydra.ml.export import (
    export_sklearn_to_onnx,
    export_xgboost_to_onnx,
    validate_onnx_export,
)

# ---------------------------------------------------------------------------
# XGBoost ONNX export
# ---------------------------------------------------------------------------


class TestExportXGBoost:
    def test_export_creates_file(self, tmp_path: Path) -> None:
        output_path = tmp_path / "model.onnx"

        mock_onnx_model = MagicMock()
        mock_onnx_model.SerializeToString = MagicMock(return_value=b"fake_onnx_bytes")

        mock_convert = MagicMock(return_value=mock_onnx_model)
        mock_float_type = MagicMock()

        mock_onnxmltools = MagicMock()
        mock_onnxmltools.convert_xgboost = mock_convert

        mock_data_types = MagicMock()
        mock_data_types.FloatTensorType = mock_float_type

        with patch.dict(
            "sys.modules",
            {
                "onnxmltools": mock_onnxmltools,
                "onnxmltools.convert": MagicMock(),
                "onnxmltools.convert.common": MagicMock(),
                "onnxmltools.convert.common.data_types": mock_data_types,
            },
        ):
            model = MagicMock()
            result = export_xgboost_to_onnx(model, ["f0", "f1", "f2"], output_path)

        assert result == output_path
        assert output_path.exists()
        assert output_path.read_bytes() == b"fake_onnx_bytes"
        mock_convert.assert_called_once()

    def test_export_creates_parent_dirs(self, tmp_path: Path) -> None:
        output_path = tmp_path / "subdir" / "nested" / "model.onnx"

        mock_onnx_model = MagicMock()
        mock_onnx_model.SerializeToString = MagicMock(return_value=b"data")

        mock_onnxmltools = MagicMock()
        mock_onnxmltools.convert_xgboost = MagicMock(return_value=mock_onnx_model)

        mock_data_types = MagicMock()
        mock_data_types.FloatTensorType = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "onnxmltools": mock_onnxmltools,
                "onnxmltools.convert": MagicMock(),
                "onnxmltools.convert.common": MagicMock(),
                "onnxmltools.convert.common.data_types": mock_data_types,
            },
        ):
            export_xgboost_to_onnx(MagicMock(), ["f0"], output_path)

        assert output_path.exists()


# ---------------------------------------------------------------------------
# Sklearn ONNX export
# ---------------------------------------------------------------------------


class TestExportSklearn:
    def test_export_creates_file(self, tmp_path: Path) -> None:
        output_path = tmp_path / "sklearn_model.onnx"

        mock_onnx_model = MagicMock()
        mock_onnx_model.SerializeToString = MagicMock(return_value=b"sklearn_onnx")

        mock_convert = MagicMock(return_value=mock_onnx_model)
        mock_float_type = MagicMock()

        mock_skl2onnx = MagicMock()
        mock_skl2onnx.convert_sklearn = mock_convert

        mock_data_types = MagicMock()
        mock_data_types.FloatTensorType = mock_float_type

        with patch.dict(
            "sys.modules",
            {
                "skl2onnx": mock_skl2onnx,
                "skl2onnx.common": MagicMock(),
                "skl2onnx.common.data_types": mock_data_types,
            },
        ):
            result = export_sklearn_to_onnx(MagicMock(), ["f0", "f1"], output_path)

        assert result == output_path
        assert output_path.exists()
        assert output_path.read_bytes() == b"sklearn_onnx"


# ---------------------------------------------------------------------------
# Validate ONNX export
# ---------------------------------------------------------------------------


class TestValidateONNXExport:
    def test_matching_predictions_returns_true(self, tmp_path: Path) -> None:
        onnx_path = tmp_path / "model.onnx"
        onnx_path.write_bytes(b"fake")

        original_preds = np.array([0.5, 0.6, 0.7], dtype=np.float32)

        original_model = MagicMock()
        original_model.predict = MagicMock(return_value=original_preds)

        # Mock ONNX session that returns matching predictions
        mock_input = MagicMock()
        mock_input.name = "features"
        mock_session = MagicMock()
        mock_session.get_inputs = MagicMock(return_value=[mock_input])
        mock_session.run = MagicMock(return_value=[original_preds])

        mock_ort = MagicMock()
        mock_ort.InferenceSession = MagicMock(return_value=mock_session)

        test_input = np.random.default_rng(42).standard_normal((3, 2)).astype(np.float32)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            result = validate_onnx_export(original_model, onnx_path, test_input)

        assert result is True

    def test_mismatched_predictions_returns_false(self, tmp_path: Path) -> None:
        onnx_path = tmp_path / "model.onnx"
        onnx_path.write_bytes(b"fake")

        original_model = MagicMock()
        original_model.predict = MagicMock(return_value=np.array([0.5, 0.6, 0.7], dtype=np.float32))

        # ONNX returns very different predictions
        mock_input = MagicMock()
        mock_input.name = "features"
        mock_session = MagicMock()
        mock_session.get_inputs = MagicMock(return_value=[mock_input])
        mock_session.run = MagicMock(return_value=[np.array([10.0, 20.0, 30.0], dtype=np.float32)])

        mock_ort = MagicMock()
        mock_ort.InferenceSession = MagicMock(return_value=mock_session)

        test_input = np.random.default_rng(42).standard_normal((3, 2)).astype(np.float32)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            result = validate_onnx_export(original_model, onnx_path, test_input)

        assert result is False
