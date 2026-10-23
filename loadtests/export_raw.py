#!/usr/bin/env python3
"""Compact raw summaries of timed bundles, for committing (loadtests/results/raw/).

  python3 loadtests/export_raw.py <bundle> [...] --to loadtests/results/raw

For each bundle, into <to>/<bundle name>/:
  environment.json                  as written by run.py (redacted)
  <label>/summary.json              k6's end-of-test summary
  <label>/analysis.json, db_time.json, cgroup.json, stats_summary.json
  <label>/docker_stats.csv.gz       one row per docker stats sample (API and DB)
  <label>/requests.csv.gz           one row per request: phase, endpoint, kind,
                                    spec id, start (ms since the first request),
                                    status, error code, X-Cache, latency and
                                    Server-Timing db/app (ms), Retry-After, VU
  cold_start/cold_start.json        when the bundle ran cold starts
  background_stats.csv              other projects' containers, once a minute

A directory written by cold_start.py run on its own (cold_start.json,
environment.json and background_stats.jsonl.gz side by side) exports the same
way, with cold_start.json at the top.

No log is copied (database and API logs stay in the raw bundle), and every
file written is scanned for the stage's secret values; a hit deletes the
export.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402

COPIED = ("summary.json", "analysis.json", "db_time.json", "cgroup.json", "stats_summary.json")
REQUEST_FIELDS = ("ph", "ep", "kind", "spec", "t_ms", "st", "code", "cache", "dur", "wait",
                  "db", "app", "bytes", "ra", "vu", "it")


def stage_secrets(bundle: Path) -> list[str]:
    import run as runner
    return runner.Stage(bundle.parent.parent, bundle.parent.name).secrets()


def jsonl_to_csv(src: Path, dest: Path, fields: tuple[str, ...]) -> int:
    """A JSONL(.gz) stream as CSV; gzipped when `dest` ends in .gz."""
    rows = 0
    opener = gzip.open if dest.suffix == ".gz" else open
    with gzip.open(src, "rt") as handle, opener(dest, "wt", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for line in handle:
            if line.startswith("{"):
                writer.writerow(json.loads(line))
                rows += 1
    return rows


def export_bundle(bundle: Path, dest_root: Path) -> Path:
    meta = json.loads((bundle / "environment.json").read_text())
    standalone = bundle / "cold_start.json"  # cold_start.py run on its own
    if standalone.exists():
        meta["timed"] = json.loads(standalone.read_text()).get("timed")
    if not meta.get("timed"):
        raise lt.LoadTestError(f"{bundle.name} is a dry run: nothing to export as a measurement")
    dest = dest_root / bundle.name
    dest.mkdir(parents=True, exist_ok=False)
    shutil.copy2(bundle / "environment.json", dest / "environment.json")
    if standalone.exists():
        shutil.copy2(standalone, dest / "cold_start.json")
    for record in meta.get("scenarios", []):
        src, target = bundle / record["label"], dest / record["label"]
        target.mkdir()
        for name in COPIED:
            if (src / name).exists():
                shutil.copy2(src / name, target / name)
        if (src / "docker_stats.jsonl.gz").exists():
            jsonl_to_csv(src / "docker_stats.jsonl.gz", target / "docker_stats.csv.gz",
                         ("ts", "name", "cpu_pct", "mem_bytes", "mem_limit_bytes", "pids"))
        if (src / "requests.jsonl.gz").exists():
            records = lt.load_requests(src / "requests.jsonl.gz")
            first = min((r["ts"] for r in records), default=0)
            with gzip.open(target / "requests.csv.gz", "wt", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=REQUEST_FIELDS, extrasaction="ignore",
                                        lineterminator="\n")
                writer.writeheader()
                for r in records:
                    writer.writerow({**r, "t_ms": r["ts"] - first})
    if (bundle / "cold_start" / "cold_start.json").exists():
        (dest / "cold_start").mkdir()
        shutil.copy2(bundle / "cold_start" / "cold_start.json",
                     dest / "cold_start" / "cold_start.json")
    if (bundle / "background_stats.jsonl.gz").exists():
        jsonl_to_csv(bundle / "background_stats.jsonl.gz", dest / "background_stats.csv",
                     ("ts", "name", "cpu_pct", "mem_mib"))
    hits = lt.secret_scan(dest, stage_secrets(bundle))
    if hits:
        shutil.rmtree(dest)
        raise lt.LoadTestError(f"export refused: a secret value appears in {hits}")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundles", type=Path, nargs="+")
    parser.add_argument("--to", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        for bundle in args.bundles:
            dest = export_bundle(bundle.resolve(), args.to.resolve())
            print(f"exported {bundle.name} -> {dest}")
    except lt.LoadTestError as error:
        print(f"export_raw.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
