"""scripts/ops/roles.sql and restore_serving_db.sh, read as files (their
behaviour against a database is in tests/integration/test_serving_restore.py)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from atus_pipeline.serving.contract import LOCKED, READ
from atus_pipeline.serving.guard import SERVING_MARKER
from atus_pipeline.serving.restore import DEFAULT_ROLES_SQL, EXPECTED_ROLE_SETTINGS

OPS = Path(__file__).resolve().parents[2] / "scripts" / "ops"
ROLES_SQL = (OPS / "roles.sql").read_text()
RESTORE_SH = OPS / "restore_serving_db.sh"


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_the_api_role_gets_select_on_exactly_the_served_tables():
    grant = re.search(r"GRANT SELECT ON TABLE(.*?)TO :\"api_role\";", ROLES_SQL, re.S)
    tables = {t.strip().removeprefix("atus.") for t in grant.group(1).split(",")}
    assert tables == set(READ)
    assert set(LOCKED) <= tables  # table-level SELECT: the API's snapshot read locks them
    verified = re.search(r"AS reads_ok\s+FROM unnest\(ARRAY\[(.*?)\]\)", ROLES_SQL, re.S)
    assert {t.strip().strip("'").removeprefix("atus.")
            for t in verified.group(1).split(",")} == set(READ)


def test_roles_sql_grants_no_write_and_holds_no_password():
    code = _code(ROLES_SQL)
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALL", "CREATE", "TEMPORARY"):
        assert not re.search(rf"\bGRANT\s+{privilege}\b", code), privilege
    assert not re.search(r"PASSWORD\s+'", code, re.I)          # no literal password
    assert re.findall(r"\bPASSWORD\s+:'(\w+)'", code) == ["api_password"]


def test_roles_sql_checks_the_serving_marker_before_changing_anything():
    code = _code(ROLES_SQL)
    check = code.index(f"LIKE '{SERVING_MARKER}%'")
    assert check < code.index("BEGIN;") < code.index("CREATE ROLE")
    assert re.search(r"AS serving_ok \\gset\s+\\if :serving_ok\s+\\else\s+DO \$\$ BEGIN "
                     r"RAISE EXCEPTION", code)


def test_roles_sql_sets_the_role_defaults_the_restore_checks():
    settings = dict(re.findall(r"ALTER ROLE :\"api_role\" SET (\w+) = '?([\w]+)'?;", ROLES_SQL))
    assert settings == EXPECTED_ROLE_SETTINGS
    assert "max_parallel_workers_per_gather" not in _code(ROLES_SQL)   # per request (WS-A)


def test_roles_sql_resets_every_setting_of_the_role_and_checks_what_is_left():
    """Review r3 (api-role-persists-changes-past-rotation): the role can set
    its own defaults, role-wide and IN DATABASE, and roles.sql only ever set
    its five, so a rotation left what a leaked password had added. It now
    resets both first, and fails unless exactly the five remain."""
    code = _code(ROLES_SQL)
    first_set = code.index('ALTER ROLE :"api_role" SET ')
    assert code.index('ALTER ROLE :"api_role" RESET ALL;') < first_set
    assert code.index('ALTER ROLE :"api_role" IN DATABASE :"DBNAME" RESET ALL;') < first_set
    check = re.search(r"pg_db_role_setting.*?= ARRAY\[(.*?)\]::text\[\]\s+AS settings_ok \\gset"
                      r"\s+\\if :settings_ok\s+\\else\s+DO \$\$ BEGIN RAISE EXCEPTION", code, re.S)
    assert check, "no settings check"
    listed = [item.strip().strip("'") for item in check.group(1).split(",")]
    assert listed == sorted(f"{k}={v}" for k, v in EXPECTED_ROLE_SETTINGS.items())
    assert "setdatabase IN (0," in check.group(0)          # role-wide and this database
    assert code.index("AS settings_ok") < code.index("COMMIT;")


def test_the_restore_wrapper_never_traces_and_writes_privately():
    text = RESTORE_SH.read_text()
    assert os.access(RESTORE_SH, os.X_OK)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set +x" in text and "umask 077" in text
    assert 'restore-serving-db --roles-sql "$repo/scripts/ops/roles.sql"' in text
    assert DEFAULT_ROLES_SQL == OPS / "roles.sql"


def test_the_restore_wrapper_recommends_only_the_hash_locked_install():
    """The wrapper handles the neondb_owner DSN, so the environment it tells the
    owner to create must be the hash-locked one (docs/deployment.md step 3,
    security.md C19), never an unpinned `uv pip install -e .`."""
    text = RESTORE_SH.read_text()
    assert not re.search(r"pip install (?!--)", text), "an install without its lock flags"
    # the header comment and the error message
    assert text.count("pip install --require-hashes --no-deps -r requirements/dev.lock.txt") == 2
    assert text.count("pip install --no-deps --no-build-isolation -e .") == 2
