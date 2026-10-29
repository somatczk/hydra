"""AGENTS.md is the one source of agent instructions.

Every CLAUDE.md only imports its sibling AGENTS.md, every nested AGENTS.md is indexed from the root,
the root plus any nested file fit Codex's default instruction budget, and the tools that need an
opt-in (Gemini CLI, Aider) are pointed at it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_SKIP_DIRS = {"node_modules", "__pycache__"}
_CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _find(name: str) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        if name in filenames:
            found.append(Path(dirpath) / name)
    return found


def test_every_claude_md_only_imports_its_sibling_agents_md() -> None:
    claude_files = _find("CLAUDE.md")
    assert _ROOT / "CLAUDE.md" in claude_files
    for path in claude_files:
        body = _HTML_COMMENT.sub("", path.read_text()).strip()
        assert body == "@AGENTS.md", path.relative_to(_ROOT)
        assert (path.parent / "AGENTS.md").is_file(), path.relative_to(_ROOT)


def test_every_agents_md_has_a_claude_md_and_a_root_index_entry() -> None:
    root_text = (_ROOT / "AGENTS.md").read_text()
    nested = [p for p in _find("AGENTS.md") if p.parent != _ROOT]
    assert nested
    for path in nested:
        rel_dir = f"`{path.parent.relative_to(_ROOT).as_posix()}/`"
        assert (path.parent / "CLAUDE.md").is_file(), rel_dir
        assert rel_dir in root_text, f"{rel_dir} missing from the root AGENTS.md index"


def test_root_plus_any_nested_file_fits_the_codex_default_budget() -> None:
    root_size = (_ROOT / "AGENTS.md").stat().st_size
    largest_nested = max(p.stat().st_size for p in _find("AGENTS.md") if p.parent != _ROOT)
    assert root_size + largest_nested < _CODEX_PROJECT_DOC_MAX_BYTES


def test_gemini_cli_and_aider_are_opted_in() -> None:
    gemini = json.loads((_ROOT / ".gemini" / "settings.json").read_text())
    assert "AGENTS.md" in gemini["context"]["fileName"]
    aider = yaml.safe_load((_ROOT / ".aider.conf.yml").read_text())
    assert "AGENTS.md" in (aider["read"] if isinstance(aider["read"], list) else [aider["read"]])
