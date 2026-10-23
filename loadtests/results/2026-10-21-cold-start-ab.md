# Cold start A/B: before and after the precompiled standard library (2026-10-21)

Brief §13, cold start. A follow-up to section 4 of the [emulation record](2026-10-21-emulation.md#4-b-cold-start-n5-per-profile). That section measured the API container's cold start at 0.1 CPU at p50 13.1 s to the first `/health`, against 7.65 s in the audit. Commit 5844ab7 then changed two things: the image precompiles the Python standard library (`python -m compileall` in the Dockerfile), and the API no longer imports python-dotenv at start-up. This run starts an image built before that commit and one built after it, alternately, under the same conditions.

Everything ran locally on 2026-10-21 between 10:21:52Z and 10:25:28Z. Nothing ran on Render or Neon.

## Result

n=8 measured starts per image, after one warm-up start each that is left out of the summary. API at `--cpus=0.1 --memory=512m`, database at the 0.25 CU profile. All values are seconds.

| Image | to first `/health` 200: p50 / min / max | to first `/api/v1/meta` 200: p50 / min / max | start-up CPU: p50 / min / max |
|---|---|---|---|
| A, before: `atus-pb-api:base` (00f3831) | **12.69** / 12.31 / 13.03 | **13.06** / 12.71 / 13.49 | 1.298 / 1.267 / 1.329 |
| B, after: `atus-api:premerge` (e5ca1fd) | **5.76** / 5.53 / 6.51 | **6.12** / 5.63 / 6.84 | 0.609 / 0.567 / 0.673 |

- The two ranges do not overlap. B's slowest start reached `/health` 5.80 s before A's fastest.
- B's p50 to `/health` is 6.93 s lower than A's: 45% of A's.
- B used 0.57–0.67 CPU-seconds to start, against 1.27–1.33 for A.

## Images

| | A (before) | B (after) |
|---|---|---|
| tag | `atus-pb-api:base` | `atus-api:premerge` |
| image ID | `sha256:0036a61f7c11633825a35d275734ea7ea78ae9da6a0f9145fd9b974718f7ba10` | `sha256:32ac0d0f1f7d6c455e77dd7213af7553a16d78d5741b81b91282a460bb64e1ab` |
| revision label | `00f3831` | `e5ca1fdb08c06a310d9010f1f0351a6977f3430c` (HEAD of `phase5` when this ran) |
| created | 2026-10-21T07:56:47Z | 2026-10-21T10:12:24Z |
| size (`docker image inspect`) | 61,079,423 B | 65,862,883 B |
| standard-library `.pyc` under `/usr/local/lib/python3.12` (site-packages excluded) | 0 (686 `.py`) | 686 (686 `.py`) |
| `atus_pipeline/config.py` imports python-dotenv | at module level (line 29) | only inside the `.env` loading branch |

- Both images are native linux/arm64 builds on python:3.12-slim with the same base digest (`sha256:a6e34c59…`).
- The image ID is the local one. Docker's containerd image store also lists it as the local repo digest. No registry digest exists for either image.
- The `.pyc` count and the dotenv import were read from inside each image (`docker run --rm --network none --entrypoint sh <image> -c 'find …'`) before the timed run.

**What the A/B does not isolate.** The two images differ by more than commit 5844ab7. Between 00f3831 and e5ca1fd:
- five commits changed the `Dockerfile` or `src/`: f8c57e7, 2f46bd2, 1e6bb4d, 5844ab7 and 82e8daa. Together they touch `config.py` and six API modules;
- 970ef67 changed `README.md`, which the image copies as package metadata.

This run compares the two images. It does not show how much of the difference each change makes, the precompiled standard library and the dotenv change included.

## Method

**Stack.** `run.py up --stage timed --profile 0.25cu` reused the volume `atus-lt-timed_pgdata`, so no restore ran. That volume was restored at 03:54:59Z from the release dump (sha256 `24098d84…`, data version `0325:serving-20261020T224252Z-a7420a5c:de834e672474`).
- The database container ran at 0.25 vCPU / 1 GiB, no swap, with statement logging on, as in the emulation record.
- The stack's own API container ran idle throughout, as it did during section 4's cold starts. It used `atus-api:lt-0b2808edb6cf`, which `run.py` built from this checkout's image inputs.

**Cold starts.** `loadtests/cold_start.py`, with two options added for this run (`--image`, given twice, and `--warmup`), in the same commit as this file. Each start used the settings of section 4:
- `docker run` on the stack network with `--cpus=0.1 --memory=512m --memory-swap=512m`, a read-only root filesystem with `/tmp` as tmpfs, all capabilities dropped, `no-new-privileges` and no Docker HEALTHCHECK;
- the production environment (`cold.env`: `ATUS_ENV=production`, edge secret, pinned data version, `WEB_CONCURRENCY=1`, the local-stack TLS hatch), the same file for both images.

**Timing.**
- The clock starts just before `docker run`, with both images already in the Docker VM.
- `/health` was polled every 0.1 s until the first `200`, then `/api/v1/meta` every 0.05 s.
- Start-up CPU is the container's cgroup `cpu.stat` `usage_usec`, read with one `docker exec` after a second `/api/v1/meta`. It therefore also covers the polls and both `/meta` requests, as section 4's figure does.

**Order.**
- One warm-up start per image came first (A, then B), so that both images' files were in the Docker VM's page cache before the measured starts.
  - Without it, the first measured start of each image would also have read that image's own layers from disk.
  - The stack's idle API runs a third image, which shares only the 4 base-image layers with A and with B.
  - The warm-ups are in the per-run table and the raw file; the summaries leave them out.
- Then 8 rounds of A, B, so slow drift on the host falls on both images. A always started first within a round; the order was not counterbalanced.
- Each container was removed before the next start, with a 2 s pause between starts.

**Functional check first.** A dry run started each image once (`--dry-run`; timings not kept). Both answered `200` on `/api/v1/meta` with this environment. The 00f3831 image predates the TLS rule, and the extra setting did not stop it starting.

## Ran alone

| Check | Before | After |
|---|---|---|
| `precondition.py` (no replay, pytest, Playwright, k6 or wrangler process; no `atus-*` container but the four protected databases and `atus-lt-timed-*`; protected databases without an active query not ours) | ok at 10:21:40Z, first poll | ok at 10:25:38Z |
| `ran_alone_check` (run.py's: the precondition, plus no other load generator, no other `atus-lt-*` stack, no other container above 5% of a core in two `docker stats` samples 3 s apart) | yes at 10:21:52Z, no reasons, no blips | yes at 10:25:37Z, no reasons, no blips |
| protected databases (`atus-postgres`, `atus-wsb-drill`, `atus-wsb-build`, `atus-emu-pg`) | 0 client backends each | 0 client backends each |
| a `ps` check of the host, outside the tools, for `docker build`, `buildx` or `compose build`, pytest, Playwright, k6, wrangler or `replay.py` | none | none |
| host load average (1 / 5 / 15 min) | 1.55 / 3.05 / 3.37 | 2.01 / 2.55 / 3.07 |

**Running containers besides ours:** the four protected databases (never started, stopped or written; only `pg_stat_activity` was read), 9 `supabase_*_dogtrax-dev` services and `timeblock-postgres`.

**Background noise.** These other containers were sampled 4 times during the run (`background_stats.csv`). Their highest single-sample CPU readings:
- `supabase_pg_meta_dogtrax-dev`: 9.6% of a core;
- `supabase_kong_dogtrax-dev`: 3.54%;
- `timeblock-postgres`: 1.15%;
- every other container: under 0.3%.

The sum of their means was 4.15% of a core.

**Host.** The same as the emulation record's: Apple M4 (Mac16,12), 10 cores, macOS 27.0.1, and Docker Desktop 29.6.2 with a VM of 10 vCPU and 7.75 GiB.

**Worktree.** HEAD was e5ca1fd. The only uncommitted changes were the tooling this run used (`loadtests/cold_start.py` and its unit test), committed together with this file. No image input was dirty.

## Per run

In run order. Throttled and periods are cgroup `cpu.stat` counts after start-up. VmHWM is PID 1's peak RSS.

| # | image | warm-up | to first `/health` 200 (s) | to first `/api/v1/meta` 200 (s) | `/meta` − `/health` (s) | start-up CPU (s) | throttled / periods | VmHWM (kB) |
|---|---|---|---|---|---|---|---|---|
| 0 | A (before) | yes, not summarized | 11.911 | 12.423 | 0.512 | 1.231 | 122 / 124 | 67508 |
| 1 | B (after) | yes, not summarized | 6.376 | 6.590 | 0.214 | 0.652 | 64 / 65 | 65656 |
| 2 | A (before) | no | 12.899 | 13.486 | 0.587 | 1.329 | 130 / 134 | 67512 |
| 3 | B (after) | no | 5.672 | 5.785 | 0.113 | 0.582 | 58 / 58 | 65700 |
| 4 | A (before) | no | 12.954 | 13.208 | 0.254 | 1.319 | 132 / 132 | 67516 |
| 5 | B (after) | no | 6.484 | 6.744 | 0.260 | 0.673 | 66 / 67 | 65672 |
| 6 | A (before) | no | 12.337 | 12.730 | 0.393 | 1.276 | 127 / 127 | 67508 |
| 7 | B (after) | no | 5.525 | 5.626 | 0.101 | 0.567 | 56 / 57 | 65664 |
| 8 | A (before) | no | 13.028 | 13.229 | 0.201 | 1.325 | 132 / 132 | 67496 |
| 9 | B (after) | no | 5.773 | 6.104 | 0.331 | 0.608 | 58 / 61 | 65676 |
| 10 | A (before) | no | 12.313 | 12.708 | 0.395 | 1.267 | 125 / 127 | 67508 |
| 11 | B (after) | no | 5.966 | 6.280 | 0.314 | 0.626 | 62 / 63 | 65672 |
| 12 | A (before) | no | 12.510 | 12.855 | 0.345 | 1.292 | 125 / 129 | 67496 |
| 13 | B (after) | no | 5.641 | 6.066 | 0.425 | 0.600 | 59 / 61 | 65676 |
| 14 | A (before) | no | 12.391 | 13.057 | 0.666 | 1.283 | 126 / 130 | 67512 |
| 15 | B (after) | no | 5.749 | 6.129 | 0.380 | 0.610 | 60 / 61 | 65672 |
| 16 | A (before) | no | 12.865 | 13.053 | 0.188 | 1.305 | 130 / 130 | 67500 |
| 17 | B (after) | no | 6.512 | 6.842 | 0.330 | 0.670 | 65 / 68 | 65676 |

**In every start:**
- `/health` refused connections until it answered `200`.
- The first `/api/v1/meta` poll after that succeeded, and so did the second `/meta`.
- `docker run` itself returned in 0.125–0.174 s.

## What the numbers show

- **Time to `/health`.** B took 5.53–6.51 s (p50 5.76) and A took 12.31–13.03 s (p50 12.69), in alternating starts under the same conditions.
- **Start-up is still CPU-bound in both images.** The container was throttled in nearly every 100 ms scheduler period: 125–132 of 127–134 periods for A and 56–66 of 57–68 for B. B's start-up used about 47% of A's CPU time (p50 0.609 against 1.298 CPU-seconds), and its time to `/health` was about 45% of A's.
- **`/api/v1/meta`** came 0.10–0.43 s after `/health` for B and 0.19–0.67 s after it for A.
- **Peak RSS** (VmHWM) was 65,664–65,700 kB for B and 67,496–67,516 kB for A.
- **Section 4's figures were for a different image.** That section measured 13.12 s at p50 for the image at 9afd1b4, which also has no standard-library `.pyc` (checked the same way: 0 of 686). Image A here took 12.69 s. The images and sessions differ, and this run does not compare them further.

## What this does not show

- **Which change is responsible.** How much of the difference comes from the precompiled standard library, the dotenv change or the other commits listed under "Images".
- **Render.** Its spin-up, its real CPU share and its CPU. These are arm64 builds on an Apple M4. Render runs the linux/amd64 image from CI, and that image was not measured here.
- **2 CU.** Only the 0.25 CU profile ran. `/health` uses no database.
- **Variance across sessions and orders.** This is one session, 8 starts per image, and one order (A before B in each round).

## Files

- [`raw/20261021T102143Z-coldstart/`](raw/20261021T102143Z-coldstart/), exported with `loadtests/export_raw.py`:
  - `cold_start.json`: every start, the warm-ups included, with timings, polls, cgroup counters, PID 1's memory and the first log lines; both images' descriptions; summaries per image (`by_image`);
  - `environment.json`: the ran-alone checks before and after, the host and git;
  - `background_stats.csv`: the other containers, once a minute.
- [`raw/precondition/precondition-coldstart-ab-before.json`](raw/precondition/precondition-coldstart-ab-before.json) and [`-after.json`](raw/precondition/precondition-coldstart-ab-after.json).
- The raw directory stays outside the repository. It was scanned for the stage's secret values, and nothing was found.

## Commands, in order

From the repository root, with `ATUS_LT_OUT_ROOT` (outside the repository) and `ATUS_LT_DUMP` exported as in the emulation record's section 12:

```text
2026-10-21T10:20:11Z  python3 loadtests/precondition.py --out <scratch>/precondition-0.json      # ok
2026-10-21T10:20:18Z  python3 loadtests/run.py up --stage timed --profile 0.25cu                 # volume reused, no restore
2026-10-21T10:20:39Z  python3 loadtests/cold_start.py --stage timed --runs 1 --image atus-pb-api:base --image atus-api:premerge --dry-run
2026-10-21T10:21:40Z  python3 loadtests/precondition.py --out <scratch>/precondition-1.json --own atus-lt-timed-api-1 --own atus-lt-timed-db-1 --own atus-lt-timed-cold
2026-10-21T10:21:43Z  python3 loadtests/cold_start.py --stage timed --runs 8 --warmup 1 --image atus-pb-api:base --image atus-api:premerge
2026-10-21T10:25:38Z  python3 loadtests/precondition.py --out <scratch>/precondition-2.json --own ...   # ok
afterwards            python3 loadtests/run.py down --stage timed                                 # volume kept
                      python3 loadtests/export_raw.py <out-root>/timed/20261021T102143Z-coldstart --to loadtests/results/raw
```

**State left behind.**
- No `atus-lt-*` container is running.
- The volume `atus-lt-timed_pgdata` remains.
- The image `atus-api:lt-0b2808edb6cf`, built for the stack's idle API, remains.
- The four protected databases were not started, stopped or written.
