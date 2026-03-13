"""Tests for hydra.ml.serving -- ONNX model inference module.

All onnxruntime usage is mocked so tests run without it installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hydra.ml.serving import ModelInference

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def models_dir(tmp_path: Path) -> Path:
    return tmp_path / "models"


def _make_mock_session(output: np.ndarray | None = None) -> MagicMock:
    """Build a mock onnxruntime.InferenceSession."""
    if output is None:
        output = np.array([[0.5]], dtype=np.float32)

    mock_input = MagicMock()
    mock_input.name = "features"

    session = MagicMock()
    session.get_inputs = MagicMock(return_value=[mock_input])
    session.run = MagicMock(return_value=[output])
    return session


def _patch_ort(session: MagicMock):
    """Create a mock onnxruntime module that returns the given session."""
    mock_ort = MagicMock()
    mock_ort.InferenceSession = MagicMock(return_value=session)
    return mock_ort


# ---------------------------------------------------------------------------
# Load + predict roundtrip
# ---------------------------------------------------------------------------


class TestLoadAndPredict:
    def test_load_and_predict_roundtrip(self, models_dir: Path) -> None:
        expected = np.array([[0.75]], dtype=np.float32)
        session = _make_mock_session(expected)
        mock_ort = _patch_ort(session)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            engine = ModelInference(models_dir)
            engine.load_model(Path("/fake/model.onnx"), "test_model")
            result = engine.predict("test_model", np.array([1.0, 2.0, 3.0]))

        assert result is not None
        np.testing.assert_array_equal(result, expected)

    def test_predict_casts_to_float32(self, models_dir: Path) -> None:
        session = _make_mock_session()
        mock_ort = _patch_ort(session)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            engine = ModelInference(models_dir)
            engine.load_model(Path("/fake/model.onnx"), "test_model")
            # Pass float64 input
            engine.predict("test_model", np.array([1.0, 2.0], dtype=np.float64))

        # Verify the session received float32
        call_args = session.run.call_args
        input_dict = call_args[1] if call_args[1] else call_args[0][1]
        actual_input = input_dict["features"]
        assert actual_input.dtype == np.float32


# ---------------------------------------------------------------------------
# Unknown model
# ---------------------------------------------------------------------------


class TestUnknownModel:
    def test_predict_unknown_returns_none(self, models_dir: Path) -> None:
        engine = ModelInference(models_dir)
        result = engine.predict("nonexistent", np.array([1.0]))
        assert result is None


# ---------------------------------------------------------------------------
# Reload model
# ---------------------------------------------------------------------------


class TestReloadModel:
    def test_reload_replaces_session(self, models_dir: Path) -> None:
        old_output = np.array([[0.1]], dtype=np.float32)
        new_output = np.array([[0.9]], dtype=np.float32)

        old_session = _make_mock_session(old_output)
        new_session = _make_mock_session(new_output)

        # First load uses old session
        mock_ort_old = _patch_ort(old_session)
        with patch.dict("sys.modules", {"onnxruntime": mock_ort_old}):
            engine = ModelInference(models_dir)
            engine.load_model(Path("/fake/model.onnx"), "test_model")
            result_old = engine.predict("test_model", np.array([1.0]))

        assert result_old is not None
        np.testing.assert_array_equal(result_old, old_output)

        # Reload uses new session
        mock_ort_new = _patch_ort(new_session)
        with patch.dict("sys.modules", {"onnxruntime": mock_ort_new}):
            engine.reload_model("test_model")
            result_new = engine.predict("test_model", np.array([1.0]))

        assert result_new is not None
        np.testing.assert_array_equal(result_new, new_output)

    def test_reload_nonexistent_is_noop(self, models_dir: Path) -> None:
        engine = ModelInference(models_dir)
        # Should not raise
        engine.reload_model("nonexistent")


# ---------------------------------------------------------------------------
# Model info
# ---------------------------------------------------------------------------


class TestGetModelInfo:
    def test_returns_metadata(self, models_dir: Path) -> None:
        session = _make_mock_session()
        mock_ort = _patch_ort(session)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            engine = ModelInference(models_dir)
            engine.load_model(Path("/fake/model.onnx"), "test_model")

        info = engine.get_model_info("test_model")
        assert info is not None
        assert info["model_name"] == "test_model"
        assert info["model_path"] == "/fake/model.onnx"
        assert info["input_name"] == "features"
        assert "loaded_at" in info

    def test_unknown_model_returns_none(self, models_dir: Path) -> None:
        engine = ModelInference(models_dir)
        assert engine.get_model_info("nonexistent") is None


# ---------------------------------------------------------------------------
# Fallback on error
# ---------------------------------------------------------------------------


class TestFallback:
    def test_predict_returns_none_on_exception(self, models_dir: Path) -> None:
        session = _make_mock_session()
        session.run = MagicMock(side_effect=RuntimeError("ONNX failure"))
        mock_ort = _patch_ort(session)

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            engine = ModelInference(models_dir)
            engine.load_model(Path("/fake/model.onnx"), "test_model")
            result = engine.predict("test_model", np.array([1.0]))

        assert result is None
