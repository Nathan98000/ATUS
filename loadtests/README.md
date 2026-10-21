# Load tests (brief §13)

Reproducible load tests for the ATUS API, run **locally** against the production-mode stack under the free-tier emulation profile, and against a laptop baseline for comparison. They exist to measure, never to guess: every number in a results file was produced by these scripts on a stated machine, and a dry run produces no numbers at all.

**Never point these at a hosting provider.** They send the Worker's edge secret straight to an API on `127.0.0.1`. The post-deploy measurements are a separate, quota-budgeted replay (`scripts/replay_deployed.py`, ADR-011 "Unknowns").

## What is here

| Path | What it is |
|---|---|
| `k6/metadata.js` | `GET /api/v1/meta`, `/activities`, `/activities/<code>` (2-, 4- and 6-digit codes in turn), `/population/metadata` |
| `k6/cached.js` | computes a warm set once in `setup()`, then repeats it: every measured request should be `X-Cache: hit` |
| `k6/uncached.js` | distinct single-year estimates with replicate SEs: the corpus's 128, then a generator of about 106,000 more |
| `k6/worstcase.js` | pooled 22-year estimates, 23-year trends and the F66 broad selectors (all 18 tier-1 codes pooled and trended) |
| `k6/mix.js` | visits drawn from the traffic model in [`docs/deployment.md`](../docs/deployment.md#realistic-demo-traffic-the-model-the-budget-uses) |
| `k6/lib/common.js` | edge headers, client addresses, metrics, the per-request record, options from the environment, the summary |
| `k6/lib/specs.js` | the spec corpus, starters, the prewarm list, the worst cases and the generators |
| `run.py` | the orchestrator: stack, restore, DB profiles, scenarios, bundles, export |
| `cold_start.py` | container start → first `200` on `/health` and on `/api/v1/meta`, N times; `--image A --image B` alternates two images (A, B, A, B, ...) with summaries per image, `--warmup K` starts each image K times first without summarizing them |
| `analyze.py` | prints a bundle (or an exported results directory) as tables |
| `precondition.py` | waits (polling, up to 3 h) until no other ATUS workload runs and the project's databases are idle, and writes the evidence |
| `report.py` | Markdown tables from timed bundles or exports: one table over several runs (`--combine`), per spec kind (`--kinds`), a sweep by level (`--saturation`), a split by endpoint or view kind (`--breakdown`), the ran-alone attestation (`--ran-alone`), the `statement_timeout` calibration (`--calibrate`), computed analyses by the API's lane class (`--lanes`) |
| `export_raw.py` | compact, committable copies of timed bundles for `results/raw/`: summaries, per-request CSV, `docker stats` CSV; no logs; secret-scanned |
| `ltcommon.py` | shared helpers: PostgreSQL log attribution, `docker stats` sampling, statistics, the "ran alone" check, secret scan |
| `results/` | committed, compact results (see [`results/README.md`](results/README.md)) |

Unit tests for the parts that turn raw output into numbers: `tests/unit/test_loadtests.py`.

## Requirements

- Docker Desktop (the stack runs in its VM), `docker compose` v2.
- k6 (`/opt/homebrew/bin/k6`, or set `ATUS_LT_K6`). run.py always passes `--no-usage-report`, so k6 sends nothing to Grafana. The scripts import nothing remote.
- Python 3.11 or newer on the host. The tools use the standard library only.
- The serving dump: a `pg_dump -Fc` file from `atus build-serving-db`, with its `.sha256` and `.manifest.json` next to it. Load tests always use the **real serving database**, never the fixture.

## Running

Raw output goes to `--out-root/<stage>/`. That is either a directory outside the repository or the ignored `loadtests/out/`, the default. run.py refuses any other place inside the repository.

```sh
export ATUS_LT_DUMP=/path/to/atus-serving-0325-<build>.dump

# 1. Build the image from this checkout, start the stack, restore the dump once,
#    apply a DB profile. Idempotent: later calls reuse the image and the volume.
python3 loadtests/run.py up --stage s1 --profile 0.25cu

# 2. A whole profile in one bundle: the scenarios (and optionally N cold starts).
python3 loadtests/run.py profile --stage s1 --profile 0.25cu \
    --scenarios metadata,cached,uncached,worstcase,mix --cold-start 5 \
    --set EXECUTOR=duration --set DURATION=120s --set VUS=2

# 3. One scenario against the running stack, here as a concurrency sweep.
python3 loadtests/run.py scenario --stage s1 uncached \
    --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8,16

# 4. The laptop baseline: same image, unconstrained, FULL database (5434).
python3 loadtests/run.py baseline --stage s1 --scenarios uncached,worstcase

# 5. Read, then copy the compact files into the repository.
python3 loadtests/analyze.py <out-root>/s1/<bundle>
python3 loadtests/run.py export <out-root>/s1/<bundle> --to loadtests/results/<name>

python3 loadtests/run.py down --stage s1            # --purge also deletes the database volume
```

`--dry-run` runs every chosen scenario at a tiny scale (one VU, a few iterations; see `DRY_RUN` in run.py) to prove that the scripts work against the real stack. A dry run is **not timed**: k6's stdout and the exported record carry no latency, and nothing from it may be reported as a measurement.

### What run.py sets up

- **The stack** is `docker-compose.prod.yml` + `docker-compose.emulation.yml` + a generated override (`<stage>/lt-override.yml`), under compose project `atus-lt-<stage>`. The override:
  - publishes the API on `127.0.0.1:8701`; the database is on `127.0.0.1:5501`;
  - sets the API's CPU and RAM (default 0.1 CPU / 512 MB, no swap: Render Free) and the database's (the profile);
  - adds statement-duration logging to the database container's command;
  - disables the API image's Docker `HEALTHCHECK`, which starts a Python process inside the 0.1-CPU container every 30 s. Render ignores it and probes `/health` over HTTP, as run.py does.
- **Image.** `atus-api:lt-<commit>` is built from this checkout's `Dockerfile`, with a `-dirty` suffix when the files the image copies have uncommitted changes. `<commit>` is the last commit that changed those files (not HEAD), so committing tooling or documents mid-campaign keeps the image under test. On this Mac it is a native arm64 build; Render runs linux/amd64. The CPU quota emulates Render's CPU share, not its micro-architecture.
- **Restore, once per stage.** The dump's sha256 is checked against its `.sha256` file and its manifest. The `restore` service then runs `pg_restore --no-owner --no-acl` as the non-superuser `neondb_owner`, followed by `VACUUM (ANALYZE)` (`deploy/stack/restore_dump.sh`). The `roles` service applies `scripts/ops/roles.sql`.
  - The restore runs unconstrained and without statement logging, because `roles.sql` sets a password.
  - The volume `atus-lt-<stage>_pgdata` is reused across profiles. Switching profile recreates only the `db` container with the new limits, then a fresh API. When the requested profile (limits and statement logging, read back from `docker inspect`) is already applied, the `db` container is kept with its warm caches and only the API is recreated, for example to change `--api-env` between bundles.
- **DB profiles.** Neon's own ratio is 1 CU = 1 vCPU + 4 GB.

  | Profile | Database container | Stands for |
  |---|---|---|
  | `0.25cu` | 0.25 vCPU, 1 GiB (`1g`), no swap | Neon Free's autoscaling floor |
  | `2cu` | 2 vCPU, **7 GiB** (`7g`), no swap | Neon Free's ceiling. Neon's ratio gives 8 GB, but the Docker VM has 7.75 GiB in total, so 7 GiB is the cap. Postgres settings are the same at every profile: the stack's, from Neon's formula for a 0.25–2 CU compute (`shared_buffers=215MB` and so on) |
  | `laptop` | unconstrained | the Docker VM (10 vCPU / 7.75 GiB here) |

- **API settings that differ from production**, all visible in each bundle's `environment.json`:
  - `ATUS_API_DAILY_ANALYSIS_CAP=10000000`. The production 600/day backstop would end a capacity run after 600 analyses. The cap itself is tested functionally (`tests/unit/api/test_protection.py` and the integration suite).
  - Optional, per run: `--max-parallel-workers` (`ATUS_API_MAX_PARALLEL_WORKERS`), `--statement-timeout-ms` (to see latencies beyond the production 85 s; raise k6's `REQ_TIMEOUT` with it), and `--api-env ATUS_API_X=VALUE` for any other `ATUS_API_*` setting, for example the lane sizes `ATUS_API_MAX_INFLIGHT` / `ATUS_API_MAX_INFLIGHT_HEAVY` when calibrating the concurrency cap (ADR-012).
  - Everything else is production's: `ATUS_ENV=production`, edge authentication, per-client buckets, lanes, statement timeout and `ATUS_EXPECTED_DATA_VERSION` (pinned to the dump's data version, as the release switch does).
- **Before each scenario** the API container is recreated (`--no-api-restart` keeps it). This empties its result cache, as a Render spin-down does. A `GET /api/v1/meta` then makes the process read its data snapshot, so the measured requests start from a warm snapshot. Cold start is measured separately.
- **Secrets.**
  - Random per stage, in `<stage>/stack.env` (mode 600, outside the repository).
  - They reach containers through `--env-file` and k6 through its process environment (`ATUS_LT_EDGE_SECRET`), never through a command line.
  - Container environments are redacted in `environment.json`, and every bundle and export is scanned for the secret values before it is reported.

### The laptop baseline

`run.py baseline` runs the same image, unconstrained by default (`--api-cpus`/`--api-memory` to change it), against the **full** database `atus-postgres` on `127.0.0.1:5434`. That database is never started, stopped or written:

- the API's connection string adds `default_transaction_read_only=on` to the API's own `BEGIN READ ONLY`;
- markers run in read-only sessions;
- statement logging is a per-session option of the API's connections (`log_min_duration_statement=0`, which needs the superuser the local development role is), never `ALTER SYSTEM`.

The full database keeps its own server settings (`jit=on`, `shared_buffers=128MB`). The API sets `jit=off` per transaction, as in production.

## What each scenario measures

| Scenario | Measures | Expected answers | Default client-address rotation |
|---|---|---|---|
| metadata | the API process alone: with the snapshot fresh, these routes run no SQL (ADR-012) | `200` | a new address every 100 requests (bucket: 120/min) |
| cached | validation, cache lookup and response for repeated analyses: hits run no SQL | `200`, `X-Cache: hit` | every 100 |
| uncached | one statistics query (plus a version recheck) per request, single-year with replicate SEs | `200`, or `422 insufficient_data` for a rare generated selection | every 5 analyses (bucket: 6/min) |
| worstcase | the requests that size `statement_timeout` (ADR-012 "Calibration": `clamp(3 × p99, 30 s, 85 s)`) | `200`; under concurrency also `503 overloaded` from the heavy lane, which is the protection working | every 5 |
| mix | a demo visit's requests (see below) | `200`, `422 insufficient_data` | one address per visit |

**Client addresses.** Each VU (each visit in the mix) uses addresses from the benchmarking range 198.18.0.0/15. Rotation keeps the per-client token buckets from dominating a capacity measurement: the buckets are tested functionally, and these runs measure compute. A `429` that still happens is recorded like any other answer.

**Defeating the cache.**
- `uncached` walks a deterministic list of distinct specs. When the list runs out, it starts again with a confidence level unique to each pass.
- `worstcase` gives every request a confidence level unique to its iteration (`SALT=on`).

The confidence level only scales the interval after the statistics queries (`analytics/variance.py`). So the database work is identical, but the cache key differs (audit F15). `atus_unexpected_cache` counts any `200` whose `X-Cache` was not the expected `miss`/`hit`.

**The mix** (`k6/mix.js`). Each visit, on average, makes:
- 3 analysis views: 1.5 from the static snapshot (starters and presets), 0.5 share-link revisits and 1.0 novel analyses, 30% of the novel ones multi-year;
- 0.5 wake calls.

These are the traffic model's numbers. Two parts are this harness's own assumptions, stated in the script:
- how a novel analysis splits into estimates, compares, trends and pooled ranges;
- that 40% of snapshot views are starters.

`MIX_MODE` decides which views reach the API:
- `api` (the default): every view, as on a stack without the edge tiers;
- `origin`: only the novel analyses and wake calls, which is what reaches Render behind the ADR-011 Worker.

The visit plan is a pure function of `(GEN_SEED, visit number)`, so a revisit re-requests exactly the spec an earlier visit computed. Its `X-Cache` (`hit`, `join` or `miss`) is whatever the API's LRU says.

**Common k6 settings** (environment variables, or `--set KEY=VALUE` through run.py):

| Setting | Meaning |
|---|---|
| `EXECUTOR` | `iterations` (`VUS`, `ITERATIONS`), `per-vu`, `duration` (`VUS`, `DURATION`), `arrival` (`RATE` per `TIME_UNIT`, `DURATION`, `PRE_VUS`, `MAX_VUS`), or `ramp` (`STAGES="30s:1,60s:2"`) |
| `BASE_URL` | default `http://127.0.0.1:8701` |
| edge secret | the environment variable named by `EDGE_SECRET_ENV` (default `ATUS_LT_EDGE_SECRET`); never `-e` |
| `REQ_TIMEOUT` | default `200s` |
| `GRACEFUL_STOP` | default `180s`, so running worst cases finish |
| `SPEC_DIR`, `SPEC_FILES` | the corpus (default: `docs/evidence/phase5/equivalence/specs/*.jsonl`) |
| `GEN_SEED` | generator seed |
| `OFFSET` | first index into a scenario's list |
| `THINK_S` | pause between requests or views |
| `ROTATE_EVERY` | requests per client address |
| `WARM_SET` | `starters`, `presets` or `prewarm` |
| `WORST_SET` | `core`, `all`, `corpus` or `core+corpus` |
| `SOURCE` | `both`, `corpus` or `generator` |
| `MIX_MODE`, `REVISIT_WINDOW`, `ORIGIN_KV_MISS` | the mix's settings, described above |

Running a script by hand (the API must accept the secret):

```sh
ATUS_LT_EDGE_SECRET="$(sed -n 's/^ATUS_STACK_EDGE_SECRET=//p' <out-root>/s1/stack.env)" \
  k6 run --no-usage-report -e EXECUTOR=duration -e VUS=2 -e DURATION=60s loadtests/k6/uncached.js
```

## Reading a bundle

One `profile`, `scenario` or `baseline` invocation writes one bundle, `<out-root>/<stage>/<UTC>-<profile>[-dry]/`.

**`environment.json`** describes the run:
- date, git commit and branch;
- the image: tag, local id, repo digests, revision label;
- the API's and the database's applied limits, read back from `docker inspect`;
- the database's settings and cluster size;
- the API's environment (redacted);
- the dump's sha256 and manifest;
- the host: model, CPU, memory, load averages before and after, Docker and k6 versions;
- `docker ps` before and after;
- **ran alone: yes/no**, checked before and after the run. "yes" requires no other load-generator process, no other `atus-lt-*` stack, no other container above 5% of a core in two `docker stats` samples 3 s apart, and the measurement precondition (`precondition`): no process matching the equivalence replay, pytest, Playwright, another k6 or wrangler; no `atus-*` container besides the four protected databases and the run's own; those databases without an active query but the run's own (`pg_stat_activity` by state, no query text). Idle containers of other projects and host apps are listed, not disqualifying;
- **background noise**: a `docker stats` sample of every other container once a minute during the bundle (`background_stats.jsonl.gz`, summarized in `background_noise`).

**Per scenario** (`<label>/`):

| File | Content |
|---|---|
| `requests.jsonl.gz` | one line per request: endpoint, view kind, spec id, phase (`setup` or `main`), start time, status, error code, `X-Cache`, `Server-Timing` db/app, latency (`dur`, k6's `http_req_duration`), bytes, `Retry-After`, request id, VU, iteration |
| `analysis.json` | from the `main` phase only, overall and per endpoint, endpoint and cache outcome, and view kind. Requests, status and error-code counts, cache outcomes; latency p50/p95/p99/max; throughput (requests and `200`s per second over the phase's wall time); `Server-Timing` db and app; the share of latency spent in the database |
| `db_time.json` | PostgreSQL's own account, the audit's method: the sum of the API sessions' logged parse, bind and execute durations between the `MARK <bundle>.<label> BEGIN/END` statements. Also the statement count, the backends, errors and statement timeouts, and `windows`, the same sum split into k6's `setup` and `main` phases by log time |
| `pg_log.txt.gz` | the database log between the markers |
| `api_log.txt.gz` | the API's JSON access log (client addresses truncated by the API) |
| `docker_stats.jsonl.gz` | about one sample per second per container, read from outside the containers: CPU (100% = one core), memory |
| `stats_summary.json` | p50/p95/max of those samples inside the k6 run |
| `cgroup.json` | cgroup v2 `cpu.stat` (CPU seconds, throttled periods) and `memory.peak` before and after, and PID 1's `VmHWM` (the API's peak RSS). Read with one `docker exec` outside the measured window |
| `api_metrics_before.txt`, `api_metrics_after.txt` | the API's Prometheus metrics: sheds, rate-limit rejections, timeouts, pool usage |
| `summary.json`, `k6.log` | k6's end-of-test summary (per-endpoint submetrics of `atus_req_duration`) and its stdout |

**`cold_start/cold_start.json`** (with `--cold-start N`), for each repetition:
- seconds from just before `docker run` to the first `200` from `/health` and from `/api/v1/meta`;
- the polls, the image and the DB profile;
- the container's memory peak, CPU time (`startup_cpu_s`) and throttling after start-up.

`cold_start.py` run on its own writes `<stage>/<UTC>-coldstart/`. That holds the same `cold_start.json`, with `by_image` summaries when it alternated images and warm-ups marked `"warmup": true`. Next to it go an `environment.json` (the ran-alone check before and after, the host, git) and `background_stats.jsonl.gz`. `export_raw.py` exports that directory like a bundle.

**The saturation point** comes from a sweep (`--sweep VUS=1,2,4,...`). `analyze.py` tabulates `200`s per second, p95 and non-200 answers per level. Saturation is the level after which throughput stops rising while p95 or errors climb. The API sheds before the database melts: its lanes allow 2 uncached analyses in flight, 1 of them heavy, and the next request waits up to 5 s and then gets `503 overloaded`. So report both the throughput plateau and the level where `overloaded` starts.

## Caveats

These hold for every result:

- **Not emulated** (audit F41):
  - Neon's remote pageserver reads, wake from suspend and the autoscaler itself; each profile is a fixed size;
  - the Render↔Neon round trip; the stack's network is a local bridge;
  - Render's real CPU share and its spin-up;
  - the cloud CPU's speed. Docker `--cpus` gives a share of an Apple M4 core.
- **Statement logging costs the database some CPU**, at 0.25 CU too. The audit measured the same way, so the numbers are comparable with [`docs/evidence/phase5/01-audit-timed.md`](../docs/evidence/phase5/01-audit-timed.md). `--no-pg-log` turns logging off, and with it the database-side attribution.
- **Clocks.** `db_time.json` windows compare host time (k6) with the Docker VM's time (PostgreSQL). They agree to within milliseconds, not exactly.
- **Background.** The host keeps running other projects' idle containers and desktop apps. The ran-alone verdict and `top_host_processes_pcpu` record what was there.
