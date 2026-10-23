#!/usr/bin/env python3
"""Read a load-test bundle (or an exported results directory) as tables.

  python loadtests/analyze.py <bundle-or-results-dir> [--recompute]

For each scenario: requests, statuses, error codes and X-Cache outcomes per
endpoint, and, for timed runs only, latency p50/p95/p99/max (client side,
k6 `http_req_duration`), throughput, the API's Server-Timing db/app split and
the database time logged by PostgreSQL between the scenario's markers.
A dry-run bundle prints functional counts only: it was never timed.

Sweeps (labels like `uncached-vus4`) are also tabulated by their swept value:
throughput, p95 and the share of non-200 answers per level, which is where the
saturation point is read (the level after which throughput stops rising while
p95 or errors climb).

--recompute rebuilds analysis.json from requests.jsonl.gz (after a change to
the analysis code).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def fmt(value) -> str:
    return "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))


def scenario_lines(label: str, sdir: Path, timed: bool) -> list[str]:
    analysis = load(sdir / "analysis.json") or load(sdir / "functional.json")
    if analysis is None:
        return [f"{label}: no analysis"]
    lines = [f"\n## {label}"]
    header = f"{'endpoint|cache':<28} {'n':>6}  status / codes"
    if timed:
        header += f"{'':<6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'rps':>7} {'db p50':>8}"
    lines.append(header)
    groups = {"ALL": analysis["overall"], **analysis.get("by_endpoint_cache", {})}
    for name, group in groups.items():
        codes = ",".join(f"{k}:{v}" for k, v in sorted(group["status"].items()))
        if group.get("error_codes"):
            codes += " " + ",".join(f"{k}:{v}" for k, v in sorted(group["error_codes"].items()))
        line = f"{name:<28} {group['requests']:>6}  {codes}"
        if timed and group.get("latency_ms", {}).get("n"):
            lat = group["latency_ms"]
            line += (f"  {fmt(lat['p50']):>8} {fmt(lat['p95']):>8} {fmt(lat['p99']):>8} "
                     f"{fmt(lat['max']):>8} {fmt(group['throughput_rps']):>7} "
                     f"{fmt(group['server_db_ms'].get('p50')):>8}")
        lines.append(line)
    db_time = load(sdir / "db_time.json")
    if timed and db_time:
        share = None
        total = analysis["overall"].get("latency_ms", {})
        if total.get("n"):
            share = db_time["db_ms"] / (total["mean"] * total["n"]) if total["mean"] else None
        lines.append(f"postgres log: {db_time['db_ms']} ms DB time over {db_time['statements']} "
                     f"statements from {db_time['backends']} backends (complete="
                     f"{db_time['complete']}); DB time / sum of latencies = "
                     f"{fmt(None if share is None else round(share, 4))}; statement timeouts "
                     f"{db_time['statement_timeouts']}")
    stats = load(sdir / "stats_summary.json")
    if timed and stats:
        for name, item in stats.items():
            lines.append(f"docker stats {name}: cpu% p50 {item['cpu_pct'].get('p50')} p95 "
                         f"{item['cpu_pct'].get('p95')} max {item['cpu_pct'].get('max')}; "
                         f"mem MiB max {item['mem_mib'].get('max')}")
    return lines


def sweep_lines(meta: dict, root: Path) -> list[str]:
    rows: dict[str, list] = {}
    for record in meta.get("scenarios", []):
        match = re.fullmatch(r"(.+)-([a-z_]+?)(\d+(?:\.\d+)?)", record["label"])
        if not match:
            continue
        analysis = load(root / record["label"] / "analysis.json")
        if not analysis or not analysis.get("timed"):
            continue
        overall = analysis["overall"]
        non200 = overall["requests"] - overall["status"].get("200", 0)
        rows.setdefault(f"{match.group(1)} by {match.group(2)}", []).append(
            (float(match.group(3)), overall.get("throughput_200_rps"),
             overall.get("latency_ms", {}).get("p95"), non200, overall["requests"]))
    lines = []
    for name, items in rows.items():
        lines.append(f"\n## sweep: {name}")
        lines.append(f"{'level':>6} {'200/s':>8} {'p95 ms':>9} {'non-200':>8} {'n':>6}")
        for level, rps, p95, bad, n in sorted(items):
            lines.append(f"{level:>6g} {fmt(rps):>8} {fmt(p95):>9} {bad:>8} {n:>6}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--recompute", action="store_true")
    args = parser.parse_args(argv)
    root = args.bundle.resolve()
    meta = load(root / "environment.json")
    if meta is None:
        print(f"analyze.py: {root} has no environment.json", file=sys.stderr)
        return 1
    timed = bool(meta.get("timed"))
    print(f"# {meta.get('bundle')}  profile={meta.get('profile')}  timed={timed}  ran alone="
          f"{meta.get('ran_alone_before', {}).get('ran_alone')}/"
          f"{meta.get('ran_alone_after', {}).get('ran_alone')}")
    for record in meta.get("scenarios", []):
        sdir = root / record["label"]
        requests = sdir / "requests.jsonl.gz"
        if args.recompute and requests.exists():
            lt.write_json(sdir / "analysis.json",
                          lt.analyze_requests(lt.load_requests(requests), with_latency=timed))
        print("\n".join(scenario_lines(record["label"], sdir, timed)))
    print("\n".join(sweep_lines(meta, root)))
    if not timed:
        print("\n(dry run: functional counts only; this bundle carries no timing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
