"""The serving database's content hash.

One SHA-256 that identifies the data a serving database serves, independent
of when or where it was built: two builds from the same full database give the
same hash, and any changed cell, row or column type changes it. The serving
``data_version`` carries its first 12 hex digits
(``<release>:serving-<build_id>:<content_hash[:12]>``), and the release
manifest records it in full so a restored copy can be checked against it.

Definition (version 1):

* per served table, in :data:`HASHED_TABLES` order: SHA-256 over the bytes of
  ``COPY (SELECT <every column, in table order> FROM atus.<table> ORDER BY
  <unique key>) TO STDOUT`` (text format), with every setting that affects
  that text pinned (:func:`pin_text_output`): ``extra_float_digits = 1``
  prints the shortest text that reads back as the exact same ``float8``;
* combined: SHA-256 over one line per table,
  ``<table>\\t<column>:<type>,...\\t<rows>\\t<table sha256>\\n``.

The metadata tables are excluded: ``ingestion_runs`` (it holds the build id,
which differs per build), ``source_files`` (provenance) and
``validation_results`` (written by ``atus validate-analytics``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg
from psycopg import sql

HASH_FORMAT = 1

# (table, unique key used for the fixed order). activities is ordered as it is
# stored: by activity code, then respondent.
HASHED_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("activity_tier1", ("code",)),
    ("activity_tier2", ("code",)),
    ("activity_codes", ("code",)),
    ("respondents", ("tucaseid",)),
    ("household_members", ("tucaseid", "lineno")),
    ("cps_persons", ("tucaseid", "lineno")),
    ("activities", ("activity_code", "tucaseid")),
    ("replicate_weights", ("tucaseid",)),
    ("pandemic_replicate_weights", ("tucaseid",)),
)
HASHED_TABLE_NAMES = tuple(name for name, _ in HASHED_TABLES)

# Settings that change COPY's text output; pinned on every session that
# produces or consumes hashed text.
TEXT_OUTPUT_SETTINGS: tuple[tuple[str, str], ...] = (
    ("extra_float_digits", "1"),
    ("DateStyle", "ISO, YMD"),
    ("IntervalStyle", "postgres"),
    ("TimeZone", "UTC"),
    ("client_encoding", "UTF8"),
    ("bytea_output", "hex"),
)


@dataclass(frozen=True)
class TableDigest:
    table: str
    columns: tuple[tuple[str, str], ...]   # (name, type) in table order
    rows: int
    sha256: str

    def line(self) -> str:
        cols = ",".join(f"{name}:{type_}" for name, type_ in self.columns)
        return f"{self.table}\t{cols}\t{self.rows}\t{self.sha256}\n"


@dataclass(frozen=True)
class ContentHash:
    content_hash: str
    tables: tuple[TableDigest, ...]

    def as_dict(self) -> dict:
        return {
            "format": HASH_FORMAT,
            "content_hash": self.content_hash,
            "tables": {
                t.table: {"rows": t.rows, "sha256": t.sha256} for t in self.tables
            },
        }


class StreamDigest:
    """SHA-256 and row count of a COPY text stream, fed chunk by chunk."""

    def __init__(self) -> None:
        self._sha = hashlib.sha256()
        self.rows = 0
        self.bytes = 0

    def update(self, chunk) -> None:
        data = bytes(chunk)
        self._sha.update(data)
        # COPY text format escapes newlines inside values, so every row ends
        # in exactly one newline.
        self.rows += data.count(b"\n")
        self.bytes += len(data)

    def hexdigest(self) -> str:
        return self._sha.hexdigest()


def pin_text_output(conn: psycopg.Connection) -> None:
    """Pin the session settings that affect COPY text (see the module doc)."""
    for name, value in TEXT_OUTPUT_SETTINGS:
        conn.execute(sql.SQL("SET {} = {}").format(sql.Identifier(name), sql.Literal(value)))


def table_columns(conn: psycopg.Connection, table: str, schema: str = "atus"
                  ) -> tuple[tuple[str, str], ...]:
    """(name, type) of every column of ``schema.table``, in table order."""
    rows = conn.execute(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (f"{schema}.{table}",),
    ).fetchall()
    if not rows:
        raise LookupError(f"table {schema}.{table} does not exist")
    return tuple((name, type_) for name, type_ in rows)


def ordered_select(table: str, columns: tuple[str, ...], key: tuple[str, ...],
                   schema: str = "atus") -> sql.Composed:
    """``SELECT <columns> FROM schema.table ORDER BY <key>``."""
    return sql.SQL("SELECT {cols} FROM {schema}.{table} ORDER BY {key}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        key=sql.SQL(", ").join(sql.Identifier(c) for c in key),
    )


def digest_query(conn: psycopg.Connection, query: sql.Composable) -> StreamDigest:
    """Digest of ``COPY (<query>) TO STDOUT`` (text format)."""
    digest = StreamDigest()
    with conn.cursor() as cur:
        with cur.copy(sql.SQL("COPY ({}) TO STDOUT").format(query)) as copy:
            for chunk in copy:
                digest.update(chunk)
    return digest


def combine(tables: list[TableDigest] | tuple[TableDigest, ...]) -> ContentHash:
    combined = hashlib.sha256()
    for table in tables:
        combined.update(table.line().encode())
    return ContentHash(content_hash=combined.hexdigest(), tables=tuple(tables))


def compute_content_hash(conn: psycopg.Connection, schema: str = "atus") -> ContentHash:
    """The content hash of the served tables in ``schema``.

    Pins the text-output settings for the rest of the caller's transaction
    (``SET LOCAL`` semantics are not used, so call it in a session or
    transaction dedicated to hashing).
    """
    pin_text_output(conn)
    digests = []
    for table, key in HASHED_TABLES:
        columns = table_columns(conn, table, schema)
        stream = digest_query(conn, ordered_select(table, tuple(c for c, _ in columns), key,
                                                   schema))
        digests.append(TableDigest(table, columns, stream.rows, stream.hexdigest()))
    return combine(digests)
