# ADR-013: Data releases, the local reload, and the serving database

**Status:** Accepted (2026-10-20, Phase 5): the local reload, the `data_version` writers, release parameterization, the serving marker and contract, the serving-database builder and its release artifact, roles and the restore procedure, the engine's year pushdown, and the hosted release procedure.

**Related:** [ADR-011](ADR-011-hosting.md) (hosting; its "Release switch" section defers to this ADR), ADR-012 (load protection; how the API uses `data_version`). Audit findings in [`../operations.md`](../operations.md): F06, F08, F19, F20, F21, F22, F23, F37, F38, F39, F40, F55, F56, F57, F66 and facts I01-I04, I12.

## Context

The audit measured the pre-Phase-5 reload on the fixture database (`docs/operations.md` 7.1, 7.4, 7.5, 7.8):

- **F08.** `atus load` TRUNCATEd and re-COPYed the live canonical tables in one transaction, holding ACCESS EXCLUSIVE on all of them for the whole load (2:29.58 on the full database). Every database-backed request blocked; with the pool's 10 s acquire timeout, later requests (cache hits included) failed with 503.
- **F19.** TRUNCATE locked the tables in list order, the engine in its own order; with analyses in flight PostgreSQL aborted the loader (5 of 5 runs).
- **F20.** The data and the run's `succeeded` status committed in separate transactions, so results could be cached under the pre-reload version, and a failure between the two left new data under the old version.
- **F21.** A REPEATABLE READ request whose snapshot predated the commit saw the truncated tables as empty.
- **F06.** `data_version` was `<release>:run<id>`; every freshly built database, including the serving prototype, reported `0325:run1`.
- **F22.** One database error inside `atus validate-analytics` failed every later benchmark, and under a read-only role the command printed nothing.
- **F23, F55.** Release `0325` and its 2003-2025 span were hard-coded in served text, the registry, fixtures and scripts.
- **F57, I03.** A serving database records the full migrations as applied, so nothing stopped `migrate`/`load`/`validate-db` from running against it, and the set of tables and columns the engine needs was known only informally.

The brief (§15) requires: no blocked or failed requests during a switch, a guaranteed `data_version` change, a meaningful `atus validate-db`, a live schema that is never empty after a failure, and a documented, tested procedure for the next multi-year release (2003-2026).

## Decision 1: build in a shadow schema, then switch tables in one short transaction

`atus load` (`src/atus_pipeline/loading/loader.py`, `switch.py`) now works in four steps:

1. **Record the attempt.** Insert an `ingestion_runs` row with status `running` (visible to operators; ignored by readers, which only read `succeeded` runs). Before that, under a session advisory lock that allows one loader per database, any run left `running` by a killed loader is marked `failed`, and the live schema must match this code's migrations exactly.
2. **Build.** In one transaction: replay every migration into schema `atus_next` (`atus.` references retargeted; SQL that names the schema unqualified is refused), so the shadow tables are structurally identical to the live ones by construction; then COPY the lexicon and the staged files into them. After the commit, `ANALYZE` the shadow tables so the first queries after the switch are planned on real statistics.
3. **Validate (optional, `atus load --validate`).** Run the `atus validate-db` suite against `atus_next`. Any failed error-severity check refuses the switch; the results are recorded in `atus.validation_results` together with the switch.
4. **Switch.** One transaction:
   - `lock_timeout` set for this transaction only;
   - `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE` on `ingestion_runs`, then the ten canonical tables in the order readers first touch them (respondents, the three lexicon tables, activities, household members, CPS persons, both replicate-weight tables, companions);
   - read the old tables' owner and grants; `DROP TABLE` the old canonical tables (RESTRICT, so a user view on them fails the switch instead of being dropped); `ALTER TABLE atus_next.<t> SET SCHEMA atus` for each; re-grant and re-own, then verify the grants match;
   - record provenance (`source_files`) and validation results; set the run to `succeeded` with its new `data_version`;
   - commit. The leftover shadow (only its unused copies of the metadata tables) is dropped afterwards.

The metadata tables (`ingestion_runs`, `source_files`, `validation_results`) are never swapped: their history stays, and run ids keep increasing within a database.

### Why readers never see a mix, and never fail

- **All-old or all-new.** A transaction that has touched any locked table holds a share lock on it, so the switch cannot commit until that transaction ends. A statement that starts while the switch holds its locks waits; after the commit, PostgreSQL re-resolves the table name after acquiring the lock and reads the new table. Cached plans of prepared statements are invalidated by the drop and the move. Each API request is one transaction, so a response is computed entirely on one version, and the version it reads from `ingestion_runs` belongs to the same commit as the data. An analysis reads that version first; when it differs from the API's in-process snapshot (whose lexicon and years the engine otherwise reuses), the engine reads its own lexicon and years in the same transaction, and the response is labelled with the version read ([api.md](../api.md), "Concurrent-reload guard").
- **The switch always gives way.** Each lock wait is bounded by `lock_timeout` (50 ms by default), capped at `deadlock_timeout / 12` (one wait per locked table plus one) so that a whole attempt ends before PostgreSQL's deadlock detector could run in a reader queued behind it. In a lock-order cycle (F19) the switch therefore times out, rolls back and retries with a short jittered backoff; the reader is never the victim. A reader waits at most one bounded lock wait per table while an attempt is underway. After `--switch-timeout` (120 s by default) the load fails with the live data unchanged.
- **REPEATABLE READ (F21).** A REPEATABLE READ transaction whose snapshot predates the commit of the shadow data, but which first touches the canonical tables after the switch, would resolve their names to the new tables and see them empty (PostgreSQL behaviour, demonstrated by a test). Before every attempt the switch therefore waits until no other session in the database has a transaction that began before the shadow data committed (`pg_stat_activity.xact_start`). This needs the loading role to see other roles' sessions: a superuser or a member of `pg_read_all_stats` does, and the loader warns when it cannot. A REPEATABLE READ transaction that starts after that commit but takes its snapshot before the switch would see the old `ingestion_runs` rows (the run's `succeeded` update is not in its snapshot) next to the new tables, so it must lock `ingestion_runs` before any statement that takes its snapshot. The API's snapshot read does (`LOCK TABLE atus.ingestion_runs IN ACCESS SHARE MODE` after `SET LOCAL` limits, `api/database.py`): the switch locks that table first, so it either committed before the snapshot or waits for the read.

### Options considered

| Option | Readers during the build | Version atomic with data | Responses consistent | Failure leaves | Verdict |
|---|---|---|---|---|---|
| A. In-place TRUNCATE + COPY (status quo) | blocked for the whole load, then 503 (F08) | no (F20) | yes, but REPEATABLE READ sees empty tables (F21) | intact, but deadlocks abort loads (F19) | rejected |
| B. Shadow schema, then `ALTER SCHEMA ... RENAME` without locks | not blocked | yes | **no**: a request, or even one statement resolving several tables, can straddle the rename and join old and new tables | intact | rejected: statistics could be computed on mixed data; grants and default privileges are tied to the schema's identity and history would need copying |
| **C. Shadow schema, per-table switch under ACCESS EXCLUSIVE with bounded waits and retries** | not blocked | yes | yes | intact | **chosen** |
| D. Load into a new database, then point the API at it | not blocked | yes | yes | intact | rejected for the local reload: the running API must be reconfigured, which locally means a restart and dropped requests (or new runtime pool-switching code in the API). It is the hosted procedure (ADR-011: a new Neon project per release, switched by an environment change) |
| E. Views over versioned schemas, swapped with `CREATE OR REPLACE VIEW` | not blocked | yes | yes | intact | rejected: the same locks as C plus a change to what the engine queries (views instead of tables), which would need its own §9 equivalence proof |

### Guarantees and the tests that prove them

| Guarantee | Test |
|---|---|
| Zero non-2xx and zero transport errors for continuous reads through the real API (uvicorn, pool, engine) during a rebuild-and-switch; reads keep being served old data while the new data sits built in the shadow; every cached or computed result pairs the old key with the old value or the new key with the new value; `/meta` never pairs one run with the other run's counts. A negative control with the old TRUNCATE loader blocks every reader and fails this test | `tests/integration/test_reload_under_api_load.py` |
| Data and `data_version` switch in one commit; identical reloads get different versions; history kept | `tests/integration/test_reload_switch.py::TestSwitch` |
| The shadow is structurally identical to the live schema (columns, types, defaults, constraints, indexes); every table is classified as swapped or kept, and no foreign key crosses the two groups | `TestSwitch` |
| A COPY failure, a failed validation, an injected failure inside the switch transaction (F20), a dependent view, a busy table that never frees, and an old transaction that never ends all leave the live data and version unchanged and mark the run failed | `TestFailedLoads`, `TestReaders`, `TestRepeatableRead` |
| A lock-order cycle with a reader (F19) never kills the reader; the switch retries | `TestReaders::test_a_lock_order_cycle_with_a_reader_never_kills_the_reader` |
| REPEATABLE READ (F21): the pitfall is real; the switch waits for older transactions; their old snapshot keeps reading the complete old tables | `TestRepeatableRead` |
| Grants survive the switch; prepared statements and long-lived engines see the new data | `TestReaders` |
| `--validate` runs validate-db on the shadow and refuses to switch on failure | `TestValidation`, `tests/integration/test_cli_data_commands.py` |

### Costs and limits

- **Disk.** The database holds the old and the new data until the switch drops the old tables: about twice the canonical data during a load (the full database is 1,980.1 MiB, `docs/operations.md`, "§2 sizes").
- **Reader waits.** Requests can wait for a lock for up to one `lock_timeout` per table during a switch attempt (50 ms by default). Nothing in this ADR measured latency; the guarantee is functional (no failures, no mixing).
- **Long transactions delay the switch.** An idle-in-transaction session (a forgotten `psql`, a notebook) holds the switch back until it ends or `--switch-timeout` expires; the error lists the blocking pids.
- **Autovacuum delays the switch too.** PostgreSQL cancels an autovacuum that blocks a lock request only after `deadlock_timeout`, and the switch's lock waits are deliberately shorter, so an autovacuum or autoanalyze running on a live canonical table holds the switch back until it finishes (bounded by `--switch-timeout`; not observed in the tests, which run on small tables).
- **Per-transaction guarantee.** Consistency holds for clients that run each request in one transaction, as the API does; a client in autocommit mode that spreads one logical request over several statements can still straddle the switch.
- **Dependent objects.** A view or other object that depends on a canonical table fails the switch loudly. Drop it before loading and recreate it afterwards.
- **Roles.** Grants on the canonical tables are copied explicitly (default privileges do not apply to tables moved into a schema). The loading role must own the live tables or be a member of their owner role.
- **The schema must be current.** `atus load` refuses to run unless every migration file is applied with a matching checksum: the shadow is built from the files, the swap must not change the schema.

## Decision 2: a globally unique `data_version`, written with the data (F06, F20)

- Migration `0003_data_version.sql` adds `ingestion_runs.data_version text`, nullable for legacy rows, `UNIQUE`, and allowed only on `succeeded` runs.
- Writers:
  - `atus load`: `<release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 random hex>`, set in the switch transaction together with the data and the `succeeded` status;
  - the serving-DB builder: `<release>:serving-<build_id>:<content_hash[:12]>`, `build_id = <UTC YYYYMMDDTHHMMSSZ>-<8 random hex>` (formatters in `atus_pipeline.database.versions`; the builder is Decision 6).
- Reader: `atus_pipeline.database.versions.read_data_version` prefers the explicit column and falls back to `<release>:run<id>:<started_at epoch micros>` for legacy rows. Every format starts with `<release>:`.
- How the API uses the version (cache keys, `X-Data-Version`, `ATUS_EXPECTED_DATA_VERSION`) belongs to ADR-012.

## Decision 3: `atus validate-analytics` evaluates, prints, then records (F22)

Each benchmark runs in its own transaction (a savepoint inside a caller's), so one database error fails one benchmark. The command prints every result before recording them in `atus.validation_results`; recording needs the owner/loader role, and under a read-only role the command reports that nothing was recorded and exits 1. `--no-persist` only prints. The serving database keeps an empty `validation_results` for this reason, and these checks never run as the API role.

## Decision 4: everything release-specific derives from the registry (F23, F55)

- `atus_pipeline.sources` holds one `Release` per BLS multi-year release: code, first and last survey year, and its source files. `multiyear_release()` derives every archive name, data-member name, URL and description from the code, so a new release is one registry entry with its official record counts (and, if BLS widened them, the column counts of the CPS and activity-summary files).
- Derived from the entry: the staging directory (`data/staging/<release>/`), expected survey years (source and database validation), the committed lexicon file `data/reference/activity_lexicon_<release>.csv`, and the lexicon label (`2003-25`).
- The engine reads the loaded release (the same statement as the data version) and names its lexicon in the harmonization warning on every result and in the unknown-code error. For release `0325` the text is byte-identical to Phases 2-4.
- Tests: the registry refactor leaves `0325` unchanged field for field; a synthetic `0326` (2003-2026) registered at runtime flows through source validation, the load, the engine, `validate-db` and the API with no other code change (`tests/integration/test_release_checklist.py`); an AST guard keeps release literals out of the data, engine and validation packages; the performance scripts take their years from the data and `/meta`.
- Not covered here: one example response in the API's OpenAPI schema still shows `0325` output (its warning names the 2003-25 lexicon), while the descriptions are release-neutral ([operations.md](../operations.md), F23); and the frontend test that the year pickers follow `/meta` belongs to the frontend workstream (added later: `frontend/src/pages/newSurveyYear.test.tsx`, `frontend/e2e/new-survey-year.spec.ts`).

### New ATUS year procedure (e.g. 2003-2026, release `0326`)

1. Read BLS's release notes and the "Changes between data files" document. Note new or recoded variables and any layout change.
2. Add a `multiyear_release("0326", first_year=2003, last_year=2026, expected_rows={...})` entry to `_REGISTRY` with the record counts from each new `*_info.txt`. Update the header constants only if a layout changed (validation fails loudly otherwise).
3. Produce `data/reference/activity_lexicon_0326.csv` from the new lexicon PDF and review the diff (see [`../source-data.md`](../source-data.md), "The activity lexicon changes per release").
4. Set `ATUS_RELEASE=0326` and run `download → extract → validate-source → migrate → load --validate → validate-db → validate-analytics`.
5. Benchmarks: the 24 existing benchmarks reproduce values BLS published for specific years, which a new release re-releases. Run them unchanged; if BLS revised a published value, record the revision, do not edit the expectation to make it pass. Add the new year's published Table A-1 values as new benchmarks.
6. Build the serving database for the new release, check its size against the provider limit, and run the equivalence tests (§9): steps 2-3 of the release procedure below.
7. Release it through the hosted switch (steps 4-10 below), which leaves the previous project in place for rollback.

## Decision 5: the serving marker and the serving contract (F57, I03)

- **Marker.** A serving database is identified by either of two explicit markers, both written by the builder: the comment on schema `atus` starts with `atus-serving`, or the latest succeeded run's `data_version` is a serving version. `atus migrate`, `atus load` and `atus validate-db` refuse to run against it (`atus_pipeline.serving.guard`), and so does `load_all` itself.
- **Contract.** `atus_pipeline.serving.contract` lists the tables and columns the engine, the API and `validate-analytics` touch. A test grants a role exactly those columns (SELECT, plus INSERT on `validation_results`) and runs the engine over every population dimension, weight scheme and variance mode, every HTTP endpoint and the benchmark suite as that role; any read outside the contract, or any API write, fails. `missing_contract_columns()` is the assertion the builder runs on its output. `atus validate-db` is outside the contract by design: it checks the full database.
- **Migrations outside a checkout (F56).** The migrations directory is `$ATUS_MIGRATIONS_DIR` or the checkout's `migrations/`. The API never needs it.

## Decision 6: a reduced serving database, derived from the full one (I01-I04)

The full database is 1,980 MiB and fits no free managed Postgres tier; the engine, the API and `atus validate-analytics` read a small, known part of it (the serving contract, Decision 5). `atus build-serving-db` (`src/atus_pipeline/serving/builder.py`) derives a serving database that keeps that part with every name, type, NULL rule and CHECK constraint the engine depends on, so the engine code runs on it unchanged.

| Table | Full DB (release 0325) | Serving DB | Why no result can change |
|---|---|---|---|
| `activities` | 4,994,172 episodes | 2,982,182 rows, one per (`tucaseid`, `activity_code`); `duration_minutes` = the summed minutes, still `smallint`; stored in `activity_code` order behind `idx_activities_activity_code` | The engine reads activities only as `sum(duration_minutes)` per respondent over a set of leaf codes. Integer sums are exact, a 0-minute episode still sums to 0, and a `smallint` column keeps `sum()` returning `bigint` (I01). The build asserts every sum fits 0-1440 |
| `replicate_weights` | 258,954 rows, `numeric(18,6)` | 250,172 rows, `float8` | The engine casts every replicate to `float8` before summing, so it receives the same value. The 8,782 omitted rows are all-NULL and belong to respondents with `final_weight IS NULL` (2020), which `final_weight > 0` excludes (I02). The build asserts both, and that the numeric -> float8 -> numeric round trip changes no cell |
| `pandemic_replicate_weights` | 18,217 rows | 16,751 rows, `float8` | The 1,466 omitted rows are all-zero and belong to respondents with `pandemic_weight = 0`, which `pandemic_weight > 0` excludes (asserted) |
| `household_members`, `cps_persons` | 702,409 and 681,624 rows | 258,954 rows each: line 1 (the respondent), the contract columns, `lineno` kept, plus `CHECK (lineno = 1)` | The engine joins them `ON ... lineno = 1` with inner-join semantics, so other lines are never read; NULLs (e.g. a missing education) are kept |
| `respondents` | 258,954 rows, 29 columns | all 258,954 rows, the 8 contract columns; point weights stay `numeric(18,6)` | Full-sample sums stay exact `numeric`; `/meta` counts every respondent |
| lexicon (`activity_tier1/2`, `activity_codes`) | 556 rows | copied whole | |
| `ingestion_runs` | the load history | one `succeeded` row: the source run's id, release, pipeline version and timestamps (so `/meta` describes the same ingested data), the serving `data_version`, and a JSON note (build id, content hash, source data version) | |
| `source_files` | provenance per run | the source run's rows (which BLS files produced the data) | |
| `validation_results` | check history | empty; `atus validate-analytics` writes there (owner role only) | |
| `activity_companions` | 6,358,042 rows | dropped | never read (I03) |

**Derived, not restated.** The builder applies the real migrations to the new database, reads the canonical tables they created (`serving/shape.py`), and recreates each reshaped table with its contract columns in canonical order, the same types (replicates overridden to `double precision`), `NOT NULL` rules and defaults, and every CHECK constraint whose columns it keeps, under the same name. After the data is in, it adds the canonical keys, indexes and foreign keys whose columns it keeps, with their canonical names. Tables are recreated rather than altered with `DROP COLUMN`, which would leave dropped-attribute slots in every row. A future migration that changes a CHECK therefore reaches the serving database without code changes. `public.schema_migrations` records 0001-0003 as applied, which is why the explicit serving marker exists (F57).

**All or nothing, source read only.** The source is read in one `REPEATABLE READ READ ONLY` transaction of a session with `default_transaction_read_only = on`; a role with only `SELECT` builds successfully (test). The target must be new (empty, or freshly migrated and empty); everything written to it, the migrations included, happens in one transaction, which commits only if every invariant holds:

1. the source assertions above count zero;
2. by default, the source has passing `atus validate-db` and `atus validate-analytics` results recorded for its loaded data, that is at or after its run's `finished_at` (a run during the load's build checked the previous data, still live then; `atus load --validate` records at exactly `finished_at`). `--allow-unvalidated` is for test fixtures;
3. served rows reconcile with the source (rows minus omitted rows; one row per (respondent, code) group);
4. every contract column and every kept CHECK constraint is present, `activity_companions` is gone, and the activities are physically in `activity_code` order (no inversion in `ctid` order);
5. every served table re-read from the target hashes to exactly the bytes streamed from the source transformation (cell-exact, `float8` bits included);
6. `validation_results` is empty and the new data version reads back.

A failure leaves the target as it was, an empty database empty (tests inject a lossy `float8` cell, an omitted row an estimate would read, minute sums above 1,440, a hash mismatch after the copy, a non-new target and a serving database as the source). Then `VACUUM (ANALYZE)` runs.

**Content hash and data version.** The content hash is SHA-256 over every served table's rows (`COPY ... TO STDOUT` text, every column, ordered by a unique key, with the settings that affect the text pinned: `extra_float_digits = 1` prints the shortest text that reads back as the same `float8`), combined over `<table>\t<column>:<type>,...\t<rows>\t<sha256>` lines (`serving/content.py`). `ingestion_runs` (it holds the build id), `source_files` and `validation_results` are excluded, so two builds from the same full database have the same content hash. The data version is `<release>:serving-<build_id>:<content_hash[:12]>` with `build_id = <UTC YYYYMMDDTHHMMSSZ>-<8 random hex>`: it differs for every build, including two builds of the same data, so caches keyed by it can never serve one database's result for another, and its suffix says which data it is. Tests: two builds from the fixture have equal content hashes and different data versions; a one-minute change in the source changes the content hash.

**Measured on the real full database (2026-10-20).** Three builds (`20261020T220809Z-ab187c5e`, `20261020T222927Z-5bf8eb66`, `20261020T224252Z-a7420a5c`) gave the same content hash, `de834e672474e24898d20c17585f0ef48cc39c0e8df2289bc96b08034dd7f833`. All nine per-table hashes equal those of the audit prototype ([`../evidence/phase5/02-serving-proto.md`](../evidence/phase5/02-serving-proto.md)), which passed 24/24 benchmarks and the engine diff, so the builder serves cell-for-cell the same rows. The database is 629,357,591 B (600.2 MiB), of which the `atus` tables are 620,363,776 B. A build took 39.8 s, and the dump about 24 s more (illustrative: a shared laptop).

## Decision 7: the release artifact is the backup

Free tiers have no scheduled backups, so a versioned dump is the backup (brief §9). `atus build-serving-db --artifact-dir DIR` (keep `DIR` outside the repository) writes three files:

| File | Content |
|---|---|
| `atus-serving-<release>-<build_id>.dump` | `pg_dump --format=custom --no-owner --no-acl`: no roles, grants or passwords |
| `....dump.sha256` | `<sha256>  <file>` (`sha256sum -c` format) |
| `....manifest.json` | release, data version, content hash, build id, every table's rows, SHA-256 and size, the omitted-row counts, the source's data version and validation evidence, database sizes, the artifact's size and SHA-256, and tool versions (server, `pg_dump`, Python, psycopg, libpq, package, git commit and dirtiness). No connection string |

- **Client.** `pg_restore` and `psql` run locally when their major version is at least the server's, otherwise from the `postgres:<server major>` image through docker (`serving/pgtools.py`). `pg_dump` runs locally only when its major version **equals** the server's (16), otherwise from `postgres:16`: a newer `pg_dump` writes an archive version that the deploy runner's `pg_restore` 16 rejects, and statements a 16 server does not know (amendment, review round 1). The restore also picks a `pg_restore` at least as new as the `pg_dump` the manifest records. The connection travels in libpq environment variables, never in a command line; tool output is redacted.
- **TLS from the owner's machine.** `sslrootcert=system` needs a CA bundle that neither the `psycopg[binary]` wheel's OpenSSL (its default path is a build directory) nor the `postgres` image (no `ca-certificates`) has. The `atus` commands set `SSL_CERT_FILE` to the operating system's bundle when it is unset (`database/dsn.py::ensure_system_roots`; macOS `/etc/ssl/cert.pem`), and the docker fallback mounts that bundle read-only and names it with `SSL_CERT_FILE` in the container. Verification is unchanged: `verify-full` against the system roots. Test: `tests/deploy/test_owner_tls.py` (opt-in, a TLS-only Postgres and a throwaway CA).
- **Reproducibility.** The dump's SHA-256 identifies the file and is not expected to repeat across builds; the content hash identifies the data and does.
- **Storage.** The owner publishes the three files as assets of an immutable GitHub release (ADR-011: release immutability on; "Each file included in a release must be under 2 GiB", https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases, read 2026-10-20). Release 0325's dump is 336,549,536 B (321 MiB). The data is public BLS microdata and the dump holds no secret. Publishing is an owner action (§19); nothing here uploads anything.
- **Dump before checking.** `atus validate-analytics` writes `validation_results`, so the builder dumps right after the build and refuses to dump a database where that table has rows. Checks run on restored copies.

## Decision 8: least-privilege roles, and the restore procedure

**Roles** (`scripts/ops/roles.sql`, run by the restore):

- the **owner/loader** is the role that restores the dump (on Neon, the console's `neondb_owner`: LOGIN CREATEDB CREATEROLE, not a superuser). It owns every object, runs `VACUUM (ANALYZE)` and `atus validate-analytics`;
- **`atus_api`** can LOG IN (connection limit 10) and SELECT the ten served tables, nothing else: no INSERT, UPDATE, DELETE or TRUNCATE, no DDL in any schema, no temporary tables (`TEMPORARY` revoked from PUBLIC on the database), no sequences, no metadata tables (`validation_results`, `source_files`, `schema_migrations`), no role memberships. Its role defaults bound every session (ADR-011 "Bounded work"): `statement_timeout = 85s`, `lock_timeout = 5s`, `idle_in_transaction_session_timeout = 30s`, `jit = off` (F37), and `default_transaction_read_only = on` as a second guard; the privileges are the boundary. `max_parallel_workers_per_gather` is not a role default: the API sets it per request once load tests decide it (F38, ADR-012).
- The script verifies its own result (attributes, memberships, no write privilege on any table or sequence, no CREATE or TEMPORARY, SELECT on all ten) and fails otherwise.
- **The password** is never in the repository: `roles.sql` takes it from a psql variable or `ATUS_API_ROLE_PASSWORD`. The restore generates 192 random bits (URL-safe, at least the 60 the brief asks), sends only its SCRAM-SHA-256 verifier to the server (so the plaintext reaches no server log or statement statistics; the verifier code is checked against the RFC 7677 example), and writes the password and the API's connection string (`sslmode=verify-full&sslrootcert=system` for a remote host) into the owner's secrets file at mode 600. A re-run reuses the stored password; `--rotate-api-password` replaces it.

**Restore** (`scripts/ops/restore_serving_db.sh`, implemented by `atus restore-serving-db`):

1. read the owner's connection string from `$ATUS_OWNER_DATABASE_URL` or the secrets file (default `~/.config/atus/neon.env`); it is never printed, and every message is redacted;
2. verify the dump against its `.sha256` file and the manifest;
3. refuse a target database that is not empty;
4. `pg_restore --no-owner --no-acl --single-transaction --exit-on-error` over the given (Neon: direct, never `-pooler`) endpoint, with a client at least as new as the server and the artifact's `pg_dump`;
5. apply `roles.sql` and write the API role's credentials back;
6. `VACUUM (ANALYZE)` every table (a scale-to-zero compute may suspend before autovacuum runs);
7. check, against the manifest: the serving marker, the data version, every table's row count, the contract, planner statistics (every table with rows has `pg_stats` rows: catalog data that a Neon suspend keeps, unlike `pg_stat_user_tables.last_analyze`), the physical order (`pg_stats.correlation` of `activities.activity_code` at least 0.99), optionally the full content hash (`--verify-content-hash`); as the API role: its session defaults, live write probes (each must fail for lack of privilege), and the plans of the worst-case analyses (narrow selectors must use the activity-code index; costs reported; F66's broad selector included);
8. measure `sum(pg_database_size(datname))` over every database the owner can see and fail above `--size-limit-bytes` (default 1,000,000,000: Neon's "1 GB" may be 10^9 or 2^30 bytes, and the smaller is assumed).

`--verify-content-hash` reads every served row: on a metered host that is roughly the data's size in egress, so it is for local drills. `--roles-only` re-applies the roles (and with `--rotate-api-password` rotates) on a restored database; it refuses a database without the serving marker, and `roles.sql` itself checks the marker before changing anything, so neither can revoke privileges on the full database.

**Restore drill (2026-10-20; functional, to be re-timed in the serialized timing phase).** The release artifact of build `20261020T224252Z-a7420a5c` (commit 202d451) was built from the full database (read only) into a container (`atus-wsb-build`, PostgreSQL 16.15) and restored with the script into a fresh PostgreSQL 16.15 cluster (`atus-wsb-drill`, `jit = off` as on Neon) whose database `atus` is owned by `neondb_owner`, a LOGIN CREATEDB CREATEROLE role that is neither a superuser nor named `atus`. Wall times are illustrative (a shared laptop).

| Step | Result |
|---|---|
| Artifact | 336,549,536 B, SHA-256 `24098d84cc05868e7607c5bba6f5c361deef31cffd1d082c0d1a2e0eee21304b`, verified against `.sha256` and manifest (0.12 s) |
| `pg_restore` (docker `postgres:16`, 16.15) | 8.0 s; no ownership, ACL or privilege error as a non-superuser |
| `roles.sql` | 0.22 s; `ALTER ROLE ... SET` accepted from a non-superuser CREATEROLE owner |
| `VACUUM (ANALYZE)`, 13 tables | 1.2 s |
| Checks | every row count, the data version and the content hash `de834e67...f833` identical to the build; correlation 1.0000 (the physical order survives dump and restore); API role defaults as set; 7 live write probes refused; plans: sleep and BLS leisure pooled over 22 years read activities by Index Scan (costs 452,649 and 504,806), all 18 tier-1 codes by Seq Scan (cost 1,102,773); 4.5 s with the content hash |
| Metered size | `sum(pg_database_size)` 651,615,308 B: 65% of 10^9 B, headroom 348,384,692 B (`atus` 628,841,495 B; `postgres`, `template0`, `template1` the rest) |
| Total | 14.5 s |
| `atus validate-analytics` (owner) | 24 passed, 0 failed |
| `ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_analytics_benchmarks.py tests/data_quality/test_api_benchmarks.py` (owner) | 11 passed |
| `atus compare-serving-db` (full DB read only vs the drill read as `atus_api`; the audit's extremes plus 150 random specifications, seed 20261020) | 161 specifications (104 estimates, 27 trends, 30 comparisons; 14 under pandemic weights; 12 identical refusals), 9,671 values: all equivalent. Largest relative SE/CI difference 3.33e-13 on non-degenerate estimates (personal-care participation pooled over 22 years); 0.146 and 0.065 on two comparisons of a population with itself and 0.060 on all 18 tier-1 codes, whose true standard error is 0 (F40), all within 1.66e-15 of their estimates' scale; 240 s. (A run against the first drill gave the same verdict: largest 2.02e-13 non-degenerate, 1.77e-15 scaled.) |

Not emulated: Neon's WAL and history growth during a restore, its metered `logical_size` (versus `pg_database_size`), whether Neon accepts `ALTER ROLE ... SET` (ADR-011 "Unknowns"; if it does not, the API's per-request settings still bound each analysis), and its pageserver.

## Decision 9: year pushdown in the engine (F39), proven equivalent

The measurements justified one engine SQL change (§13): the per-respondent activity CTE aggregated every year's episodes even for a single-year request (`queries.py`; the audit's timed probe on the serving prototype at 0.25 CU: a single-year estimate 2.90 / 3.39 s -> 0.50 / 0.90 s with the years pushed down, F39). It now aggregates only the requested years' respondents:

```sql
WHERE a.activity_code = ANY(%(activity_codes)s)
  AND a.tucaseid IN (SELECT yr.tucaseid FROM atus.respondents yr WHERE yr.data_year = ANY(%(years)s))
```

**Why it cannot change a result.** `activity_minutes` feeds a LEFT JOIN from respondents already filtered to `data_year = ANY(years)`; the rows the pushdown drops belong to other years' respondents and could never match. `tucaseid` is the respondents key, so the restriction is a semi-join and duplicates nothing. Counts and `numeric` sums are therefore identical; replicate `float8` sums can differ only by summation order (the criterion below).

**Proof.**

- `tests/unit/analytics/test_year_pushdown_sql.py`: the pushdown line is the only difference from the Phase 2-4 query (`build_query(push_down_years=False)`).
- `tests/integration/test_year_pushdown.py`: on the randomized and the hand-computed fixtures, the audit's extreme specifications plus 250 random ones (raw sufficient statistics: counts and `numeric` sums exactly equal, replicate sums within 1e-12) and 120 through the engine (outputs equivalent under the criterion).
- Read only on the full database (`default_transaction_read_only = on`): 71 specifications (the extremes plus 60 random, seed 7), 89 queries, 202,272 values: counts and `numeric` sums identical in every row, largest relative replicate-sum difference 1.43e-14. On the restored serving database: 161 specifications, 193 queries, 530,892 values, largest 1.11e-14.

`ANALYTICS_VERSION` stays 0.2: no served number changes beyond the plan-order noise the criterion allows.

Parallel query (F38) and JIT (F37) are not engine changes: they change plans, not formulas. `jit = off` is the API role's default (and the emulation's server setting); the parallel-worker limit is a per-request setting of the API (`ATUS_API_MAX_PARALLEL_WORKERS`, ADR-012), decided by the load tests.

## Decision 10: the equivalence criterion (§9, F40)

The normative text is in [`../operations.md`](../operations.md), "Equivalence criterion (§9)", and the code in `atus_pipeline.serving.equivalence`. In short, for every specification, the full and the serving database (or two renderings of a query) must give:

- **exactly equal**: point estimates, `n_respondents`, `n_participants`, weighted populations, days, labels, years, the echoed spec and the warnings; and the type and text of every error;
- **standard errors and confidence bounds** within `|a - b| <= 1e-12 * max(|a|, |b|, S)`, where `S` is the largest absolute point value of the estimate they belong to (for a comparison's difference, also both groups'). The scale floor `S` is required by F40/I12: a zero-variance estimate's "standard error" is floating-point noise (7.83e-12 vs 7.93e-12 on 1,440 minutes, 1.3e-2 relative), which a purely relative bound rejects. Measured noise is far inside the bound: at most 6.24e-13 relative on non-degenerate standard errors between the two databases, 8.57e-14 between two runs on one database (I04).

`atus compare-serving-db` runs the randomized diff (the audit's extreme specifications plus N random ones over every population dimension, weight scheme, variance mode and operation) between two databases, both read only.

## Release procedure on the free stack (§15)

1. **Full database.** `atus download -> extract -> validate-source -> migrate -> load --validate -> validate-db -> validate-analytics`. The builder refuses a source without recorded passing results for its loaded data.
2. **Build and prove.** Into a new local PostgreSQL 16 database: `atus build-serving-db --target <url> --artifact-dir <dir outside the repo>`. Then `atus compare-serving-db --serving <url> --specs 200` (the full database is only read) and check the manifest's sizes against the provider limit.
3. **Drill.** Restore the artifact into a fresh local `postgres:16` cluster as a non-superuser owner with `scripts/ops/restore_serving_db.sh --verify-content-hash`, then run `atus validate-analytics` and `ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_analytics_benchmarks.py tests/data_quality/test_api_benchmarks.py` against it as the owner. Never run `test_full_database.py` or `atus validate-db` there: they check the full database and fail by design.
4. **Publish the artifact** (owner): the dump, `.sha256` and manifest as assets of a new immutable GitHub release.
5. **Restore into a new target** (owner): a **new Neon project** per release (Postgres 16, the region of ADR-011, database `atus`, autoscaling 0.25-2 CU on the compute itself). A second ~0.65 GB copy in the same project would exceed its 1 GB during the restore; each project has its own storage and 100 CU-hours. Put the `neondb_owner` direct connection string into a new file for this project, `~/.config/atus/neon-<release>-<build8>.env` (mode 600), and run `scripts/ops/restore_serving_db.sh --artifact <dump> --secrets-file <that file>`; it writes `ATUS_API_DATABASE_URL` back into that file. Keep the previous project's file until that project is deleted: a rollback needs its DSN ([`../deployment.md`](../deployment.md#5-load-the-serving-db-into-neon-owner)).
6. **Smoke test** a local API against the new project with that connection string (read only).
7. **Snapshot.** CI builds the static snapshot for the new `DATA_VERSION` (the manifest's `data_version`) without deploying it.
8. **Switch** (owner): in Render, set `ATUS_DATABASE_URL` (the new `ATUS_API_DATABASE_URL`) and `ATUS_EXPECTED_DATA_VERSION` together and "Save and deploy"; then the deploy workflow publishes the Worker variables and the new snapshot (ADR-011 "Release switch"). Between the two, uncached analyses come from the new release while every page load — open tabs, reloads and new visitors alike — still reads the old snapshot's metadata and starter results (ADR-011 accepted risk 10 accepts this mixed window). The SPA then says the service is switching releases and that reloading does not change that yet; it offers "Reload page" only once the snapshot pointer names the release the API serves ([frontend.md, "Server state and caching"](../frontend.md#server-state-and-caching)).
9. **Verify** `/api/v1/meta`, `X-Data-Version` and the post-deploy replay. Through the Worker a plain `/api/v1/meta` comes from the new snapshot; the API's own answer needs a query string (`/api/v1/meta?check=1`, a counted metadata lookup, ADR-015) or a live analysis.
10. **Keep the previous project** until the new one is verified; delete it only with the owner's approval.

**What makes the switch safe.**

- *The version always changes.* Every build has a new data version (test), and the API answers 503 `data_version_mismatch` while the database it reaches is not the expected one (ADR-012), so a half-done switch fails loudly instead of serving mixed data.
- *No cache crosses versions.* The API cache, the KV cache (stored only when `X-Data-Version` equals the Worker's `DATA_VERSION`) and the snapshot (named by the data version) are all keyed by it.
- *No failed requests.* The zero-failure proof against the fixture database is the local switch (Decision 1, `tests/integration/test_reload_under_api_load.py`: continuous reads, zero failures, the version changes). In the hosted switch each database is complete and immutable, and requests keep being served by the old instance and project until Render routes to the new deploy. Whether Render Free switches an environment change without a gap is unverified (ADR-011); the Worker's single retry on a connection failure and the SPA's retry budget cover a restart. This part is checked after the first real switch, not by a test here.

**Rollback.** Set the two Render variables back to the previous project's values and redeploy; the Worker variables follow through the deploy workflow. If a project is lost, restore its release artifact into a new project (the artifact is the backup).

**A new ATUS year** follows Decision 4's procedure; its serving database goes through steps 2-10, and its size is re-checked against the provider limit for every release (release 0325 uses 65% of 10^9 B).

### Guarantees of the serving database and the tests that prove them

| Guarantee | Test |
|---|---|
| Two builds from the same full database: same content hash, different data versions; a changed source changes the hash | `tests/integration/test_serving_build.py` |
| The serving database gives equivalent engine results (the extremes plus 250 random specifications) and API responses (GET bodies identical, analyses equivalent), on the randomized and the hand-computed fixtures | `test_serving_build.py` |
| Shape: the contract columns in canonical order, types (replicates `float8`), NULL rules, every kept CHECK by name, keys, indexes, foreign keys; `sum(duration_minutes)` is `bigint`; served rows and omitted rows as designed; the run row describes the source run; the marker makes `migrate`/`load`/`validate-db` refuse | `test_serving_build.py`, `tests/unit/test_serving_shape.py` |
| The source is only read (a SELECT-only role builds); any failed invariant leaves the target unchanged; an unvalidated source is refused | `test_serving_build.py` |
| The artifact is a checksummed `pg_dump -Fc` with a manifest holding no secret; the restore as a non-superuser owner reproduces the content hash; a changed artifact or a non-empty target is refused | `tests/integration/test_serving_restore.py`, `tests/unit/test_serving_artifact.py` |
| `atus_api` reads everything the API needs (every endpoint, engine results equal to the owner's) and cannot INSERT, UPDATE, DELETE, TRUNCATE, `SELECT ... FOR UPDATE`, LOCK, create tables, temporary tables, schemas, views, functions or indexes, drop, alter, comment, use sequences or read the metadata tables; its defaults hold; passwords rotate; `roles.sql` refuses to run without a password or on a database without the serving marker, and so does `--roles-only` | `test_serving_restore.py`, `tests/unit/test_ops_scripts.py` |
| No secret in output or reports; the secrets file stays mode 600; the SCRAM verifier is correct | `test_serving_restore.py`, `tests/unit/test_dsn.py`, `tests/unit/test_serving_secrets_file.py` |
| The equivalence criterion as specified | `tests/unit/test_serving_equivalence.py` |

### Costs and limits of the serving database

- **Headroom.** Release 0325 uses 65% of Neon Free's 1 GB (if 10^9 B) after a restore; WAL and history growth during a restore on Neon is not measured.
- **Verification egress.** Re-hashing every row on a metered host costs about the data's size in egress; the default restore checks counts, version, marker, statistics and plans instead.
- **Not byte-reproducible dumps.** Only the content hash repeats.
- **The marker is a guard, not a boundary.** An owner can remove the schema comment; the serving data version still marks the database.
- **Role settings on Neon** are unverified until the first real restore (the drill emulated a non-superuser CREATEROLE owner).

## Consequences

- A local `atus load` no longer interrupts a running API: requests are served the old data until the switch and the new data after it, and never fail because of the load.
- Caches keyed by `data_version` are invalidated by construction on every load and every serving build, across databases.
- A load needs twice the canonical data's disk space while it runs, and an old open transaction can delay the switch.
- A new release is a registry entry and a lexicon file; the tests above fail if any other code needs to change.
- The hosted database is a 0.63 GB serving database that serves the same numbers (the §9 criterion), carries a unique data version, refuses full-database commands, and is restored from a checksummed release artifact that doubles as its backup.
- The API reaches it as a role that can only read; a leaked API connection string cannot change data.

## Amendments (implementation, 2026-10-21)

The decisions are unchanged. This section records where the implementation differs from the text above, and why. Nothing is deployed, and no release has been published.

### Decision 9: the pushed-down CTE is `MATERIALIZED`

Commit `9afd1b4` changed `src/atus_pipeline/analytics/queries.py`.

- **What the §9 HTTP equivalence run found** ([`../evidence/phase5/equivalence/README.md`](../evidence/phase5/equivalence/README.md)):
  - Some specifications combine correlated respondent filters: year, `pandemic_weight > 0` and diary dates. For these, the planner badly underestimated the base rows.
  - With the inlined, year-pushed-down CTE, it then chose a nested loop that re-ran the activity aggregate once per respondent.
  - `population-071` hit the statement timeout on the full database.
- **The fix.** Materializing the CTE computes the aggregate once, as the Phase 2-4 plan did.
- **Why no value changes.** The same rows are aggregated and joined, so no value changes and `ANALYTICS_VERSION` stays 0.2.
- **Tests.**
  - `tests/unit/analytics/test_year_pushdown_sql.py` now asserts that the rendered SQL differs from the Phase 2-4 query only by the pushdown and `MATERIALIZED`.
  - `tests/data_quality/test_query_plans.py` (opt-in, full database) checks the hazard specifications: their plans, the production statement budget, and their sufficient statistics against the Phase 2-4 query.
- **Evidence.** The three-way run repeated after the fix found every specification equivalent ([`../evidence/phase5/equivalence/final-9afd1b4/README.md`](../evidence/phase5/equivalence/final-9afd1b4/README.md)).
- **Scope of the earlier figures.** The proof figures under Decision 9 were measured before this change.

### Release procedure: the committed release manifest (steps 4, 7 and 8)

- **Step 4.** After publishing the three files, the owner records the release in `releases/current.json` with `scripts/ops/release_manifest.py write`:
  - the tag, data version and content hash;
  - every asset's size and SHA-256.

  The owner then commits the file.
- **How deploy.yml uses it.**
  - `.github/workflows/deploy.yml` reads only this file.
  - `preflight` refuses to run unless its status is `published`.
  - The `snapshot` job downloads exactly those assets and verifies them.
  - Reason: a deploy can only serve a release that someone committed.
- **Current state.** The file says `"status": "unpublished"`. Release 0325's artifact exists locally but is not published.
- **Step 7.** No workflow builds only the snapshot. "CI builds the static snapshot … without deploying it" is the `snapshot` job of a `deploy.yml` run for the commit that updates `releases/current.json`. It runs before the `production` approval.
- **Step 8.** While that run waits, the owner changes the two Render variables. Approving the `deploy` job then:
  - redeploys the API image by digest and waits for `/health`;
  - publishes the Worker with the new `DATA_VERSION` and snapshot;
  - runs the smoke, which expects the new data version.
- **Order enforced (review round 2).** Before `wrangler deploy`, the deploy job's wait step requires the origin's `/health` to report `expected_data_version` (Render's `ATUS_EXPECTED_DATA_VERSION`, read from the configuration) equal to the release it publishes. A run approved before the Render switch, or any other deploy from `main` once the manifest names the new release (a hotfix included), fails there with nothing published at the edge; the API image it deployed stays bound to the old data. Before, only the post-deploy smoke noticed, after the new snapshot and `DATA_VERSION` were public over an API still serving the old release.
- **The window between the two.** From the Render switch until `deploy` publishes, a reload cannot give a visitor one release: the API answers the new `X-Data-Version`, the snapshot every page load reads is still the old release, and the Worker stores nothing in KV (its `DATA_VERSION` is the old one). This holds for new visitors too, not only for open tabs. The SPA's notice says so instead of offering a reload that cannot help (review round 1). Approving `deploy` promptly is what ends the window.
- **Rollback.** Set the two Render variables back, and roll the edge back with `wrangler rollback` to the Worker version from before the release's deploy (its vars and snapshot come back with it; review round 2: neither step needs CI, so both work while the image scan blocks every deploy). Then restore the previous `releases/current.json` on `main` and run `deploy.yml` from `main` ([`../deployment.md`](../deployment.md#rollback)); preflight runs only `main` or a `vX.Y.Z` tag.
- **A step the procedure omits.** The governor's `NEON_PROJECT_ID` must move with each new Neon project. It is a var committed in `edge/wrangler.jsonc`, and `deploy.yml` does not set it. The project-scoped `NEON_API_KEY` (a Worker secret) must move too. Otherwise the governor keeps reading the old project's usage ([ADR-015](ADR-015-edge-cache-snapshot-and-governor.md), "Staleness across releases"). While both are empty, the governor runs in conservative mode.

### Still open

- **Restore drill timing.** The wall times under Decision 8 remain illustrative. A serialized, timed drill is committed with the load tests (2026-10-21, alone on the machine; [`../../loadtests/results/2026-10-21-emulation.md`](../../loadtests/results/2026-10-21-emulation.md), section 3): the production restore tool restored the same release artifact into a fresh `postgres:16` with a non-superuser owner in 15.6 s of wall time (`pg_restore` 8.0 s, `VACUUM (ANALYZE)` 1.3 s, the serving checks with the full content hash 5.2 s), all 22 checks passed, and `sum(pg_database_size)` was 649,903,180 B, 65% of the 1,000,000,000 B budget. On Neon the restore runs over the network into remote storage, so the time does not transfer.
- **Parallel query (F38).** `ATUS_API_MAX_PARALLEL_WORKERS` stays unset (the server default applies). The emulation runs decided it ([ADR-012, "Calibration (§13)"](ADR-012-load-and-quota-protection.md#calibration-13)): `0` is faster at the 0.25 CU floor (the F66 compare 69.21 → 38.58 s at p50) but slower at 2 CU (worst-case p99 3.03 → 3.53 s), so ADR-011's rule (adopt it only if it also wins at 2 CU) leaves it unset. If the compute is fixed at 0.25 CU, it is set once its §9 proof passes.
- **Release literals (Decision 4, "Not covered here").** `frontend/openapi.json` names the "2003-25 multi-year lexicon" only in the warning of one example response; its descriptions are release-neutral. A frontend test now serves `/meta` with years through 2026 (`frontend/src/pages/newSurveyYear.test.tsx`, `frontend/e2e/new-survey-year.spec.ts`).
