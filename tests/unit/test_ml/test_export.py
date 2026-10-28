"""Tests for hydra.ml.export -- the ONNX export with embedded spec metadata.

onnxmltools/onnxruntime are mocked (CI installs only the dev extra); the fake
ONNX model captures the ``custom_metadata_map`` entries, so the SM-11 fix is
asserted at the real seam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from hydra.ml.export import export_xgboost_to_onnx, validate_onnx_export
from hydra.ml.features import DEFAULT_FEATURE_SPEC, FeatureSpec


class _MetaEntry:
    """One captured custom_metadata_map entry."""

    def __init__(self) -> None:
        self.key: str = ""
        self.value: str = ""


@dataclass
class _MetaProps:
    """The repeated metadata_props container on the fake ModelProto."""

    entries: list[_MetaEntry] = field(default_factory=list)

    def add(self) -> _MetaEntry:
        entry = _MetaEntry()
        self.entries.append(entry)
        return entry

    def as_dict(self) -> dict[str, str]:
        return {e.key: e.value for e in self.entries}


@dataclass
class _FakeOnnxModel:
    """A stand-in for the onnx ModelProto returned by convert_xgboost."""

    metadata_props: _MetaProps = field(default_factory=_MetaProps)

    def serialize_to_string(self) -> bytes:
        return b"fake_onnx_bytes"

    # the real ModelProto API name the exporter calls
    SerializeToString = serialize_to_string


def _patch_onnxmltools(model: _FakeOnnxModel) -> dict[str, MagicMock]:
    mock_onnxmltools = MagicMock()
    mock_onnxmltools.convert_xgboost = MagicMock(return_value=model)
    mock_data_types = MagicMock()
    return {
        "onnxmltools": mock_onnxmltools,
        "onnxmltools.convert": MagicMock(),
        "onnxmltools.convert.common": MagicMock(),
        "onnxmltools.convert.common.data_types": mock_data_types,
    }


def _spec() -> FeatureSpec:
    return FeatureSpec(
        spec_version=3,
        horizon=4,
        window_1h=2000,
        window_4h=500,
        window_1d=120,
        feature_names=DEFAULT_FEATURE_SPEC.feature_names,
    )


class TestExportXGBoost:
    def test_writes_the_spec_metadata_into_custom_metadata_map(self, tmp_path: Path) -> None:
        """SM-11: the artifact carries the feature version and the spec."""
        onnx_model = _FakeOnnxModel()
        output_path = tmp_path / "model.onnx"
        with patch.dict("sys.modules", _patch_onnxmltools(onnx_model)):
            result = export_xgboost_to_onnx(
                MagicMock(), ["f0", "f1", "f2"], output_path, spec=_spec()
            )

        assert result == output_path
        assert output_path.read_bytes() == b"fake_onnx_bytes"
        assert onnx_model.metadata_props.as_dict() == {
            "feature_version": "3",
            "spec_version": "3",
            "horizon": "4",
            "feature_names": json.dumps(["f0", "f1", "f2"]),
        }

    def test_export_without_a_spec_is_marked_untrusted(self, tmp_path: Path) -> None:
        """A spec-less export carries feature_version=0, which serving distrusts."""
        onnx_model = _FakeOnnxModel()
        with patch.dict("sys.modules", _patch_onnxmltools(onnx_model)):
            export_xgboost_to_onnx(MagicMock(), ["f0"], tmp_path / "model.onnx")

        metadata = onnx_model.metadata_props.as_dict()
        assert metadata["feature_version"] == "0"
        assert "spec_version" not in metadata
        assert metadata["feature_names"] == json.dumps(["f0"])

    def test_export_creates_parent_dirs(self, tmp_path: Path) -> None:
        output_path = tmp_path / "subdir" / "nested" / "model.onnx"
        with patch.dict("sys.modules", _patch_onnxmltools(_FakeOnnxModel())):
            export_xgboost_to_onnx(MagicMock(), ["f0"], output_path, spec=_spec())
        assert output_path.exists()


# ---------------------------------------------------------------------------
# Validate ONNX export
# ---------------------------------------------------------------------------


class TestValidateONNXExport:
    def _run(self, tmp_path: Path, onnx_output: np.ndarray) -> bool:
        onnx_path = tmp_path / "model.onnx"
        onnx_path.write_bytes(b"fake")

        original_preds = np.array([0.5, 0.6, 0.7], dtype=np.float32)
        original_model = MagicMock()
        original_model.predict = MagicMock(return_value=original_preds)

        mock_input = MagicMock()
        mock_input.name = "features"
        mock_session = MagicMock()
        mock_session.get_inputs = MagicMock(return_value=[mock_input])
        mock_session.run = MagicMock(return_value=[onnx_output])

        mock_ort = MagicMock()
        mock_ort.InferenceSession = MagicMock(return_value=mock_session)

        test_input = np.random.default_rng(42).standard_normal((3, 2)).astype(np.float32)
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            return validate_onnx_export(original_model, onnx_path, test_input)

    def test_matching_predictions_returns_true(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, np.array([0.5, 0.6, 0.7], dtype=np.float32)) is True

    def test_mismatched_predictions_returns_false(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, np.array([10.0, 20.0, 30.0], dtype=np.float32)) is False

    def test_tolerance_is_honoured(self, tmp_path: Path) -> None:
        onnx_path = tmp_path / "model.onnx"
        onnx_path.write_bytes(b"fake")
        original = MagicMock()
        original.predict = MagicMock(return_value=np.array([1.0, 2.0]))
        mock_input = MagicMock()
        mock_input.name = "features"
        session = MagicMock()
        session.get_inputs = MagicMock(return_value=[mock_input])
        session.run = MagicMock(return_value=[np.array([1.0, 2.0 + 1e-4])])
        ort = MagicMock()
        ort.InferenceSession = MagicMock(return_value=session)
        test_input = np.ones((2, 2), dtype=np.float32)
        with patch.dict("sys.modules", {"onnxruntime": ort}):
            assert validate_onnx_export(original, onnx_path, test_input, tolerance=1e-3)
            assert not validate_onnx_export(original, onnx_path, test_input, tolerance=1e-6)
