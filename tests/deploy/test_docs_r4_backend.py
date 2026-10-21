"""Documentation accuracy: the confirmed backend findings of the Phase 5
review, round 4, where the docs were (part of) the defect.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, and reads the source of truth next to the statement where there is one.
Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

from .conftest import REPO_ROOT
from .test_docs_accuracy import (
    ADR,
    ADR_011,
    ADR_013,
    DEPLOYMENT,
    DOCS_DIR,
    OPERATIONS,
    SECURITY,
    _flat,
    _read,
    _row,
    _section,
)

ROLES_SQL = REPO_ROOT / "scripts" / "ops" / "roles.sql"
CHECKS = REPO_ROOT / "src" / "atus_pipeline" / "serving" / "checks.py"
API_INIT = REPO_ROOT / "src" / "atus_pipeline" / "api" / "__init__.py"
PROTECTION = REPO_ROOT / "src" / "atus_pipeline" / "api" / "protection.py"
RUNNER = REPO_ROOT / "src" / "atus_pipeline" / "api" / "analysis_runner.py"
PIN_TEST = REPO_ROOT / "tests" / "unit" / "api" / "test_response_format.py"
ADR_012 = ADR / "ADR-012-load-and-quota-protection.md"
API_DOC = DOCS_DIR / "api.md"
DATABASE_DOC = DOCS_DIR / "database.md"


# --- api-role-can-write-large-objects ----------------------------------------------------


def test_no_document_says_the_api_role_cannot_write_at_all():
    """api-role-can-write-large-objects: any role can create large objects (no privilege
    controls them), so atus_api could store data in the serving DB after turning its
    read-only default off, while roles.sql's closing message, security.md C9, R4 and the
    leaked-password row said it could not write. roles.sql now removes the role's large
    objects and fails on any; the docs name the exposure as R16."""
    roles = _read(ROLES_SQL)
    closing = [line for line in roles.splitlines()
               if line.startswith("\\echo roles.sql: :api_role")]
    assert len(closing) == 1
    assert "cannot write, create objects" not in closing[0]
    assert "large objects" in closing[0] and "R16" in closing[0]
    assert "FROM pg_largeobject_metadata" in roles and "lo_unlink" in roles
    assert "AS large_objects_ok" in roles

    security = _read(SECURITY)
    c9 = _flat(_section(security, "**C9. ", "**C10. "))
    assert "Large objects are the exception (R16)" in c9
    row = _flat(_row(security, "`atus_api` password (in `ATUS_DATABASE_URL`)"))
    assert "No writes to data (C9)" not in row
    assert "large objects up to the project's storage limit (R16)" in row
    r4 = _flat(_section(security, "**R4. ", "**R5. "))
    assert "privileges still refuse every write." not in r4
    assert "They do not refuse large objects" in r4
    r16 = _flat(_section(security, "**R16. ", "## Verification status"))
    for needle in ("EXECUTE to PUBLIC", "must be owner of large object", "ADMIN OPTION",
                   "serving: no large objects", "test_a_rotation_removes_the_large_objects"):
        assert needle in r16, needle

    assert '"no large objects"' in _read(CHECKS)
    assert "Large objects are the exception" in _flat(_read(DATABASE_DOC))
    assert "a test proves the role cannot write any table" in _flat(_read(ADR_011))
    assert "cannot change data." not in _read(ADR_013)
    rotation = _flat(_section(_read(DEPLOYMENT), "### Rotation", "### TLS to the database"))
    assert "removes the large objects the role stored" in rotation
    assert "serving: no large objects" in _flat(_row(_read(OPERATIONS), "`atus_api` password"))


# --- daily-cap-charged-for-db-failures ---------------------------------------------------


def test_the_docs_say_which_analyses_keep_their_daily_cap_unit():
    """daily-cap-charged-for-db-failures: the API took a daily-cap unit before the
    analysis transaction opened and never gave it back, so an outage used up the day;
    ADR-012 and operations.md said the day's compute was not spent and blamed every
    daily_capacity_reached on the governor. The runner now refunds a computation that
    fails before the engine starts, and the docs say so."""
    assert "def refund(self)" in _read(PROTECTION)
    assert "daily_cap.refund()" in _read(RUNNER)
    cap = _flat(_section(_read(ADR_012), "### Daily cap (process-wide backstop)",
                         "### Admission lanes"))
    assert "Only computations that ran keep their unit." in cap
    for code in ("database_unavailable", "database_paused", "overloaded", "data_version_mismatch"):
        assert code in cap, code
    operations = _read(OPERATIONS)
    row = _flat(_row(operations, '503 `daily_capacity_reached`; 429 `rate_limited` with '
                                 '`budget: "prefix"` or `"metadata"`'))
    assert "the API's own backstop cap" in row and "resets on a restart" in row
    unavailable = _flat(_row(operations, "503 `database_unavailable`"))
    assert "the API gives back its own daily-cap unit and analysis token" in unavailable
    assert "fails before the engine starts" in _flat(_read(API_DOC))


# --- response-format-pin-misses-engine-output --------------------------------------------


def test_the_docs_say_what_the_response_format_pin_covers():
    """response-format-pin-misses-engine-output: the pin fingerprinted stub results only,
    while api.md and the RESPONSE_FORMAT docstring said any warning or label change
    without a bump fails CI. The pin now covers the real engine's responses to a fixed
    set of requests, and the docs say that and what it leaves out."""
    pin = _read(PIN_TEST)
    assert "ENGINE_PINNED" in pin and "ENGINE_REQUESTS" in pin
    row = _flat(_row(_read(API_DOC), "`X-Response-Format` (`1`)"))
    assert "the real engine's responses to a fixed set of requests" in row
    assert "add a request to ENGINE_REQUESTS" in row
    docstring = _flat(_read(API_INIT))
    assert "ENGINE_REQUESTS" in docstring


# --- equivalence-nonfinite-blind-spot ----------------------------------------------------


def test_the_criterion_states_the_non_finite_rule():
    """equivalence-nonfinite-blind-spot: a NaN or infinite SE, CI bound or replicate sum
    passed as equal to any finite value, and extra replicate columns were ignored."""
    definition = _flat(_section(_read(OPERATIONS), "### Definition", "### Why these bounds"))
    assert "A non-finite value (NaN, ±inf) matches only the same non-finite value" in definition
    assert "column count" in definition and "length of every replicate array" in definition
