# Dry run, 2026-10-21 (not timed)

The functional proof that every load-test path works against the **real serving database**, before any timed run. It was run with `loadtests/run.py` at tiny scale: one VU and a few iterations per scenario.

**This record contains no performance numbers.** These files hold no latency, throughput or duration, and nothing here may be quoted as a measurement.

- **Stack:**
  - API image `atus-api:lt-928f50e30350`, built from commit `928f50e` (local image id `sha256:fec18e69d0bf…`, linux/arm64);
  - the production compose stack with the emulation override (API 0.1 CPU / 512 MB);
  - compose project `atus-lt-final`.
- **Database:**
  - the serving release artifact `atus-serving-0325-20261020T224252Z-a7420a5c.dump`, its sha256 `24098d84…1304b` checked against its `.sha256` file and its manifest;
  - restored with `pg_restore --no-owner --no-acl` as `neondb_owner` (not a superuser), then `VACUUM (ANALYZE)` and `scripts/ops/roles.sql`;
  - data version `0325:serving-20261020T224252Z-a7420a5c:de834e672474`.
- **Baseline:** the same image, unconstrained, against the full database `atus-postgres` (5434) in read-only transactions; data version `0325:run1:1788919495952073`.
- **Ran alone:** "yes" before and after every bundle, by the criteria in [`../../README.md`](../../README.md#reading-a-bundle). Other projects' idle containers kept running; each `environment.json` lists them.
- **Checks:** every k6 check passed, every k6 run exited 0, and every scenario's database-log attribution found both markers.

| Bundle | Profile | Scenarios (k6 settings) | Requests and outcomes |
|---|---|---|---|
| `20261021T032951Z-2cu-dry` | 2 CU (2 vCPU / 7 GB) | all five at the dry-run defaults; 2 cold starts | metadata 12 × 200 (no SQL logged); cached 4 × 200 `hit`; uncached 3 × 200 `miss`; worstcase 6 × 200 `miss` (W1–W6); mix 28 × 200 (12 analyses `miss`, 16 metadata and wake); both cold starts reached `/api/v1/meta` |
| `20261021T033254Z-2cu-dry` | 2 CU | uncached, `SOURCE=generator`, sweep `VUS=1,2` | 3 + 3 × 200 `miss` |
| `20261021T033350Z-2cu-dry` | 2 CU | mix, `MIX_MODE=origin`, 6 visits | 8 × 200 (5 novel analyses `miss`, 3 wake calls) |
| `20261021T033430Z-2cu-dry` | 2 CU | cached, `WARM_SET=presets` (22 specs), `ORDER=random`, 22 iterations | 22 × 200 `hit` |
| `20261021T033510Z-2cu-dry` | 2 CU | worstcase, `WORST_SET=core+corpus` (5 core + 38 corpus specs), 43 iterations | 43 × 200 `miss` |
| `20261021T033718Z-0.25cu-dry` | 0.25 CU (0.25 vCPU / 1 GB) | all five, 1 iteration each; 1 cold start | every request 200; cold start reached `/api/v1/meta` |
| `20261021T034120Z-laptop-dry` | unconstrained | metadata, uncached, 1 iteration each | every request 200 |
| `20261021T034217Z-laptop-fulldb-dry` | laptop baseline (full DB) | all five at the dry-run defaults | the same outcomes as the 2 CU bundle above |

The DB profiles were applied by recreating only the database container on the same volume. Each bundle's `environment.json` holds the limits read back from `docker inspect`.

Statement counts come from PostgreSQL's log. They are statements, not timings, so they are listed in each `functional.json` under `db_statements_logged`, and they match the API's documented costs:
- metadata and cache hits: 0 statements;
- an uncached estimate: 6 statements per request, counting `BEGIN` and `COMMIT` ([`docs/api.md`](../../../docs/api.md#statements-per-request)).
