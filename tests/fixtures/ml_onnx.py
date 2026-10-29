"""Shared fakes for the ml_train pipeline: a real ONNX artifact without the ml-training extra.

CI installs only the dev extra, so ``xgboost`` and ``onnxmltools`` are faked at
the ``sys.modules`` seam — but the fake converter serialises a REAL, minimal
ONNX model (a ``ReduceMax`` over axis 1 of a dynamic ``float[rows, cols]``
input, plus whatever metadata the exporter wrote), so ``onnxruntime`` — which IS
a runtime dependency — loads, runs and reads back the artifact for real. The
fake classifier predicts each row's NaN-safe maximum, so on any NaN-free sample
its predictions match the ONNX graph exactly, and its per-row ``(k,)`` output
shape is what a real ``XGBClassifier.predict`` returns.

When the ml-training extra IS installed (a dev checkout, the ML image), the
honest-path tests skip the fakes and exercise the REAL converter through
``ml_pipeline_fakes``; only the tests that need fake misbehaviour (a parity
failure, a spec-less artifact, a stalling fit) keep the fakes unconditionally.
"""

from __future__ import annotations

import importlib.util
import json
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

# ---------------------------------------------------------------------------
# A real ONNX model, hand-encoded (ModelProto protobuf)
# ---------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | 0x80 if n else low)
        if not n:
            return bytes(out)


def _field(num: int, wire: int, payload: bytes | int) -> bytes:
    key = _varint((num << 3) | wire)
    if isinstance(payload, int):
        return key + _varint(payload)
    return key + _varint(len(payload)) + payload


def _dim(name: bytes) -> bytes:
    return _field(2, 2, name)  # Dimension.dim_param (a dynamic dimension)


def _dim_value(n: int) -> bytes:
    return _field(1, 0, n)  # Dimension.dim_value (a fixed dimension)


def _float_type(rank: int) -> bytes:
    """TypeProto: a FLOAT tensor of dynamic shape (``rank`` named dimensions)."""
    shape = b"".join(_field(1, 2, _dim(n.encode())) for n in ("rows", "cols")[:rank])
    tensor = _field(1, 0, 1) + _field(2, 2, shape)  # elem_type FLOAT + shape
    return _field(1, 2, tensor)  # TypeProto.tensor_type


def _value_info(name: str, rank: int) -> bytes:
    return _field(1, 2, name.encode()) + _field(2, 2, _float_type(rank))


def _attr_int(name: str, value: int) -> bytes:
    # AttributeProto: name, type INT (2), i
    return _field(1, 2, name.encode()) + _field(20, 0, 2) + _field(3, 0, value)


def _attr_ints(name: str, values: list[int]) -> bytes:
    # AttributeProto: name, type INTS (7), packed ints
    packed = b"".join(_varint(v) for v in values)
    return _field(1, 2, name.encode()) + _field(20, 0, 7) + _field(8, 2, packed)


def reduce_max_onnx(metadata: dict[str, str] | list[tuple[str, str]]) -> bytes:
    """A real ONNX model computing ``y = ReduceMax(x, axis=1, keepdims=0)``.

    ``metadata`` entries are encoded as ``metadata_props``, exactly where the
    exporter writes the feature spec and where serving reads it back.
    """
    entries: list[tuple[str, str]] = (
        list(metadata.items()) if isinstance(metadata, dict) else list(metadata)
    )
    node = (
        _field(1, 2, b"x")
        + _field(2, 2, b"y")
        + _field(4, 2, b"ReduceMax")
        + _field(5, 2, _attr_ints("axes", [1]))
        + _field(5, 2, _attr_int("keepdims", 0))
    )
    graph = (
        _field(1, 2, node)
        + _field(2, 2, b"g")
        + _field(11, 2, _value_info("x", 2))
        + _field(12, 2, _value_info("y", 1))
    )
    props = b"".join(
        _field(14, 2, _field(1, 2, key.encode()) + _field(2, 2, value_.encode()))
        for key, value_ in entries
    )
    return _field(1, 0, 8) + _field(8, 2, _field(2, 0, 13)) + _field(7, 2, graph) + props


# ---------------------------------------------------------------------------
# A deterministic confidence model (the P5-C serving fixture)
# ---------------------------------------------------------------------------


def _tensor_float(name: bytes, dims: list[int], values: list[float]) -> bytes:
    """TensorProto: dims, FLOAT, packed float_data, name."""
    packed = struct.pack(f"<{len(values)}f", *values)
    return (
        b"".join(_field(1, 0, d) for d in dims)
        + _field(2, 0, 1)
        + _field(4, 2, packed)
        + _field(8, 2, name)
    )


def _tensor_int64(name: bytes, dims: list[int], values: list[int]) -> bytes:
    """TensorProto: dims, INT64, packed int64_data, name."""
    packed = b"".join(_varint(v) for v in values)
    return (
        b"".join(_field(1, 0, d) for d in dims)
        + _field(2, 0, 7)
        + _field(7, 2, packed)
        + _field(8, 2, name)
    )


def _value_info_rowcol(name: str, second: int | None) -> bytes:
    """ValueInfoProto: a FLOAT tensor with a dynamic row dim and a fixed/absent
    second dim."""
    first = _field(1, 2, _dim(b"rows"))
    second_dim = (
        _field(1, 2, _dim_value(second)) if second is not None else _field(1, 2, _dim(b"cols"))
    )
    tensor = _field(1, 0, 1) + _field(2, 2, first + second_dim)
    return _field(1, 2, name.encode()) + _field(2, 2, tensor)


def confidence_onnx(
    metadata: dict[str, str] | list[tuple[str, str]],
    feature_count: int,
    *,
    column: int = 0,
    scale: float = 0.01,
) -> bytes:
    """A real ONNX model computing ``y = Gather(x, [column], axis=1) * scale``.

    The P5-C serving fixture: P(up) = scale * x[:, column] — a hand-checkable
    number in [0, 1] when the column is an RSI/MFI-style 0-100 feature, and
    NaN-safe: no other column enters the arithmetic, so the all-NaN
    higher-timeframe feature blocks of a single-stream backtest context
    cannot poison the prediction.
    """
    entries: list[tuple[str, str]] = (
        list(metadata.items()) if isinstance(metadata, dict) else list(metadata)
    )
    gather = (
        _field(1, 2, b"x")
        + _field(1, 2, b"index")
        + _field(2, 2, b"col")
        + _field(4, 2, b"Gather")
        + _field(5, 2, _attr_int("axis", 1))
    )
    mul = _field(1, 2, b"col") + _field(1, 2, b"scale") + _field(2, 2, b"y") + _field(4, 2, b"Mul")
    graph = (
        _field(1, 2, gather)
        + _field(1, 2, mul)
        + _field(2, 2, b"g")
        + _field(5, 2, _tensor_int64(b"index", [1], [column]))
        + _field(5, 2, _tensor_float(b"scale", [1], [scale]))
        + _field(11, 2, _value_info("x", 2))
        + _field(12, 2, _value_info_rowcol("y", 1))
    )
    props = b"".join(
        _field(14, 2, _field(1, 2, key.encode()) + _field(2, 2, value_.encode()))
        for key, value_ in entries
    )
    return _field(1, 0, 8) + _field(8, 2, _field(2, 0, 13)) + _field(7, 2, graph) + props


def frozen_spec_metadata(
    feature_names: list[str], spec_version: int, horizon: int
) -> dict[str, str]:
    """The metadata serving's frozen-spec check reads (feature_version matches SM-11)."""
    return {
        "feature_names": json.dumps(feature_names),
        "spec_version": str(spec_version),
        "horizon": str(horizon),
        "feature_version": str(spec_version),
    }


def two_output_classifier_onnx(
    metadata: dict[str, str] | list[tuple[str, str]],
    feature_count: int,
    *,
    scale: float = 0.01,
) -> bytes:
    """A real ONNX classifier shaped like an xgboost export: two outputs.

    ``label`` (width 1) = x[:, 0], then ``probabilities`` (width 2) =
    x[:, [0, 1]] * scale - so P(up) = scale * x[:, 1] comes from the SECOND
    output, and a consumer that reads the first would answer x[:, 0] instead.
    Serving reads the output named ``probabilities``; this fixture pins that.
    """
    entries: list[tuple[str, str]] = (
        list(metadata.items()) if isinstance(metadata, dict) else list(metadata)
    )
    label_node = (
        _field(1, 2, b"x")
        + _field(1, 2, b"label_index")
        + _field(2, 2, b"label")
        + _field(4, 2, b"Gather")
        + _field(5, 2, _attr_int("axis", 1))
    )
    gather_probs = (
        _field(1, 2, b"x")
        + _field(1, 2, b"probs_index")
        + _field(2, 2, b"probs_cols")
        + _field(4, 2, b"Gather")
        + _field(5, 2, _attr_int("axis", 1))
    )
    mul_probs = (
        _field(1, 2, b"probs_cols")
        + _field(1, 2, b"scale")
        + _field(2, 2, b"probabilities")
        + _field(4, 2, b"Mul")
    )
    graph = (
        _field(1, 2, label_node)
        + _field(1, 2, gather_probs)
        + _field(1, 2, mul_probs)
        + _field(2, 2, b"g")
        + _field(5, 2, _tensor_int64(b"label_index", [1], [0]))
        + _field(5, 2, _tensor_int64(b"probs_index", [2], [0, 1]))
        + _field(5, 2, _tensor_float(b"scale", [1], [scale]))
        + _field(11, 2, _value_info("x", 2))
        + _field(12, 2, _value_info_rowcol("label", 1))
        + _field(12, 2, _value_info_rowcol("probabilities", 2))
    )
    props = b"".join(
        _field(14, 2, _field(1, 2, key.encode()) + _field(2, 2, value_.encode()))
        for key, value_ in entries
    )
    return _field(1, 0, 8) + _field(8, 2, _field(2, 0, 13)) + _field(7, 2, graph) + props


# ---------------------------------------------------------------------------
# The fake converter surface (onnxmltools)
# ---------------------------------------------------------------------------


class _MetaEntry:
    def __init__(self) -> None:
        self.key: str = ""
        self.value: str = ""


class _MetaProps:
    """The repeated metadata_props container, capturing entries like ModelProto."""

    def __init__(self) -> None:
        self.entries: list[_MetaEntry] = []

    def add(self) -> _MetaEntry:
        entry = _MetaEntry()
        self.entries.append(entry)
        return entry

    def as_dict(self) -> dict[str, str]:
        return {e.key: e.value for e in self.entries}


class FakeModelProto:
    """What the fake ``convert_xgboost`` returns: captures metadata, serialises real ONNX."""

    def __init__(self, carry_metadata: bool = True) -> None:
        self.metadata_props = _MetaProps()
        self._carry_metadata = carry_metadata

    def serialize_to_string(self) -> bytes:
        entries = self.metadata_props.entries if self._carry_metadata else []
        return reduce_max_onnx([(e.key, e.value) for e in entries])

    # the real ModelProto API name the exporter calls
    SerializeToString = serialize_to_string


def fake_onnxmltools(proto: FakeModelProto) -> dict[str, Any]:
    """A ``sys.modules`` patch dict making ``from onnxmltools import convert_xgboost`` work."""
    module = MagicMock()
    module.convert_xgboost = MagicMock(return_value=proto)
    data_types = MagicMock()
    data_types.FloatTensorType = MagicMock(side_effect=lambda shape: ("features", shape))
    return {
        "onnxmltools": module,
        "onnxmltools.convert": MagicMock(),
        "onnxmltools.convert.common": MagicMock(),
        "onnxmltools.convert.common.data_types": data_types,
    }


# ---------------------------------------------------------------------------
# The fake classifier (xgboost)
# ---------------------------------------------------------------------------


def nan_safe_row_max(x: Any) -> np.ndarray:
    """Predict each row's NaN-safe maximum — the parity partner of ReduceMax."""
    return np.nan_to_num(np.asarray(x, dtype=np.float64), nan=-1e9).max(axis=1)


def fake_xgboost() -> tuple[dict[str, Any], MagicMock]:
    """A ``sys.modules`` patch dict plus its mock classifier.

    ``fit`` is a no-op (recorded); ``predict`` returns one label per row (the
    shape a real ``XGBClassifier.predict`` returns), matching the ReduceMax
    ONNX graph on NaN-free samples.
    """
    model = MagicMock()
    model.fit = MagicMock()
    model.predict = MagicMock(side_effect=nan_safe_row_max)
    module = MagicMock()
    module.XGBClassifier = MagicMock(return_value=model)
    return {"xgboost": module}, model


def ml_training_extra_installed() -> bool:
    """True when the ml-training extra's converter stack is importable."""
    return all(importlib.util.find_spec(name) is not None for name in ("xgboost", "onnxmltools"))


def ml_fake_modules(proto: FakeModelProto | None = None) -> dict[str, Any]:
    """The ``sys.modules`` fakes for the pipeline (the dev-extra tier)."""
    if proto is None:
        proto = FakeModelProto()
    return {**fake_xgboost()[0], **fake_onnxmltools(proto)}


@contextmanager
def ml_pipeline_fakes(proto: FakeModelProto | None = None) -> Iterator[None]:
    """Patch the training pipeline's modules, unless the real stack is installed.

    With the extra present the patch is skipped entirely — an empty
    ``patch.dict("sys.modules", ...)`` would erase every module imported inside
    it on exit — so the honest-path tests train and convert through the real
    xgboost and onnxmltools; only CI's dev-extra tier gets the fakes.
    """
    if ml_training_extra_installed():
        yield
        return
    with patch.dict("sys.modules", ml_fake_modules(proto)):
        yield
