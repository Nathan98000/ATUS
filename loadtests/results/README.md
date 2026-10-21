# Load-test results

Compact results copied from load-test bundles with `python3 loadtests/run.py export <bundle> --to loadtests/results/<name>`. Raw bundles (per-request records, database and API logs, `docker stats` streams) stay outside the repository; [`../README.md`](../README.md#reading-a-bundle) describes them.

Each exported run is a directory:

| File | Timed run | Dry run |
|---|---|---|
| `environment.json` | machine, image, limits, DB settings, ran-alone verdicts, scenario list | the same |
| `<scenario>/analysis.json` | per-endpoint counts, latency percentiles, throughput, Server-Timing | — |
| `<scenario>/db_time.json` | PostgreSQL-logged DB time between markers | — |
| `<scenario>/stats_summary.json`, `cgroup.json` | API and DB CPU and memory | — |
| `<scenario>/summary.json` | k6's end-of-test summary | — |
| `<scenario>/functional.json` | — | statuses, error codes and X-Cache outcomes only |
| `cold_start/cold_start.json` | per-repetition timings and their summary | whether every start reached `/api/v1/meta` |

`python3 loadtests/analyze.py loadtests/results/<name>` prints any of them as tables.

**Dry runs are not measurements.** They exist to prove that the scripts work against the real serving database. Their records carry no latency, and nothing in them may be quoted as performance.

## Index

| Directory | Kind | What it shows |
|---|---|---|
| [`2026-10-21-dryrun/`](2026-10-21-dryrun/) | dry run (not timed) | every scenario, the three DB profiles, the laptop baseline and the cold start run end to end against the real serving dump |
| [`2026-10-21-emulation.md`](2026-10-21-emulation.md) with [`raw/`](raw/) | **timed** | the restore drill, cold starts, every scenario at 0.25 CU and 2 CU, the laptop baseline, saturation sweeps, lane calibration, the `statement_timeout` calibration, the recommendations and the calibration applied in ADR-012 (brief §13, §10) |

`raw/` holds the timed bundles exported by `loadtests/export_raw.py` (compact: no logs, no secrets; per-request rows as `requests.csv.gz`, `docker stats` as `docker_stats.csv.gz`), the restore drill's report, and the precondition snapshots. `python3 loadtests/report.py` regenerates every table of the timed record from it.
