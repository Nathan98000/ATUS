#!/usr/bin/env python3
"""Markdown tables from timed load-test bundles (or exported result directories).

  python3 loadtests/report.py <bundle-or-results-dir> [...]           all tables
  python3 loadtests/report.py --kinds <dir> --label worstcase           per spec kind
  python3 loadtests/report.py --ran-alone <dir> [...]                   attestation rows
  python3 loadtests/report.py --combine "0.25 CU=<dir>#uncached#estimate|miss" ...
                                                         one table over several runs
  python3 loadtests/report.py --lanes "0.25 CU mix=<dir>#mix" ...
                                       computed analyses by the API's lane class

Every number printed comes from the bundle's own files: analysis.json
(k6 per-request records, main phase), db_time.json (PostgreSQL's statement
log between the markers), cgroup.json (cgroup v2 counters and PID 1's VmHWM)
and stats_summary.json (docker stats). --kinds and --saturation read the
per-request rows: requests.jsonl.gz in a raw bundle, requests.csv.gz in an
export (export_raw.py). Latencies are client side (k6 http_req_duration).
Dry runs are refused: they carry no timing.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402


def load(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def sec(ms) -> str:
    return "-" if ms is None else f"{ms / 1000:.2f}"


def msf(ms) -> str:
    return "-" if ms is None else (f"{ms:.0f}" if ms >= 100 else f"{ms:.1f}")


def counts(group: dict) -> str:
    parts = [f"{k}×{v}" for k, v in sorted(group.get("status", {}).items())]
    codes = group.get("error_codes") or {}
    if codes:
        parts.append("(" + ", ".join(f"{k} {v}" for k, v in sorted(codes.items())) + ")")
    return " ".join(parts)


def kib_to_mib(text: str | None) -> str:
    match = re.match(r"(\d+) kB", text or "")
    return "-" if not match else f"{int(match.group(1)) / 1024:.1f}"


def load_records(sdir: Path) -> list[dict]:
    """Per-request records from a raw bundle (requests.jsonl.gz) or an export
    (requests.csv.gz, written by export_raw.py)."""
    if (sdir / "requests.jsonl.gz").exists():
        return lt.load_requests(sdir / "requests.jsonl.gz")
    records = []
    with gzip.open(sdir / "requests.csv.gz", "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            for key in ("st", "vu", "it", "bytes"):
                row[key] = int(row[key]) if row[key] not in ("", None) else None
            for key in ("dur", "wait", "db", "app"):
                row[key] = float(row[key]) if row[key] not in ("", None) else None
            records.append(row)
    return records


def throttled(delta: dict) -> str:
    """Share of CFS periods in which the cgroup was throttled."""
    periods, hit = delta.get("nr_periods"), delta.get("nr_throttled")
    return "-" if not periods else f"{hit / periods:.0%}"


def scenario_metrics(root: Path, label: str, group: str | None = None) -> dict:
    """One scenario's numbers. `group` ("estimate|miss") restricts the latency,
    status and throughput columns to one endpoint and X-Cache outcome."""
    sdir = root / label
    analysis = load(sdir / "analysis.json")
    if not analysis or not analysis.get("timed"):
        raise SystemExit(f"report.py: {sdir} is not a timed scenario")
    overall = analysis["overall"] if not group else analysis["by_endpoint_cache"][group]
    db_time = load(sdir / "db_time.json") or {}
    cgroup = load(sdir / "cgroup.json") or {}
    stats = load(sdir / "stats_summary.json") or {}
    api_delta, db_delta = cgroup.get("api_delta", {}), cgroup.get("db_delta", {})
    lat = overall.get("latency_ms", {})
    total_latency = (lat.get("mean") or 0) * (lat.get("n") or 0)
    window = overall.get("window_s") or 0
    api_name = next((n for n in stats if n.endswith("-api-1") or n.endswith("baseline-api")), None)
    db_name = next((n for n in stats if n != api_name), None)
    main_db = (db_time.get("windows") or {}).get("main", {})
    return {
        "label": label, "n": overall["requests"], "counts": counts(overall),
        "lat": lat, "lat200": overall.get("latency_200_ms", {}),
        "rps": overall.get("throughput_rps"), "rps200": overall.get("throughput_200_rps"),
        "window_s": window,
        "server_db_p50": overall.get("server_db_ms", {}).get("p50"),
        "server_app_p50": overall.get("server_app_ms", {}).get("p50"),
        "pg_db_ms_main": main_db.get("db_ms", db_time.get("db_ms")),
        "pg_statements_main": main_db.get("statements", db_time.get("statements")),
        "pg_complete": db_time.get("complete"),
        "pg_statement_timeouts": db_time.get("statement_timeouts"),
        "pg_share": (main_db.get("db_ms", 0) / total_latency) if total_latency else None,
        "api_vmhwm_mib": kib_to_mib((api_delta.get("proc1_after") or {}).get("VmHWM")),
        "api_cgroup_peak_mib": None if not api_delta.get("memory_peak_after")
        else round(api_delta["memory_peak_after"] / 2 ** 20, 1),
        "api_cpu_s": api_delta.get("cpu_usage_s"), "api_throttled": throttled(api_delta),
        "db_cpu_s": db_delta.get("cpu_usage_s"), "db_throttled": throttled(db_delta),
        "db_cpu_pct_p50": (stats.get(db_name) or {}).get("cpu_pct", {}).get("p50"),
        "db_cpu_pct_max": (stats.get(db_name) or {}).get("cpu_pct", {}).get("max"),
        "api_cpu_pct_p50": (stats.get(api_name) or {}).get("cpu_pct", {}).get("p50"),
        "api_cpu_pct_max": (stats.get(api_name) or {}).get("cpu_pct", {}).get("max"),
        "db_mem_mib_max": (stats.get(db_name) or {}).get("mem_mib", {}).get("max"),
        "api_mem_mib_max": (stats.get(api_name) or {}).get("mem_mib", {}).get("max"),
        "by_endpoint": analysis.get("by_endpoint", {}),
    }


def latency_table(rows: list[tuple[str, dict]], unit: str = "s") -> list[str]:
    fmt = sec if unit == "s" else msf
    out = [f"| Run | n | status (codes) | p50 | p95 | p99 | max ({unit}) | req/s | 200/s | "
           f"Server-Timing db p50 / app p50 (ms) | PG-logged DB time (s) / share of latency | "
           f"PG statements |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, m in rows:
        lat = m["lat"]
        share = "-" if m["pg_share"] is None else f"{m['pg_share']:.1%}"
        pg = "-" if m["pg_db_ms_main"] is None else f"{m['pg_db_ms_main'] / 1000:.1f}"
        out.append(f"| {name} | {m['n']} | {m['counts']} | {fmt(lat.get('p50'))} | "
                   f"{fmt(lat.get('p95'))} | {fmt(lat.get('p99'))} | {fmt(lat.get('max'))} | "
                   f"{m['rps']} | {m['rps200']} | {msf(m['server_db_p50'])} / "
                   f"{msf(m['server_app_p50'])} | {pg} / {share} | {m['pg_statements_main']} |")
    return out


def resource_table(rows: list[tuple[str, dict]]) -> list[str]:
    out = ["| Run | wall (s) | API VmHWM (MiB) | API cgroup peak (MiB) | API CPU s (periods "
           "throttled) | API CPU % p50 / max | DB CPU s (periods throttled) | DB CPU % p50 / max | "
           "DB mem max (MiB) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for name, m in rows:
        out.append(f"| {name} | {m['window_s']} | {m['api_vmhwm_mib']} | {m['api_cgroup_peak_mib']}"
                   f" | {m['api_cpu_s']} ({m['api_throttled']}) | {m['api_cpu_pct_p50']} / "
                   f"{m['api_cpu_pct_max']} | {m['db_cpu_s']} ({m['db_throttled']}) | "
                   f"{m['db_cpu_pct_p50']} / {m['db_cpu_pct_max']} | {m['db_mem_mib_max']} |")
    return out


def breakdown_table(root: Path, label: str, by: str, unit: str = "ms") -> list[str]:
    """One scenario split by endpoint, endpoint|X-Cache or view kind (analysis.json)."""
    analysis = load(root / label / "analysis.json")
    fmt = sec if unit == "s" else msf
    out = [f"| {by} | n | status (codes) | p50 | p95 | p99 | max ({unit}) | "
           "Server-Timing db p50 (ms) |",
           "|---|---|---|---|---|---|---|---|"]
    for name, group in analysis[f"by_{by}"].items():
        lat = group.get("latency_ms", {})
        out.append(f"| {name.replace('|', ' ')} | {group['requests']} | {counts(group)} | "
                   f"{fmt(lat.get('p50'))} | {fmt(lat.get('p95'))} | {fmt(lat.get('p99'))} | "
                   f"{fmt(lat.get('max'))} | {msf(group.get('server_db_ms', {}).get('p50'))} |")
    return out


def endpoint_table(m: dict, unit: str = "ms") -> list[str]:
    fmt = sec if unit == "s" else msf
    out = [f"| Endpoint | n | status (codes) | p50 | p95 | p99 | max ({unit}) | req/s |",
           "|---|---|---|---|---|---|---|---|"]
    for name, group in m["by_endpoint"].items():
        lat = group.get("latency_ms", {})
        out.append(f"| {name} | {group['requests']} | {counts(group)} | {fmt(lat.get('p50'))} | "
                   f"{fmt(lat.get('p95'))} | {fmt(lat.get('p99'))} | {fmt(lat.get('max'))} | "
                   f"{group.get('throughput_rps')} |")
    return out


def kinds_table(root: Path, label: str, field: str = "kind") -> list[str]:
    records = [r for r in load_records(root / label) if r.get("ph") == "main"]
    groups: dict[str, list] = defaultdict(list)
    for record in records:
        groups[record[field]].append(record)
    out = ["| Kind | n | status (codes) | min | p50 | p95 | p99 | max (s) | "
           "Server-Timing db p50 (s) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for name in sorted(groups):
        rows = groups[name]
        st = lt.summarize([r["dur"] for r in rows])
        db = lt.summarize([r["db"] for r in rows])
        statuses = defaultdict(int)
        codes = defaultdict(int)
        for r in rows:
            statuses[str(r["st"])] += 1
            if r["code"]:
                codes[r["code"]] += 1
        group = {"status": dict(statuses), "error_codes": dict(codes)}
        out.append(f"| {name} | {len(rows)} | {counts(group)} | {sec(st.get('min'))} | "
                   f"{sec(st.get('p50'))} | "
                   f"{sec(st.get('p95'))} | {sec(st.get('p99'))} | {sec(st.get('max'))} | "
                   f"{sec(db.get('p50'))} |")
    return out


def saturation_table(root: Path) -> list[str]:
    """A sweep bundle (labels like uncached-vus4): one row per level."""
    meta = load(root / "environment.json")
    out = ["| Concurrency (VUs) | n | 200 | 503 overloaded | 503 analysis_timeout | other | "
           "200/s | "
           "200 latency p50 / p95 / p99 / max (s) | time to 503 overloaded p50 / max (s) | "
           "DB CPU % p50 | API CPU % p50 / max |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for record in meta["scenarios"]:
        match = re.fullmatch(r".+-[a-z_]+?(\d+)", record["label"])
        if not match:
            continue
        m = scenario_metrics(root, record["label"])
        records = [r for r in load_records(root / record["label"]) if r.get("ph") == "main"]
        ok = [r["dur"] for r in records if r["st"] == 200]
        shed = [r["dur"] for r in records if r["code"] == "overloaded"]
        timeouts = sum(1 for r in records if r["code"] == "analysis_timeout")
        other = len(records) - len(ok) - len(shed) - timeouts
        a, b = lt.summarize(ok), lt.summarize(shed)
        out.append(f"| {match.group(1)} | {len(records)} | {len(ok)} | {len(shed)} | {timeouts} | "
                   f"{other} | {m['rps200']} | {sec(a.get('p50'))} / {sec(a.get('p95'))} / "
                   f"{sec(a.get('p99'))} / {sec(a.get('max'))} | {sec(b.get('p50'))} / "
                   f"{sec(b.get('max'))} | {m['db_cpu_pct_p50']} | {m['api_cpu_pct_p50']} / "
                   f"{m['api_cpu_pct_max']} |")
    return out


# The harness's specs whose year queries reach the API's heavy threshold, by id.
_HEAVY_SPEC = re.compile(r"^(W\d|genp-)")
_LANE_ORDER = ("light estimate", "light compare", "heavy estimate", "heavy trend",
               "heavy compare")


def lane_class(record: dict) -> str:
    """'<light|heavy> <endpoint>': the admission lane the API's rule
    (api/protection.py is_heavy: len(years), doubled for a compare, >= 8) gives
    this harness's request, read from its endpoint and spec id.

    The harness sends no spec between 2 and 7 year queries (k6/lib/specs.js):
    every trend covers the full period (23 years); W1-W6 cover 22 or 23 years
    (W6 is a compare); `genp-` pooled estimates cover 8, 12 or 22 years; every
    other spec (the corpus, `gen1-`, `genc-`, presets, starters) is one year:
    1 year query, or 2 for a compare."""
    spec, endpoint = record.get("spec") or "", record.get("ep") or ""
    heavy = endpoint == "trend" or bool(_HEAVY_SPEC.match(spec))
    return f"{'heavy' if heavy else 'light'} {endpoint}"


def lanes_table(rows: list[tuple[str, Path, str]]) -> list[str]:
    """Per run and lane class, the analyses that computed (main phase, 200,
    X-Cache miss): Server-Timing db (the database transaction, without the wait
    for a lane) and the client latency."""
    out = ["| Run | lane, endpoint | n | Server-Timing db min | p50 | p95 | max (s) | "
           "latency max (s) |",
           "|---|---|---|---|---|---|---|---|"]
    for name, root, label in rows:
        groups: dict[str, list] = defaultdict(list)
        for r in load_records(root / label):
            if (r.get("ph") == "main" and r.get("ep") in ("estimate", "trend", "compare")
                    and r["st"] == 200 and r.get("cache") == "miss"):
                groups[lane_class(r)].append(r)
        for cls in sorted(groups, key=lambda c: (_LANE_ORDER + (c,)).index(c)):
            db = lt.summarize([r["db"] for r in groups[cls]])
            dur = lt.summarize([r["dur"] for r in groups[cls]])
            out.append(f"| {name} | {cls} | {db['n']} | {sec(db.get('min'))} | "
                       f"{sec(db.get('p50'))} | {sec(db.get('p95'))} | {sec(db.get('max'))} | "
                       f"{sec(dur.get('max'))} |")
    return out


def _run_item(item: str) -> tuple[str, Path, str, str]:
    """[NAME=]PATH[#LABEL[#GROUP]] -> (name, root, label, group); the label
    defaults to the bundle's first scenario."""
    name, _, rest = item.rpartition("=") if "=" in item.split("#")[0] else ("", "", item)
    path, _, sel = rest.partition("#")
    label, _, group = sel.partition("#")
    root = Path(path).resolve()
    meta = load(root / "environment.json")
    label = label or meta["scenarios"][0]["label"]
    return name or f"{meta['profile']} {label}", root, label, group


def calibration(root: Path, label: str) -> dict:
    """clamp(3 x p99, 30 s, 85 s) over every main-phase request of a worst-case run."""
    m = scenario_metrics(root, label)
    p99 = m["lat"].get("p99")
    raw = 3 * p99 / 1000
    return {"p99_s": round(p99 / 1000, 2), "three_p99_s": round(raw, 1),
            "clamped_s": round(min(max(raw, 30.0), 85.0), 1)}


def ran_alone_rows(roots: list[Path]) -> list[str]:
    out = ["| Bundle | ran alone before / after | precondition before / after | load avg before → "
           "after (1 min) | protected DBs: active queries not ours | background CPU % (max of any "
           "container) |",
           "|---|---|---|---|---|---|"]
    for root in roots:
        meta = load(root / "environment.json")
        before, after = meta.get("ran_alone_before", {}), meta.get("ran_alone_after", {})
        pre_b = before.get("precondition", {})
        pre_a = after.get("precondition", {})
        busy = sum(len(v.get("active_not_ours", [])) for v in
                   (pre_b.get("protected_db_activity") or {}).values()) + sum(
            len(v.get("active_not_ours", [])) for v in
            (pre_a.get("protected_db_activity") or {}).values())
        bg = (meta.get("background_noise") or {}).get("cpu_pct_max") or {}
        top = max(bg.items(), key=lambda kv: kv[1]) if bg else ("-", "-")
        out.append(f"| `{meta['bundle']}` | {before.get('ran_alone')} / {after.get('ran_alone')} |"
                   f" {pre_b.get('ok')} / {pre_a.get('ok')} | "
                   f"{(before.get('load_average_1_5_15') or ['-'])[0]} → "
                   f"{(after.get('load_average_1_5_15') or ['-'])[0]} | {busy} | "
                   f"{top[1]} ({top[0]}) |")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--kinds", action="store_true")
    parser.add_argument("--field", default="kind")
    parser.add_argument("--label")
    parser.add_argument("--ran-alone", action="store_true")
    parser.add_argument("--unit", default="s", choices=("s", "ms"))
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--group", help='one endpoint|X-Cache group, e.g. "estimate|miss"')
    parser.add_argument("--combine", action="store_true",
                        help="one latency and one resource table over every argument, each "
                             "given as [NAME=]PATH[#LABEL[#GROUP]]")
    parser.add_argument("--saturation", action="store_true", help="a sweep bundle, by level")
    parser.add_argument("--breakdown", choices=("endpoint", "endpoint_cache", "kind"),
                        help="one PATH#LABEL split by endpoint, endpoint|X-Cache or view kind")
    parser.add_argument("--table", choices=("latency", "resource", "both"), default="both")
    parser.add_argument("--lanes", action="store_true",
                        help="computed analyses by the API's lane class, over every argument "
                             "given as [NAME=]PATH[#LABEL]")
    args = parser.parse_args(argv)
    if args.lanes:
        items = [_run_item(str(item)) for item in args.roots]
        print("\n".join(lanes_table([(name, root, label) for name, root, label, _ in items])))
        return 0
    if args.combine:
        rows = []
        for item in map(str, args.roots):
            name, root, label, group = _run_item(item)
            rows.append((name, scenario_metrics(root, label, group or None)))
        if args.table in ("latency", "both"):
            print("\n".join(latency_table(rows, args.unit)))
        if args.table == "both":
            print()
        if args.table in ("resource", "both"):
            print("\n".join(resource_table(rows)))
        return 0
    if args.breakdown:
        path, _, label = args.roots[0].partition("#")
        print("\n".join(breakdown_table(Path(path).resolve(), label, args.breakdown, args.unit)))
        return 0
    if args.saturation:
        for root in args.roots:
            print("\n".join(saturation_table(Path(root).resolve())))
        return 0
    if args.ran_alone:
        print("\n".join(ran_alone_rows([Path(r).resolve() for r in args.roots])))
        return 0
    for root in args.roots:
        root = Path(root).resolve()
        meta = load(root / "environment.json")
        if not meta or not meta.get("timed"):
            print(f"report.py: {root} is not a timed bundle", file=sys.stderr)
            return 1
        labels = [args.label] if args.label else [r["label"] for r in meta["scenarios"]]
        if args.kinds:
            for label in labels:
                print(f"\n{meta['bundle']} / {label}\n")
                print("\n".join(kinds_table(root, label, args.field)))
            continue
        if args.calibrate:
            for label in labels:
                print(label, json.dumps(calibration(root, label)))
            continue
        suffix = f" [{args.group.replace('|', ' ')}]" if args.group else ""
        rows = [(f"{meta['profile']} {label}{suffix}", scenario_metrics(root, label, args.group))
                for label in labels]
        print(f"\n### {meta['bundle']}\n")
        print("\n".join(latency_table(rows, args.unit)))
        print()
        print("\n".join(resource_table(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
