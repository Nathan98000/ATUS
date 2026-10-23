# Load tests under the free-tier emulation profile (2026-10-21)

Brief §13 (performance under free-tier limits, measured only) and §10 (calibrating `statement_timeout` and the concurrency cap on the emulation profile).

**What this is.** Timed runs of the [`loadtests/`](../README.md) tooling against the production-mode stack (`docker-compose.prod.yml` + `docker-compose.emulation.yml`) on this laptop, with the **real serving release artifact** restored into the stack database. The API ran at Render Free's 0.1 CPU / 512 MB. The database ran at Neon Free's 0.25 CU floor and at its 2 CU ceiling. A laptop baseline used the same image unconstrained against the full database.

**What this is not.** Nothing here ran on Neon, Render or Cloudflare. The emulation gives the CPU and RAM shares, not the providers' hardware, storage or network (section 10). Every number below was produced by the runs listed in section 11 on 2026-10-21 between 03:53 and 07:14 UTC; nothing is estimated or carried over from earlier reports. Section 14 is the exception: it was added later and summarizes a separate cold-start run (10:21–10:25 UTC), recorded in its own file. Where a run was too small to support a conclusion, the text says so.

## Summary

- **Restore drill (A).** The production restore tool restored the release artifact into a fresh `postgres:16` with a non-superuser owner in **15.6 s** of wall time: `pg_restore` 8.0 s, `VACUUM (ANALYZE)` 1.3 s, the serving checks with the full content hash 5.2 s. All 22 checks passed. `sum(pg_database_size)` afterwards: **649,903,180 B**, which is 65% of the 1,000,000,000 B budget.
- **Cold start (B).** From `docker run` to the first `200` from `/health` at 0.1 CPU: p50 **13.1 s** (n=5, DB at 0.25 CU) and 12.9 s (n=5, at 2 CU). The first `/api/v1/meta` followed 0.3–0.6 s later. Start-up costs 1.3–1.4 CPU-seconds of import, and at 0.1 CPU that takes about 13 s. This excludes Render's own spin-up.
  - A later A/B on the same day (section 14) compared an image built before commit 5844ab7, which precompiles the standard library, with one built after it. At 0.25 CU: p50 **12.69 s** before and **5.76 s** after (n=8 each).
- **Metadata and cache hits.** These run no SQL. The 0.1-CPU API serves **33–35 metadata requests/s** and **67–71 cache hits/s**, the same at both DB profiles. p50 is 1.6–1.9 ms with one client. p95 is about 90–100 ms, because the API's CPU quota (10 ms per 100 ms period) runs out and the process waits for the next period: it was throttled in 96–97% of periods during the metadata runs.
- **Uncached single-year estimates with replicate SEs** (n=59 distinct specs, one at a time): p50 / p95 / p99 **0.99 / 3.19 / 4.43 s at 0.25 CU** and **79 / 142 / 211 ms at 2 CU** (laptop full DB: 0.34 / 0.63 / 0.98 s). PostgreSQL's log puts 96.6–99.1% of the summed latency in the database.
- **Worst cases** (5 kinds × n=5, plus the F66 broad-selector compare, one at a time, parallel workers at the server default):
  - **0.25 CU:** pooled 22-year leisure and 23-year trends take **21–22 s** at p50. The F66 broad selectors take **33–35 s** pooled or trended, and **69–71 s** for the compare by sex. Overall p99 is **70.5 s**.
  - **2 CU:** 2.0–3.0 s at p50, p99 **3.0 s**.
  - **Laptop full DB:** 10.9–12.1 s.
  - The audit measured 47–52 s at 0.25 CU for the non-F66 worst cases on commit 387fc2d, with the prototype serving DB and the audit's PostgreSQL settings (`shared_buffers=128MB`). The same requests now take 21–22 s. These runs do not isolate which change is responsible: the year-pushdown CTE, the release build of the serving DB, or the stack's Neon settings.
- **`ATUS_API_MAX_PARALLEL_WORKERS=0`:**
  - At 0.25 CU the F66 compare drops from 69.2 to **38.6 s** at p50, the other F66 requests from 33–35 to 28 s, and single-year estimates from 0.99 to 0.70 s at p50. Overall worst-case p99 goes from 70.5 to 40.5 s.
  - At 2 CU it is slower: worst-case p99 rises from 3.03 to 3.53 s, and the F66 compare from 2.99 to 3.44 s at p50.
- **Saturation through the real lanes** (2 in flight, 1 heavy, 5 s wait), uncached single-year estimates:
  - **0.25 CU:** throughput peaks at **1 concurrent client (1.14 successful analyses/s)**. At 2 it falls to 0.70/s, because both lanes share 0.25 vCPU. `503 overloaded` starts at **4** concurrent clients (9 of 101 requests) and grows to 246 of 354 at 16. Shed requests were answered after 5.0 s at p50. No `analysis_timeout` occurred.
  - **2 CU:** throughput plateaus at **2 concurrent clients (about 24 successful analyses/s)** and holds at 23.4–23.8/s up to 16 clients, with no 503. Queued requests waited under 1 s for a lane.
  - Opening the lanes to 8 (calibration only) lowered throughput at higher concurrency: at 2 CU, 11.3/s at 8 clients instead of 23.5/s. The cap of 2 protects throughput, not just the database.
  - One lane gives more light-analysis throughput at 0.25 CU (1.14–1.20/s), but in a realistic mix it sheds light analyses behind heavy ones (section 5.6).
- **Demo mix** (the traffic model's visits; 10 minutes at 2 visits/min, only what reaches the origin behind the edge): every request answered 200. Novel analyses had p50 1.2 s (light) and 10.4 s (heavy) at 0.25 CU, and 0.11 s and 1.02 s at 2 CU. This mix is small (21 analyses per profile). In a busier mix (6 visits/min, every view sent to the API, 405–427 requests), 2 CU answered everything. At 0.25 CU the lanes shed 11 requests, all heavy, and no light one. With a single general lane the same visits lost 20, 10 of them light.
- **Memory.** API PID 1 peak RSS (VmHWM) was at most **76.1 MiB** in any run, and its cgroup peak at most 59.3 MiB, against 512 MB. The DB container's working set (`docker stats`) was at most 419 MiB of 0.25 CU's 1 GiB.
- **`statement_timeout` calibration**, `clamp(3 × p99 of the worst analyses incl. F66, 30 s, 85 s)`:
  - **0.25 CU floor: 3 × 70.5 = 211.6 s → 85 s.**
  - **2 CU: 3 × 3.03 = 9.1 s → 30 s.**
  - Production autoscales between the two, so the floor governs: **keep 85 s**. At the floor, 85 s is only 1.2 × the F66 compare's p99. Two heavy analyses running at once at 0.25 CU took 88–106 s for the F66 requests (heavy lane opened to 2, section 5.5), which 85 s would cancel.
- **Recommendations** (section 9): keep `ATUS_API_MAX_INFLIGHT=2`, `ATUS_API_MAX_INFLIGHT_HEAVY=1` and `ATUS_API_ADMISSION_WAIT_S=5`. Leave `ATUS_API_MAX_PARALLEL_WORKERS` unset under ADR-011's rule: 0 does not win at 2 CU. 0 becomes the right setting if the compute is fixed at 0.25 CU (the calibration lever), subject to the §9 proof.

## 1. What ran, and how

**Machine.**
- Apple M4 (Mac16,12), 10 cores, 24 GiB, macOS 27.0.1.
- Docker Desktop 29.6.2, with a VM of 10 vCPU and 7.75 GiB (8,321,515,520 B). Compose 5.3.1.
- k6 v2.2.0 (always `--no-usage-report`; no remote imports). Host Python 3.14.7.

**Code and image.**
- The tooling ran at commit `f3e439d` on `phase5`. `loadtests/report.py` and `loadtests/export_raw.py` (read-only post-processing) were uncommitted during the runs and are committed with these results; that is why every bundle after the first warm-up records `worktree_dirty: true` (`20261021T035629Z-0.25cu` records `false`).
- API image `atus-api:lt-9afd1b4e2e0e` was built from this checkout at `9afd1b4`, the last commit that changed the image's inputs (`image_inputs_dirty: false`).
  - Local image id `sha256:059436d2576a…`, linux/arm64, 61 MB.
  - `/health` reported build `9afd1b4e2e0e1184e63b674a88cfc5500a6feb96`.
  - Render runs linux/amd64. Docker's `--cpus` emulates a CPU share of an M4 core, not the cloud CPU.

**Database.**
- The serving release artifact `atus-serving-0325-20261020T224252Z-a7420a5c.dump`: 336,549,536 B, sha256 `24098d84cc05868e7607c5bba6f5c361deef31cffd1d082c0d1a2e0eee21304b`.
  - The sha256 was checked against its `.sha256` file and its manifest before each restore.
  - Data version `0325:serving-20261020T224252Z-a7420a5c:de834e672474`.
- Restored once into the stack's fresh volume `atus-lt-timed_pgdata` by the stack's own path:
  - `deploy/stack/restore_dump.sh`: `pg_restore --no-owner --no-acl --single-transaction` as the non-superuser `neondb_owner`, then `vacuumdb --analyze`;
  - then `scripts/ops/roles.sql`.
- Cluster size during the runs: 649,960,524 B in the warm-up bundles, 650,122,836 B in every other bundle.
- PostgreSQL 16.15 (`postgres:16@sha256:f1c3376c…`) with the stack's Neon settings: `jit=off`, `shared_buffers=215MB`, `max_connections=839`, `max_worker_processes=17`, `max_parallel_workers=8`. The PostgreSQL defaults stayed in place: `max_parallel_workers_per_gather=2`, `work_mem=4MB`, `effective_cache_size=4GB`.
- Statement-duration logging (`log_min_duration_statement=0`) was on, as in the audit.

| Profile | Database container | API container |
|---|---|---|
| 0.25 CU (Neon Free floor) | `--cpus 0.25`, 1 GiB, no swap, `shm_size` 1 GiB | 0.1 CPU, 512 MiB, no swap |
| 2 CU (Neon Free ceiling) | `--cpus 2`, 7 GiB (Neon's ratio gives 8 GB; the VM has 7.75 GiB), no swap | 0.1 CPU, 512 MiB, no swap |
| Laptop baseline | the full database `atus-postgres` (5434), unconstrained, its own settings (`jit=on` server-wide, `shared_buffers=128MB`, `shm` 64 MiB); every API session read-only | unconstrained |

**API settings.**
- Production's (`ATUS_ENV=production`, edge authentication, per-client buckets, lanes 2/1, 5 s admission wait, pool of 4, `statement_timeout` 85 s, `jit=off` per transaction, `ATUS_EXPECTED_DATA_VERSION` pinned), except:
  - `ATUS_API_DAILY_ANALYSIS_CAP=10000000` in every run. Production's 600/day would end a capacity run; it is tested functionally.
  - Per run, as listed in section 11: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` for the worst-case runs, so latencies beyond 85 s are seen instead of cut off. The two `MAX_PARALLEL_WORKERS=0` uncached runs kept the API settings of the worst-case run before them, 600 s timeout included; none of their requests took more than 3.1 s. Also `ATUS_API_MAX_PARALLEL_WORKERS=0`, and the lane and pool sizes for the calibration runs.
- Every bundle's `environment.json` lists the API environment, redacted, as read back from the container.

**Method.**
- **Latency** is client-side: k6's `http_req_duration` per request, from the `main` phase only. Requests made in k6's `setup()` (metadata reads, the cached scenario's warm set) are excluded.
- **Percentiles** use linear interpolation between closest ranks, the audit's method. With n=5 per worst-case kind, p95 and p99 are close to the maximum, and the tables give n.
- **Throughput** is requests (or `200`s) divided by the wall time between the first request's start and the last response.
- **API versus database time**, two ways:
  - the API's own `Server-Timing` (`db` = time in database transactions, `app` = the rest), per request;
  - PostgreSQL's statement log, as in the audit: the sum of the API backends' logged parse, bind and execute durations between `MARK <bundle>.<label> BEGIN/END` statements, restricted to k6's main phase. Every scenario found both markers.
- **CPU and memory:**
  - `docker stats`, sampled about once a second from outside the containers (100% = one core);
  - cgroup v2 `cpu.stat` (CPU seconds, throttled periods) and `memory.peak`, read before and after each scenario, outside the measured window;
  - PID 1's `VmHWM` (the API process's peak RSS).
- **Isolation per scenario.** Each scenario started a fresh API container, so the result cache was empty. A `GET /api/v1/meta` then read the data snapshot before the measured requests. The cached scenario's 1-VU and 4-VU levels shared one API process, so its warm set was computed once.
- **Warm database.** Within a block of runs at one profile, the database container was kept running (the same `StartedAt` in every bundle of the block).
  - 0.25 CU ran in two blocks, each with its own database container: from 03:55 (`20261021T035629Z` to `051548Z`) and from 06:36 (`063653Z` to `070335Z`). 2 CU ran in one block from 05:26; its container stayed up, idle, during the laptop baseline, and `062537Z` used it afterwards.
  - Each block began with one warm-up bundle: 6 worst cases and 6 single-year estimates.
  - The warm-ups are listed in section 11 and are not reported as measurements.
- **Defeating the cache.**
  - Uncached runs walk a deterministic list of distinct specs: the corpus's 128 single-year estimates with replicate SEs, then a generator.
  - Worst cases carry a confidence level unique to each request, which leaves the SQL unchanged but makes every request a cache miss.
  - In each 60-request uncached run, one corpus spec (`errors-weird-020`) has the same canonical cache key as an earlier one and was a `hit`. The uncached tables report the 59 misses (`estimate miss`).
  - Generated specs answered `422 insufficient_data` 2.2–2.4% of the time in the 2 CU sweeps (1,257–2,856 generated specs per level; the 0.25 CU levels ran too few to give a rate). Those still ran their statistics query and are counted under "other" in the sweeps.
- **Client addresses.** Each k6 VU used its own addresses from 198.18.0.0/15, rotated every 5 analyses or every 100 other requests. The per-client buckets therefore never limited a capacity run: no `429` occurred anywhere.

## 2. Ran alone: precondition and attestation

Before every bundle, `loadtests/precondition.py` waited (polling every 60 s, for up to 3 h) until all of the following held, and recorded the snapshot:

- no process matched `scripts/equivalence/replay.py`, `pytest`, `playwright`, another `k6` or `wrangler`;
- no `atus-*` container ran other than `atus-postgres`, `atus-wsb-drill`, `atus-wsb-build`, `atus-emu-pg` and this run's own `atus-lt-timed-*` containers;
- those four databases showed no active query except this run's in `pg_stat_activity`, read by state with no query text.

It held at the first poll every time. No wait was needed.

`run.py` checked the same precondition again before and after each bundle (`ran_alone_before`/`ran_alone_after` in `environment.json`). The check also required:

- no other load generator;
- no other `atus-lt-*` stack;
- no other container above 5% of a core in two `docker stats` samples taken 3 s apart.

Other projects' containers kept running throughout: 9 `supabase_*_dogtrax-dev` services and `timeblock-postgres`. They were sampled once a minute during every bundle as background noise.

Every bundle below ran alone before and after. No protected database had an active query that was not ours.

| Bundle | ran alone before / after | precondition before / after | load avg before → after (1 min) | protected DBs: active queries not ours | background CPU % (max of any container) |
|---|---|---|---|---|---|
| `20261021T035629Z-0.25cu` | yes / yes | True / True | 2.89 → 1.59 | 0 | 9.71 (supabase_pg_meta_dogtrax-dev) |
| `20261021T040144Z-0.25cu` | yes / yes | True / True | 1.46 → 2.59 | 0 | 9.59 (supabase_pg_meta_dogtrax-dev) |
| `20261021T042009Z-0.25cu` | yes / yes | True / True | 2.69 → 1.24 | 0 | 10.21 (supabase_pg_meta_dogtrax-dev) |
| `20261021T043356Z-0.25cu` | yes / yes | True / True | 1.2 → 1.33 | 0 | 1.01 (timeblock-postgres) |
| `20261021T043548Z-0.25cu` | yes / yes | True / True | 1.2 → 3.15 | 0 | 10.14 (supabase_pg_meta_dogtrax-dev) |
| `20261021T044610Z-0.25cu` | yes / yes | True / True | 2.54 → 3.19 | 0 | 1.62 (supabase_realtime_dogtrax-dev) |
| `20261021T045348Z-0.25cu` | yes / yes | True / True | 2.37 → 2.36 | 0 | 1.7 (timeblock-postgres) |
| `20261021T045813Z-0.25cu` | yes / yes | True / True | 2.06 → 1.5 | 0 | 1.58 (supabase_studio_dogtrax-dev) |
| `20261021T050129Z-0.25cu` | yes / yes | True / True | 1.42 → 1.21 | 0 | 3.0 (supabase_realtime_dogtrax-dev) |
| `20261021T050324Z-0.25cu` | yes / yes | True / True | 1.25 → 1.4 | 0 | 7.64 (supabase_pg_meta_dogtrax-dev) |
| `20261021T051548Z-0.25cu` | yes / yes | True / True | 1.34 → 1.22 | 0 | 8.33 (supabase_pg_meta_dogtrax-dev) |
| `20261021T052710Z-2cu` | yes / yes | True / True | 1.35 → 1.6 | 0 | 8.91 (supabase_pg_meta_dogtrax-dev) |
| `20261021T052843Z-2cu` | yes / yes | True / True | 1.93 → 1.99 | 0 | 1.25 (supabase_realtime_dogtrax-dev) |
| `20261021T053052Z-2cu` | yes / yes | True / True | 1.76 → 2.03 | 0 | 0.73 (timeblock-postgres) |
| `20261021T053248Z-2cu` | yes / yes | True / True | 1.87 → 1.72 | 0 | 1.96 (supabase_realtime_dogtrax-dev) |
| `20261021T053350Z-2cu` | yes / yes | True / True | 1.63 → 3.23 | 0 | 10.22 (supabase_pg_meta_dogtrax-dev) |
| `20261021T054401Z-2cu` | yes / yes | True / True | 2.33 → 2.44 | 0 | 0.26 (supabase_realtime_dogtrax-dev) |
| `20261021T054515Z-2cu` | yes / yes | True / True | 2.01 → 1.71 | 0 | 5.49 (supabase_studio_dogtrax-dev) |
| `20261021T054942Z-2cu` | yes / yes | True / True | 1.92 → 1.21 | 0 | 1.68 (timeblock-postgres) |
| `20261021T055223Z-2cu` | yes / yes | True / True | 1.17 → 1.29 | 0 | 1.67 (supabase_realtime_dogtrax-dev) |
| `20261021T055313Z-2cu` | yes / yes | True / True | 1.57 → 3.31 | 0 | 9.4 (supabase_pg_meta_dogtrax-dev) |
| `20261021T060549Z-2cu` | yes / yes | True / True | 3.01 → 1.49 | 0 | 4.96 (supabase_studio_dogtrax-dev) |
| `20261021T061645Z-laptop-fulldb` | yes / yes | True / True | 1.38 → 1.89 | 0 | 1.68 (atus-lt-timed-db-1) |
| `20261021T061825Z-laptop-fulldb` | yes / yes | True / True | 1.62 → 1.68 | 0 | 8.75 (supabase_pg_meta_dogtrax-dev) |
| `20261021T062430Z-laptop-fulldb` | yes / yes | True / True | 1.69 → 1.87 | 0 | 0.16 (supabase_realtime_dogtrax-dev) |
| `20261021T062537Z-2cu` | yes / yes | True / True | 2.07 → 1.37 | 0 | 10.69 (supabase_pg_meta_dogtrax-dev) |
| `20261021T063653Z-0.25cu` | yes / yes | True / True | 1.4 → 1.28 | 0 | 5.89 (supabase_studio_dogtrax-dev) |
| `20261021T064154Z-0.25cu` | yes / yes | True / True | 1.22 → 1.18 | 0 | 9.07 (supabase_pg_meta_dogtrax-dev) |
| `20261021T065213Z-0.25cu` | yes / yes | True / True | 1.58 → 1.77 | 0 | 8.55 (supabase_pg_meta_dogtrax-dev) |
| `20261021T070335Z-0.25cu` | yes / yes | True / True | 1.4 → 2.18 | 0 | 10.08 (supabase_pg_meta_dogtrax-dev) |

Notes on the attestation:

- The background column is the highest single once-a-minute sample of any other container. The sum of all other containers' mean CPU stayed between 0.5% and 11.0% of one core per bundle.
- The host load average (1 min) ranged from 1.17 to 3.31 at bundle boundaries.
- The host's busiest processes were usually the Docker VM itself, Docker's backend, the orchestrator's own Python and desktop apps (coreaudiod, WindowServer, the Claude desktop app). At four bundle boundaries macOS's background indexers were busier: `mediaanalysisd` at 114% of a core after `20261021T040144Z-0.25cu` (the 0.25 CU worst cases) and before `042009Z`, and `corespotlightd` at 170% and 197% after `045348Z` and `053248Z`. Host processes were sampled only at the boundaries, so whether these ran during a bundle is unknown; the host has 10 cores and the database at most 2. Each bundle lists them under `top_host_processes_pcpu`.
- The precondition snapshots taken before each step are in `raw/precondition/`.

## 3. A. Restore drill (timed)

**Command.** `scripts/ops/restore_serving_db.sh --artifact <dump> --secrets-file <private> --verify-content-hash --report <file>`, which is `atus restore-serving-db`, the production restore tool.

**Target.** A fresh `postgres:16` container (`atus-lt-drill-pg`, the stack's pinned digest and Neon settings, unconstrained) on `127.0.0.1:5502`.
- It was bootstrapped by the stack's `deploy/postgres/initdb` exactly as Neon's console creates a project: owner `neondb_owner` with LOGIN, CREATEDB and CREATEROLE, **not a superuser** (the report confirms `owner_is_superuser: false`), and database `atus`.
- `pg_restore` and `psql` ran from the `postgres:16` image, version 16.15, since the host has no client.
- The dump file was in the host's page cache, because it had been hashed just before.
- Started 2026-10-21T03:54:11Z.

| Step | Wall time (s) |
|---|---|
| sha256 of the dump, standalone (`shasum -a 256`), against `.sha256` and the manifest | 0.61 |
| fresh container → ready (initdb, `neondb_owner` and `atus` created, TCP up) | 0.82 |
| restore tool: verify artifact (sha256 against `.sha256` and manifest, size) | 0.12 |
| restore tool: `pg_restore --no-owner --no-acl --single-transaction --exit-on-error` | 8.01 |
| restore tool: `scripts/ops/roles.sql` (`atus_api`, SCRAM verifier) | 0.27 |
| restore tool: `VACUUM (ANALYZE)`, 13 tables | 1.26 |
| restore tool: serving checks, including the full **content hash** of every served row | 5.23 |
| restore tool: API-role checks (session defaults, write probes, worst-case `EXPLAIN`s) | 0.04 |
| **restore tool total** (its own clock / wall clock around it) | **15.42 / 15.59** |
| **whole drill** (sha256 + fresh container + restore tool) | **17.11** |

**Result.**
- All **22 of 22 checks passed**: serving marker, data version, row counts of every table (2,982,182 activities, 258,954 respondents, ...), serving contract, planner statistics, physical order, content hash `de834e672474…`, the API role's defaults, the API role being unable to write, the worst-case plans, and the size.
- **`sum(pg_database_size(datname))` = 649,903,180 B** (`atus` 627,129,367 B; `postgres`, `template0` and `template1` 22,773,813 B together). That is 65% of 1,000,000,000 B, leaving 350,096,820 B of headroom. An independent read as the bootstrap superuser gave the same sum.
- The stack's own restore path, used for the load tests, took 9.0 s from container ready to restored and analyzed. That is `run.py`'s untimed record, given for comparison only.

**Not emulated.** On Neon the same tool restores over the network into an autoscaling compute with remote storage, so this time does not transfer.

The drill container was removed afterwards.

## 4. B. Cold start (n=5 per profile)

**Method.** `docker run` of the stage's image with Render's limits (`--cpus=0.1 --memory=512m --memory-swap=512m`), the production environment, a read-only root filesystem, all capabilities dropped and no Docker HEALTHCHECK, on the stack network.
- Timed from just before `docker run` (the image already in the VM) to the first `200` from `/health`, polled every 0.1 s, and from `/api/v1/meta`, polled every 0.05 s.
- The first `/meta` reads the data snapshot from the database (11 statements).

| DB profile | to first `/health` 200: p50 / p95 / max (s) | to first `/api/v1/meta` 200: p50 / p95 / max (s) | start-up CPU (s) | throttled periods | VmHWM after `/meta` |
|---|---|---|---|---|---|
| 0.25 CU | 13.12 / 13.45 / 13.46 | 13.72 / 13.93 / 13.98 | 1.29–1.38 | 129–137 of 129–140 | 66.2 MiB (67,756–67,772 kB) |
| 2 CU | 12.88 / 13.79 / 13.85 | 13.21 / 14.20 / 14.28 | 1.29–1.43 | 127–141 of 129–143 | 66.2 MiB (67,760–67,772 kB) |

**What the numbers show.**
- Start-up is CPU-bound import time: 1.3–1.4 CPU-seconds, delivered at 0.1 CPU, with the container throttled in nearly every scheduler period.
- The database profile cannot affect `/health`, so the difference between the two rows is noise.
- `/meta` came 0.3–0.6 s after `/health`, and every first `/meta` poll after `/health` succeeded.
- The audit measured 7.65 s at p50 for an earlier image (387fc2d) whose import took 0.71–1.11 CPU-seconds. The Phase 5 API now imports more.

**Not included.** Render's own spin-up (image pull, scheduling, routing) and Neon's wake from suspend.

**Later.** The image measured here (9afd1b4) has no compiled standard library. Section 14 measures the image built after commit 5844ab7 against one built before it.

## 5. C. Emulation profiles: 0.25 CU and 2 CU

### 5.1 Metadata and cached analyses (no SQL)

`metadata` sends `/meta`, `/activities`, `/activities/<code>` and `/population/metadata` in a loop. `cached` repeats the 22-spec preset warm set, and every measured request was an `X-Cache: hit`. Each level ran for 60 s.

| Run | n | status (codes) | p50 | p95 | p99 | max (ms) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.25 CU metadata, 1 VU | 1972 | 200×1972 | 1.9 | 99.4 | 106 | 704 | 32.852 | 32.852 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |
| 0.25 CU metadata, 4 VUs | 2096 | 200×2096 | 99.9 | 294 | 398 | 794 | 34.813 | 34.813 | 0.0 / 0.6 | 0.0 / 0.0% | 0 |
| 2 CU metadata, 1 VU | 1980 | 200×1980 | 1.9 | 100 | 107 | 791 | 32.963 | 32.963 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |
| 2 CU metadata, 4 VUs | 2104 | 200×2104 | 99.6 | 291 | 389 | 900 | 34.974 | 34.974 | 0.0 / 0.6 | 0.0 / 0.0% | 0 |
| 0.25 CU cached, 1 VU | 4102 | 200×4102 | 1.6 | 89.4 | 96.1 | 101 | 68.298 | 68.298 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |
| 0.25 CU cached, 4 VUs | 4282 | 200×4282 | 89.6 | 104 | 107 | 603 | 71.255 | 71.255 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |
| 2 CU cached, 1 VU | 4023 | 200×4023 | 1.6 | 89.7 | 95.1 | 103 | 67.03 | 67.03 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |
| 2 CU cached, 4 VUs | 4163 | 200×4163 | 91.0 | 104 | 107 | 192 | 69.273 | 69.273 | 0.0 / 0.7 | 0.0 / 0.0% | 0 |

**What the numbers show.**
- PostgreSQL logged 0 statements in every one of these runs.
- The API is CPU-bound at 0.1 CPU: 33–35 metadata requests/s and 67–71 hits/s, about the same with 1 or 4 clients and at either DB profile.
- With 4 clients, the extra clients only queue, so p50 rises to about 90–100 ms.
- The p95 of about 90–100 ms even with one client is the CFS quota at work. The API was throttled in 75–97% of 100 ms periods (resources table, 5.7). Render's real CPU share may be enforced differently (not emulated).

Per endpoint at 0.25 CU with 1 VU (ms):

| endpoint | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| activities | 493 | 200×493 | 94.2 | 106 | 190 | 704 | 0.0 |
| activity_detail | 493 | 200×493 | 1.9 | 92.8 | 97.1 | 99.5 | 0.0 |
| meta | 493 | 200×493 | 1.5 | 89.7 | 95.3 | 99.4 | 0.0 |
| population_metadata | 493 | 200×493 | 1.4 | 91.3 | 96.2 | 98.9 | 0.0 |

`/activities` (70 KB of JSON) is the slowest metadata route: p50 94 ms, p99 190 ms.

### 5.2 Uncached single-year estimates with replicate SEs (n=59 distinct, one at a time)

| Run | n | status (codes) | p50 | p95 | p99 | max (ms) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.25 CU, workers default | 59 | 200×59 | 994 | 3186 | 4432 | 4600 | 0.818 | 0.818 | 990 / 1.5 | 71.4 / 99.1% | 354 |
| 0.25 CU, MAX_PARALLEL_WORKERS=0 | 59 | 200×59 | 703 | 2013 | 2632 | 3085 | 1.095 | 1.095 | 698 / 1.4 | 53.1 / 98.7% | 354 |
| 2 CU, workers default | 59 | 200×59 | 78.6 | 142 | 211 | 233 | 11.824 | 11.824 | 77.0 / 0.7 | 4.8 / 96.6% | 354 |
| 2 CU, MAX_PARALLEL_WORKERS=0 | 59 | 200×59 | 78.9 | 209 | 252 | 266 | 10.592 | 10.592 | 77.6 / 0.7 | 5.4 / 97.0% | 354 |
| laptop full DB, workers default | 59 | 200×59 | 340 | 633 | 978 | 1020 | 2.696 | 2.696 | 339 / 0.7 | 21.6 / 98.9% | 354 |

Notes on the table:

- The "PG statements" column counts the whole scenario, including the one `hit`: 59 × 6 = 354. An uncached estimate runs 6 statements in PostgreSQL's log, as documented in `docs/api.md`.
- The 59 specs are the corpus's first single-year estimates with replicate SEs. They differ in activity, population, measure and weights, so their costs differ, which is the spread between p50 and p99.
- At 2 CU, `MAX_PARALLEL_WORKERS=0` gives the same p50 and a higher p95.

### 5.3 Worst cases (n=5 per kind, one at a time)

**Kinds.**
- W1: pooled 22-year BLS leisure.
- W2 and W3: 23-year trends of `12` and `010101`.
- W4 and W5: the F66 broad selector (all 18 tier-1 codes), pooled 22 years and as a 23-year trend.
- W6: the F66 selector pooled 22 years, men vs women. This is two statistics queries, and the heavy kind most likely to hit the timeout.

**Settings.** `statement_timeout` was raised to 600 s for these runs, so the whole latency is visible. Each request carried a unique confidence level, so all 30 were cache misses with identical SQL.

**Overall:**

| Run | n | status (codes) | p50 | p95 | p99 | max (s) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.25 CU, workers default | 30 | 200×30 | 28.91 | 69.26 | 70.52 | 71.02 | 0.029 | 0.029 | 28861 / 2.6 | 1039.1 / 99.7% | 185 |
| 0.25 CU, MAX_PARALLEL_WORKERS=0 | 30 | 200×30 | 24.20 | 39.42 | 40.54 | 40.71 | 0.038 | 0.038 | 24196 / 2.2 | 781.8 / 99.7% | 185 |
| 2 CU, workers default | 30 | 200×30 | 2.24 | 3.01 | 3.03 | 3.04 | 0.42 | 0.42 | 2236 / 1.1 | 71.1 / 99.7% | 185 |
| 2 CU, MAX_PARALLEL_WORKERS=0 | 30 | 200×30 | 2.40 | 3.46 | 3.53 | 3.56 | 0.399 | 0.399 | 2400 / 1.1 | 75.0 / 99.7% | 185 |
| laptop full DB, workers default | 30 | 200×30 | 10.99 | 12.05 | 12.07 | 12.07 | 0.09 | 0.09 | 10988 / 1.1 | 333.7 / 99.9% | 185 |

**Per kind, 0.25 CU, parallel workers at the server default (2 per gather):**

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 5 | 200×5 | 17.48 | 21.35 | 25.65 | 25.77 | 25.80 | 21.34 |
| W2-trend23-12 | 5 | 200×5 | 19.88 | 21.50 | 23.99 | 24.01 | 24.01 | 21.50 |
| W3-trend23-010101 | 5 | 200×5 | 18.81 | 21.81 | 23.39 | 23.57 | 23.61 | 21.81 |
| W4-F66-pooled22-tier1 | 5 | 200×5 | 34.19 | 34.99 | 42.01 | 42.02 | 42.02 | 34.99 |
| W5-F66-trend23-tier1 | 5 | 200×5 | 32.02 | 33.49 | 41.81 | 41.95 | 41.99 | 33.49 |
| W6-F66-compare22-sex | 5 | 200×5 | 67.59 | 69.21 | 70.67 | 70.95 | 71.02 | 69.20 |

**Per kind, 0.25 CU, `ATUS_API_MAX_PARALLEL_WORKERS=0`:**

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 5 | 200×5 | 20.20 | 20.70 | 22.46 | 22.47 | 22.47 | 20.70 |
| W2-trend23-12 | 5 | 200×5 | 19.89 | 20.80 | 21.03 | 21.03 | 21.03 | 20.80 |
| W3-trend23-010101 | 5 | 200×5 | 18.52 | 20.78 | 21.50 | 21.64 | 21.68 | 20.71 |
| W4-F66-pooled22-tier1 | 5 | 200×5 | 25.93 | 28.19 | 29.61 | 29.62 | 29.62 | 28.18 |
| W5-F66-trend23-tier1 | 5 | 200×5 | 26.41 | 27.62 | 29.13 | 29.33 | 29.39 | 27.61 |
| W6-F66-compare22-sex | 5 | 200×5 | 37.19 | 38.58 | 40.59 | 40.69 | 40.71 | 38.58 |

**Per kind, 2 CU, workers default:**

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 5 | 200×5 | 1.99 | 2.12 | 2.15 | 2.15 | 2.15 | 2.12 |
| W2-trend23-12 | 5 | 200×5 | 1.92 | 2.08 | 2.10 | 2.10 | 2.10 | 2.08 |
| W3-trend23-010101 | 5 | 200×5 | 1.88 | 2.03 | 2.04 | 2.04 | 2.04 | 2.03 |
| W4-F66-pooled22-tier1 | 5 | 200×5 | 2.33 | 2.67 | 2.70 | 2.70 | 2.70 | 2.67 |
| W5-F66-trend23-tier1 | 5 | 200×5 | 2.33 | 2.68 | 2.69 | 2.69 | 2.69 | 2.68 |
| W6-F66-compare22-sex | 5 | 200×5 | 2.98 | 2.99 | 3.04 | 3.04 | 3.04 | 2.99 |

**Per kind, 2 CU, `ATUS_API_MAX_PARALLEL_WORKERS=0`:**

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 5 | 200×5 | 2.00 | 2.17 | 2.25 | 2.26 | 2.26 | 2.17 |
| W2-trend23-12 | 5 | 200×5 | 1.95 | 2.12 | 2.12 | 2.12 | 2.12 | 2.11 |
| W3-trend23-010101 | 5 | 200×5 | 1.92 | 2.05 | 2.08 | 2.08 | 2.08 | 2.05 |
| W4-F66-pooled22-tier1 | 5 | 200×5 | 2.54 | 2.69 | 2.73 | 2.73 | 2.73 | 2.69 |
| W5-F66-trend23-tier1 | 5 | 200×5 | 2.56 | 2.69 | 2.75 | 2.76 | 2.77 | 2.69 |
| W6-F66-compare22-sex | 5 | 200×5 | 3.41 | 3.44 | 3.54 | 3.56 | 3.56 | 3.44 |

**What the numbers show.**
- At 0.25 CU with the server default, the F66 compare (W6) is the slowest request at 69–71 s, about 3.2 × W1. The single-group F66 requests (W4, W5) take 1.6 × W1.
- With parallel workers off at 0.25 CU:
  - W6 drops to 38.6 s at p50;
  - W4 and W5 drop to 28 s;
  - W1–W3 do not change (20.7–20.8 s against 21.4–21.8 s).
- At 2 CU every kind takes 2.0–3.0 s, and parallel workers help slightly: turning them off adds 0.01–0.45 s per kind at p50.
- PostgreSQL's log puts 99.7–99.9% of the worst cases' summed latency in the database. The API's `Server-Timing` app time was 1.1–2.6 ms per request at p50 (5.8).

### 5.4 Saturation through the real admission lanes

**Settings.** Uncached single-year estimates, distinct specs, with N concurrent clients for 120 s per level. The API kept production's lanes: `MAX_INFLIGHT=2`, `MAX_INFLIGHT_HEAVY=1`, `ADMISSION_WAIT_S=5`, pool 4, parallel workers at the server default.

**0.25 CU:**

| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | 200/s | 200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | DB CPU % p50 | API CPU % p50 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 139 | 138 | 0 | 0 | 1 | 1.136 | 0.70 / 1.93 / 4.15 / 5.01 | - / - | 24.9 | 0.8 / 2.3 |
| 2 | 85 | 85 | 0 | 0 | 0 | 0.703 | 2.39 / 7.79 / 10.79 / 10.80 | - / - | 24.9 | 0.5 / 3.5 |
| 4 | 101 | 92 | 9 | 0 | 0 | 0.749 | 4.50 / 9.34 / 11.39 / 11.40 | 5.01 / 5.08 | 24.8 | 0.7 / 3.4 |
| 8 | 172 | 112 | 59 | 0 | 1 | 0.892 | 6.09 / 8.81 / 11.12 / 11.86 | 5.01 / 5.11 | 24.8 | 1.1 / 4.6 |
| 16 | 354 | 107 | 246 | 0 | 1 | 0.851 | 6.64 / 9.65 / 12.43 / 13.07 | 5.01 / 5.58 | 24.9 | 1.6 / 5.7 |

**2 CU:**

| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | 200/s | 200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | DB CPU % p50 | API CPU % p50 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1936 | 1895 | 0 | 0 | 41 | 15.777 | 0.06 / 0.10 / 0.14 / 0.21 | - / - | 123.7 | 3.8 / 5.0 |
| 2 | 2969 | 2903 | 0 | 0 | 66 | 24.19 | 0.08 / 0.15 / 0.21 / 0.40 | - / - | 200.0 | 5.9 / 8.3 |
| 4 | 2929 | 2863 | 0 | 0 | 66 | 23.801 | 0.16 / 0.27 / 0.34 / 0.53 | - / - | 200.2 | 6.1 / 8.6 |
| 8 | 2893 | 2827 | 0 | 0 | 66 | 23.497 | 0.32 / 0.49 / 0.59 / 0.82 | - / - | 200.0 | 6.1 / 8.2 |
| 16 | 2885 | 2819 | 0 | 0 | 66 | 23.377 | 0.64 / 0.89 / 0.98 / 1.13 | - / - | 200.2 | 6.2 / 8.5 |

**Saturation point.**
- **0.25 CU: 1 concurrent analysis.**
  - The database is already at its 0.25-vCPU quota with one client (DB CPU p50 24.9%, throttled in 99% of periods), and 200s per second are highest at 1 (1.14/s).
  - With a second analysis in flight both slow down: p50 goes from 0.70 to 2.39 s, and throughput falls to 0.70/s.
  - From 4 clients the lanes shed (`503 overloaded` after the 5 s wait, `Retry-After: 10`). Admitted requests then take 4.5–6.6 s at p50, and throughput settles at 0.75–0.89/s.
  - The API's own CPU stayed under 6% of a core.
- **2 CU: 2 concurrent analyses.**
  - Throughput rises from 15.8/s at 1 client to 24.2/s at 2, where the database reaches its 2-vCPU quota (DB CPU 200%).
  - It stays at 23.4–23.8/s up to 16 clients. Queued requests waited under 1 s for a lane, so nothing was shed: at 16 clients the time outside the database (`Server-Timing` `app`, which includes the lane wait) was 0.84 s at p99 and 0.95 s at most, and the whole latency 0.98 s at p99.
- **Errors.**
  - `503 analysis_timeout`: 0 at every level.
  - `429`: 0.
  - Transport errors: 0.
  - `422 insufficient_data`: 2.2–2.4% of the generated specs at 2 CU, as expected (the same deterministic specs in every sweep).
  - Across every run in this record, all 640 `503 overloaded` answers carried `Retry-After: 10`. The API's `/metrics` counted 0 pool request errors, 0 connection errors or lost connections, and 0 query timeouts in every run.

### 5.5 Calibration runs for the lanes (not production settings)

**Lanes and pool opened to 8** (`ATUS_API_MAX_INFLIGHT=8`, `_HEAVY=8`, `ATUS_API_POOL_MAX_SIZE=8`), the same uncached sweep, which shows the database's own curve.

0.25 CU:

| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | 200/s | 200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | DB CPU % p50 | API CPU % p50 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 145 | 144 | 0 | 0 | 1 | 1.196 | 0.70 / 1.97 / 3.85 / 4.47 | - / - | 24.9 | 0.8 / 2.4 |
| 2 | 85 | 85 | 0 | 0 | 0 | 0.702 | 2.51 / 6.95 / 10.48 / 10.50 | - / - | 24.8 | 0.6 / 3.5 |
| 4 | 65 | 65 | 0 | 0 | 0 | 0.526 | 6.99 / 15.67 / 17.39 / 19.50 | - / - | 24.9 | 0.5 / 3.0 |
| 8 | 62 | 62 | 0 | 0 | 0 | 0.466 | 16.10 / 32.52 / 40.81 / 51.69 | - / - | 24.9 | 0.6 / 6.2 |

2 CU:

| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | 200/s | 200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | DB CPU % p50 | API CPU % p50 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1940 | 1899 | 0 | 0 | 41 | 15.816 | 0.06 / 0.10 / 0.14 / 0.22 | - / - | 123.4 | 3.7 / 4.5 |
| 2 | 2984 | 2916 | 0 | 0 | 68 | 24.281 | 0.08 / 0.14 / 0.21 / 0.35 | - / - | 200.2 | 5.9 / 8.0 |
| 4 | 2244 | 2195 | 0 | 0 | 49 | 18.258 | 0.20 / 0.39 / 0.58 / 0.80 | - / - | 199.6 | 6.9 / 10.2 |
| 8 | 1385 | 1357 | 0 | 0 | 28 | 11.267 | 0.65 / 1.21 / 1.79 / 2.40 | - / - | 199.0 | 5.8 / 8.6 |

**One general lane** (`ATUS_API_MAX_INFLIGHT=1`, `_HEAVY=1`) at 0.25 CU, the same sweep as 5.4 from 2 clients upwards:

| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | 200/s | 200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | DB CPU % p50 | API CPU % p50 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | 140 | 139 | 0 | 0 | 1 | 1.144 | 1.51 / 3.40 / 5.43 / 8.61 | - / - | 24.9 | 0.8 / 2.8 |
| 4 | 152 | 144 | 7 | 0 | 1 | 1.168 | 2.81 / 6.17 / 7.84 / 9.30 | 5.01 / 5.08 | 24.9 | 0.9 / 2.4 |
| 8 | 205 | 148 | 57 | 0 | 0 | 1.197 | 4.90 / 6.28 / 6.95 / 7.19 | 5.01 / 5.20 | 24.9 | 1.0 / 3.5 |
| 16 | 382 | 149 | 231 | 0 | 2 | 1.18 | 5.49 / 6.45 / 7.42 / 8.44 | 5.01 / 5.15 | 24.9 | 1.5 / 4.4 |

**Two heavy analyses at once** (`ATUS_API_MAX_INFLIGHT_HEAVY=2`, 2 clients, W1–W5 twice each, `statement_timeout` 600 s):

0.25 CU:

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 2 | 200×2 | 65.72 | 66.16 | 66.55 | 66.58 | 66.59 | 66.15 |
| W2-trend23-12 | 2 | 200×2 | 61.65 | 62.90 | 64.03 | 64.13 | 64.16 | 62.88 |
| W3-trend23-010101 | 2 | 200×2 | 60.40 | 69.58 | 77.84 | 78.58 | 78.76 | 69.57 |
| W4-F66-pooled22-tier1 | 2 | 200×2 | 102.70 | 104.47 | 106.06 | 106.21 | 106.24 | 104.47 |
| W5-F66-trend23-tier1 | 2 | 200×2 | 87.85 | 95.55 | 102.49 | 103.10 | 103.26 | 95.52 |

2 CU:

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 2 | 200×2 | 2.14 | 2.23 | 2.31 | 2.31 | 2.31 | 2.22 |
| W2-trend23-12 | 2 | 200×2 | 2.05 | 2.14 | 2.22 | 2.23 | 2.23 | 2.14 |
| W3-trend23-010101 | 2 | 200×2 | 2.32 | 2.41 | 2.49 | 2.50 | 2.50 | 2.41 |
| W4-F66-pooled22-tier1 | 2 | 200×2 | 3.22 | 3.23 | 3.24 | 3.24 | 3.24 | 3.22 |
| W5-F66-trend23-tier1 | 2 | 200×2 | 2.69 | 2.83 | 2.95 | 2.96 | 2.96 | 2.82 |

**What the numbers show.**
- **Without the cap, concurrency lowers throughput at both profiles.**
  - 0.25 CU: 1.20/s at 1 client, 0.70/s at 2, 0.53/s at 4, 0.47/s at 8. With 8 in flight, p99 reaches 40.8 s.
  - 2 CU: 15.8/s at 1, 24.3/s at 2, 18.3/s at 4, 11.3/s at 8.
  - Against the real lanes at the same client counts (5.4), the cap of 2 kept throughput up: 0.75–0.89/s instead of 0.47–0.53/s at 0.25 CU, and 23.5–23.8/s instead of 11.3–18.3/s at 2 CU.
- **One lane at 0.25 CU:** 200s per second held at 1.14–1.20 from 2 to 16 clients. That is 34–63% above the production lanes at the same client counts (0.70–0.89/s, 5.4), and an admitted request took 1.51 s at p50 with 2 clients instead of 2.39 s. Sheds started at 4 clients, as with two lanes. In a flood of light analyses only, one lane is the better setting at the floor. The API-view mix in 5.6 shows what one lane costs when heavy analyses are in the stream.
- **Two heavy analyses at once:**
  - At 0.25 CU each took 2.6–3.6 × its p50 when alone: W1–W3 60–79 s instead of 21–22 s, and W4/W5 88–106 s instead of 33–35 s. Running heavy analyses in parallel at the floor loses throughput, and the F66 requests then exceed 85 s.
  - At 2 CU each took 2.0–3.2 s instead of 2.0–2.7 s, so two heavy lanes would nearly double heavy throughput there.

### 5.6 Realistic demo mix

**Generator.** `k6/mix.js` draws visits from the traffic model in `docs/deployment.md`: per visit, 3 analysis views (1.5 snapshot, 0.5 share-link revisit, 1.0 novel, 30% of novel ones heavy) and 0.5 wake calls. Visits arrive at a constant rate for 10 minutes. Each visit uses its own client address. Production API settings.

**Origin view, 2 visits/min.** `MIX_MODE=origin` sends only what reaches Render behind the Worker: novel analyses and wake calls. Snapshot views, metadata and KV-hit revisits are answered at the edge in production.

**What the 2 visits/min rate stands for.** About 5× the spike day's average rate if its 300 visits spread over about 12 h (25 per hour, against 120 per hour here), or a spike day's visits concentrated into about 2.5 hours. 21 visits ran per profile, so the percentiles below rest on 8 heavy and 13 light analyses.

0.25 CU, by view kind (ms):

| kind | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| novel_heavy | 8 | 200×8 | 10454 | 15880 | 17714 | 18172 | 10448 |
| novel_light | 13 | 200×13 | 1177 | 1978 | 2186 | 2238 | 1171 |
| wake | 10 | 200×10 | 3.9 | 4.4 | 4.5 | 4.5 | 0.0 |

2 CU, by view kind (ms):

| kind | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| novel_heavy | 8 | 200×8 | 1019 | 1628 | 1851 | 1906 | 1014 |
| novel_light | 13 | 200×13 | 113 | 153 | 155 | 155 | 107 |
| wake | 10 | 200×10 | 3.9 | 5.1 | 5.8 | 6.0 | 0.0 |

**API view, 6 visits/min.** `MIX_MODE=api` sends every view to the API, as on a stack without the edge tiers: metadata, starters and presets (computed once, then cache hits), revisits and novel analyses. This is busier than the model and stresses the lanes. 61 visits ran at 2 CU. At 0.25 CU, k6 dropped 2 scheduled visits in each run while starting extra VUs (visits there lasted up to 76 s), so those runs carry 58 visits.

0.25 CU, by view kind (ms):

| kind | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| activities | 58 | 200×58 | 8.9 | 103 | 191 | 306 | 0.0 |
| activity_detail | 22 | 200×22 | 1.2 | 2.2 | 65.6 | 82.4 | 0.0 |
| meta | 58 | 200×58 | 2.0 | 4.7 | 5.1 | 5.4 | 0.0 |
| novel_heavy | 21 | 200×13 422×1 503×7 (insufficient_data 1, overloaded 7) | 7166 | 13269 | 18046 | 19240 | 6792 |
| novel_light | 35 | 200×34 422×1 (insufficient_data 1) | 1683 | 3643 | 3976 | 4127 | 1679 |
| population_metadata | 58 | 200×58 | 1.4 | 92.9 | 94.6 | 94.8 | 0.0 |
| preset | 56 | 200×52 503×4 (overloaded 4) | 1426 | 23697 | 27836 | 30519 | 1091 |
| revisit | 7 | 200×6 422×1 (insufficient_data 1) | 4.6 | 16390 | 17645 | 17959 | 0.0 |
| revisit_fallback | 9 | 200×9 | 4.3 | 4.6 | 4.7 | 4.7 | 0.0 |
| starter | 46 | 200×46 | 4.0 | 3267 | 18398 | 28552 | 0.0 |
| wake | 35 | 200×35 | 3.4 | 4.3 | 4.4 | 4.4 | 0.0 |

2 CU, by view kind (ms):

| kind | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| activities | 61 | 200×61 | 9.4 | 14.3 | 218 | 520 | 0.0 |
| activity_detail | 24 | 200×24 | 1.2 | 1.8 | 2.1 | 2.1 | 0.0 |
| meta | 61 | 200×61 | 1.9 | 5.0 | 5.2 | 5.3 | 0.0 |
| novel_heavy | 21 | 200×20 422×1 (insufficient_data 1) | 838 | 1362 | 1855 | 1978 | 833 |
| novel_light | 38 | 200×37 422×1 (insufficient_data 1) | 111 | 188 | 225 | 230 | 107 |
| population_metadata | 61 | 200×61 | 1.4 | 1.9 | 2.3 | 2.6 | 0.0 |
| preset | 58 | 200×58 | 130 | 2015 | 2163 | 2198 | 125 |
| revisit | 8 | 200×7 422×1 (insufficient_data 1) | 4.8 | 870 | 1243 | 1336 | 0.0 |
| revisit_fallback | 9 | 200×9 | 4.3 | 4.7 | 4.8 | 4.8 | 0.0 |
| starter | 49 | 200×49 | 4.9 | 133 | 1184 | 2128 | 0.0 |
| wake | 37 | 200×37 | 3.9 | 4.6 | 4.7 | 4.7 | 0.0 |

The same API-view mix at 0.25 CU (same seed, so the same visits) with **one general lane** (`ATUS_API_MAX_INFLIGHT=1`, `_HEAVY=1`), by view kind (ms):

| kind | n | status (codes) | p50 | p95 | p99 | max (ms) | Server-Timing db p50 (ms) |
|---|---|---|---|---|---|---|---|
| activities | 58 | 200×58 | 8.3 | 46.1 | 248 | 439 | 0.0 |
| activity_detail | 22 | 200×22 | 1.0 | 1.6 | 23.2 | 28.9 | 0.0 |
| meta | 58 | 200×58 | 2.0 | 4.9 | 23.6 | 47.5 | 0.0 |
| novel_heavy | 21 | 200×14 422×1 503×6 (insufficient_data 1, overloaded 6) | 6523 | 11652 | 12152 | 12276 | 6449 |
| novel_light | 35 | 200×31 422×1 503×3 (insufficient_data 1, overloaded 3) | 1267 | 5156 | 5590 | 5644 | 962 |
| population_metadata | 58 | 200×58 | 1.5 | 25.3 | 54.3 | 90.7 | 0.0 |
| preset | 56 | 200×47 503×9 (overloaded 9) | 1238 | 20760 | 21678 | 21755 | 650 |
| revisit | 7 | 200×6 422×1 (insufficient_data 1) | 4.8 | 11226 | 11405 | 11450 | 0.0 |
| revisit_fallback | 9 | 200×9 | 3.6 | 4.9 | 4.9 | 4.9 | 0.0 |
| starter | 46 | 200×44 503×2 (overloaded 2) | 4.7 | 4145 | 13844 | 21068 | 0.0 |
| wake | 35 | 200×35 | 3.3 | 4.5 | 4.6 | 4.6 | 0.0 |

All mixes, overall:

| Run | n | status (codes) | p50 | p95 | p99 | max (ms) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.25 CU, origin, 2 visits/min | 31 | 200×31 | 1151 | 11388 | 16207 | 18172 | 0.054 | 0.054 | 1146 / 3.1 | 98.5 / 99.4% | 130 |
| 2 CU, origin, 2 visits/min | 31 | 200×31 | 112 | 1088 | 1668 | 1906 | 0.054 | 0.054 | 107 / 2.3 | 9.6 / 97.6% | 130 |
| 0.25 CU, API, 6 visits/min | 405 | 200×391 422×3 503×11 (insufficient_data 3, overloaded 11) | 4.5 | 7092 | 23378 | 30519 | 0.654 | 0.631 | 0.0 / 1.4 | 479.8 / 86.6% | 586 |
| 0.25 CU, API, 6 visits/min, 1 lane | 405 | 200×382 422×3 503×20 (insufficient_data 3, overloaded 20) | 4.7 | 6509 | 20696 | 21755 | 0.656 | 0.618 | 0.0 / 1.7 | 369.9 / 72.3% | 553 |
| 2 CU, API, 6 visits/min | 427 | 200×424 422×3 (insufficient_data 3) | 4.7 | 846 | 2013 | 2198 | 0.704 | 0.699 | 0.0 / 1.6 | 51.0 / 94.8% | 672 |

**What the numbers show.** At 2 visits/min (origin view), all 31 requests answered 200 at both profiles, with no shed: light novel analyses took 1.18 s p50 at 0.25 CU and 113 ms at 2 CU; heavy ones (full-period trends and 8–22-year pooled estimates) took 10.4 s and 1.02 s. In the API-view mix at 6 visits/min, 2 CU answered all 427 requests (424 × 200, 3 × `422 insufficient_data`). At 0.25 CU with production lanes, 11 of 405 were shed with `503 overloaded`, and every one of them was a heavy analysis: the heavy lane was busy for longer than the 5 s wait while a 23-year trend ran (trend misses p50 21.7 s). Every light novel analysis was answered (p50 1.68 s, p95 3.64 s). Metadata and cache hits stayed at p50 1.2–8.9 ms. With one general lane on the same visits, sheds rose to 20, and 10 of them were light single-year analyses: 3 novel, 2 starters and 5 presets. Light analyses that got through were faster at p50 (1.27 s), but their p95 rose to 5.16 s. No `analysis_timeout`, `429` or transport error occurred in any mix.

### 5.7 Memory and CPU

Per scenario:
- API: peak RSS of the process (VmHWM after the scenario), its cgroup's `memory.peak`, and CPU seconds with the share of throttled CFS periods.
- Database: CPU seconds (cgroup), `docker stats` CPU (100% = one core) and memory.

The cgroup deltas cover the whole k6 run, including `setup()`. That is why the cached runs at 1 VU show the warm set's database CPU.

| Run | wall (s) | API VmHWM (MiB) | API cgroup peak (MiB) | API CPU s (periods throttled) | API CPU % p50 / max | DB CPU s (periods throttled) | DB CPU % p50 / max | DB mem max (MiB) |
|---|---|---|---|---|---|---|---|---|
| 0.25 CU metadata 1 VU | 60.027 | 67.3 | 50.2 | 6.043 (96%) | 10.0 / 10.6 | 0.981 (21%) | 1.1 / 5.1 | 266.7 |
| 0.25 CU cached 1 VU | 60.061 | 68.9 | 51.1 | 6.24 (75%) | 9.9 / 11.2 | 11.853 (84%) | 3.2 / 26.5 | 300.2 |
| 0.25 CU uncached | 72.097 | 67.5 | 50.1 | 0.532 (1%) | 0.6 / 1.8 | 18.121 (99%) | 24.9 / 27.3 | 294.6 |
| 0.25 CU worst case | 1042.246 | 70.1 | 53.1 | 3.379 (1%) | 0.3 / 3.5 | 260.384 (100%) | 24.9 / 27.6 | 350.6 |
| 0.25 CU worst case, workers 0 | 784.22 | 70.5 | 53.4 | 1.987 (1%) | 0.2 / 3.5 | 195.934 (100%) | 24.9 / 27.3 | 320.8 |
| 0.25 CU saturation 16 VUs | 125.749 | 73.1 | 55.8 | 2.178 (3%) | 1.6 / 5.7 | 31.458 (99%) | 24.9 / 27.0 | 311.6 |
| 0.25 CU lanes 8, 8 VUs | 133.157 | 70.3 | 53.3 | 1.339 (1%) | 0.6 / 6.2 | 33.423 (99%) | 24.9 / 27.8 | 419.3 |
| 0.25 CU mix origin | 574.703 | 68.3 | 50.7 | 1.731 (0%) | 0.2 / 1.8 | 33.287 (65%) | 3.1 / 27.0 | 290.2 |
| 0.25 CU mix API | 619.254 | 70.8 | 55.9 | 3.422 (2%) | 0.3 / 5.4 | 104.97 (91%) | 24.6 / 27.1 | 324.9 |
| 0.25 CU mix API, 1 lane | 617.7 | 69.3 | 52.2 | 3.253 (2%) | 0.3 / 6.2 | 98.2 (89%) | 24.4 / 27.2 | 301.4 |
| 2 CU metadata 4 VUs | 60.16 | 67.2 | 49.7 | 6.05 (97%) | 10.0 / 10.9 | 1.019 (0%) | 0.6 / 6.0 | 267.3 |
| 2 CU uncached | 4.99 | 67.4 | 50.1 | 0.174 (1%) | 3.0 / 3.7 | 6.068 (3%) | 120.1 / 124.9 | 283.6 |
| 2 CU worst case | 71.345 | 69.7 | 52.2 | 0.302 (0%) | 0.3 / 1.0 | 79.269 (9%) | 101.2 / 157.6 | 346.3 |
| 2 CU saturation 16 VUs | 120.59 | 73.4 | 56.6 | 7.343 (5%) | 6.2 / 8.5 | 240.145 (98%) | 200.2 / 205.4 | 326.9 |
| 2 CU lanes 8, 8 VUs | 120.443 | 76.1 | 59.3 | 6.887 (22%) | 5.8 / 8.6 | 240.449 (99%) | 199.0 / 209.0 | 452.1 |
| 2 CU mix origin | 572.1 | 67.8 | 50.8 | 1.672 (0%) | 0.2 / 1.1 | 19.654 (0%) | 1.9 / 100.1 | 290.6 |
| 2 CU mix API | 606.819 | 69.5 | 52.7 | 3.366 (1%) | 0.3 / 4.2 | 61.854 (0%) | 3.2 / 101.9 | 307.7 |
| laptop worst case | 334.038 | 69.7 | 52.9 | 0.425 (-) | 0.1 / 1.0 | 344.778 (-) | 100.1 / 167.6 | 313.7 |
| laptop uncached | 21.888 | 67.4 | 49.9 | 0.187 (-) | 0.7 / 1.7 | 25.458 (-) | 114.6 / 132.0 | 286.2 |

**Memory.**
- The API process peaked at 76.1 MiB RSS. Its cgroup peaked at 59.3 MiB, against a 512 MiB limit.
- These peaks include a full result cache. Every 2 CU sweep level computed 1,355–2,914 analyses (cache misses, API metrics) in one API process, and the API's `atus_analysis_cache_entries` read 256 (the LRU's size) after each of them, the 76.1 MiB run included. That leaves 6.7 × headroom on RSS. The audit had measured about 20 KB per entry and about 70 MB with the cache full, on an earlier build.
- At 0.25 CU the database's `docker stats` working set was at most 419 MiB of its 1 GiB. Its cgroup's `memory.peak`, which also counts the page cache and shared buffers, reached at most 567 MiB (`cgroup.json`), so the 0.25 CU database never came near its memory limit, and the 0.65 GB cluster was never fully cached either. Its latencies come from the CPU quota.

**CPU.**
- At 0.25 CU the database was throttled in 98–100% of periods in the runs that kept an analysis in flight (uncached, worst cases, sweeps), and in 65–91% in the mixes, which had idle gaps.
- At 2 CU, one single-year estimate at a time used about 1.2 cores (`docker stats` p50 120–124%), one worst case about 1.0 (p50 101%), and two or more analyses used both cores (p50 199–200%).

### 5.8 API versus database time

**PostgreSQL's log.** The share of client latency spent in the API's database statements (PG-logged DB time ÷ sum of latencies, main phase):
- uncached single-year: 98.7–99.1% at 0.25 CU, 96.6–97.0% at 2 CU, 98.9% on the laptop (5.2);
- worst cases: 99.7–99.9% everywhere (5.3).

**`Server-Timing`.**
- `app` (API time outside database transactions) p50: 1.4–1.5 ms for uncached estimates at 0.25 CU and 0.7 ms at 2 CU, 1.1–2.6 ms for worst cases, and 0.6–0.7 ms for metadata and hits.
- Under load at 0.25 CU, `app` grows with the time spent waiting for a lane, and the database's share of the latency falls accordingly (the 5.4 sweep, all answers):

| Run | n | status (codes) | p50 | p95 | p99 | max (s) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 VU | 139 | 200×138 422×1 (insufficient_data 1) | 0.70 | 1.92 | 4.15 | 5.01 | 1.145 | 1.136 | 695 / 1.6 | 119.6 / 98.6% | 823 |
| 2 VUs | 85 | 200×85 | 2.39 | 7.79 | 10.79 | 10.80 | 0.703 | 0.703 | 2390 / 2.2 | 237.9 / 98.5% | 504 |
| 4 VUs | 101 | 200×92 503×9 (overloaded 9) | 4.80 | 8.80 | 11.39 | 11.40 | 0.822 | 0.749 | 2107 / 2280 | 242.2 / 49.8% | 546 |
| 8 VUs | 172 | 200×112 422×1 503×59 (insufficient_data 1, overloaded 59) | 5.11 | 8.20 | 10.09 | 11.86 | 1.37 | 0.892 | 1243 / 4504 | 246.4 / 25.0% | 673 |
| 16 VUs | 354 | 200×107 422×1 503×246 (insufficient_data 1, overloaded 246) | 5.01 | 7.81 | 10.05 | 13.07 | 2.815 | 0.851 | 0.0 / 5004 | 246.6 / 12.5% | 649 |


**Where the rest goes.** The remaining client latency is the 0.1-CPU API's own request handling plus throttling waits. It is visible in the metadata and cached runs (5.1), not in analyses.

## 6. D. Laptop baseline (unconstrained API, FULL database)

The same image, unconstrained, against `atus-postgres:5434` (data version `0325:run1:1788919495952073`). Every API session was read-only (`default_transaction_read_only=on` plus the API's `BEGIN READ ONLY`). The same uncached and worst-case specs were used, one at a time. The full database was never started, stopped or written; its statement durations were logged through a per-session option of the API's connections.

| Run | n | status (codes) | p50 | p95 | p99 | max (s) | req/s | 200/s | Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | PG statements |
|---|---|---|---|---|---|---|---|---|---|---|---|
| uncached single-year (59 misses) | 59 | 200×59 | 0.34 | 0.63 | 0.98 | 1.02 | 2.696 | 2.696 | 339 / 0.7 | 21.6 / 98.9% | 354 |
| worst cases, 6 kinds × 5 | 30 | 200×30 | 10.99 | 12.05 | 12.07 | 12.07 | 0.09 | 0.09 | 10988 / 1.1 | 333.7 / 99.9% | 185 |

| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | Server-Timing db p50 (s) |
|---|---|---|---|---|---|---|---|---|
| W1-pooled22-leisure | 5 | 200×5 | 10.53 | 11.04 | 11.09 | 11.09 | 11.09 | 11.04 |
| W2-trend23-12 | 5 | 200×5 | 10.51 | 10.97 | 11.00 | 11.01 | 11.01 | 10.97 |
| W3-trend23-010101 | 5 | 200×5 | 10.46 | 10.89 | 10.91 | 10.91 | 10.92 | 10.88 |
| W4-F66-pooled22-tier1 | 5 | 200×5 | 10.67 | 11.96 | 12.06 | 12.07 | 12.07 | 11.96 |
| W5-F66-trend23-tier1 | 5 | 200×5 | 10.66 | 11.99 | 12.05 | 12.06 | 12.06 | 11.99 |
| W6-F66-compare22-sex | 5 | 200×5 | 11.43 | 11.45 | 11.51 | 11.52 | 11.52 | 11.45 |

**Compared with the serving database.**
- The full database is slower than the serving database at 2 CU (W1 11.0 s against 2.1 s) but faster than at 0.25 CU (11.0 s against 21.4 s).
- It runs with `jit=on` server-wide (the API turns JIT off per transaction), `shared_buffers=128MB`, unconstrained CPU, and the full episode-level schema.
- Its F66 requests cost about the same as the others: 11.5–12.0 s at p50, max 12.1 s. On the serving database the F66 penalty is large only under the 0.25 CU quota: W6 is 3.2 × W1 there, against 1.4 × at 2 CU.

## 7. Across profiles (p50 / p99, seconds)

| Request | 0.25 CU | 2 CU | laptop, full DB |
|---|---|---|---|
| metadata route (1 client) | 0.0019 / 0.106 | 0.0019 / 0.107 | not run |
| cached analysis (1 client) | 0.0016 / 0.096 | 0.0016 / 0.095 | not run |
| uncached single-year estimate, SE | 0.99 / 4.43 | 0.079 / 0.211 | 0.34 / 0.98 |
| same, `MAX_PARALLEL_WORKERS=0` | 0.70 / 2.63 | 0.079 / 0.252 | not run |
| worst case, non-F66 (W1–W3, by kind) | 21.4–21.8 / 23.6–25.8 | 2.03–2.12 / 2.04–2.15 | 10.9–11.0 / 10.9–11.1 |
| worst case, F66 pooled or trend (W4, W5) | 33.5–35.0 / 42.0 | 2.67–2.68 / 2.69–2.70 | 12.0 / 12.1 |
| worst case, F66 compare (W6) | 69.2 / 71.0 | 2.99 / 3.04 | 11.5 / 11.5 |
| all worst cases (n=30) | 28.9 / 70.5 | 2.24 / 3.03 | 11.0 / 12.1 |
| cold start to `/health` | 13.1 (p50) | 12.9 (p50) | not run |

All cells are copied from the tables in sections 4–6. The cold start runs the API at 0.1 CPU in both columns; only the database differs.

## 8. `statement_timeout` calibration

The rule, from ADR-011 and ADR-012: `statement_timeout = clamp(3 × p99 of the worst analyses including the F66 broad selectors, 30 s, 85 s)`. Here p99 is taken over all 30 worst-case requests of each run (W1–W6, 5 each), one at a time, from `report.py --calibrate`.

| Profile | parallel workers | p99 (s) | 3 × p99 (s) | clamped |
|---|---|---|---|---|
| **0.25 CU (floor)** | server default (production today) | 70.52 | 211.6 | **85 s** |
| 0.25 CU | 0 | 40.54 | 121.6 | 85 s |
| **2 CU (ceiling)** | server default | 3.03 | 9.1 | **30 s** |
| 2 CU | 0 | 3.53 | 10.6 | 30 s |
| laptop, full DB (for reference) | server default | 12.07 | 36.2 | 36.2 s |

**Decision: keep `ATUS_API_STATEMENT_TIMEOUT_MS=85000`.** The deadline stays 90 s, and the role default stays 85 s.

- The compute autoscales from 0.25 CU, and a request can arrive while it is at the floor, so the floor's value governs.
- The clamp's ceiling exists because of the proxy limits (ADR-011: every limit at or below 100 s, under Cloudflare's 125 s).

**Said plainly, the target headroom is not reachable at the floor.**
- With the server's default parallelism, 85 s is 1.2 × the F66 compare's p99 (71.0 s max). It is 2.0 × the single-group F66 maximum (42.0 s) and 3.3 × the non-F66 maximum (25.8 s).
- Any extra CPU contention at 0.25 CU pushes F66 requests past 85 s:
  - two heavy analyses at once took 88–106 s for F66 (5.5), which `MAX_INFLIGHT_HEAVY=1` prevents;
  - a light analysis running beside a heavy one also shares the 0.25 vCPU, and nothing prevents that.
- Such a request becomes `503 analysis_timeout` (`Retry-After: 60`), never a wrong or cached answer.
- At 2 CU the same requests take about 3 s, with 28 × headroom.
- How quickly Neon's autoscaler leaves 0.25 CU under load was not measured (F41). It decides how often the floor's numbers apply.

## 9. Recommendations from the data

| Setting | Today | Recommendation | Evidence |
|---|---|---|---|
| `ATUS_API_MAX_INFLIGHT` | 2 | **keep 2** | 2 CU: throughput peaks at 2 in flight (24.2/s against 15.8/s at 1, and 18.3/s at 4 without a cap). 0.25 CU: 1 in flight is best for throughput (1.14/s against 0.70/s at 2), but the API-view mix at 0.25 CU (5.6, the same visits) shows the cost of one lane. One lane shed 20 of 405 requests, 10 of them light single-year analyses. Two lanes shed 11, all heavy, and no light one. With 1 lane, one heavy analysis (at least 5.3 s at 0.25 CU, and 17–71 s for the worst cases) would block every light one behind it, and they would be shed after 5 s. Two lanes let a light analysis run beside a heavy one. |
| `ATUS_API_MAX_INFLIGHT_HEAVY` | 1 | **keep 1** | Two heavy analyses at once at 0.25 CU took 2.6–3.6 × as long each, so throughput fell and F66 exceeded 85 s (88–106 s). At 2 CU a second heavy lane would nearly double heavy throughput, but the API cannot know the current CU. Revisit only if the compute's minimum is raised. |
| `ATUS_API_ADMISSION_WAIT_S` | 5 | **keep 5** | A queued light analysis is admitted if a lane frees within the wait. At 0.25 CU a light analysis takes 0.99 s p50 and 3.19 s p95 when alone, so 5 s covers a queue of one behind a light request, and nothing behind a heavy one (at least 5.3 s of database time for an 8-year pooled estimate, 17–71 s for the worst cases). At 2 CU no request waited more than about 1 s even at 16 clients. Shed requests were answered after 5.0 s at p50 (max 5.6 s). A longer wait would only lengthen the answer to a request that will be shed. |
| `ATUS_API_MAX_PARALLEL_WORKERS` | unset (server default 2) | **leave unset** (ADR-011 rule), and **set 0 if the compute is fixed at 0.25 CU** | 0.25 CU: 0 halves the F66 compare (69.2 → 38.6 s p50) and speeds up single-year estimates (0.99 → 0.70 s p50). 2 CU: 0 is slower (worst p99 3.03 → 3.53 s, single-year p95 142 → 209 ms). ADR-011 adopts 0 "only if it also wins at 2 CU in emulation", and it does not. Adoption in either case needs the §9 equivalence proof: the audit's probe found point sums identical and replicate sums within 1.34e-14 relative, which is not yet the §9 run. |
| `ATUS_API_STATEMENT_TIMEOUT_MS` | 85000 | **keep 85000** | Section 8. |
| `ATUS_API_POOL_MAX_SIZE` | 4 | **keep 4** | With 2 lanes, analyses hold at most 2 connections, and metadata, hits and the snapshot read need none or one. No pool timeout occurred in any run. |

Changing a default is a code and ADR change. This record makes no code change to the API: every value above either stays as it is or is conditional on an owner decision (the compute's minimum). Section 13 records how ADR-012 applied these recommendations.

## 10. What these runs do not show

**Not emulated** (audit F41):
- Neon's remote pageserver reads, wake from suspend, and the autoscaler. Each profile is a fixed size.
- The Render↔Neon round trip; the stack uses a local bridge network.
- Render's real CPU-share mechanism and its spin-up.
- The cloud CPU's speed and micro-architecture: these runs used an arm64 build on an Apple M4, while Render runs linux/amd64.

**Measurement effects:**
- Statement-duration logging (`log_min_duration_statement=0`) costs the database some CPU, at 0.25 CU too. The audit measured the same way.
- The k6 clock is the host's and PostgreSQL's is the Docker VM's. They are compared only to split DB time between k6's setup and main phases.

**Settings:**
- The stack's `effective_cache_size` is PostgreSQL's 4 GB default at every profile, including 0.25 CU with 1 GiB. Neon's value at 0.25 CU may differ, which could change plans.
- 2 CU ran with 7 GiB, not 8 GB.

**Sample sizes:**
- The worst-case percentiles rest on n=5 per kind.
- The origin mix rests on 21 analyses per profile.
- Use the per-kind maxima, not p99 alone.

**Workload not covered:**
- Compares, trends and pooled ranges other than the worst cases were measured only inside the mixes.
- `/activities` search and 4xx paths were not load-tested here; they are covered functionally.

**Variance:** each profile was measured once, in one session. Run-to-run variance was not measured.

## 11. Raw files

Raw bundles stayed outside the repository: per-request records, PostgreSQL and API logs, the `docker stats` streams and per-stage secrets. `loadtests/results/raw/` holds compact copies with no logs and no secrets. Each bundle was scanned for the stage's secret values before export.

| Bundle | Profile | What ran (k6 settings; API overrides besides the daily cap) |
|---|---|---|
| `20261021T035629Z-0.25cu` | 0.25cu | **warm-up, not reported**: worstcase (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); uncached (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T040144Z-0.25cu` | 0.25cu | worstcase (WORST_SET=all, ITERATIONS=30, VUS=1, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T042009Z-0.25cu` | 0.25cu | worstcase (WORST_SET=all, ITERATIONS=30, VUS=1, REQ_TIMEOUT=700s); API: `ATUS_API_MAX_PARALLEL_WORKERS=0`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T043356Z-0.25cu` | 0.25cu | uncached (EXECUTOR=iterations, VUS=1, ITERATIONS=60); API: `ATUS_API_MAX_PARALLEL_WORKERS=0`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T043548Z-0.25cu` | 0.25cu | uncached-vus1 (EXECUTOR=duration, DURATION=120s, VUS=1); uncached-vus2 (EXECUTOR=duration, DURATION=120s, VUS=2); uncached-vus4 (EXECUTOR=duration, DURATION=120s, VUS=4); uncached-vus8 (EXECUTOR=duration, DURATION=120s, VUS=8); API: `ATUS_API_MAX_INFLIGHT=8`, `ATUS_API_MAX_INFLIGHT_HEAVY=8`, `ATUS_API_POOL_MAX_SIZE=8` |
| `20261021T044610Z-0.25cu` | 0.25cu | worstcase (WORST_SET=core, VUS=2, ITERATIONS=10, REQ_TIMEOUT=700s); API: `ATUS_API_MAX_INFLIGHT_HEAVY=2`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T045348Z-0.25cu` | 0.25cu | cold start ×5; metadata-vus1 (EXECUTOR=duration, DURATION=60s, VUS=1); metadata-vus4 (EXECUTOR=duration, DURATION=60s, VUS=4) |
| `20261021T045813Z-0.25cu` | 0.25cu | cached-vus1 (WARM_SET=presets, EXECUTOR=duration, DURATION=60s, VUS=1); cached-vus4 (WARM_SET=presets, EXECUTOR=duration, DURATION=60s, VUS=4) |
| `20261021T050129Z-0.25cu` | 0.25cu | uncached (EXECUTOR=iterations, VUS=1, ITERATIONS=60) |
| `20261021T050324Z-0.25cu` | 0.25cu | uncached-vus1 (EXECUTOR=duration, DURATION=120s, VUS=1); uncached-vus2 (EXECUTOR=duration, DURATION=120s, VUS=2); uncached-vus4 (EXECUTOR=duration, DURATION=120s, VUS=4); uncached-vus8 (EXECUTOR=duration, DURATION=120s, VUS=8); uncached-vus16 (EXECUTOR=duration, DURATION=120s, VUS=16) |
| `20261021T051548Z-0.25cu` | 0.25cu | mix (MIX_MODE=origin, EXECUTOR=arrival, RATE=2, DURATION=10m) |
| `20261021T052710Z-2cu` | 2cu | **warm-up, not reported**: worstcase (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); uncached (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T052843Z-2cu` | 2cu | worstcase (WORST_SET=all, ITERATIONS=30, VUS=1, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T053052Z-2cu` | 2cu | worstcase (WORST_SET=all, ITERATIONS=30, VUS=1, REQ_TIMEOUT=700s); API: `ATUS_API_MAX_PARALLEL_WORKERS=0`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T053248Z-2cu` | 2cu | uncached (EXECUTOR=iterations, VUS=1, ITERATIONS=60); API: `ATUS_API_MAX_PARALLEL_WORKERS=0`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T053350Z-2cu` | 2cu | uncached-vus1 (EXECUTOR=duration, DURATION=120s, VUS=1); uncached-vus2 (EXECUTOR=duration, DURATION=120s, VUS=2); uncached-vus4 (EXECUTOR=duration, DURATION=120s, VUS=4); uncached-vus8 (EXECUTOR=duration, DURATION=120s, VUS=8); API: `ATUS_API_MAX_INFLIGHT=8`, `ATUS_API_MAX_INFLIGHT_HEAVY=8`, `ATUS_API_POOL_MAX_SIZE=8` |
| `20261021T054401Z-2cu` | 2cu | worstcase (WORST_SET=core, VUS=2, ITERATIONS=10, REQ_TIMEOUT=700s); API: `ATUS_API_MAX_INFLIGHT_HEAVY=2`, `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T054515Z-2cu` | 2cu | cold start ×5; metadata-vus1 (EXECUTOR=duration, DURATION=60s, VUS=1); metadata-vus4 (EXECUTOR=duration, DURATION=60s, VUS=4) |
| `20261021T054942Z-2cu` | 2cu | cached-vus1 (WARM_SET=presets, EXECUTOR=duration, DURATION=60s, VUS=1); cached-vus4 (WARM_SET=presets, EXECUTOR=duration, DURATION=60s, VUS=4) |
| `20261021T055223Z-2cu` | 2cu | uncached (EXECUTOR=iterations, VUS=1, ITERATIONS=60) |
| `20261021T055313Z-2cu` | 2cu | uncached-vus1 (EXECUTOR=duration, DURATION=120s, VUS=1); uncached-vus2 (EXECUTOR=duration, DURATION=120s, VUS=2); uncached-vus4 (EXECUTOR=duration, DURATION=120s, VUS=4); uncached-vus8 (EXECUTOR=duration, DURATION=120s, VUS=8); uncached-vus16 (EXECUTOR=duration, DURATION=120s, VUS=16) |
| `20261021T060549Z-2cu` | 2cu | mix (MIX_MODE=origin, EXECUTOR=arrival, RATE=2, DURATION=10m) |
| `20261021T061645Z-laptop-fulldb` | laptop-fulldb | **warm-up, not reported**: worstcase (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); uncached (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T061825Z-laptop-fulldb` | laptop-fulldb | worstcase (WORST_SET=all, ITERATIONS=30, VUS=1, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T062430Z-laptop-fulldb` | laptop-fulldb | uncached (EXECUTOR=iterations, VUS=1, ITERATIONS=60) |
| `20261021T062537Z-2cu` | 2cu | mix (MIX_MODE=api, EXECUTOR=arrival, RATE=6, DURATION=10m) |
| `20261021T063653Z-0.25cu` | 0.25cu | **warm-up, not reported**: worstcase (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); uncached (WORST_SET=all, ITERATIONS=6, REQ_TIMEOUT=700s); API: `ATUS_API_STATEMENT_TIMEOUT_MS=600000` |
| `20261021T064154Z-0.25cu` | 0.25cu | uncached-vus2 (EXECUTOR=duration, DURATION=120s, VUS=2); uncached-vus4 (EXECUTOR=duration, DURATION=120s, VUS=4); uncached-vus8 (EXECUTOR=duration, DURATION=120s, VUS=8); uncached-vus16 (EXECUTOR=duration, DURATION=120s, VUS=16); API: `ATUS_API_MAX_INFLIGHT=1`, `ATUS_API_MAX_INFLIGHT_HEAVY=1` |
| `20261021T065213Z-0.25cu` | 0.25cu | mix (MIX_MODE=api, EXECUTOR=arrival, RATE=6, DURATION=10m) |
| `20261021T070335Z-0.25cu` | 0.25cu | mix (MIX_MODE=api, EXECUTOR=arrival, RATE=6, DURATION=10m); API: `ATUS_API_MAX_INFLIGHT=1`, `ATUS_API_MAX_INFLIGHT_HEAVY=1` |

**Files in each `raw/<bundle>/`:**
- `environment.json`: machine, image, limits, DB settings, the redacted API environment, ran-alone and precondition evidence, and background noise.
- `background_stats.csv`: other containers, once a minute.
- `<scenario>/summary.json`: k6's end-of-test summary.
- `<scenario>/analysis.json`: per endpoint, cache outcome and view kind.
- `<scenario>/db_time.json`: PostgreSQL-logged DB time between the markers.
- `<scenario>/cgroup.json`: cgroup CPU and memory, and PID 1's VmHWM.
- `<scenario>/stats_summary.json`.
- `<scenario>/docker_stats.csv.gz`: about one sample per second for the API and DB containers.
- `<scenario>/requests.csv.gz`: one row per request (phase, endpoint, kind, spec id, relative start, status, error code, X-Cache, latency, Server-Timing db/app, Retry-After, VU).
- `cold_start/cold_start.json`, in the two cold-start bundles.

**Elsewhere in `raw/`:**
- `raw/restore-drill/drill.json` and `restore-report.json`: the restore drill.
- `raw/precondition/`: one precondition snapshot per step.

**Reproducing.**
- Every table above was generated by `python3 loadtests/report.py` (`--combine`, `--kinds`, `--saturation`, `--breakdown`, `--ran-alone`, `--calibrate`) from the raw bundles. Run against `loadtests/results/raw/<bundle>` instead, the same commands reproduce every table byte for byte (checked before committing).
- `--kinds` and `--saturation` read the per-request rows: `requests.csv.gz` in the export.
- The commands that produced the bundles are listed in section 12.

**State left behind.**
- No `atus-lt-*` container is running: the stack was stopped with `run.py down --stage timed`, and the drill container was removed.
- Two volumes remain:
  - `atus-lt-timed_pgdata`, the restored serving database of these runs;
  - `atus-lt-final_pgdata`, from the earlier dry run.

  `python3 loadtests/run.py down --stage <stage> --out-root <out-root> --purge` deletes either.
- The image `atus-api:lt-9afd1b4e2e0e` remains.
- `atus-postgres`, `atus-wsb-drill`, `atus-wsb-build` and `atus-emu-pg` were never started, stopped or written. Only read-only sessions touched `atus-postgres`, for the baseline and for `pg_stat_activity`; the other three were only read through `pg_stat_activity`.

## 12. Commands, in order

Run from the repository root with `ATUS_LT_OUT_ROOT` (outside the repository) and `ATUS_LT_DUMP` (the release artifact) exported. Each step was preceded by `python3 loadtests/precondition.py --out <file>`, and only one step ran at a time. The restore drill (section 3) ran first, from 03:54:11Z.

```text
2026-10-21T03:56:09Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --statement-timeout-ms 600000 --scenarios worstcase,uncached --set WORST_SET=all --set ITERATIONS=6 --set REQ_TIMEOUT=700s
2026-10-21T04:01:24Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --statement-timeout-ms 600000 --scenarios worstcase --set WORST_SET=all --set ITERATIONS=30 --set VUS=1 --set REQ_TIMEOUT=700s
2026-10-21T04:19:48Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --statement-timeout-ms 600000 --max-parallel-workers 0 --scenarios worstcase --set WORST_SET=all --set ITERATIONS=30 --set VUS=1 --set REQ_TIMEOUT=700s
2026-10-21T04:33:54Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=iterations --set VUS=1 --set ITERATIONS=60
2026-10-21T04:35:30Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --api-env ATUS_API_MAX_INFLIGHT=8 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=8 --api-env ATUS_API_POOL_MAX_SIZE=8 --scenarios uncached --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8
2026-10-21T04:45:51Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --statement-timeout-ms 600000 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=2 --scenarios worstcase --set WORST_SET=core --set VUS=2 --set ITERATIONS=10 --set REQ_TIMEOUT=700s
2026-10-21T04:53:29Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --cold-start 5 --scenarios metadata --set EXECUTOR=duration --set DURATION=60s --sweep VUS=1,4
2026-10-21T04:58:10Z  python3 loadtests/run.py scenario --stage timed cached --set WARM_SET=presets --set EXECUTOR=duration --set DURATION=60s --sweep VUS=1,4 --no-api-restart
2026-10-21T05:01:26Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=iterations --set VUS=1 --set ITERATIONS=60
2026-10-21T05:03:23Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8,16
2026-10-21T05:15:45Z  python3 loadtests/run.py scenario --stage timed mix --set MIX_MODE=origin --set EXECUTOR=arrival --set RATE=2 --set DURATION=10m
2026-10-21T05:26:48Z  python3 loadtests/run.py profile --stage timed --profile 2cu --statement-timeout-ms 600000 --scenarios worstcase,uncached --set WORST_SET=all --set ITERATIONS=6 --set REQ_TIMEOUT=700s
2026-10-21T05:28:24Z  python3 loadtests/run.py profile --stage timed --profile 2cu --statement-timeout-ms 600000 --scenarios worstcase --set WORST_SET=all --set ITERATIONS=30 --set VUS=1 --set REQ_TIMEOUT=700s
2026-10-21T05:30:34Z  python3 loadtests/run.py profile --stage timed --profile 2cu --statement-timeout-ms 600000 --max-parallel-workers 0 --scenarios worstcase --set WORST_SET=all --set ITERATIONS=30 --set VUS=1 --set REQ_TIMEOUT=700s
2026-10-21T05:32:46Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=iterations --set VUS=1 --set ITERATIONS=60
2026-10-21T05:33:33Z  python3 loadtests/run.py profile --stage timed --profile 2cu --api-env ATUS_API_MAX_INFLIGHT=8 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=8 --api-env ATUS_API_POOL_MAX_SIZE=8 --scenarios uncached --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8
2026-10-21T05:43:42Z  python3 loadtests/run.py profile --stage timed --profile 2cu --statement-timeout-ms 600000 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=2 --scenarios worstcase --set WORST_SET=core --set VUS=2 --set ITERATIONS=10 --set REQ_TIMEOUT=700s
2026-10-21T05:44:57Z  python3 loadtests/run.py profile --stage timed --profile 2cu --cold-start 5 --scenarios metadata --set EXECUTOR=duration --set DURATION=60s --sweep VUS=1,4
2026-10-21T05:49:39Z  python3 loadtests/run.py scenario --stage timed cached --set WARM_SET=presets --set EXECUTOR=duration --set DURATION=60s --sweep VUS=1,4 --no-api-restart
2026-10-21T05:52:20Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=iterations --set VUS=1 --set ITERATIONS=60
2026-10-21T05:53:11Z  python3 loadtests/run.py scenario --stage timed uncached --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8,16
2026-10-21T06:05:47Z  python3 loadtests/run.py scenario --stage timed mix --set MIX_MODE=origin --set EXECUTOR=arrival --set RATE=2 --set DURATION=10m
2026-10-21T06:16:40Z  python3 loadtests/run.py baseline --stage timed --statement-timeout-ms 600000 --scenarios worstcase,uncached --set WORST_SET=all --set ITERATIONS=6 --set REQ_TIMEOUT=700s
2026-10-21T06:18:21Z  python3 loadtests/run.py baseline --stage timed --statement-timeout-ms 600000 --scenarios worstcase --set WORST_SET=all --set ITERATIONS=30 --set VUS=1 --set REQ_TIMEOUT=700s
2026-10-21T06:24:26Z  python3 loadtests/run.py baseline --stage timed --scenarios uncached --set EXECUTOR=iterations --set VUS=1 --set ITERATIONS=60
2026-10-21T06:25:18Z  python3 loadtests/run.py profile --stage timed --profile 2cu --scenarios mix --set MIX_MODE=api --set EXECUTOR=arrival --set RATE=6 --set DURATION=10m
2026-10-21T06:36:31Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --statement-timeout-ms 600000 --scenarios worstcase,uncached --set WORST_SET=all --set ITERATIONS=6 --set REQ_TIMEOUT=700s
2026-10-21T06:41:35Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --api-env ATUS_API_MAX_INFLIGHT=1 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=1 --scenarios uncached --set EXECUTOR=duration --set DURATION=120s --sweep VUS=2,4,8,16
2026-10-21T06:51:54Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --scenarios mix --set MIX_MODE=api --set EXECUTOR=arrival --set RATE=6 --set DURATION=10m
2026-10-21T07:03:17Z  python3 loadtests/run.py profile --stage timed --profile 0.25cu --api-env ATUS_API_MAX_INFLIGHT=1 --api-env ATUS_API_MAX_INFLIGHT_HEAVY=1 --scenarios mix --set MIX_MODE=api --set EXECUTOR=arrival --set RATE=6 --set DURATION=10m
```

## 13. Calibration applied (2026-10-21)

ADR-012 ["Calibration (§13)"](../../docs/adr/ADR-012-load-and-quota-protection.md#calibration-13) records the decisions taken from sections 5–9. This section adds no new run. Its one table is read from the same bundles, with `report.py --lanes`, a read-only post-processing mode added after the runs.

**Values.** Every setting keeps its code default:

| Setting | Value | From |
|---|---|---|
| `ATUS_API_STATEMENT_TIMEOUT_MS` and the `atus_api` role default | 85000 (85 s); the deadline stays 90 s | section 8 |
| `ATUS_API_MAX_INFLIGHT` / `_HEAVY` | 2 / 1 | sections 5.4–5.6 |
| `ATUS_API_ADMISSION_WAIT_S` | 5 | sections 5.2, 5.4, 5.6 |
| `ATUS_API_MAX_PARALLEL_WORKERS` | unset (server default) | sections 5.2, 5.3 |
| heavy classifier | 8 year queries | below |
| `ATUS_API_POOL_MAX_SIZE` | 4 | sections 5.4, 9 |

**No confirmation run.** Nothing changed, so no latency or behaviour can differ from what sections 4–7 measured. The runs used production's lanes, timeout and parallel-worker setting, apart from the per-run overrides listed in section 11.

**Heavy-classifier check.** This table covers every analysis that computed (main phase, `200`, `X-Cache: miss`), grouped by the lane the API's rule gives it: `len(years)`, doubled for a compare, ≥ 8.

- The harness sends no spec between 2 and 7 year queries:
  - every trend covers 23 years;
  - W1–W6 cover 22–23 years;
  - `genp-` pooled estimates cover 8, 12 or 22 years;
  - every other spec is one year.
- Database time is `Server-Timing` `db`. It excludes the wait for a lane.

```text
python3 loadtests/report.py --lanes \
  "0.25 CU, uncached, 1 client=loadtests/results/raw/20261021T050129Z-0.25cu#uncached" \
  "0.25 CU, worst cases, 1 client=loadtests/results/raw/20261021T040144Z-0.25cu#worstcase" \
  "0.25 CU, origin mix=loadtests/results/raw/20261021T051548Z-0.25cu#mix" \
  "0.25 CU, API mix, 1 lane=loadtests/results/raw/20261021T070335Z-0.25cu#mix" \
  "0.25 CU, API mix, 2 lanes=loadtests/results/raw/20261021T065213Z-0.25cu#mix" \
  "2 CU, uncached, 1 client=loadtests/results/raw/20261021T055223Z-2cu#uncached" \
  "2 CU, worst cases, 1 client=loadtests/results/raw/20261021T052843Z-2cu#worstcase" \
  "2 CU, origin mix=loadtests/results/raw/20261021T060549Z-2cu#mix" \
  "2 CU, API mix, 2 lanes=loadtests/results/raw/20261021T062537Z-2cu#mix"
```

| Run | lane, endpoint | n | Server-Timing db min | p50 | p95 | max (s) | latency max (s) |
|---|---|---|---|---|---|---|---|
| 0.25 CU, uncached, 1 client | light estimate | 59 | 0.18 | 0.99 | 3.18 | 4.60 | 4.60 |
| 0.25 CU, worst cases, 1 client | heavy estimate | 10 | 17.48 | 29.91 | 41.97 | 42.02 | 42.02 |
| 0.25 CU, worst cases, 1 client | heavy trend | 15 | 18.81 | 23.61 | 41.30 | 41.98 | 41.99 |
| 0.25 CU, worst cases, 1 client | heavy compare | 5 | 67.58 | 69.20 | 70.65 | 71.01 | 71.02 |
| 0.25 CU, origin mix | light estimate | 9 | 0.36 | 1.06 | 1.72 | 1.80 | 1.80 |
| 0.25 CU, origin mix | light compare | 4 | 1.15 | 1.47 | 2.14 | 2.23 | 2.24 |
| 0.25 CU, origin mix | heavy estimate | 3 | 5.47 | 7.41 | 8.33 | 8.43 | 8.44 |
| 0.25 CU, origin mix | heavy trend | 5 | 9.81 | 11.14 | 16.86 | 18.17 | 18.17 |
| 0.25 CU, API mix, 1 lane | light estimate | 47 | 0.08 | 0.91 | 1.57 | 2.57 | 5.95 |
| 0.25 CU, API mix, 1 lane | light compare | 16 | 0.79 | 1.35 | 2.62 | 2.63 | 2.93 |
| 0.25 CU, API mix, 1 lane | heavy estimate | 10 | 5.79 | 6.48 | 8.19 | 8.39 | 9.56 |
| 0.25 CU, API mix, 1 lane | heavy trend | 13 | 8.97 | 18.52 | 21.28 | 21.61 | 21.75 |
| 0.25 CU, API mix, 2 lanes | light estimate | 50 | 0.33 | 1.37 | 3.25 | 3.68 | 3.68 |
| 0.25 CU, API mix, 2 lanes | light compare | 19 | 1.16 | 3.02 | 4.85 | 5.18 | 5.99 |
| 0.25 CU, API mix, 2 lanes | heavy estimate | 10 | 5.33 | 7.77 | 10.21 | 10.21 | 13.27 |
| 0.25 CU, API mix, 2 lanes | heavy trend | 12 | 9.51 | 21.72 | 27.84 | 28.54 | 30.52 |
| 2 CU, uncached, 1 client | light estimate | 59 | 0.02 | 0.08 | 0.14 | 0.23 | 0.23 |
| 2 CU, worst cases, 1 client | heavy estimate | 10 | 1.99 | 2.24 | 2.69 | 2.70 | 2.70 |
| 2 CU, worst cases, 1 client | heavy trend | 15 | 1.88 | 2.08 | 2.69 | 2.69 | 2.69 |
| 2 CU, worst cases, 1 client | heavy compare | 5 | 2.97 | 2.99 | 3.03 | 3.04 | 3.04 |
| 2 CU, origin mix | light estimate | 9 | 0.06 | 0.09 | 0.15 | 0.15 | 0.16 |
| 2 CU, origin mix | light compare | 4 | 0.11 | 0.13 | 0.14 | 0.14 | 0.15 |
| 2 CU, origin mix | heavy estimate | 3 | 0.58 | 0.73 | 0.80 | 0.80 | 0.81 |
| 2 CU, origin mix | heavy trend | 5 | 1.00 | 1.06 | 1.74 | 1.90 | 1.91 |
| 2 CU, API mix, 2 lanes | light estimate | 53 | 0.04 | 0.11 | 0.16 | 0.22 | 0.23 |
| 2 CU, API mix, 2 lanes | light compare | 21 | 0.10 | 0.14 | 0.18 | 0.21 | 0.22 |
| 2 CU, API mix, 2 lanes | heavy estimate | 12 | 0.54 | 0.68 | 0.84 | 0.84 | 0.85 |
| 2 CU, API mix, 2 lanes | heavy trend | 19 | 0.88 | 1.94 | 2.14 | 2.19 | 2.20 |

**What the numbers show.**

- **The rule separated the two classes in every run.** Every heavy request used more database time than every light one at the same profile:
  - at 0.25 CU, one analysis at a time (the 1-client runs and the one-lane mix): light at most 4.60 s, heavy at least 5.79 s;
  - at 0.25 CU with two lanes busy: light at most 5.18 s, heavy at least 5.33 s;
  - at 2 CU: light at most 0.23 s, heavy at least 0.54 s.
- **The margin at the boundary is narrow at 0.25 CU.** The costliest single-year spec (4.60 s) is close to the cheapest 8-year pooled estimate (5.33–5.79 s). The medians are far apart: 0.91–1.37 s for light estimates, against 6.48–29.91 s for heavy pooled estimates and 11.14–23.61 s for trends.
- **Not measured.** Specs of 2–7 year queries: pooled ranges of 2–7 years, compares over 2–3 years, and trends shorter than 8 years. The threshold's immediate neighbourhood is therefore untested.
- **Decision.** No measured request was misclassified, so the threshold stays at 8. The edge governor uses the same rule.

## 14. Later: cold start A/B after the standard-library fix (2026-10-21)

Added after sections 1–13 and measured separately, with its own method, ran-alone evidence and raw files: [`2026-10-21-cold-start-ab.md`](2026-10-21-cold-start-ab.md). Sections 4 and 7 above stay as measured.

Commit 5844ab7 precompiles the Python standard library in the image and keeps python-dotenv off the API's start-up. The A/B compared two images, alternately, on the stack of these runs at 0.25 CU:
- A: `atus-pb-api:base`, built at 00f3831, before 5844ab7;
- B: `atus-api:premerge`, built at e5ca1fd, after it.

Each image got one warm-up start, left out of the summary, then 8 measured starts. The API ran at 0.1 CPU / 512 MB.

| Image | to first `/health` 200: p50 / min / max (s) | to first `/api/v1/meta` 200: p50 / min / max (s) | start-up CPU: p50 / min / max (s) |
|---|---|---|---|
| A, before (00f3831) | 12.69 / 12.31 / 13.03 | 13.06 / 12.71 / 13.49 | 1.298 / 1.267 / 1.329 |
| B, after (e5ca1fd) | 5.76 / 5.53 / 6.51 | 6.12 / 5.63 / 6.84 | 0.609 / 0.567 / 0.673 |

**Limits of the comparison.**
- The two images also differ by other commits between 00f3831 and e5ca1fd. The A/B does not isolate 5844ab7.
- It ran at 0.25 CU only, in one session, with A started before B in every round.
- Render's spin-up and its amd64 CPU are not emulated.
