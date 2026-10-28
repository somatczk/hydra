"""Shared fakes for the ml_train pipeline: a real ONNX artifact without the ml-training extra.

CI installs only the dev extra, so ``xgboost`` and ``onnxmltools`` are faked at
the ``sys.modules`` seam — but the fake converter serialises a REAL, minimal
ONNX model (a ``ReduceMax`` over axis 1 of a dynamic ``float[rows, cols]``
input, plus whatever metadata the exporter wrote), so ``onnxruntime`` — which IS
a runtime dependency — loads, runs and reads back the artifact for real. The
fake classifier predicts each row's NaN-safe maximum, so on any NaN-free sample
its predictions match the ONNX graph exactly, and its per-row ``(k,)`` output
shape is what a real ``XGBClassifier.predict`` returns.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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
