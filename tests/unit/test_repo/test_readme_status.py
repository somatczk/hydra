"""Each **Working** row in the README status table names a proof, and each test it names exists."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_TEST_REF = re.compile(r"`((?:tests/\S+?\.py)?)(?:::([\w:]+))?(?:\[[^\]]*\])?`")
_COMMAND_PREFIXES = ("cd dashboard && ", "promtool ", "uv run ", "make ")
_LABEL = re.compile(r"\*\*([^*]+?)\*\*")
_LABELS = ("Working", "Partial", "Known gap", "Removed")


def _status_rows() -> list[tuple[str, str, str, str]]:
    text = (_ROOT / "README.md").read_text()
    section = text.split("\n## Status\n", 1)[1].split("\n## ", 1)[0]
    rows: list[tuple[str, str, str, str]] = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] not in ("Area", "") and not cells[0].startswith("-"):
            rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _label(status: str) -> str:
    match = _LABEL.search(status)
    return match.group(1) if match else ""


def _test_refs(cell: str) -> list[tuple[str, str | None]]:
    refs: list[tuple[str, str | None]] = []
    current = ""
    for path, name in _TEST_REF.findall(cell):
        if not path and not name:
            continue
        current = path or current
        refs.append((current, name or None))
    return refs


def test_status_table_is_parsed() -> None:
    rows = _status_rows()
    assert len(rows) > 30
    assert any(_label(status).startswith("Working") for _, _, status, _ in rows)


def test_every_row_carries_a_legend_label() -> None:
    unlabelled = [
        f"{area} / {capability}"
        for area, capability, status, _ in _status_rows()
        if not _label(status).startswith(_LABELS)
    ]
    assert unlabelled == []


def test_every_working_row_names_a_proof() -> None:
    unproven = []
    for area, capability, status, proof in _status_rows():
        if not _label(status).startswith("Working"):
            continue
        commands = [c for c in re.findall(r"`([^`]+)`", proof) if c.startswith(_COMMAND_PREFIXES)]
        if not _test_refs(proof) and not commands:
            unproven.append(f"{area} / {capability}")
    assert unproven == []


def test_every_named_test_exists() -> None:
    missing = []
    for _, capability, status, proof in _status_rows():
        for path, name in _test_refs(f"{status} {proof}"):
            target = _ROOT / path
            func = name.rsplit("::", 1)[-1] if name else None
            if not path or not target.is_file():
                missing.append(f"{capability}: {path or '(no file)'}")
            elif func and not re.search(rf"^\s*(async\s+)?def {func}\(", target.read_text(), re.M):
                missing.append(f"{capability}: {path}::{name}")
    assert missing == []
