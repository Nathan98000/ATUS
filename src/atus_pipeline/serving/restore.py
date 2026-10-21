"""Restore a serving-database release artifact (``atus restore-serving-db``,
run by ``scripts/ops/restore_serving_db.sh``).

The procedure of brief §9 and ADR-013, in order:

1. read the owner's connection string from ``$ATUS_OWNER_DATABASE_URL`` or the
   owner's secrets file (default ``~/.config/atus/neon.env``); it is never
   printed, and when both are set they must name the same database
   (:func:`owner_url`);
2. verify the dump against its ``.sha256`` file and its manifest;
3. refuse a target database that is not empty (each release goes into a new
   database: on Neon a new project, because one project's 1 GB covers every
   database and branch in it); ``--roles-only`` skips the restore and instead
   refuses a database without the serving marker (``serving/guard.py``). It
   also finishes a restore that failed after ``pg_restore`` committed: step 6
   runs then too, when tables lack planner statistics;
4. ``pg_restore --no-owner --no-acl --single-transaction --exit-on-error``
   over the connection given (Neon: the direct endpoint), with a client at
   least as new as the server's major version and the artifact's
   ``pg_dump``;
5. apply ``scripts/ops/roles.sql`` with the API role's password (reused from
   the secrets file, or newly generated: 192 bits), sent as a SCRAM verifier,
   and write the password and the API's connection string back into the
   secrets file (mode 600). ``--roles-only`` never generates one for a role
   that already exists unless ``--rotate-api-password`` asks for it: that
   would silently break the deployed connection string;
6. ``VACUUM (ANALYZE)`` every table (a scale-to-zero compute may suspend
   before autovacuum runs);
7. checks: the serving checks against the manifest (``serving/checks.py``),
   the API role's session defaults, live proof that the API role cannot
   write, and the worst-case query plans as the API role;
8. the metered size, ``sum(pg_database_size(datname))`` over every database
   the owner can see, against the provider's limit.

Every message passes through :func:`atus_pipeline.database.dsn.redact` with
the known secrets; the optional JSON report holds no secret either.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from ..database.dsn import describe, ensure_system_roots, redact
from ..validation.results import CheckResult
from . import pgtools
from .artifact import manifest_for, read_manifest, verify_artifact
from .checks import explain_worst_cases, serving_checks, tables_without_statistics
from .guard import serving_marker
from .secrets_file import (
    API_PASSWORD_KEY,
    API_URL_KEY,
    DEFAULT_SECRETS_FILE,
    OWNER_URL_KEY,
    api_database_url,
    new_password,
    read_env_file,
    scram_sha256_verifier,
    update_env_file,
)

DEFAULT_SIZE_LIMIT_BYTES = 1_000_000_000   # Neon Free: "1 GB" per project (1e9 or 2^30:
#                                            undocumented; the smaller one is assumed)
DEFAULT_ROLES_SQL = Path(__file__).resolve().parents[3] / "scripts" / "ops" / "roles.sql"

# The API role's session defaults roles.sql sets (ADR-011 "Bounded work").
EXPECTED_ROLE_SETTINGS = {
    "statement_timeout": "85s",
    "lock_timeout": "5s",
    "idle_in_transaction_session_timeout": "30s",
    "jit": "off",
    "default_transaction_read_only": "on",
}

# Statements the API role must not be able to run (the probe turns the
# read-only default off first, so the privileges themselves are tested).
WRITE_PROBES = {
    "INSERT": "INSERT INTO atus.validation_results (check_name, severity, passed) "
              "VALUES ('probe', 'error', true)",
    "UPDATE": "UPDATE atus.respondents SET final_weight = final_weight WHERE false",
    "DELETE": "DELETE FROM atus.activities WHERE false",
    "TRUNCATE": "TRUNCATE atus.ingestion_runs",
    "CREATE TABLE": "CREATE TABLE atus.restore_probe (x int)",
    "CREATE TEMP TABLE": "CREATE TEMP TABLE restore_probe (x int)",
    "DROP TABLE": "DROP TABLE atus.activity_tier1",
}


class RestoreError(RuntimeError):
    pass


@dataclass
class RestoreOptions:
    artifact: Path
    secrets_file: Path = DEFAULT_SECRETS_FILE
    api_role: str = "atus_api"
    rotate_api_password: bool = False
    size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES
    verify_content_hash: bool = False
    roles_sql: Path = DEFAULT_ROLES_SQL
    pg_client_image: str | None = None
    roles_only: bool = False


@dataclass
class RestoreReport:
    target: str = ""
    owner_is_superuser: bool | None = None
    data_version: str = ""
    artifact_sha256: str = ""
    server_version: str = ""
    tools: dict[str, str] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    sizes: dict = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    # roles.sql set a password the secrets file did not hold before: from then
    # on only the file's ATUS_API_DATABASE_URL works, whatever the checks say.
    api_password_changed: bool = False
    passed: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def owner_url(options: RestoreOptions) -> str:
    """The owner's connection string: the environment first, then the file.

    The API role's password and connection string are written into the
    secrets file, which the runbooks keep one per Neon project. So when both
    the environment and the file name the owner's database, they must name
    the same one (user, host, port, database): a stale exported variable
    would otherwise rotate another project's password and write its DSN
    into this project's file (review r1). Nothing is changed then."""
    from_env = os.environ.get(OWNER_URL_KEY)
    path = options.secrets_file.expanduser()
    from_file = read_env_file(path).get(OWNER_URL_KEY) if path.is_file() else None
    if from_env:
        if from_file and describe(from_file) != describe(from_env):
            raise RestoreError(
                f"${OWNER_URL_KEY} names {describe(from_env)}, but {path} names "
                f"{describe(from_file)}; the API role's credentials are written to {path}, "
                f"so both must name the same database. Unset {OWNER_URL_KEY}, or pass "
                "that database's --secrets-file. Nothing was changed."
            )
        return from_env
    if not path.is_file():
        raise RestoreError(
            f"set {OWNER_URL_KEY}, or put {OWNER_URL_KEY}=<owner connection string> "
            f"in {path} (chmod 600)"
        )
    if from_file is None:
        raise RestoreError(f"{path} has no {OWNER_URL_KEY}")
    return from_file


def _role_settings(conn: psycopg.Connection) -> dict[str, str]:
    return {name: conn.execute(f"SHOW {name}").fetchone()[0] for name in EXPECTED_ROLE_SETTINGS}


def write_probes(conn: psycopg.Connection) -> dict[str, str]:
    """Try each write as the connected role, in a READ WRITE transaction that
    is always rolled back (so the privileges are tested, not the role's
    read-only default); returns statement -> outcome (``refused`` when
    PostgreSQL denied it for lack of privilege). ``conn`` must be in
    autocommit mode."""
    conn.read_only = False
    outcomes = {}
    for label, statement in WRITE_PROBES.items():
        try:
            with conn.transaction(force_rollback=True):
                conn.execute(statement)
            outcomes[label] = "ALLOWED"
        except psycopg.errors.InsufficientPrivilege:
            outcomes[label] = "refused"
        except psycopg.Error as exc:
            outcomes[label] = f"failed otherwise: {type(exc).__name__}"
    return outcomes


def _check(name: str, passed: bool, observed: str, detail: str = "") -> CheckResult:
    return CheckResult(name=f"restore: {name}", passed=passed, severity="error",
                       observed=observed, detail=detail)


def restore_client_major(server_major: int, manifest: dict) -> int:
    """The oldest ``pg_restore`` major that can read this artifact: the
    server's, or the recorded ``pg_dump``'s if newer (an archive written by a
    newer ``pg_dump`` has a header an older ``pg_restore`` rejects)."""
    recorded = (manifest.get("tools") or {}).get("pg_dump") or ""
    return max(server_major, pgtools.parse_major(recorded) or 0)


def restore(options: RestoreOptions, echo: Callable[[str], None] = print) -> RestoreReport:
    url = owner_url(options)
    owner_password = conninfo_to_dict(url).get("password") or ""
    secrets: list[str] = [owner_password]
    # sslrootcert=system needs the system's CA bundle on the owner's machine
    # (docs/deployment.md "TLS to the database"); the API role's URL uses it
    # for any remote host, whatever the owner's URL says.
    ensure_system_roots(url, api_database_url(url, options.api_role, "unused"))

    def say(message: str) -> None:
        echo(redact(message, *secrets))

    report = RestoreReport(target=describe(url))
    clock = time.perf_counter()

    def step(name: str, started: float) -> None:
        report.seconds[name] = round(time.perf_counter() - started, 2)

    manifest = read_manifest(manifest_for(options.artifact))
    if not options.roles_only:
        started = time.perf_counter()
        report.artifact_sha256 = verify_artifact(options.artifact, manifest)
        step("verify artifact", started)
        say(f"Artifact {options.artifact.name}: SHA-256 matches its manifest "
            f"({manifest['data_version']})")
    report.data_version = manifest["data_version"]

    with psycopg.connect(url, autocommit=True) as conn:
        report.server_version = conn.execute("SHOW server_version").fetchone()[0]
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000
        dbname, superuser = conn.execute(
            "SELECT current_database(), rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        report.owner_is_superuser = superuser
        say(f"Target {report.target} (PostgreSQL {report.server_version}"
            f"{', superuser' if superuser else ''})")
        occupied = conn.execute(
            "SELECT to_regnamespace('atus') IS NOT NULL "
            "OR to_regclass('public.schema_migrations') IS NOT NULL"
        ).fetchone()[0]
        marker = serving_marker(conn) if occupied else None
        api_role_exists = conn.execute(
            "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = %s)", (options.api_role,)
        ).fetchone()[0]
    if options.roles_only and not occupied:
        raise RestoreError("--roles-only needs a restored serving database")
    if options.roles_only and not marker:
        # roles.sql revokes PUBLIC's privileges and creates a login role: never
        # on a database that is not a serving build (the full database, say).
        raise RestoreError(
            "--roles-only needs a restored serving database, and this one has no "
            "serving marker (is the owner's connection string pointing at the full "
            "database?); nothing was changed"
        )
    if not options.roles_only and occupied:
        raise RestoreError(
            "the target database is not empty: restore each release into a new, empty "
            "database (on Neon, a new project)"
            + (". It holds a restored serving database: if an earlier restore into it "
               "failed after pg_restore (at roles.sql, say), finish that one with "
               "--roles-only" if marker else "")
        )

    # The API role's password: reused unless rotating, so a re-run keeps the
    # deployed connection string valid.
    path = options.secrets_file.expanduser()
    stored = read_env_file(path) if path.is_file() else {}
    if (options.roles_only and not options.rotate_api_password
            and not stored.get(API_PASSWORD_KEY) and api_role_exists):
        # Re-applying roles.sql would set a new password on the live role: the
        # API's connection string would stop working, silently (review r2).
        raise RestoreError(
            f"{path} holds no {API_PASSWORD_KEY}, but the role {options.api_role} exists: "
            "--roles-only would give it a new password, and the API's ATUS_DATABASE_URL "
            "would stop working. Pass the --secrets-file the restore wrote, or add "
            "--rotate-api-password to replace the password on purpose; nothing was changed"
        )

    if not options.roles_only:
        pg_restore = pgtools.resolve("pg_restore", url,
                                     server_major=restore_client_major(server_major, manifest),
                                     image=options.pg_client_image)
        report.tools["pg_restore"] = f"{pg_restore.version()} ({pg_restore.origin})"
        say(f"Restoring with {report.tools['pg_restore']} ...")
        started = time.perf_counter()
        with options.artifact.open("rb") as dump:
            pg_restore.run(["--no-owner", "--no-acl", "--single-transaction",
                            "--exit-on-error", f"--dbname={dbname}"], stdin=dump)
        step("pg_restore", started)

    password = stored.get(API_PASSWORD_KEY)
    if options.rotate_api_password or not password:
        password = new_password()
    changed = password != stored.get(API_PASSWORD_KEY)
    verifier = scram_sha256_verifier(password)
    api_url = api_database_url(url, options.api_role, password)
    secrets += [password, verifier, api_url]

    psql = pgtools.resolve("psql", url, server_major=server_major,
                           image=options.pg_client_image,
                           extra_env={"ATUS_API_ROLE_PASSWORD": verifier})
    report.tools["psql"] = f"{psql.version()} ({psql.origin})"
    started = time.perf_counter()
    result = psql.run(["-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"api_role={options.api_role}",
                       f"--dbname={dbname}", "-f", "-"],
                      input_bytes=options.roles_sql.read_bytes(), secrets=tuple(secrets))
    step("roles.sql", started)
    report.api_password_changed = changed
    say(result.stdout.decode(errors="replace").strip())
    update_env_file(path, {API_PASSWORD_KEY: password, API_URL_KEY: api_url})
    say(f"Wrote {API_PASSWORD_KEY} and {API_URL_KEY} to {path} (mode 600)")

    with psycopg.connect(url, autocommit=True) as conn:
        # --roles-only also finishes a restore that failed after pg_restore
        # (roles.sql refused, say): that database has no planner statistics
        # yet. A suspend never loses them (pg_stats is catalog data).
        if not options.roles_only or tables_without_statistics(conn):
            started = time.perf_counter()
            tables = conn.execute(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname IN ('atus', 'public') AND tableowner = current_user "
                "ORDER BY 1"
            ).fetchall()
            for (table,) in tables:
                conn.execute(f"VACUUM (ANALYZE) {table}")
            step("VACUUM (ANALYZE)", started)
            say(f"VACUUM (ANALYZE): {len(tables)} tables")

        started = time.perf_counter()
        checks = serving_checks(conn, manifest, content_hash=options.verify_content_hash)
        step("serving checks", started)
        report.sizes = sizes(conn)

    started = time.perf_counter()
    with psycopg.connect(api_url, autocommit=True) as api:
        settings = _role_settings(api)
        checks.append(_check(
            "API role session defaults", settings == EXPECTED_ROLE_SETTINGS,
            ", ".join(f"{k}={v}" for k, v in settings.items()),
        ))
        probes = write_probes(api)
        checks.append(_check(
            "API role cannot write", all(v == "refused" for v in probes.values()),
            ", ".join(f"{k} {v}" for k, v in probes.items()),
        ))
        checks.extend(explain_worst_cases(api))
    step("API role checks", started)

    limit = options.size_limit_bytes
    if limit:
        metered = report.sizes["sum_pg_database_size_bytes"]
        checks.append(_check(
            "metered size under the limit", metered <= limit,
            f"{metered:,} B of {limit:,} B ({metered / limit:.0%}); "
            f"headroom {limit - metered:,} B",
            "sum(pg_database_size(datname)) over the databases the owner can see",
        ))
    report.checks = [asdict(c) for c in checks]
    report.passed = all(c.passed or c.severity != "error" for c in checks)
    report.seconds["total"] = round(time.perf_counter() - clock, 2)
    for c in checks:
        status = "PASS" if c.passed else ("FAIL" if c.severity == "error" else "WARN")
        say(f"  [{status}] {c.name}  {c.observed}")
        if not c.passed and c.detail:
            say(f"         -> {c.detail}")
    return report


def sizes(conn: psycopg.Connection) -> dict:
    """The size the provider meters, approximately: every database's size."""
    rows = conn.execute(
        "SELECT datname, has_database_privilege(datname, 'CONNECT'), "
        "CASE WHEN has_database_privilege(datname, 'CONNECT') "
        "     THEN pg_database_size(datname) END "
        "FROM pg_database ORDER BY datname"
    ).fetchall()
    return {
        "database_bytes": conn.execute("SELECT pg_database_size(current_database())")
        .fetchone()[0],
        "sum_pg_database_size_bytes": sum(size for _, ok, size in rows if ok),
        "databases": {name: size for name, ok, size in rows if ok},
        "databases_not_visible": [name for name, ok, _ in rows if not ok],
    }


def write_report(report: RestoreReport, path: Path, *secrets: str) -> None:
    path.write_text(redact(json.dumps(report.as_dict(), indent=2, default=str), *secrets) + "\n")
