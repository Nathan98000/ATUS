# Phase 5 audit: serialized free-tier-fit measurements (timed agent)

Provenance:

- Produced by the audit's single serialized measurement agent ("timed") on 2026-10-20, 18:00–19:06 UTC, against commit 387fc2d.
- The agent could not write its own `REPORT.md`; the subagent harness blocks report files. What follows is its structured output, copied verbatim: the summary first, then one section per answer, each with its evidence line.
- The appendix copies the raw summary tables verbatim from the agent's log files.
- Redaction: no connection strings appear in the report text. The scripts in the raw directory use only the public local dev defaults (`atus:atus`).
- Raw directory: `docs/evidence/phase5/audit/timed/`. It is session scratch space and is not committed.

The operations document summarizes these results; see `docs/operations.md`, section "Free-tier fit measurements".

---

## Summary (verbatim)

I ran the 7.9 free-tier-fit and 7.3 saturation measurements on 2026-10-20 between 18:00 and 19:06 UTC, one at a time. This was the only ATUS audit workload running. Other projects' containers (10 supabase_* containers and timeblock-postgres) and host apps kept running; the host load average was 3.0 at the start and 4.7 at the end, and docker stats snapshots are in env_snapshot_start.txt and env_snapshot_end.txt. Machine: Apple M4, 10 cores, 24 GB, Docker Desktop 29.6.2 with a VM of 10 vCPU and 7.75 GiB.

**API image and limits.** I built a throwaway image from python:3.12-slim (digest a6e34c59...) with a non-editable install of a git archive of 387fc2d, run as `uvicorn --factory atus_pipeline.api.app:create_app` (one process). The API ran with `docker run --cpus=0.1 --memory=512m --memory-swap=512m`.

**DB profiles.** Profiles (a) and (b) ran the prototype serving DB (598 MB) in atus-emu-pg:
- (a) 0.25 CU: `--cpus 0.25 --memory 1g`.
- (b) 2 CU: `--cpus 2 --memory 8g`; the VM has only 7.75 GiB.
- Sizing basis: Neon says "1CU = 1vCPU, 4GB RAM" (neon.com/autoscaling-report), and its docs give about 4 GB RAM per CU with a 0.75 GB cache at 0.25 CU (neon.com/docs/manage/computes). shared_buffers stays at 128 MB, which matches Neon's formula (neon.com/docs/reference/compatibility).
- JIT: I ran sessions with jit=off because Neon's public compute Dockerfile configures Postgres without --with-llvm. I also measured a jit=on sensitivity run.
- (c) was the full DB on 5434, read-only sessions, with everything unconstrained.

**How DB time was measured.** I set `log_min_duration_statement=0` per API session through a connection option, so no ALTER SYSTEM was used anywhere. A separate session wrote MARK statements before and after each request, and DB time is the sum of the API backends' logged parse, bind and execute durations between the marks.

**Answers.**
- **RSS peak:** 70.4 MB for the process (cgroup peak 55 MB) after 300 distinct analyses, which filled the 256-entry LRU and evicted 44. That is more than 7x under 512 MB, at about 20 KB per cache entry.
- **Cold start:** p50 7.65 s to /health at 0.1 CPU, with /meta about 0.1 s later. This excludes Render's own spin-up.
- **Latency at 0.25 CU:** worst cases (pooled 22 years and 23-year trends) take 47 to 52 s at p50, and single-year estimates 3 to 7 s. At 2 CU the worst cases take 2.7 to 3.4 s. On the laptop with the full DB they take 13.7 to 15.6 s at p50 and up to 24 s.
- **Where the time goes:** the DB is more than 99.5% of the time. The 0.1-CPU API adds 7 to 17 ms per request, or about 0.1 to 0.2 s for trends.
- **JIT:** turning JIT on makes the 0.25 CU worst case 64 to 71 s.
- **Cold DB cache:** a cold first request after a DB restart adds 0 to 6 s locally. Neon's remote storage is not emulated.
- **Saturation at 0.25 CU:** DB CPU saturates first, already at 2 concurrent requests. At 16 concurrent, 6 of 16 requests got a 503 `database_unavailable` after exactly 10.0 s (pool acquire timeout). API CPU never limited anything. At 2 CU all 16 succeeded within 8 s.

**Optimization probes** (experiments only, no repo change). Both would need the §9 equivalence proof:
- Turning parallel query off halves the 0.25 CU worst case: 40 s down to 19 s.
- Restricting the activity CTE to the requested years speeds up a single-year estimate from 2.9–3.4 s to 0.5–0.9 s.
- In both, counts and point sums are identical; replicate sums differ at most 1.3e-14 relative.

**State left behind.**
- atus-emu-pg was recreated without limits on the same volume, port, image and env. `docker update` cannot remove CPU or memory limits, which is why it was recreated. It is running.
- All atus-audit-* containers are removed, and the 337 MB image is removed. The Dockerfile is kept at docs/evidence/phase5/audit/timed/image/Dockerfile.

**REPORT.md was not written.** The harness blocked writing report .md files for subagents, so the report content is in this structured output. All raw outputs, scripts and logs are in docs/evidence/phase5/audit/timed/.

---

## 7.9-rss (verbatim)

Setup: API at 0.1 CPU / 512 MB (no swap); prototype serving DB unconstrained, jit=off.

Memory at each point (process VmRSS = ps rss / VmHWM; cgroup memory.current):
- At the first /health 200: process 64.2 MB, cgroup 45.7 MB, anon 41.2 MB, 5 threads. docker stats showed 43.3 MiB of 512 MiB.
- After the pool opened plus 5 s idle: unchanged.
- After the metadata GETs: 65.2 MB.
- After 100 distinct uncached analyses: 68.2 MB (cgroup 49.0 MB).
- After 256 analyses, when the LRU is full: 70.3 MB (cgroup 51.7 MB).
- After 300 analyses (44 evictions): 70.4 MB. cgroup memory.peak reached 52.9 MB here and 55.5 MB in the final snapshot.

The workload was 120 single-year estimates, 60 23-year trends, 60 compares and 60 pooled estimates. Every one returned 200 with X-Cache: miss.

Under concurrency, the cgroup peaked at 49.0 MB (16-way burst at 0.25 CU) and 49.6 MB (16-way at 2 CU). After the latency matrices, VmHWM was 65.9 to 68.2 MB.

Verdict: about 70 MB peak against 512 MB, more than 7x margin. It fits.

Per-cache-entry size:
- Measured: (70256 − 65208) kB / 256 ≈ 19.7 KB per entry, which includes allocator growth.
- Deep sys.getsizeof of the cached payloads: 4.5 KB (1-year estimate), 5.8 KB (pooled 22-year), 8.3 KB (compare), 21.2 KB (23-year trend), plus a 105 B key and about 0.3 KB of LRU overhead.
- 256 trend entries come to about 5.5 MB.

Evidence: docs/evidence/phase5/audit/timed/rss.json, rss.log, entrysize.json, sat_a_neon025.json and sat_b_neon2cu.json (api_mem_max_bytes), matrix_*.json (api_mem_after). Code: src/atus_pipeline/config.py:29,63 (cache size 256); src/atus_pipeline/api/cache.py:54-81 (LRU); src/atus_pipeline/api/app.py:62-72 (pool and cache created in the lifespan).

## 7.9-coldstart (verbatim)

The API ran under docker run --cpus=0.1 --memory=512m with the image already cached in the VM.

With the DB unconstrained (n=5):
- docker run to the first 200 from /health: p50 7.65 s, p95 8.21 s, min 6.87 s, max 8.29 s.
- To the first 200 from /api/v1/meta: p50 7.74 s, p95 8.32 s, max 8.40 s. The first /meta poll always succeeded.

With the DB at 0.25 CU (n=5):
- To /health: p50 8.95 s, max 10.70 s.
- To /meta: p50 9.15 s, max 11.04 s.

Startup consumes 0.71 to 1.11 CPU-seconds, essentially import time; it was throttled for 6.2 to 9.7 s of that. The DB profile cannot affect /health, so the difference between the two batches is noise. Render's own spin-up (image pull and scheduling) is not included.

Evidence: docs/evidence/phase5/audit/timed/coldstart_dbUnconstrained.json, coldstart_dbNeon025.json and coldstart.py. Code: src/atus_pipeline/api/app.py:60-75 (non-blocking pool.open(wait=False)); src/atus_pipeline/api/routes/health.py:28 (async /health, no DB).

## 7.9-latency (verbatim)

Total wall time (p50 / p95). The DB accounted for more than 99.5% of every uncached request. API time = total − DB.

(a) Neon 0.25 CU (0.25 CPU / 1 GB, jit off), API 0.1 CPU:
- /meta: 0.17 / 0.24 s (DB 0.16 s).
- /activities: 0.01 / 0.05 s.
- Cached estimate: 0.008 / 0.010 s.
- Single-year estimate with SE (010101, 2025): 6.95 / 7.35 s; warm-up and early rounds 3.0 to 3.7 s.
- Compare by sex: 6.38 / 7.54 s.
- Pooled 22-year leisure (BLS): 51.7 / 52.4 s, max 52.5 s.
- Trend 23 years, tier-1 12: 46.9 / 51.3 s.
- Trend 23 years, 010101: 48.5 / 50.4 s.
- Pandemic 2019-2020: 2.88 / 3.11 s.
- API time: 7 to 17 ms for estimates and compares, 96 to 189 ms for trends.

(b) Neon 2 CU:
- /meta 0.03 s; single-year estimate 0.38 / 0.45 s; compare 0.44 / 0.48 s.
- Pooled 22-year 2.80 / 3.35 s.
- Trends 2.69 to 2.96 s at p50, 3.04 s at p95.
- Pandemic 0.57 s.
- API time 11 to 16 ms (trends about 100 ms).

(c) Laptop, full DB, nothing constrained (jit on):
- /meta 0.05 s; single-year estimate 1.88 / 2.53 s; compare 3.25 / 4.64 s.
- Pooled 13.7 / 22.3 s (max 23.8 s).
- Trend 12: 15.6 / 22.6 s.
- Trend 010101: 14.8 / 22.6 s.
- Pandemic 3.42 s.

(c2) Serving DB with nothing constrained performed about the same as (b): worst cases 2.5 to 2.8 s.

JIT sensitivity at (a), jit on:
- Pooled 64.5 s p50 (max 70.7 s); trend 12 64.3 s (max 70.8 s); single-year 4.2 s.

Statement counts from the server log:
- Uncached estimate, trend or pandemic request: 9. Compare: 10. /meta: 5. /activities: 3. Cache hit: 3.

Mid-run step in (a): latency stepped up between rounds 2 and 3, so read (a) as a band of about 33 to 52 s for worst cases. The DB container was throttled in 98% of CFS periods during the run.

Evidence: docs/evidence/phase5/audit/timed/matrix_a_neon025_jitoff.{json,csv,log,_pglog.txt.gz}, matrix_b_neon2cu_jitoff.*, matrix_c_laptop_fulldb.*, matrix_c2_laptop_servingdb_jitoff.*, matrix_a_neon025_jiton.*, matrix.py, lib.py (attribute_db_time), neon_docs_notes.txt. Neon sources: https://neon.com/autoscaling-report ('In Neon, 1CU = 1vCPU, 4GB RAM'), https://neon.com/docs/manage/computes, https://neon.com/docs/reference/compatibility, https://github.com/neondatabase/neon/blob/main/compute/compute-node.Dockerfile (no --with-llvm).

## 7.9-cold-db-cache (verbatim)

Profile (a), worst case (pooled 22-year), with the API restarted fresh at 0.1 CPU.

After docker restart of the DB plus a page-cache drop for its data files (mincore residency confirmed at 0.0%):
- First request: 32.3 s, 48.8 s and 47.1 s.
- Immediate warm repeat: 33.2 s, 45.0 s and 40.9 s.

After the restart only (shared_buffers empty, OS cache warm):
- First request: 40.4 s and 39.9 s.
- Warm repeat: 38.5 s and 38.9 s.

Locally, a cold cache adds 0 to 6 s, which is within the noise of a CPU-bound query. Not emulated: Neon remote-pageserver reads and wake-from-suspend.

Evidence: docs/evidence/phase5/audit/timed/coldcache_a_oscold.json, coldcache_a_pgonly.json, coldcache_*_pglog.txt.gz, coldcache.py, pgcache.py

## 7.3 (verbatim)

Setup: bursts of distinct uncached single-year estimates with SE, released together by a thread barrier. API 0.1 CPU; pool max_size=10 with a 10 s acquire timeout.

At 0.25 CU, by concurrency c (the DB's CPU limit is reached from c=2 onward):
- c=1: 2.3 to 6.1 s.
- c=2: 4.4 to 5.4 s.
- c=4: 7.9 to 13.2 s.
- c=8: 18.5 to 39.0 s, all 200.
- c=16: 10 requests returned 200 after 16.6 to 45.2 s, and 6 returned 503 'database_unavailable' at 10.04 to 10.13 s. The pool was exhausted, and the code maps PoolTimeout to an error saying the DB is 'currently unavailable', with no Retry-After.

API CPU used 0.03 to 0.51 CPU-seconds per burst and was throttled in at most 6 of 397 periods. API memory stayed at or below 49 MB. The threadpool (40 threads) was never limiting.

Order of saturation: DB CPU first, then pool acquire timeouts produce 503s. The API's CPU and memory never limited anything.

At 2 CU, c=16 succeeded with all 200s in 1.3 to 7.9 s, and DB CPU averaged 1.35 of 2 vCPU over the sampling window.

Units: 'backends' in the raw files includes parallel workers, because they inherit application_name.

Evidence: docs/evidence/phase5/audit/timed/sat_a_neon025.json, sat_a_neon025.log, sat_b_neon2cu.json, sat_b_neon2cu.log, sat.py. Code: src/atus_pipeline/api/app.py:62-68 (min_size=1, max_size=10); src/atus_pipeline/api/dependencies.py:24,35-43 (10 s timeout raises DatabaseUnavailableError); src/atus_pipeline/api/errors.py:141-149 (503 database_unavailable 'currently unavailable').

## serving-db (verbatim)

The prototype serving DB in atus-emu-pg was present and working, as described in exp-serving-proto (598 MB, data_version 0325:run1, 258,954 respondents). I did not modify its data; all sessions were read-only.

The container was recreated without limits at the end, on the same volume atus-emu-pgdata, port 5441, image postgres:16 and env. It is running with cpu.max=max and memory.max=max. postgresql.auto.conf is empty, because no ALTER SYSTEM was used. Its bridge IP changed to 172.17.0.3.

Evidence: docs/evidence/phase5/audit/timed/profile_a_apply.txt, profile_b_apply.txt, profile_restore.txt, atus-emu-pg_inspect_before_recreate.json; ../exp-serving-proto/final_state.txt

## other (verbatim)

REPORT.md could not be written: the subagent harness blocked report .md files. The tables and methodology are in this structured output instead.

The raw data, scripts and Dockerfile are in docs/evidence/phase5/audit/timed/ (image/Dockerfile is kept).

Background noise I could not remove:
- Containers supabase_*_dogtrax-dev (10 running) and timeblock-postgres, idle at under 2% CPU.
- Host apps, including an unrelated Python process at about 38% CPU, Brave and WindowServer.
- Host load average 2.98 at the start and 4.73 at the end.

Evidence: docs/evidence/phase5/audit/timed/env_snapshot_start.txt, env_snapshot_end.txt, image/Dockerfile, image/image_versions.txt

---

## Appendix: raw summary tables (verbatim copies of the agent's log files)

How to read these:

- The `matrix_*.log` summary lines leave out round 0, the warm-up, and round -1 where present.
- `wall_s` is client wall time. `db_ms` is the sum of the API backends' logged statement durations between MARK statements. `api_ms` is wall time minus DB time. `stmts` is the number of statements the API backends sent, counted from the server log.
- In profile (a), the trend 010101 case logged 8 statements in one sample and 9 in the others. The audit did not investigate why.

### Latency matrix (a) Neon 0.25 CU emulation, jit=off: matrix_a_neon025_jitoff.log (summary lines)

```text
meta                         n=10 st=['200'] cache=['None'] wall_s[p50=0.2 p95=0.2 max=0.3] db_ms[p50=161.0 p95=229.4 max=266.5] api_ms[p50=6.9 p95=8.4 max=8.6] stmts=[5]
activities                   n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.0 max=0.1] db_ms[p50=1.7 p95=2.0 max=2.0] api_ms[p50=10.8 p95=43.5 max=69.1] stmts=[3]
cached_estimate              n=10 st=['200'] cache=['hit'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=0.2 p95=0.3 max=0.3] api_ms[p50=7.8 p95=9.2 max=10.1] stmts=[3]
est_1y_010101_2025           n=7 st=['200'] cache=['miss'] wall_s[p50=7.0 p95=7.3 max=7.4] db_ms[p50=6948.9 p95=7335.7 max=7369.8] api_ms[p50=11.3 p95=12.2 max=12.3] stmts=[9]
cmp_1y_sex_010101_2025       n=7 st=['200'] cache=['miss'] wall_s[p50=6.4 p95=7.5 max=7.6] db_ms[p50=6293.7 p95=7523.3 max=7583.7] api_ms[p50=16.7 p95=78.6 max=103.0] stmts=[10]
pooled22_leisure_bls         n=5 st=['200'] cache=['miss'] wall_s[p50=51.7 p95=52.4 max=52.5] db_ms[p50=51670.6 p95=52346.3 max=52488.6] api_ms[p50=15.7 p95=20.6 max=20.7] stmts=[9]
trend23_tier1_12             n=5 st=['200'] cache=['miss'] wall_s[p50=46.9 p95=51.3 max=52.0] db_ms[p50=46688.2 p95=51100.5 max=51782.1] api_ms[p50=188.9 p95=195.8 max=195.9] stmts=[9]
trend23_6digit_010101        n=5 st=['200'] cache=['miss'] wall_s[p50=48.5 p95=50.4 max=50.8] db_ms[p50=48508.7 p95=50341.5 max=50700.7] api_ms[p50=95.8 p95=181.1 max=200.9] stmts=[8, 9]
pandemic_2019_2020_sleep     n=7 st=['200'] cache=['miss'] wall_s[p50=2.9 p95=3.1 max=3.1] db_ms[p50=2867.4 p95=3097.0 max=3105.2] api_ms[p50=12.4 p95=13.5 max=13.7] stmts=[9]
```

### Latency matrix (b) Neon 2 CU emulation, jit=off: matrix_b_neon2cu_jitoff.log (summary lines)

```text
meta                         n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.1 max=0.1] db_ms[p50=25.2 p95=74.7 max=112.5] api_ms[p50=7.1 p95=16.0 max=16.0] stmts=[5]
activities                   n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=1.6 p95=2.0 max=2.1] api_ms[p50=10.4 p95=12.5 max=13.5] stmts=[3]
cached_estimate              n=10 st=['200'] cache=['hit'] wall_s[p50=0.0 p95=0.1 max=0.1] db_ms[p50=0.3 p95=0.4 max=0.4] api_ms[p50=8.9 p95=64.8 max=109.6] stmts=[3]
est_1y_010101_2025           n=7 st=['200'] cache=['miss'] wall_s[p50=0.4 p95=0.5 max=0.5] db_ms[p50=373.4 p95=441.8 max=452.4] api_ms[p50=11.1 p95=11.6 max=11.7] stmts=[9]
cmp_1y_sex_010101_2025       n=7 st=['200'] cache=['miss'] wall_s[p50=0.4 p95=0.5 max=0.5] db_ms[p50=426.4 p95=463.2 max=464.9] api_ms[p50=12.4 p95=13.1 max=13.1] stmts=[10]
pooled22_leisure_bls         n=5 st=['200'] cache=['miss'] wall_s[p50=2.8 p95=3.4 max=3.4] db_ms[p50=2779.6 p95=3338.8 max=3428.3] api_ms[p50=15.7 p95=17.5 max=17.5] stmts=[9]
trend23_tier1_12             n=5 st=['200'] cache=['miss'] wall_s[p50=3.0 p95=3.0 max=3.1] db_ms[p50=2851.8 p95=2964.2 max=2989.4] api_ms[p50=99.6 p95=106.9 max=107.0] stmts=[9]
trend23_6digit_010101        n=5 st=['200'] cache=['miss'] wall_s[p50=2.7 p95=2.9 max=2.9] db_ms[p50=2578.0 p95=2800.0 max=2800.2] api_ms[p50=91.6 p95=111.4 max=114.3] stmts=[9]
pandemic_2019_2020_sleep     n=7 st=['200'] cache=['miss'] wall_s[p50=0.6 p95=0.6 max=0.6] db_ms[p50=558.3 p95=604.8 max=614.0] api_ms[p50=11.7 p95=13.5 max=14.1] stmts=[9]
```

### Latency matrix (c) laptop, full DB, unconstrained, jit=on: matrix_c_laptop_fulldb.log (summary lines)

```text
meta                         n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.1 max=0.1] db_ms[p50=42.2 p95=51.8 max=53.3] api_ms[p50=6.6 p95=8.3 max=8.4] stmts=[5]
activities                   n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=1.8 p95=2.4 max=2.6] api_ms[p50=9.8 p95=13.0 max=14.0] stmts=[3]
cached_estimate              n=10 st=['200'] cache=['hit'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=0.3 p95=0.7 max=0.8] api_ms[p50=7.2 p95=10.1 max=10.4] stmts=[3]
est_1y_010101_2025           n=7 st=['200'] cache=['miss'] wall_s[p50=1.9 p95=2.5 max=2.6] db_ms[p50=1865.1 p95=2516.9 max=2553.3] api_ms[p50=11.3 p95=13.4 max=14.2] stmts=[9]
cmp_1y_sex_010101_2025       n=7 st=['200'] cache=['miss'] wall_s[p50=3.3 p95=4.6 max=4.6] db_ms[p50=3238.4 p95=4625.4 max=4634.3] api_ms[p50=12.7 p95=14.5 max=14.9] stmts=[10]
pooled22_leisure_bls         n=5 st=['200'] cache=['miss'] wall_s[p50=13.7 p95=22.3 max=23.8] db_ms[p50=13659.2 p95=22249.0 max=23790.9] api_ms[p50=14.0 p95=16.7 max=16.9] stmts=[9]
trend23_tier1_12             n=5 st=['200'] cache=['miss'] wall_s[p50=15.6 p95=22.5 max=24.0] db_ms[p50=15538.3 p95=22527.3 max=23965.4] api_ms[p50=21.4 p95=23.2 max=23.2] stmts=[9]
trend23_6digit_010101        n=5 st=['200'] cache=['miss'] wall_s[p50=14.8 p95=22.6 max=24.1] db_ms[p50=14787.5 p95=22566.8 max=24100.8] api_ms[p50=25.9 p95=31.4 max=32.6] stmts=[9]
pandemic_2019_2020_sleep     n=7 st=['200'] cache=['miss'] wall_s[p50=3.4 p95=3.5 max=3.5] db_ms[p50=3403.1 p95=3513.4 max=3537.4] api_ms[p50=10.0 p95=13.2 max=13.8] stmts=[9]
```

### Latency matrix (c2) laptop, serving prototype, unconstrained, jit=off: matrix_c2_laptop_servingdb_jitoff.log (summary lines)

```text
meta                         n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=23.1 p95=24.6 max=24.7] api_ms[p50=6.6 p95=7.3 max=7.5] stmts=[5]
activities                   n=10 st=['200'] cache=['None'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=1.6 p95=1.8 max=1.8] api_ms[p50=8.8 p95=13.6 max=15.3] stmts=[3]
cached_estimate              n=10 st=['200'] cache=['hit'] wall_s[p50=0.0 p95=0.0 max=0.0] db_ms[p50=0.2 p95=0.4 max=0.4] api_ms[p50=7.9 p95=8.9 max=9.1] stmts=[3]
est_1y_010101_2025           n=7 st=['200'] cache=['miss'] wall_s[p50=0.3 p95=0.4 max=0.4] db_ms[p50=293.0 p95=392.4 max=412.4] api_ms[p50=10.5 p95=12.3 max=12.4] stmts=[9]
cmp_1y_sex_010101_2025       n=7 st=['200'] cache=['miss'] wall_s[p50=0.5 p95=0.7 max=0.7] db_ms[p50=451.4 p95=651.1 max=677.7] api_ms[p50=12.4 p95=15.1 max=15.2] stmts=[10]
pooled22_leisure_bls         n=5 st=['200'] cache=['miss'] wall_s[p50=2.7 p95=3.7 max=3.9] db_ms[p50=2642.7 p95=3685.9 max=3860.2] api_ms[p50=16.4 p95=18.6 max=18.8] stmts=[9]
trend23_tier1_12             n=5 st=['200'] cache=['miss'] wall_s[p50=2.8 p95=3.0 max=3.1] db_ms[p50=2756.2 p95=3020.4 max=3078.0] api_ms[p50=22.6 p95=26.2 max=26.5] stmts=[9]
trend23_6digit_010101        n=5 st=['200'] cache=['miss'] wall_s[p50=2.5 p95=2.7 max=2.7] db_ms[p50=2507.0 p95=2650.4 max=2665.7] api_ms[p50=22.7 p95=29.7 max=31.3] stmts=[9]
pandemic_2019_2020_sleep     n=7 st=['200'] cache=['miss'] wall_s[p50=0.5 p95=0.6 max=0.6] db_ms[p50=538.5 p95=554.3 max=554.8] api_ms[p50=10.2 p95=12.8 max=13.3] stmts=[9]
```

### JIT sensitivity (a) 0.25 CU, jit=on: matrix_a_neon025_jiton.log (summary lines)

```text
est_1y_010101_2025           n=3 st=['200'] cache=['miss'] wall_s[p50=4.2 p95=4.3 max=4.3] db_ms[p50=4072.4 p95=4247.9 max=4267.4] api_ms[p50=16.9 p95=83.9 max=91.3] stmts=[9]
pooled22_leisure_bls         n=3 st=['200'] cache=['miss'] wall_s[p50=64.5 p95=70.1 max=70.7] db_ms[p50=64474.2 p95=70069.1 max=70690.8] api_ms[p50=15.1 p95=106.5 max=116.7] stmts=[9]
trend23_tier1_12             n=3 st=['200'] cache=['miss'] wall_s[p50=64.3 p95=70.1 max=70.8] db_ms[p50=64279.2 p95=70028.4 max=70667.2] api_ms[p50=130.6 p95=220.7 max=230.7] stmts=[9]
```

### Saturation (a) 0.25 CU: sat_a_neon025.log

```text
c= 1 wall=  6.10s statuses={'200': 1} lat200={'n': 1, 'min': 6.103084500064142, 'p50': 6.103084500064142, 'p95': 6.103084500064142, 'max': 6.103084500064142} api_cpu=0.046619 api_thr_s=0.0 db_cpu=1.542384 maxActive=2 maxConns=2 errs=[]
c= 1 wall=  2.29s statuses={'200': 1} lat200={'n': 1, 'min': 2.2865290419431403, 'p50': 2.2865290419431403, 'p95': 2.2865290419431403, 'max': 2.2865290419431403} api_cpu=0.029492 api_thr_s=0.0 db_cpu=0.598627 maxActive=2 maxConns=2 errs=[]
c= 2 wall=  5.13s statuses={'200': 2} lat200={'n': 2, 'min': 5.122394167003222, 'p50': 5.125566437491216, 'p95': 5.1284214809304105, 'max': 5.12873870797921} api_cpu=0.054006 api_thr_s=0.016467 db_cpu=1.312704 maxActive=4 maxConns=4 errs=[]
c= 2 wall=  5.41s statuses={'200': 2} lat200={'n': 2, 'min': 4.414720666944049, 'p50': 4.91439312545117, 'p95': 5.364098338107579, 'max': 5.4140655839582905} api_cpu=0.058304 api_thr_s=0.0 db_cpu=1.357568 maxActive=4 maxConns=4 errs=[]
c= 4 wall= 13.21s statuses={'200': 4} lat200={'n': 4, 'min': 7.898092208080925, 'p50': 9.897798229474574, 'p95': 12.74370418488397, 'max': 13.210585540975444} api_cpu=0.138424 api_thr_s=0.090412 db_cpu=3.32168 maxActive=8 maxConns=8 errs=[]
c= 8 wall= 39.03s statuses={'200': 8} lat200={'n': 8, 'min': 18.509135916945525, 'p50': 25.35530860396102, 'p95': 38.845869295479496, 'max': 39.02451620798092} api_cpu=0.356394 api_thr_s=0.091839 db_cpu=9.777933 maxActive=15 maxConns=15 errs=[]
c=16 wall= 45.24s statuses={'200': 10, '503': 6} lat200={'n': 10, 'min': 16.625494749983773, 'p50': 38.67371062503662, 'p95': 45.143054220546034, 'max': 45.239545583026484} api_cpu=0.509146 api_thr_s=1.91645 db_cpu=11.277704 maxActive=17 maxConns=17 errs=['{"error":{"code":"database_unavailable","message":"The analytical database is currently unavailable. Try again shortly.","details":null}}']
```

### Saturation (b) 2 CU: sat_b_neon2cu.log

```text
c= 1 wall=  0.37s statuses={'200': 1} lat200={'n': 1, 'min': 0.3692407499765977, 'p50': 0.3692407499765977, 'p95': 0.3692407499765977, 'max': 0.3692407499765977} api_cpu=0.01833 api_thr_s=0.0 db_cpu=0.684074 maxActive=0 maxConns=1 errs=[]
c= 1 wall=  0.16s statuses={'200': 1} lat200={'n': 1, 'min': 0.15965225000400096, 'p50': 0.15965225000400096, 'p95': 0.15965225000400096, 'max': 0.15965225000400096} api_cpu=0.01915 api_thr_s=0.0 db_cpu=0.265661 maxActive=0 maxConns=1 errs=[]
c= 2 wall=  0.24s statuses={'200': 2} lat200={'n': 2, 'min': 0.21859716589096934, 'p50': 0.2275178119307384, 'p95': 0.23554639336653055, 'max': 0.23643845797050744} api_cpu=0.030107 api_thr_s=0.020395 db_cpu=0.561821 maxActive=0 maxConns=2 errs=[]
c= 2 wall=  0.38s statuses={'200': 2} lat200={'n': 2, 'min': 0.3254969170084223, 'p50': 0.3546097709913738, 'p95': 0.38081133957603014, 'max': 0.3837226249743253} api_cpu=0.027764 api_thr_s=0.0 db_cpu=0.762061 maxActive=0 maxConns=2 errs=[]
c= 4 wall=  0.82s statuses={'200': 4} lat200={'n': 4, 'min': 0.43258216592948884, 'p50': 0.6848586875130422, 'p95': 0.8051410652173216, 'max': 0.8173213340342045} api_cpu=0.057503 api_thr_s=0.318892 db_cpu=1.622952 maxActive=6 maxConns=7 errs=[]
c= 8 wall=  4.84s statuses={'200': 8} lat200={'n': 8, 'min': 1.5241989579517394, 'p50': 2.8973079579882324, 'p95': 4.557571448234375, 'max': 4.81319416698534} api_cpu=0.219837 api_thr_s=1.791015 db_cpu=7.89094 maxActive=13 maxConns=14 errs=[]
c=16 wall=  7.94s statuses={'200': 16} lat200={'n': 16, 'min': 1.288059249985963, 'p50': 6.472364146029577, 'p95': 7.824403750535566, 'max': 7.936545375036076} api_cpu=0.299454 api_thr_s=1.536173 db_cpu=15.045345 maxActive=17 maxConns=17 errs=[]
```

### API memory: rss.log

```text
S0_first_health mem.current 47951872 mem.peak 48730112 VmRSS 64188 kB VmHWM 64188 kB threads 5 | atus-audit-api-rss	0.16%	43.27MiB / 512MiB
S1_idle_5s_pool_open mem.current 47460352 mem.peak 49332224 VmRSS 64188 kB VmHWM 64188 kB threads 5 | atus-audit-api-rss	0.17%	43.27MiB / 512MiB
S2_after_metadata mem.current 48607232 mem.peak 49610752 VmRSS 65208 kB VmHWM 65208 kB threads 6 | atus-audit-api-rss	0.18%	44.17MiB / 512MiB
after_50_analyses mem.current 50188288 mem.peak 50970624 VmRSS 66544 kB VmHWM 66544 kB threads 6 | atus-audit-api-rss	0.23%	45.47MiB / 512MiB
after_100_analyses mem.current 51380224 mem.peak 52678656 VmRSS 68196 kB VmHWM 68196 kB threads 6 | atus-audit-api-rss	0.20%	47.1MiB / 512MiB
after_150_analyses mem.current 52334592 mem.peak 53379072 VmRSS 68880 kB VmHWM 68880 kB threads 6 | atus-audit-api-rss	0.21%	47.92MiB / 512MiB
after_200_analyses mem.current 53043200 mem.peak 54083584 VmRSS 69568 kB VmHWM 69568 kB threads 6 | atus-audit-api-rss	0.16%	48.45MiB / 512MiB
after_256_analyses mem.current 54255616 mem.peak 54792192 VmRSS 70256 kB VmHWM 70256 kB threads 6 | atus-audit-api-rss	0.30%	49.13MiB / 512MiB
after_300_analyses mem.current 54128640 mem.peak 55148544 VmRSS 70364 kB VmHWM 70364 kB threads 6 | atus-audit-api-rss	0.32%	49.22MiB / 512MiB
final_idle_5s mem.current 53362688 mem.peak 55451648 VmRSS 70364 kB VmHWM 70364 kB threads 6 | atus-audit-api-rss	0.36%	49.29MiB / 512MiB
analyses 300 wall summary {'n': 300, 'min': 0.06656395900063217, 'p50': 0.2853652505436912, 'p95': 1.6918005288986024, 'max': 2.9327227079775184}
bytes per response summary {'n': 300, 'min': 967, 'p50': 1108.5, 'p95': 8317.1, 'max': 8383}
```

### Cache entry size: entrysize.log

```text
estimate_1y_SE {'payload_deep_bytes': 4524, 'key_bytes': 105, 'json_bytes': 1040, 'approx_lru_overhead_bytes': 316}
estimate_pooled22_SE {'payload_deep_bytes': 5778, 'key_bytes': 105, 'json_bytes': 1506, 'approx_lru_overhead_bytes': 316}
compare_1y_SE {'payload_deep_bytes': 8343, 'key_bytes': 105, 'json_bytes': 2624, 'approx_lru_overhead_bytes': 316}
trend23_SE {'payload_deep_bytes': 21171, 'key_bytes': 105, 'json_bytes': 8528, 'approx_lru_overhead_bytes': 316}
```

### Cold start, DB unconstrained: coldstart_dbUnconstrained.log

```text
{"i": 0, "docker_run_cmd_s": 0.25755037495400757, "to_first_health_s": 7.6096917919348925, "to_first_meta_s": 7.727703458047472, "health_polls": 67, "meta_polls": 1, "second_meta_wall_s": 0.01212995802052319, "second_meta_status": 200, "memory_current": 47656960, "memory_peak": 48701440, "proc1": {"VmHWM": "64400 kB", "VmRSS": "64400 kB", "RssAnon": "41984 kB", "RssFile": "22416 kB", "Threads": "6"}}
{"i": 1, "docker_run_cmd_s": 0.21653612493537366, "to_first_health_s": 8.289047333993949, "to_first_meta_s": 8.399939666967839, "health_polls": 77, "meta_polls": 1, "second_meta_wall_s": 0.01389245898462832, "second_meta_status": 200, "memory_current": 47681536, "memory_peak": 48726016, "proc1": {"VmHWM": "64404 kB", "VmRSS": "64404 kB", "RssAnon": "41988 kB", "RssFile": "22416 kB", "Threads": "6"}}
{"i": 2, "docker_run_cmd_s": 0.1963594580302015, "to_first_health_s": 6.86840725003276, "to_first_meta_s": 6.900565874995664, "health_polls": 61, "meta_polls": 1, "second_meta_wall_s": 0.08877262508030981, "second_meta_status": 200, "memory_current": 47935488, "memory_peak": 48685056, "proc1": {"VmHWM": "64388 kB", "VmRSS": "64388 kB", "RssAnon": "41980 kB", "RssFile": "22408 kB", "Threads": "6"}}
{"i": 3, "docker_run_cmd_s": 0.22192404104862362, "to_first_health_s": 7.886107916012406, "to_first_meta_s": 7.988710333011113, "health_polls": 71, "meta_polls": 1, "second_meta_wall_s": 0.012049582903273404, "second_meta_status": 200, "memory_current": 47390720, "memory_peak": 48685056, "proc1": {"VmHWM": "64396 kB", "VmRSS": "64396 kB", "RssAnon": "41980 kB", "RssFile": "22416 kB", "Threads": "6"}}
{"i": 4, "docker_run_cmd_s": 0.177626125048846, "to_first_health_s": 7.645801999955438, "to_first_meta_s": 7.7424277500249445, "health_polls": 69, "meta_polls": 1, "second_meta_wall_s": 0.023164000012911856, "second_meta_status": 200, "memory_current": 47906816, "memory_peak": 48685056, "proc1": {"VmHWM": "64400 kB", "VmRSS": "64400 kB", "RssAnon": "41984 kB", "RssFile": "22416 kB", "Threads": "6"}}
{
 "summary_to_first_health_s": {
  "n": 5,
  "min": 6.86840725003276,
  "p50": 7.645801999955438,
  "p95": 8.20845945039764,
  "max": 8.289047333993949
 },
 "summary_to_first_meta_s": {
  "n": 5,
  "min": 6.900565874995664,
  "p50": 7.7424277500249445,
  "p95": 8.317693800176494,
  "max": 8.399939666967839
 },
 "summary_docker_run_cmd_s": {
  "n": 5,
  "min": 0.177626125048846,
  "p50": 0.21653612493537366,
  "p95": 0.2504251081729308,
  "max": 0.25755037495400757
 },
 "summary_second_meta_wall_s": {
  "n": 5,
  "min": 0.012049582903273404,
  "p50": 0.01389245898462832,
  "p95": 0.0756509000668302,
  "max": 0.08877262508030981
 }
}
```

### Cold start, DB at 0.25 CU: coldstart_dbNeon025.log

```text
{"i": 0, "docker_run_cmd_s": 0.12338504195213318, "to_first_health_s": 10.701177749899216, "to_first_meta_s": 11.039509583963081, "health_polls": 96, "meta_polls": 1, "second_meta_wall_s": 0.3023460829863325, "second_meta_status": 200, "memory_current": 47296512, "memory_peak": 48721920, "proc1": {"VmHWM": "64388 kB", "VmRSS": "64388 kB", "RssAnon": "41984 kB", "RssFile": "22404 kB", "Threads": "6"}}
{"i": 1, "docker_run_cmd_s": 0.16879862500354648, "to_first_health_s": 8.94895766605623, "to_first_meta_s": 8.990962708019651, "health_polls": 80, "meta_polls": 1, "second_meta_wall_s": 0.20522683300077915, "second_meta_status": 200, "memory_current": 47853568, "memory_peak": 48680960, "proc1": {"VmHWM": "64400 kB", "VmRSS": "64400 kB", "RssAnon": "41984 kB", "RssFile": "22416 kB", "Threads": "6"}}
{"i": 2, "docker_run_cmd_s": 0.12454762507695705, "to_first_health_s": 9.141242792014964, "to_first_meta_s": 9.153561375103891, "health_polls": 84, "meta_polls": 1, "second_meta_wall_s": 0.06754420802462846, "second_meta_status": 200, "memory_current": 47931392, "memory_peak": 48726016, "proc1": {"VmHWM": "64380 kB", "VmRSS": "64380 kB", "RssAnon": "41984 kB", "RssFile": "22396 kB", "Threads": "6"}}
{"i": 3, "docker_run_cmd_s": 0.13703066704329103, "to_first_health_s": 8.925118000013754, "to_first_meta_s": 9.238289500004612, "health_polls": 81, "meta_polls": 1, "second_meta_wall_s": 0.29699750000145286, "second_meta_status": 200, "memory_current": 47988736, "memory_peak": 48955392, "proc1": {"VmHWM": "64404 kB", "VmRSS": "64404 kB", "RssAnon": "41988 kB", "RssFile": "22416 kB", "Threads": "6"}}
{"i": 4, "docker_run_cmd_s": 0.12669254199136049, "to_first_health_s": 8.27353912498802, "to_first_meta_s": 8.467895334004425, "health_polls": 76, "meta_polls": 1, "second_meta_wall_s": 0.38963187497574836, "second_meta_status": 200, "memory_current": 47128576, "memory_peak": 48701440, "proc1": {"VmHWM": "64376 kB", "VmRSS": "64376 kB", "RssAnon": "41980 kB", "RssFile": "22396 kB", "Threads": "6"}}
{
 "summary_to_first_health_s": {
  "n": 5,
  "min": 8.27353912498802,
  "p50": 8.94895766605623,
  "p95": 10.389190758322366,
  "max": 10.701177749899216
 },
 "summary_to_first_meta_s": {
  "n": 5,
  "min": 8.467895334004425,
  "p50": 9.153561375103891,
  "p95": 10.679265567171386,
  "max": 11.039509583963081
 },
 "summary_docker_run_cmd_s": {
  "n": 5,
  "min": 0.12338504195213318,
  "p50": 0.12669254199136049,
  "p95": 0.16244503341149538,
  "max": 0.16879862500354648
 },
 "summary_second_meta_wall_s": {
  "n": 5,
  "min": 0.06754420802462846,
  "p50": 0.29699750000145286,
  "p95": 0.37217471657786516,
  "max": 0.38963187497574836
 }
}
```

### Cold DB cache, OS page cache dropped: coldcache_a_oscold.log

```text
rep0 cold: status=200 wall=32.34s cache=miss
rep0 warm: status=200 wall=33.22s cache=miss
rep0: cold wall=32.34s db=32324.225000000002ms | warm wall=33.22s db=33193.801ms | cache: drop=False files_bytes=650134084 resident_bytes=0 resident_MB=0.0 of 620.0 MB (0.0%)
rep1 cold: status=200 wall=48.84s cache=miss
rep1 warm: status=200 wall=44.99s cache=miss
rep1: cold wall=48.84s db=48824.822ms | warm wall=44.99s db=44950.94300000001ms | cache: drop=False files_bytes=650134084 resident_bytes=0 resident_MB=0.0 of 620.0 MB (0.0%)
rep2 cold: status=200 wall=47.08s cache=miss
rep2 warm: status=200 wall=40.91s cache=miss
rep2: cold wall=47.08s db=47069.66799999999ms | warm wall=40.91s db=40898.842ms | cache: drop=False files_bytes=650134084 resident_bytes=0 resident_MB=0.0 of 620.0 MB (0.0%)
```

### Cold DB cache, Postgres restart only: coldcache_a_pgonly.log

```text
rep0 cold: status=200 wall=40.43s cache=miss
rep0 warm: status=200 wall=38.48s cache=miss
rep0: cold wall=40.43s db=40408.115ms | warm wall=38.48s db=38435.147ms | cache: drop=False files_bytes=650134084 resident_bytes=406192128 resident_MB=387.4 of 620.0 MB (62.5%)
rep1 cold: status=200 wall=39.88s cache=miss
rep1 warm: status=200 wall=38.90s cache=miss
rep1: cold wall=39.88s db=39855.122ms | warm wall=38.90s db=38880.121999999996ms | cache: drop=False files_bytes=650134084 resident_bytes=406192128 resident_MB=387.4 of 620.0 MB (62.5%)
```

### Parallel-query probe at 0.25 CU: probe_parallel.txt

```text
parallel_default: 40.63s
parallel_off: 19.21s
parallel_default: 39.49s
parallel_off: 19.11s
ints+numeric point sums identical: True | max rel diff: 1.3361373103538273e-14
```

### Year-pushdown probe at 0.25 CU: explain_yearpushdown.txt

```text
as_shipped: 2.90s rows=1
year_pushdown: 0.50s rows=1
as_shipped: 3.39s rows=1
year_pushdown: 0.90s rows=1
identical result rows: False
year,n,n_part identical: True | numeric point sums identical: True | max relative diff over all 486 values: 7.382106185170559e-15
```

### DB profile application: profile_a_apply.txt, profile_b_apply.txt

```text
Tue Oct 20 18:09:42 UTC 2026
atus-emu-pg
NanoCpus=250000000 Memory=1073741824 Swap=1073741824
25000 100000
1073741824
929128448

Tue Oct 20 18:52:56 UTC 2026
atus-emu-pg
NanoCpus=2000000000 Memory=8589934592 Swap=8589934592
200000 100000
8589934592
```

### Neon documentation notes used for the profiles: neon_docs_notes.txt

```text
Neon official sources consulted 2026-10-20 (public pages, no account):
1. https://neon.com/docs/manage/computes
   - "Each CU allocates approximately 4 GB of RAM to the database instance, along with associated CPU and local SSD resources."
   - size table: 0.25 CU = 1 GB RAM, compute cache 0.75 GB, max_connections 104; 2 CU = 8 GB RAM, cache 6 GB, max_connections 839
   - "In Neon, up to 75% of your compute's RAM is used for data caching." (shared_buffers = 75% RAM only on fixed computes >= 18 CU)
   - "The Neon Free plan supports computes with up to 2 CU (8 GB of RAM)."
   - No vCPU column on this page.
2. https://neon.com/autoscaling-report  (Neon's own page)
   - "In Neon, 1CU = 1vCPU, 4GB RAM."   -> 0.25 CU = 0.25 vCPU / 1 GB; 2 CU = 2 vCPU / 8 GB
3. https://neon.com/docs/reference/compatibility
   - shared_buffers default 128MB; formula shared_buffers_mb = max(128, (1023 + backends*256)/1024) -> 128 MB at these sizes
   - effective_cache_size "set based on the compute cache size of your maximum Neon compute size"
   - max_worker_processes := 13 + floor(2 * max_compute_size); maintenance_work_mem from min compute size
   - jit and work_mem: not mentioned
4. https://neon.com/docs/introduction/plans
   - Free: "Up to 2 CU (8 GB RAM)"; "100 CU-hours/project/month"; scale to zero "5 min inactivity; cannot disable"; 1 GB storage per project
5. https://github.com/neondatabase/neon/blob/main/compute/compute-node.Dockerfile (public source, fetched 2026-10-20)
   - Postgres configure flags: --enable-debug --with-openssl --with-uuid=ossp --with-icu --with-libxml --with-libxslt --with-lz4 (+ --with-zstd);
     no --with-llvm  => Neon computes have no JIT provider (pg_jit_available() false). Third-party reports agree.
     The postgres:16 Docker image used for emulation HAS LLVM JIT (pg_jit_available() = true, jit=on).
Emulation choices derived from the above:
 - profile (a) 0.25 CU: docker update --cpus 0.25 --memory 1g --memory-swap 1g atus-emu-pg
 - profile (b) 2 CU:    docker update --cpus 2 --memory 8g --memory-swap 8g atus-emu-pg  (Docker VM has only 7.75 GiB, so RAM is effectively unconstrained)
 - shared_buffers left at 128MB (= Neon formula value); OS page cache inside the container memory cgroup stands in for Neon's compute cache (75% of RAM)
 - jit=off for (a)/(b) API sessions (connection option -c jit=off) to match Neon's no-LLVM build; jit=on sensitivity measured separately
 - NOT emulated: Neon's remote pageserver reads on compute-cache miss (local SSD here is faster), network RTT Render<->Neon, Neon wake-from-suspend latency
```

### Throwaway API image: image/Dockerfile

```text
# THROWAWAY prototype API image for the Phase 5 free-tier-fit audit (not a production Dockerfile).
# Build context: a `git archive 387fc2d pyproject.toml README.md LICENSE src` export (./src here).
FROM python:3.12-slim
# procps only so `ps` can report RSS inside the container during the audit.
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/ /app/
# Non-editable install of the repository package (runtime deps only, no [dev]).
RUN pip install --no-cache-dir /app && pip freeze > /pip-freeze.txt && rm -rf /app/src
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=0
EXPOSE 8000
# One uvicorn process, no reload, no extra workers (uvicorn default = 1 worker, in-process).
CMD ["uvicorn", "--factory", "atus_pipeline.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
```

### Throwaway API image runtime packages: image/image_versions.txt (first 26 lines)

```text
Python 3.12.15
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
atus-pipeline @ file:///app
certifi==2026.10.19
cffi==2.1.1
click==8.5.0
curl_cffi==0.16.3
fastapi==0.156.0
h11==0.16.0
idna==3.20
opentelemetry-api==1.45.1
psycopg==3.3.6
psycopg-binary==3.3.6
psycopg-pool==3.3.3
pycparser==3.11
pydantic==2.14.1
pydantic_core==2.50.1
python-dotenv==1.2.4
starlette==1.9.0
typing-inspection==0.4.4
typing_extensions==4.16.0
uvicorn==0.54.0
/usr/local/lib/python3.12/site-packages/atus_pipeline/__init__.py
10
```
