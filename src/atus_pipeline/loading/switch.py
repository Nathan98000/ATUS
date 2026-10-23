"""Build-and-switch: load into a shadow schema, then swap its tables in.

``atus load`` used to TRUNCATE and COPY the live tables in one transaction,
holding ACCESS EXCLUSIVE on every canonical table for the whole load (2.5 min
on the full database): every API request blocked and then failed (audit F08),
concurrent analyses could deadlock the load (F19), and the run was marked
``succeeded`` in a later transaction than the data commit (F20). The loader
now builds the new data next to the live data and switches to it in one short
transaction (docs/adr/ADR-013-data-releases-and-serving-db.md):

1. **Build.** :func:`create_shadow` replays the migrations into schema
   ``atus_next``, so the shadow tables are structurally identical to the live
   ones; the loader COPYs into them. Readers never touch ``atus_next``.
2. **Validate (optional).** ``atus validate-db`` checks run against the
   shadow, before anything is live.
3. **Switch.** :func:`switch` locks the live tables (``ingestion_runs`` first,
   then the canonical tables in the order readers touch them), drops the old
   canonical tables, moves the shadow tables into ``atus``, restores their
   owner and grants, and runs the caller's ``publish`` step, which marks the
   run ``succeeded`` and writes its ``data_version``, all in one transaction.
   Metadata tables (``ingestion_runs``, ``source_files``,
   ``validation_results``) are never swapped: their history stays.

Why readers see either all-old or all-new data:

* A transaction that touched any locked table before the switch holds a
  share lock on it, so the switch cannot commit until that transaction ends.
* A statement that starts while the switch holds its locks waits, then
  re-resolves the table names after the commit (PostgreSQL re-checks the
  name after acquiring a lock) and reads the new tables. Cached plans of
  prepared statements are invalidated by the drop and the move.

Why no reader fails:

* The switch is always the one that gives up. Each lock wait is bounded by a
  ``lock_timeout`` small enough that a full attempt ends before PostgreSQL's
  ``deadlock_timeout`` fires in any waiting reader, so a lock-order cycle with
  a reader (F19) ends with the switch rolling back, never with the reader
  being killed. The switch then retries with backoff until ``timeout_s``.
* A reader therefore waits at most one bounded lock wait per table per
  attempt (50 ms by default), and only while a switch attempt is underway.

REPEATABLE READ (F21): a REPEATABLE READ transaction whose snapshot predates
the commit of the shadow data, but which first touches the canonical tables
after the switch, would resolve their names to the new tables and see them
empty. :func:`switch` therefore first waits until no transaction older than
the shadow data's commit remains (``pg_stat_activity.xact_start``; the
loading role needs to see other roles' sessions there, which a superuser or a
member of ``pg_read_all_stats`` does, and is warned otherwise).

A REPEATABLE READ transaction that starts after that commit but takes its
snapshot before the switch commits, and first touches a table after it,
would see the old ``ingestion_runs`` rows (the publish step's update is not
in its snapshot) next to the new tables (moved in by name, their rows
committed before the snapshot): the old data version with the new data. The
guard above cannot see such a reader coming. A REPEATABLE READ reader must
therefore lock ``ingestion_runs`` (ACCESS SHARE) before any statement that
takes its snapshot, as the API's snapshot read does
(``api.database.SNAPSHOT_LOCK``): the switch, which locks that table first,
then either committed before the snapshot or waits for the reader.

The guarantee is per transaction, so it holds for clients that run each
request in one transaction, as the API does (one pooled connection and one
transaction per request).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql

from ..database.migrate import LIVE_SCHEMA, build_schema_copy
from .errors import LoadError

log = logging.getLogger(__name__)

SHADOW_SCHEMA = "atus_next"

# Canonical data tables: rebuilt by every load and swapped in by the switch,
# in the order the switch locks them (the order readers first touch them: the
# engine reads respondents, then the lexicon, then activities and the joined
# tables).
CANONICAL_TABLES: tuple[str, ...] = (
    "respondents",
    "activity_tier1",
    "activity_tier2",
    "activity_codes",
    "activities",
    "household_members",
    "cps_persons",
    "replicate_weights",
    "pandemic_replicate_weights",
    "activity_companions",
)

# Pipeline metadata: append-only history, never swapped.
METADATA_TABLES: tuple[str, ...] = ("ingestion_runs", "source_files", "validation_results")

# Every API request reads ingestion_runs (the data version) first, so it is
# locked first; it is locked but not swapped.
LOCK_ORDER: tuple[str, ...] = ("ingestion_runs", *CANONICAL_TABLES)

_GRANTABLE = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"}
)


@dataclass(frozen=True)
class SwitchOptions:
    """Tuning for the switch; the defaults suit a local database under API load."""

    lock_timeout_ms: int = 50      # upper bound of each lock wait (readers wait at most this)
    timeout_s: float = 120.0       # give up (live data unchanged) after this long


def _table(schema: str, table: str) -> sql.Identifier:
    return sql.Identifier(schema, table)


def create_shadow(conn: psycopg.Connection, migrations_dir: Path) -> None:
    """(Re)create the empty shadow schema from the migrations, in the caller's
    transaction."""
    build_schema_copy(conn, migrations_dir, SHADOW_SCHEMA)


def drop_shadow(conn: psycopg.Connection) -> None:
    """Drop the shadow schema and whatever is left in it (own transaction)."""
    with conn.transaction():
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SHADOW_SCHEMA))
        )


def analyze_shadow(conn: psycopg.Connection) -> None:
    """Collect planner statistics on the new tables before they go live, so
    the first queries after the switch are planned on real statistics."""
    for table in CANONICAL_TABLES:
        with conn.transaction():
            conn.execute(sql.SQL("ANALYZE {}").format(_table(SHADOW_SCHEMA, table)))


def effective_lock_timeout_ms(conn: psycopg.Connection, requested_ms: int) -> int:
    """The per-wait lock timeout: the requested value, capped so that one whole
    attempt (at most one wait per locked table) ends before ``deadlock_timeout``
    elapses in any reader that queued behind it."""
    with conn.transaction():
        deadlock_ms = conn.execute(
            "SELECT setting::int FROM pg_settings WHERE name = 'deadlock_timeout'"
        ).fetchone()[0]
    return max(1, min(requested_ms, deadlock_ms // (len(LOCK_ORDER) + 1)))


def sees_all_sessions(conn: psycopg.Connection) -> bool:
    """Whether this role can see other roles' transactions in
    ``pg_stat_activity`` (superuser or ``pg_read_all_stats``); without it
    :func:`older_transactions` only sees this role's own sessions."""
    with conn.transaction():
        return conn.execute(
            "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]


def older_transactions(conn: psycopg.Connection, cutoff: datetime) -> list[int]:
    """Backend pids of other sessions in this database whose transaction began
    before ``cutoff`` (their snapshots may predate the shadow data)."""
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT pid FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
              AND xact_start < %s
            ORDER BY pid
            """,
            (cutoff,),
        ).fetchall()
    return [row[0] for row in rows]


def _privileges(conn: psycopg.Connection) -> dict[str, tuple[str, list[tuple[str, bool, str]]]]:
    """Owner and explicit grants of each live canonical table:
    ``{table: (owner, [(privilege, grantable, grantee or "PUBLIC"), ...])}``."""
    rows = conn.execute(
        """
        SELECT c.relname,
               pg_get_userbyid(c.relowner),
               a.privilege_type,
               a.is_grantable,
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.grantee = c.relowner
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN LATERAL aclexplode(c.relacl) a ON true
        WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind = 'r'
        """,
        (LIVE_SCHEMA, list(CANONICAL_TABLES)),
    ).fetchall()
    out: dict[str, tuple[str, list[tuple[str, bool, str]]]] = {}
    for table, owner, privilege, grantable, grantee, to_owner in rows:
        grants = out.setdefault(table, (owner, []))[1]
        if privilege is not None and not to_owner:
            grants.append((privilege, grantable, grantee))
    return out


def _restore_privileges(
    conn: psycopg.Connection, privileges: dict[str, tuple[str, list[tuple[str, bool, str]]]]
) -> None:
    """Give the moved-in tables the old tables' grants and owner, then verify.

    Grants come first, while the loading role still owns the new tables (a
    GRANT by a non-owner only warns); the owner change then rewrites the
    grantor. Default privileges do not apply to tables moved into a schema,
    which is why the grants are copied explicitly.
    """
    for table, (_owner, grants) in privileges.items():
        target = _table(LIVE_SCHEMA, table)
        for privilege, grantable, grantee in grants:
            if privilege not in _GRANTABLE:
                raise LoadError(f"unexpected privilege {privilege!r} on atus.{table}")
            conn.execute(
                sql.SQL("GRANT {} ON {} TO {}{}").format(
                    sql.SQL(privilege),
                    target,
                    sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee),
                    sql.SQL(" WITH GRANT OPTION") if grantable else sql.SQL(""),
                )
            )
    restored = _privileges(conn)
    for table, (owner, _grants) in privileges.items():
        if restored[table][0] != owner:
            conn.execute(
                sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    _table(LIVE_SCHEMA, table), sql.Identifier(owner)
                )
            )
    restored = _privileges(conn)
    for table, (owner, grants) in privileges.items():
        new_owner, new_grants = restored[table]
        if new_owner != owner or sorted(new_grants) != sorted(grants):
            raise LoadError(
                f"could not reproduce the owner and grants of atus.{table} on the new "
                "table (the loading role must own the live tables or be a member of "
                "their owner role)"
            )


def _swap(conn: psycopg.Connection, lock_timeout_ms: int) -> None:
    """Inside the switch transaction: lock, drop the old tables, move the new
    ones in. Raises LockNotAvailable when a lock wait times out."""
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{lock_timeout_ms}ms",))
    for table in LOCK_ORDER:
        conn.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(_table(LIVE_SCHEMA, table))
        )
    privileges = _privileges(conn)
    # RESTRICT (the default): an unexpected dependent object such as a user
    # view fails the switch loudly instead of being dropped with the tables.
    conn.execute(
        sql.SQL("DROP TABLE {}").format(
            sql.SQL(", ").join(_table(LIVE_SCHEMA, t) for t in CANONICAL_TABLES)
        )
    )
    for table in CANONICAL_TABLES:
        conn.execute(
            sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
                _table(SHADOW_SCHEMA, table), sql.Identifier(LIVE_SCHEMA)
            )
        )
    _restore_privileges(conn, privileges)


def _backoff_s(attempt: int) -> float:
    # Short and jittered: readers flow freely between attempts.
    return min(0.05 * 2 ** min(attempt, 5), 1.0) * random.uniform(0.5, 1.0)


def switch(
    conn: psycopg.Connection,
    *,
    publish: Callable[[psycopg.Connection], None],
    cutoff: datetime,
    options: SwitchOptions,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Swap the shadow tables into ``atus`` and run ``publish`` in the same
    transaction. ``conn`` must be in autocommit mode; ``cutoff`` is a server
    timestamp taken after the shadow data committed. Returns the number of
    attempts; raises :class:`LoadError` (live data unchanged) on timeout.
    """
    lock_timeout_ms = effective_lock_timeout_ms(conn, options.lock_timeout_ms)
    if not sees_all_sessions(conn):
        log.warning(
            "This role cannot see other roles' sessions in pg_stat_activity, so the "
            "switch cannot wait for their transactions older than the new data "
            "(REPEATABLE READ readers among them could see the new tables empty). "
            "Grant pg_read_all_stats to the loading role."
        )
    deadline = time.monotonic() + options.timeout_s
    attempt = 0
    while True:
        attempt += 1
        blockers = older_transactions(conn, cutoff)
        if blockers:
            reason = (
                f"{len(blockers)} transaction(s) older than the new data are still open "
                f"(pids {', '.join(map(str, blockers[:10]))})"
            )
        else:
            try:
                with conn.transaction():
                    _swap(conn, lock_timeout_ms)
                    publish(conn)
            except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
                reason = f"tables busy ({type(exc).__name__})"
            except psycopg.errors.DependentObjectsStillExist as exc:
                detail = (exc.diag.message_detail or "").replace("\n", "; ")
                raise LoadError(
                    "other database objects depend on the live tables "
                    f"({detail or exc}); drop them before loading and recreate them "
                    "afterwards. The live data is unchanged."
                ) from exc
            else:
                log.info("Switched to the new data (attempt %d)", attempt)
                return attempt
        if time.monotonic() >= deadline:
            raise LoadError(
                f"could not switch to the new data within {options.timeout_s:g} s: {reason}. "
                "The live data is unchanged."
            )
        log.debug("switch attempt %d deferred: %s", attempt, reason)
        sleep(_backoff_s(attempt))
