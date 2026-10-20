# ADR-013: Data releases, the local reload, and the serving database

**Status:** Accepted (2026-10-20, Phase 5) for the local reload, the `data_version` writers, release parameterization, the serving marker and the serving contract. The serving-database builder, its content hash, and the hosted release switch are added to this ADR together with the builder (see the last section).

**Related:** [ADR-011](ADR-011-hosting.md) (hosting; its "Release switch" section defers to this ADR), ADR-012 (load protection; how the API uses `data_version`). Audit findings in [`../operations.md`](../operations.md): F06, F08, F19, F20, F21, F22, F23, F55, F56, F57 and fact I03.

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

- **All-old or all-new.** A transaction that has touched any locked table holds a share lock on it, so the switch cannot commit until that transaction ends. A statement that starts while the switch holds its locks waits; after the commit, PostgreSQL re-resolves the table name after acquiring the lock and reads the new table. Cached plans of prepared statements are invalidated by the drop and the move. Each API request is one transaction, so a response is computed entirely on one version, and the version it reads from `ingestion_runs` belongs to the same commit as the data.
- **The switch always gives way.** Each lock wait is bounded by `lock_timeout` (50 ms by default), capped at `deadlock_timeout / 12` (one wait per locked table plus one) so that a whole attempt ends before PostgreSQL's deadlock detector could run in a reader queued behind it. In a lock-order cycle (F19) the switch therefore times out, rolls back and retries with a short jittered backoff; the reader is never the victim. A reader waits at most one bounded lock wait per table while an attempt is underway. After `--switch-timeout` (120 s by default) the load fails with the live data unchanged.
- **REPEATABLE READ (F21).** A REPEATABLE READ transaction whose snapshot predates the commit of the shadow data, but which first touches the canonical tables after the switch, would resolve their names to the new tables and see them empty (PostgreSQL behaviour, demonstrated by a test). Before every attempt the switch therefore waits until no other session in the database has a transaction that began before the shadow data committed (`pg_stat_activity.xact_start`). This needs the loading role to see other sessions' activity: the database owner or a member of `pg_read_all_stats` does.

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
- **Dependent objects.** A view or other object that depends on a canonical table fails the switch loudly. Drop it before loading and recreate it afterwards.
- **Roles.** Grants on the canonical tables are copied explicitly (default privileges do not apply to tables moved into a schema). The loading role must own the live tables or be a member of their owner role.
- **The schema must be current.** `atus load` refuses to run unless every migration file is applied with a matching checksum: the shadow is built from the files, the swap must not change the schema.

## Decision 2: a globally unique `data_version`, written with the data (F06, F20)

- Migration `0003_data_version.sql` adds `ingestion_runs.data_version text`, nullable for legacy rows, `UNIQUE`, and allowed only on `succeeded` runs.
- Writers:
  - `atus load`: `<release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 random hex>`, set in the switch transaction together with the data and the `succeeded` status;
  - the serving-DB builder: `<release>:serving-<build_id>:<content_hash[:12]>`, `build_id = <UTC YYYYMMDDTHHMMSSZ>-<8 random hex>` (formatters in `atus_pipeline.database.versions`; the builder itself comes with the last section).
- Reader: `atus_pipeline.database.versions.read_data_version` prefers the explicit column and falls back to `<release>:run<id>:<started_at epoch micros>` for legacy rows. Every format starts with `<release>:`.
- How the API uses the version (cache keys, `X-Data-Version`, `ATUS_EXPECTED_DATA_VERSION`) belongs to ADR-012.

## Decision 3: `atus validate-analytics` evaluates, prints, then records (F22)

Each benchmark runs in its own transaction (a savepoint inside a caller's), so one database error fails one benchmark. The command prints every result before recording them in `atus.validation_results`; recording needs the owner/loader role, and under a read-only role the command reports that nothing was recorded and exits 1. `--no-persist` only prints. The serving database keeps an empty `validation_results` for this reason, and these checks never run as the API role.

## Decision 4: everything release-specific derives from the registry (F23, F55)

- `atus_pipeline.sources` holds one `Release` per BLS multi-year release: code, first and last survey year, and its source files. `multiyear_release()` derives every archive name, data-member name, URL and description from the code, so a new release is one registry entry with its official record counts (and, if BLS widened them, the column counts of the CPS and activity-summary files).
- Derived from the entry: the staging directory (`data/staging/<release>/`), expected survey years (source and database validation), the committed lexicon file `data/reference/activity_lexicon_<release>.csv`, and the lexicon label (`2003-25`).
- The engine reads the loaded release (the same statement as the data version) and names its lexicon in the harmonization warning on every result and in the unknown-code error. For release `0325` the text is byte-identical to Phases 2-4.
- Tests: the registry refactor leaves `0325` unchanged field for field; a synthetic `0326` (2003-2026) registered at runtime flows through source validation, the load, the engine, `validate-db` and the API with no other code change (`tests/integration/test_release_checklist.py`); an AST guard keeps release literals out of the data, engine and validation packages; the performance scripts take their years from the data and `/meta`.
- Not covered here: the API's OpenAPI descriptions still name the 2003-25 lexicon (API workstream), and the frontend test that the year pickers follow `/meta` belongs to the frontend workstream (the frontend already builds them from `/meta`).

### New ATUS year procedure (e.g. 2003-2026, release `0326`)

1. Read BLS's release notes and the "Changes between data files" document. Note new or recoded variables and any layout change.
2. Add a `multiyear_release("0326", first_year=2003, last_year=2026, expected_rows={...})` entry to `_REGISTRY` with the record counts from each new `*_info.txt`. Update the header constants only if a layout changed (validation fails loudly otherwise).
3. Produce `data/reference/activity_lexicon_0326.csv` from the new lexicon PDF and review the diff (see [`../source-data.md`](../source-data.md), "The activity lexicon changes per release").
4. Set `ATUS_RELEASE=0326` and run `download → extract → validate-source → migrate → load --validate → validate-db → validate-analytics`.
5. Benchmarks: the 24 existing benchmarks reproduce values BLS published for specific years, which a new release re-releases. Run them unchanged; if BLS revised a published value, record the revision, do not edit the expectation to make it pass. Add the new year's published Table A-1 values as new benchmarks.
6. Build the serving database for the new release, check its size against the provider limit, and run the equivalence tests (§9).
7. Release it through the hosted switch (ADR-011 "Release switch"), which leaves the previous project in place for rollback.

## Decision 5: the serving marker and the serving contract (F57, I03)

- **Marker.** A serving database is identified by either of two explicit markers, both written by the builder: the comment on schema `atus` starts with `atus-serving`, or the latest succeeded run's `data_version` is a serving version. `atus migrate`, `atus load` and `atus validate-db` refuse to run against it (`atus_pipeline.serving.guard`), and so does `load_all` itself.
- **Contract.** `atus_pipeline.serving.contract` lists the tables and columns the engine, the API and `validate-analytics` touch. A test grants a role exactly those columns (SELECT, plus INSERT on `validation_results`) and runs the engine over every population dimension, weight scheme and variance mode, every HTTP endpoint and the benchmark suite as that role; any read outside the contract, or any API write, fails. `missing_contract_columns()` is the assertion the builder runs on its output. `atus validate-db` is outside the contract by design: it checks the full database.
- **Migrations outside a checkout (F56).** The migrations directory is `$ATUS_MIGRATIONS_DIR` or the checkout's `migrations/`. The API never needs it.

## Serving database build and the hosted release switch

The reproducible serving-DB builder (content hash over every served table, `pg_dump -Fc` artifact, restore drill, roles script) and the zero-failure proof of the hosted switch are added here with the builder. Until then the hosted procedure is ADR-011's "Release switch": a new Neon project per release, the API's `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` changed together, and the previous project kept for rollback.

## Consequences

- A local `atus load` no longer interrupts a running API: requests are served the old data until the switch and the new data after it, and never fail because of the load.
- Caches keyed by `data_version` are invalidated by construction on every load and every serving build, across databases.
- A load needs twice the canonical data's disk space while it runs, and an old open transaction can delay the switch.
- A new release is a registry entry and a lexicon file; the tests above fail if any other code needs to change.
