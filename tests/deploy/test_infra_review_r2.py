"""Runbooks and workflows: the confirmed infra findings of the Phase 5 review, round 2.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script), the test reads it too, so a change
there fails here until the docs follow. Read-only; needs neither Docker nor
the network.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
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


# --- trivy-gate-blocks-data-rollback ---------------------------------------------------

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TRIVY_ENTRY = re.compile(r"^(?P<id>(?:CVE-\d{4}-\d{4,}|GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}))"
                         r" exp:(?P<exp>\d{4}-\d{2}-\d{2})$")
TRIVY_HORIZON_DAYS = 31


def trivyignore_problems(text: str, today: date) -> list[str]:
    """Every entry of .trivyignore: one ID with an expiry at most 31 days out, after a
    comment that gives the reason (pure; the policy of operations.md's runbook)."""
    problems, reason = [], False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            reason = False
        elif stripped.startswith("#"):
            reason = len(stripped.lstrip("# ")) >= 10
        else:
            match = TRIVY_ENTRY.match(stripped)
            if not match:
                problems.append(f"line {number}: not '<CVE or GHSA id> exp:YYYY-MM-DD'")
                continue
            if not reason:
                problems.append(f"line {number}: {match['id']} has no reason comment above it")
            if date.fromisoformat(match["exp"]) > today + timedelta(days=TRIVY_HORIZON_DAYS):
                problems.append(f"line {number}: {match['id']} expires more than "
                                f"{TRIVY_HORIZON_DAYS} days out")
    return problems


def test_the_image_scan_has_a_reviewed_expiring_allowlist():
    """trivy-gate-blocks-data-rollback: a newly fixable HIGH/CRITICAL CVE (or a failed
    Trivy DB download) blocked every deploy, the data rollback's deploy included, with
    no override and no runbook."""
    image = _read(WORKFLOWS / "image.yml")
    (gate,) = [line for line in image.splitlines() if "--exit-code 1" in line]
    assert "trivy image" in gate and "--ignorefile .trivyignore" in gate
    assert "--ignorefile /dev/null" in image      # the SARIF report lists everything
    assert trivyignore_problems(_read(REPO_ROOT / ".trivyignore"), date.today()) == []
    # The policy itself.
    today = date(2026, 11, 2)
    good = ("# 2026-11-02 owner: libfoo, not reachable from the API\n"
            "CVE-2026-12345 exp:2026-12-02\n")
    assert trivyignore_problems(good, today) == []
    assert trivyignore_problems("CVE-2026-12345 exp:2026-12-02\n", today)        # no reason
    assert trivyignore_problems("# a reason, reviewed\nCVE-2026-12345\n", today)  # no expiry
    assert trivyignore_problems("# a reason, reviewed\nCVE-2026-12345 exp:2027-06-01\n",
                                today)                                           # too far


def test_a_scan_blocked_deploy_has_a_runbook_and_rollbacks_skip_the_pipeline():
    """trivy-gate-blocks-data-rollback: the runbook, and the data rollback rolls the edge
    back with wrangler rollback (no build, no scan) before the manifest and deploy steps."""
    operations = _read(OPERATIONS)
    runbook = _flat(_section(operations, "### The image scan blocks a deploy", "\n### "))
    for needed in (".trivyignore", "exp:", "PYTHON_DIGEST", "compile.sh",
                   "Render deploy hook", "wrangler rollback"):
        assert needed in runbook, needed
    data = _section(operations, "- **Data: the release switch in reverse**", "- **API host:**")
    assert data.index("wrangler rollback") < data.index("releases/current.json") \
        < data.index("gh workflow run deploy.yml")
    assert "#the-image-scan-blocks-a-deploy" in data
    deployment = _section(_read(DEPLOYMENT), "- **Data:** the release switch in reverse",
                          "- **API host:**")
    assert deployment.index("wrangler rollback") < deployment.index("gh workflow run deploy.yml")
