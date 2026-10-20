"""No release-specific literals outside the release registry (audit F23, F55).

Everything that depends on the BLS release (file names, year spans, lexicon
paths and labels) must derive from ``atus_pipeline.sources``, so that a new
release is one registry entry. This scans the data, engine and validation
code for string constants (docstrings and comments excluded) and calls that
hard-code release 0325 or its 2003-2025 span.

Not scanned: ``sources.py`` (the registry itself), ``config.py`` (the
default release), and the API package, whose OpenAPI descriptions still name
the 2003-25 lexicon (tracked separately for the API workstream).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "atus_pipeline"
SCANNED = ("analytics", "database", "loading", "serving", "validation", "transformation")
FORBIDDEN = re.compile(r"\b0325\b|2003[-–](?:20)?25\b")

# Deliberate, reviewed exceptions: (file, fragment of the literal, reason).
ALLOWED = {
    ("validation/db_checks.py", "3 episodes in the 2003-25 release"):
        "a recorded fact about release 0325's data, explaining a warning-level check",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _findings() -> list[str]:
    found = []
    for package in SCANNED:
        for path in sorted((PACKAGE / package).rglob("*.py")):
            relative = path.relative_to(PACKAGE).as_posix()
            tree = ast.parse(path.read_text())
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and FORBIDDEN.search(node.value)):
                    if not any(relative == f and fragment in node.value
                               for f, fragment in ALLOWED):
                        found.append(f"{relative}:{node.lineno}: {node.value[:60]!r}")
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "range"
                        and [getattr(a, "value", None) for a in node.args[:2]] == [2003, 2026]):
                    found.append(f"{relative}:{node.lineno}: range(2003, 2026)")
    return found


def test_no_release_literals_outside_the_registry():
    assert _findings() == []


def test_the_scan_detects_literals(tmp_path, monkeypatch):
    """Guard the guard: a planted literal is reported."""
    planted = tmp_path / "analytics"
    planted.mkdir()
    (planted / "planted.py").write_text(
        '"""Docstrings may mention 2003-25."""\n'
        'NOTE = "the 2003-25 lexicon"\n'
        "YEARS = range(2003, 2026)\n"
    )
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PACKAGE", tmp_path)
    monkeypatch.setattr(module, "SCANNED", ("analytics",))
    assert len(_findings()) == 2
