"""Documentation accuracy: the confirmed backend findings of the Phase 5
review, round 3, where the docs were (part of) the defect.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, and reads the source of truth next to the statement where there is one.
Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

from .conftest import REPO_ROOT
from .test_docs_accuracy import (
    ADR,
    DEPLOYMENT,
    OPERATIONS,
    SECURITY,
    _flat,
    _read,
    _row,
    _section,
)

ROLES_SQL = REPO_ROOT / "scripts" / "ops" / "roles.sql"
RESTORE = REPO_ROOT / "src" / "atus_pipeline" / "serving" / "restore.py"
CHECKS = REPO_ROOT / "src" / "atus_pipeline" / "serving" / "checks.py"
SERVING_CLI = REPO_ROOT / "src" / "atus_pipeline" / "serving_cli.py"
RESTORE_TESTS = REPO_ROOT / "tests" / "integration" / "test_serving_restore.py"


def _rotation_item(number: int) -> str:
    rotation = _section(_read(DEPLOYMENT), "### Rotation", "### TLS to the database")
    return _flat(_section(rotation, f"\n{number}. **", f"\n{number + 1}. **"))


# --- post-leak-check-blind-to-rls ------------------------------------------------------


def test_a_leaked_owner_credential_is_answered_with_a_new_project_only():
    """post-leak-check-blind-to-rls: after a leak of the owner DSN or NEON_API_KEY the
    runbooks offered an owner-run `verify-serving-db --content-hash` as the alternative
    to a restore into a new project, and operations.md's "check afterwards" was "the
    content-hash check passes". That check re-hashes rows only, as the connecting role:
    a row-level security policy aimed at atus_api (which the owner bypasses) or a
    search_path setting with a shadow function changes what the API computes and passes
    it, and re-running roles.sql removes neither. The new project is now the only
    answer, and every place says why the check is no substitute."""
    security = _read(SECURITY)
    for secret in ("`neondb_owner` DSN", "`NEON_API_KEY` (optional)"):
        row = _flat(_row(security, secret))
        assert "verify-serving-db" not in row, secret
        assert "new Neon project" in row or "new project" in row, secret
    owner = _flat(_row(security, "`neondb_owner` DSN"))
    assert "without changing any row" in owner and "row-level security" in owner
    assert "re-running roles.sql undoes none of it" in owner

    item = _rotation_item(7)
    assert "atus verify-serving-db --manifest" not in item          # no command to run
    assert "Restore the sha256-verified release dump into a new project" in item
    assert "Do not rely on atus verify-serving-db --content-hash" in item
    assert "or the Cloudflare deploy token while NEON_API_KEY was set" in item
    assert "Or skip the check" not in item
    assert "calls for a restore into a new project, item 7" in _flat(_read(DEPLOYMENT))
    assert "with its data check" not in _read(DEPLOYMENT) + _read(OPERATIONS)

    row = _flat(_row(_read(OPERATIONS), "Neon owner password, `NEON_API_KEY`"))
    assert "the content-hash check passes" not in row
    assert "restore the release into a new project" in row and "no substitute" in row
    # The check itself says what it covers.
    assert "covers row contents only" in _flat(_read(CHECKS))
    assert "Row contents only" in _flat(_read(SERVING_CLI))


# --- api-role-persists-changes-past-rotation ------------------------------------------


def test_the_api_password_leak_and_its_rotation_are_described_as_they_are():
    """api-role-persists-changes-past-rotation: the atus_api role can set its own
    defaults and password and hold its 10 connections; security.md listed reads and
    compute only, and the rotation runbook said open connections keep working. The
    rotation now resets every setting of the role (roles.sql), checks
    pg_db_role_setting, and ends the role's sessions; the docs say so and name the
    proof."""
    sql = _read(ROLES_SQL)
    assert 'ALTER ROLE :"api_role" RESET ALL;' in sql
    assert 'ALTER ROLE :"api_role" IN DATABASE :"DBNAME" RESET ALL;' in sql
    restore = _read(RESTORE)
    assert "def role_settings_check(" in restore and "def end_sessions(" in restore

    row = _flat(_row(_read(SECURITY), "`atus_api` password (in `ATUS_DATABASE_URL`)"))
    assert "ALTER ROLE atus_api [IN DATABASE atus] SET" in row
    assert "hold all 10 of its connections" in row
    assert "resets every setting of the role" in row and "ends the role's open sessions" \
        in row
    r4 = _flat(_section(_read(SECURITY), "**R4.", "**R5."))
    test = "test_a_rotation_undoes_what_a_leaked_api_password_could_change"
    assert test in r4 and f"def {test}(" in _read(RESTORE_TESTS)

    item = _rotation_item(2)
    assert "open connections keep working" not in item
    assert "ends the role's open sessions" in item and "Restart compute" in item
    row = _flat(_row(_read(OPERATIONS), "`atus_api` password"))
    assert "[PASS] restore: API role settings are roles.sql's alone" in row
    assert "[PASS] restore: API role's earlier sessions ended" in row
    roles = _flat(_read(ADR / "ADR-013-data-releases-and-serving-db.md"))
    assert "resets every setting of the role, role-wide and for the database" in roles
