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
