# Architecture

Phase 1 is a batch ETL system with one job: turn the official BLS ATUS
multi-year microdata release into a validated PostgreSQL database that later
phases can trust. This document explains the stages, the responsibilities of
each component, and the decisions behind them — specifically for this project,
not as generic ETL doctrine.

## Pipeline stages

```text
   bls.gov ───────────────► data/raw/*.zip ───────► data/staging/0325/*.dat ──┐
            atus download            atus extract                             │
            (curl_cffi,              (streaming unzip,                        │
             manifest.json)           row counts)                             │
                                                                              │
                              atus validate-source  ◄─────────────────────────┤
                              (headers, official row counts, years)           │
                                                                              │
   PostgreSQL "atus" schema ◄────────────────────────────────────────────────-┘
            atus migrate    (plain-SQL migrations, checksummed runner)
            atus load       (row transformers → COPY, one atomic transaction)
            atus validate-db (scalar / violation / cross-file checks)
```

Every stage is a separate CLI command with an inspectable artifact between
stages (files on disk, rows in tables). Nothing requires a GUI or an
undocumented step, and each command is safe to re-run.

## Components

### `sources.py` — the source registry

A declarative description of every official file in a release: URL, archive
member, exact expected header (all 133 respondent columns, all 161 weight
columns, …), and the official record count BLS publishes inside each archive.
Everything downstream — download, extraction, validation, loading — is driven
by this registry, so supporting a future release (e.g. `0326`) is one new
registry entry plus updated row counts, not code changes scattered across the
pipeline.

### Acquisition (`acquisition/download.py`, `manifest.py`)

Downloads single copies of the published files, sequentially with a delay, and
records provenance in `data/manifest.json`: URL, size, SHA-256, timestamps.
BLS's CDN rejects non-browser TLS clients (plain `curl`/`requests` get
HTTP 403 no matter the User-Agent), so the downloader uses `curl_cffi`
browser impersonation; the tradeoffs and the manual fallback are documented in
[source-data.md](source-data.md). Raw archives are treated as immutable — no
stage ever modifies `data/raw/`.

### Staging (`staging/extract.py`)

Copies the CSV data member out of each archive into
`data/staging/<release>/`, streaming (the ATUS-CPS file is 1.2 GB
uncompressed), and records staged row counts in the manifest. Staging exists
so validation and loading read plain files whose exact content is recorded,
and so a corrupted extraction can never masquerade as source truth.

### Source validation (`validation/source_checks.py`)

Fails the pipeline before the database is involved if a staged file's header
deviates from the registered layout, its row count differs from the official
BLS count, or a survey year is missing. A failure here means a corrupted
download or a BLS layout change — both require human attention, not a retry.

### Transformation (`transformation/`)

Pure functions, no I/O. `common.py` holds the field-level rules (ATUS sentinel
handling, implied decimals, diary times, zero-padded activity codes);
`tables.py` maps each source row to a canonical row, one explicit line per
column, so the lineage in [data-lineage.md](data-lineage.md) can be audited
against the code directly. Unknown or malformed values raise
`TransformError` — the pipeline never guesses. This is where three real
data surprises were caught during development (fractional cents in allocated
earnings, an undocumented `TUECYTD=0`, missing-but-asked who codes), each now
handled explicitly and documented.

### Loading (`loading/loader.py`)

Streams staged rows through the transformers into PostgreSQL with the COPY
protocol. Semantics: **one atomic rebuild** — all canonical tables are
truncated and reloaded inside a single transaction, so the database is always
either the previous complete state or the new complete state. A mid-load
failure (including any constraint violation) changes nothing. Run metadata
(`atus.ingestion_runs`, `atus.source_files`) records what was loaded from
which exact files; failed runs remain visible with their error.

Memory stays flat: the only thing held in memory is the set of respondent case
IDs (~259k integers), used to filter the ATUS-CPS file to respondent
households.

### Database validation (`validation/db_checks.py`)

~35 checks in three families, results persisted to `atus.validation_results`:

- **scalar** — measured values vs. authoritative expectations (official BLS
  record counts, BLS-documented 2003/2004 interview counts, lexicon sizes);
- **violations** — ATUS invariants that must hold row-by-row: every diary sums
  to exactly 1440 minutes, cumulative durations are running totals, episode
  clock times match durations modulo midnight, no diary dates inside the 2020
  collection gap, weight-definition rules, roster/CPS/who referential
  structure;
- **cross-file** — episode durations are re-aggregated and compared against
  the independently produced BLS Activity Summary file for a sample of cases.

### Migrations (`database/migrate.py`, `migrations/`)

Plain `.sql` files applied in filename order, each in its own transaction,
tracked with checksums in `public.schema_migrations`. Editing an applied
migration fails loudly. Alembic was considered and deliberately not used:
Phase 1's schema is SQL-first and small, a ~100-line runner is fully readable,
and the tracking table makes a later move to a heavier tool trivial.

## Design decisions and tradeoffs

**Streaming stdlib CSV instead of pandas.** Transformations here are row-wise
and typed; nothing needs dataframe semantics. Streaming keeps peak memory
flat regardless of file size, keeps the transformers pure and unit-testable,
and avoids a heavy dependency whose dtype inference would work against the
explicit typing this project is about.

**Truncate-and-rebuild instead of incremental upserts.** BLS re-releases the
*entire* multi-year dataset once a year (weights and harmonized codes can
change for past years, not just the new year), so "incremental" loading of a
new release would be incorrect anyway. Atomic rebuild is the simplest
semantics that is always right; per-release/versioned storage is a Phase 2+
concern and the run/source metadata already distinguishes releases.

**Canonical ≠ exhaustive.** The database is a *curated canonical* model: the
columns analyses will foreseeably need, exactly typed and documented, rather
than all 133 respondent + 265 CPS variables as opaque text. The full variable
set remains available in the immutable raw/staged files, and adding a column
is a migration plus one transformer line (both covered by tests). The
excluded-by-design files (Activity Summary, Case History, Eldercare Roster)
are documented with reasons in [source-data.md](source-data.md).

**Constraints in the schema, expectations in validation.** Rules BLS documents
unconditionally (the 2020 weight rule, code formats, tier prefixes,
who-placeholder structure) are CHECK/FK constraints — the database cannot hold
data that violates them. Expectations that are empirical or could legitimately
change (row counts, topcodes, year coverage) are validation checks that report
rather than block.

## What Phase 1 deliberately does not do

No API, no web UI, no statistical engine, no caching, no orchestration
framework, no cloud infrastructure. One module boundary exists for each real
concern, and none for hypothetical ones. See [roadmap.md](roadmap.md).
