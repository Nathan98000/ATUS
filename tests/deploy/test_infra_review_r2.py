"""Runbooks and workflows: the confirmed infra findings of the Phase 5 review, round 2.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script), the test reads it too, so a change
there fails here until the docs follow. Read-only; needs neither Docker nor
the network.
"""

from __future__ import annotations

import re
from pathlib import Path

from .conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
DEPLOYMENT = DOCS_DIR / "deployment.md"
OPERATIONS = DOCS_DIR / "operations.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, single spaces, no backticks: wording checks ignore wrapping and code spans."""
    return re.sub(r"\s+", " ", text.replace("`", ""))


def _section(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))] if end else text[begin:]


def _step(section: str, number: int) -> str:
    """Numbered step ``number`` of a runbook section (up to the next top-level step)."""
    begin = section.index(f"\n{number}. ")
    following = re.search(rf"\n{number + 1}\. ", section[begin:])
    return section[begin:begin + following.start()] if following else section[begin:]


# --- replay-local-smoke-proves-wrong-db ---------------------------------------------------


def test_the_data_release_replays_prove_which_database_answered():
    """replay-local-smoke-proves-wrong-db: step 1 left the full-database API on :8000, so
    step 5's API on the new project could not bind and the smoke replayed the full
    database against itself. Step 1 stops its API; steps 5 and 7 name the release's
    data version, which replay_deployed.py checks."""
    release = _section(_read(OPERATIONS), "### Data release", "### New ATUS year")
    record = _step(release, 1)
    assert record.index("--record") < record.index("kill %1")
    for number in (5, 7):
        replay = _step(release, number)
        assert "replay_deployed.py" in replay and "--expect-data-version" in replay, number
    assert "kill %1" in _step(release, 5)
    site = _flat(_section(_read(DEPLOYMENT), "### 9. Quota-budgeted post-deploy replay",
                          "- **What it sends.**"))
    assert "--expected expected-<release>.json --expect-data-version" in site
    script = _read(REPO_ROOT / "scripts" / "replay_deployed.py")
    assert '"--expect-data-version"' in script and "def check_data_version" in script
