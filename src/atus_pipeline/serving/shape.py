"""The serving schema, derived from the canonical schema the migrations build.

The serving database keeps every table, column, type, NULL rule and CHECK
constraint the engine depends on (brief §9). Rather than restating that DDL
by hand, the builder reads the canonical tables that the real migrations
created in the target database and derives the serving tables from them:

* a *reshaped* table keeps only its serving-contract columns
  (``serving/contract.py``), with the same types, ``NOT NULL`` rules and
  defaults, every CHECK constraint whose columns it keeps (same name and
  definition), plus the serving-only constraints listed in :data:`RESHAPED`;
* keys, indexes and foreign keys whose columns it keeps are added after the
  data is loaded, with their canonical names (so the indexes match
  ``migrations/0002``);
* tables are recreated rather than altered with ``DROP COLUMN``, which would
  leave dropped-attribute slots in every row.

A future migration that changes a CHECK or a type therefore reaches the
serving database without editing this module, and :func:`missing_checks`
proves after the build that nothing was lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from .contract import READ

SCHEMA = "atus"

# Never read by the engine, the API or validate-analytics (I03); its foreign
# key targets the episode key that the reshaped activities table no longer has.
DROPPED_TABLES: tuple[str, ...] = ("activity_companions",)


@dataclass(frozen=True)
class Reshape:
    """How one canonical table becomes its serving table."""

    columns: tuple[str, ...]
    type_overrides: dict[str, str] = field(default_factory=dict)
    extra_checks: tuple[tuple[str, str], ...] = ()   # (name, CHECK expression)
    comment: str = ""


def _replicates(table: str) -> Reshape:
    columns = READ[table]
    return Reshape(
        columns=columns,
        # Exactly the value the engine computes with: queries.py casts every
        # replicate to float8 before summing. The build asserts the cast is
        # lossless (numeric -> float8 -> numeric round trip).
        type_overrides={c: "double precision" for c in columns if c != "tucaseid"},
        comment=(
            "Serving database: replicate weights stored as float8 (the value the "
            "engine casts to); rows that every estimate excludes are omitted."
        ),
    )


_LINE1 = (("serving_line1_only", "lineno = 1"),)

RESHAPED: dict[str, Reshape] = {
    "respondents": Reshape(
        columns=READ["respondents"],
        comment="Serving database: the columns the engine and API read.",
    ),
    "household_members": Reshape(
        columns=READ["household_members"],
        extra_checks=_LINE1,
        comment="Serving database: line-1 rows (the respondent) only.",
    ),
    "cps_persons": Reshape(
        columns=READ["cps_persons"],
        extra_checks=_LINE1,
        comment="Serving database: line-1 rows (the respondent) only.",
    ),
    "activities": Reshape(
        columns=READ["activities"],
        comment=(
            "Serving database: one row per (tucaseid, activity_code); "
            "duration_minutes is the sum of that code's episode minutes. Rows are "
            "stored in activity_code order."
        ),
    ),
    "replicate_weights": _replicates("replicate_weights"),
    "pandemic_replicate_weights": _replicates("pandemic_replicate_weights"),
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    not_null: bool
    default: str | None


@dataclass(frozen=True)
class Constraint:
    name: str
    kind: str                    # c = check, p = primary key, u = unique, f = foreign key
    definition: str              # pg_get_constraintdef, schema-qualified
    columns: tuple[str, ...]
    references: str | None       # referenced table (foreign keys)


@dataclass(frozen=True)
class Index:
    name: str
    definition: str              # pg_get_indexdef, schema-qualified
    columns: tuple[str, ...]


@dataclass(frozen=True)
class TableShape:
    name: str
    columns: tuple[Column, ...]
    constraints: tuple[Constraint, ...]
    indexes: tuple[Index, ...]   # indexes that do not back a constraint


def read_shapes(conn: psycopg.Connection, schema: str = SCHEMA) -> dict[str, TableShape]:
    """Columns, constraints and indexes of every table in ``schema``.

    Definitions are rendered with an empty ``search_path`` so every name in
    them is schema-qualified (a role named ``atus`` has schema ``atus`` on its
    default path through ``$user``, which would hide the qualification).
    """
    with conn.transaction():
        previous = conn.execute("SELECT current_setting('search_path')").fetchone()[0]
        conn.execute("SELECT set_config('search_path', '', true)")
        tables = [r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname",
            (schema,),
        )]
        shapes = {}
        for table in tables:
            qualified = f"{schema}.{table}"
            columns = tuple(
                Column(name, type_, not_null, default)
                for name, type_, not_null, default in conn.execute(
                    """
                    SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                           pg_get_expr(d.adbin, d.adrelid)
                    FROM pg_attribute a
                    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attnum
                    """,
                    (qualified,),
                )
            )
            constraints = tuple(
                Constraint(name, kind, definition, tuple(cols), referenced)
                for name, kind, definition, cols, referenced in conn.execute(
                    """
                    SELECT con.conname, con.contype, pg_get_constraintdef(con.oid),
                           ARRAY(SELECT a.attname FROM unnest(con.conkey) k
                                 JOIN pg_attribute a
                                   ON a.attrelid = con.conrelid AND a.attnum = k
                                 ORDER BY a.attnum),
                           (SELECT relname FROM pg_class WHERE oid = con.confrelid)
                    FROM pg_constraint con
                    WHERE con.conrelid = %s::regclass AND con.contype IN ('c', 'p', 'u', 'f')
                    ORDER BY con.conname
                    """,
                    (qualified,),
                )
            )
            indexes = tuple(
                Index(name, definition, tuple(cols))
                for name, definition, cols in conn.execute(
                    """
                    SELECT ic.relname, pg_get_indexdef(i.indexrelid),
                           ARRAY(SELECT a.attname FROM unnest(i.indkey) k
                                 JOIN pg_attribute a
                                   ON a.attrelid = i.indrelid AND a.attnum = k
                                 ORDER BY a.attnum)
                    FROM pg_index i
                    JOIN pg_class ic ON ic.oid = i.indexrelid
                    WHERE i.indrelid = %s::regclass
                      AND NOT EXISTS (SELECT FROM pg_constraint con
                                      WHERE con.conindid = i.indexrelid
                                        AND con.conrelid = i.indrelid)
                      AND i.indexprs IS NULL AND i.indpred IS NULL
                    ORDER BY ic.relname
                    """,
                    (qualified,),
                )
            )
            shapes[table] = TableShape(table, columns, constraints, indexes)
        # set_config(..., true) lasts until the end of the enclosing
        # transaction, not this savepoint: put the caller's path back.
        conn.execute("SELECT set_config('search_path', %s, true)", (previous,))
    return shapes


def _kept(names: tuple[str, ...], retained: tuple[str, ...]) -> bool:
    return bool(names) and set(names) <= set(retained)


def serving_columns(shape: TableShape, reshape: Reshape) -> tuple[Column, ...]:
    """The serving table's columns: the contract columns in canonical order."""
    by_name = {c.name: c for c in shape.columns}
    missing = [c for c in reshape.columns if c not in by_name]
    if missing:
        raise ValueError(f"atus.{shape.name} has no column(s) {missing}")
    return tuple(
        Column(c.name, reshape.type_overrides.get(c.name, c.type), c.not_null, c.default)
        for c in shape.columns if c.name in reshape.columns
    )


def kept_checks(shape: TableShape, reshape: Reshape) -> tuple[Constraint, ...]:
    return tuple(
        con for con in shape.constraints
        if con.kind == "c" and _kept(con.columns, reshape.columns)
    )


def create_table_sql(shape: TableShape, reshape: Reshape, schema: str = SCHEMA) -> str:
    """``CREATE TABLE`` for the serving shape (no keys, indexes or foreign keys)."""
    lines = []
    for column in serving_columns(shape, reshape):
        line = f"    {column.name} {column.type}"
        if column.not_null:
            line += " NOT NULL"
        if column.default is not None:
            line += f" DEFAULT {column.default}"
        lines.append(line)
    for con in kept_checks(shape, reshape):
        lines.append(f"    CONSTRAINT {con.name} {con.definition}")
    for name, expression in reshape.extra_checks:
        lines.append(f"    CONSTRAINT {name} CHECK ({expression})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {schema}.{shape.name} (\n{body}\n)"


def reshape_sql(shapes: dict[str, TableShape], schema: str = SCHEMA) -> list[str]:
    """Statements that replace the canonical data tables with empty serving
    tables (one ``DROP TABLE`` for all of them, so their foreign keys between
    each other do not matter; RESTRICT fails on any other dependent object)."""
    dropped = [*DROPPED_TABLES, *RESHAPED]
    statements = ["DROP TABLE " + ", ".join(f"{schema}.{t}" for t in dropped) + " RESTRICT"]
    for table, reshape in RESHAPED.items():
        statements.append(create_table_sql(shapes[table], reshape, schema))
        if reshape.comment:
            literal = reshape.comment.replace("'", "''")
            statements.append(f"COMMENT ON TABLE {schema}.{table} IS '{literal}'")
    return statements


def post_load_sql(shapes: dict[str, TableShape], schema: str = SCHEMA) -> list[str]:
    """Keys, then indexes, then foreign keys of the reshaped tables, for every
    canonical one whose columns the serving table keeps (built once, after
    the data is in)."""
    keys, indexes, foreign = [], [], []
    for table, reshape in RESHAPED.items():
        shape = shapes[table]
        for con in shape.constraints:
            if not _kept(con.columns, reshape.columns):
                continue
            statement = f"ALTER TABLE {schema}.{table} ADD CONSTRAINT {con.name} {con.definition}"
            if con.kind in ("p", "u"):
                keys.append(statement)
            elif con.kind == "f" and con.references not in DROPPED_TABLES:
                foreign.append(statement)
        indexes.extend(
            index.definition for index in shape.indexes if _kept(index.columns, reshape.columns)
        )
    return [*keys, *indexes, *foreign]


def missing_checks(canonical: dict[str, TableShape], serving: dict[str, TableShape]) -> list[str]:
    """Canonical CHECK constraints on kept columns that the serving tables lack
    (by name and definition); empty when the shape was preserved."""
    missing = []
    for table, reshape in RESHAPED.items():
        have = {(c.name, c.definition) for c in serving[table].constraints if c.kind == "c"}
        for con in kept_checks(canonical[table], reshape):
            if (con.name, con.definition) not in have:
                missing.append(f"{table}.{con.name}")
    return missing
