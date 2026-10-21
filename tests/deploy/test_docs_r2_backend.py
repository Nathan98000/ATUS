"""Documentation accuracy: the confirmed backend docs findings of the Phase 5
review, round 2.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, and reads the source of truth next to the statement where there is one.
Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

from .conftest import REPO_ROOT
from .test_docs_accuracy import ADR, DOCS, ENV_PRODUCTION, _flat, _name, _read, _section

CONFIG = REPO_ROOT / "src" / "atus_pipeline" / "config.py"
RUNNER = REPO_ROOT / "src" / "atus_pipeline" / "api" / "analysis_runner.py"
ADR_012 = ADR / "ADR-012-load-and-quota-protection.md"
SNAPSHOT_DB_TESTS = REPO_ROOT / "tests" / "integration" / "api" / "test_snapshot_db.py"


# --- dry-run-stale-snapshot-422 --------------------------------------------------------


def test_the_dry_run_is_not_said_to_see_a_data_change():
    """dry-run-stale-snapshot-422: the dry run answers from the snapshot before the
    reload guard runs, so "exactly the error the computation would raise first" held
    only on the snapshot's data, and "uncached analyses detect a change themselves"
    not for those the dry run refuses. Both statements now name the window."""
    runner = _flat(_read(RUNNER).split('"""', 2)[1])
    assert "exactly the error the computation would raise first on the snapshot's data" \
        in runner
    assert "a year or activity code the load added is refused against the stale snapshot" \
        in runner and "never invalidates it itself" in runner
    adr = _read(ADR_012)
    dry_run = _flat(_section(adr, "- **Dry run.**", "- **Reload guard.**"))
    assert "would raise first on the snapshot's data" in dry_run
    assert "cannot see data that changed after the snapshot was read" in dry_run
    stale = _flat(_section(adr, "- **A stale snapshot**", "- **Snapshot reads after"))
    assert "but only those the dry run lets through" in stale
    assert "that 422 never invalidates the snapshot itself" in stale
    test = "test_a_code_the_load_added_is_refused_until_the_snapshot_is_renewed"
    assert test in stale and f"def {test}(" in _read(SNAPSHOT_DB_TESTS)


# --- verify-ca-accepted-without-hostname-check -----------------------------------------


def test_no_document_offers_verify_ca_as_an_alternative():
    """verify-ca-accepted-without-hostname-check: production accepts only verify-full,
    and every line of the docs that names verify-ca says why it is refused."""
    assert 'VERIFYING_SSLMODES = ("verify-full",)' in _read(CONFIG)
    named = 0
    for path in (*DOCS, ENV_PRODUCTION):
        for line in _read(path).splitlines():
            if "verify-ca" in line:
                named += 1
                assert "host name" in line, (_name(path), line[:160])
    assert named >= 4          # api.md, deployment.md, security.md R11, the template
