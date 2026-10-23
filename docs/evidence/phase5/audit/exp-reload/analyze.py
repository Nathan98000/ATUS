"""Summarize one experiment dir: requests.csv + events.jsonl + locks.jsonl.
Times are reported relative to the loader's TRUNCATE ('Canonical tables truncated'
log line, i.e. all 10 ACCESS EXCLUSIVE locks held) = L0, and to the release of the
locks = C (the 'Ingestion run N succeeded' log line, which follows the data
commit by < 10 ms in these runs)."""
import collections
import csv
import json
import statistics
import sys
from pathlib import Path

d = Path(sys.argv[1])
ev = [json.loads(line) for line in open(d / "events.jsonl")]
rows = list(csv.DictReader(open(d / "requests.csv")))
for r in rows:
    r["ts"], r["te"], r["dur"] = float(r["t_start"]), float(r["t_end"]), float(r["dur_s"])


def evt(pred):
    for e in ev:
        if pred(e):
            return e["t"]
    return None


L0 = evt(lambda e: e.get("msg", "").startswith("Canonical tables truncated")) or \
    evt(lambda e: e["event"] == "loader_start")
LS = evt(lambda e: e["event"] == "loader_start")
C = evt(lambda e: e.get("msg", "").endswith("succeeded") and "Ingestion run" in e.get("msg", ""))
FAIL = evt(lambda e: e["event"] == "loader_done" and not e.get("ok"))
END = C or FAIL or evt(lambda e: e["event"] == "loader_done")
print(f"loader_start={LS - L0 if LS else None:+.3f}s  truncate_done(L0)=0  release(C)={(END - L0):+.3f}s  "
      f"loader_ok={C is not None}")
for e in ev:
    if e["event"] in ("inflight_start", "inflight_statement_done", "inflight_committed", "loader_done", "hold_start", "hold_end"):
        extra = {k: v for k, v in e.items() if k not in ("t", "event", "tb", "tables")}
        print(f"  event {e['event']:<24} t={e['t'] - L0:+8.3f}s {extra if extra else ''}")


def phase(r):
    if r["te"] < LS:
        return "1_before_load"
    if r["ts"] >= END + 0.05:
        return "4_started_after_release"
    if r["ts"] < LS and r["te"] >= LS:
        return "2_inflight_at_load_start"
    return "3_started_during_load"


def q(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))] if vals else float("nan")


groups = collections.defaultdict(list)
for r in rows:
    groups[(phase(r), r["kind"])].append(r)
print(f"\n{'phase':<26}{'kind':<18}{'n':>5}  {'p50':>7} {'p95':>7} {'max':>7}  outcomes(status/error/x-cache/value -> n)")
for (ph, kind), rs in sorted(groups.items()):
    durs = [r["dur"] for r in rs]
    oc = collections.Counter(
        f"{r['status']}/{r['error_code'] or '-'}/{r['x_cache'] or '-'}/{r['value'] or '-'}"
        + (f"/run{r['run_id']}" if r["run_id"] else "") + (f"/EXC:{r['exc'][:40]}" if r["exc"] else "")
        for r in rs)
    print(f"{ph:<26}{kind:<18}{len(rs):>5}  {q(durs, .5):7.3f} {q(durs, .95):7.3f} {max(durs):7.3f}  "
          + "; ".join(f"{k} x{v}" for k, v in oc.most_common()))

# requests that were blocked across the release: started before END, ended after END
print("\nRequests started during the load window and completed after release:")
spanning = [r for r in rows if LS <= r["ts"] < END and r["te"] >= END and r["kind"] != "health_probe"]
oc = collections.Counter((r["kind"], r["status"], r["error_code"] or "-", r["x_cache"] or "-", r["value"] or "-",
                          r["run_id"] or "-") for r in spanning)
for k, v in oc.most_common():
    print(f"  {k} x{v}")
if spanning:
    print(f"  wait: min {min(r['dur'] for r in spanning):.2f}s max {max(r['dur'] for r in spanning):.2f}s; "
          f"completed {min(r['te'] for r in spanning) - END:+.3f}..{max(r['te'] for r in spanning) - END:+.3f}s after release")

# 503 timing: first 503 relative to L0, duration distribution
e503 = [r for r in rows if r["status"] == "503"]
if e503:
    print(f"\n503s: n={len(e503)} codes={collections.Counter(r['error_code'] for r in e503)} "
          f"first at start{min(r['ts'] for r in e503) - L0:+.2f}s/end{min(r['te'] for r in e503) - L0:+.2f}s; "
          f"dur p50 {q([r['dur'] for r in e503], .5):.2f}s min {min(r['dur'] for r in e503):.2f}s max {max(r['dur'] for r in e503):.2f}s")
    by = collections.Counter(r["kind"] for r in e503)
    print("  by kind:", dict(by))

# health probe latency during load window
hp = [r for r in rows if r["kind"] == "health_probe" and LS <= r["ts"] < END]
if hp:
    print(f"\n/health during load window: n={len(hp)} status={collections.Counter(r['status'] for r in hp)} "
          f"p50 {q([r['dur'] for r in hp], .5) * 1000:.1f}ms p99 {q([r['dur'] for r in hp], .99) * 1000:.1f}ms max {max(r['dur'] for r in hp) * 1000:.1f}ms")

# throughput timeline in 5 s buckets
print("\ncompletions per 5 s bucket (relative to L0): bucket: ok200 / 503 / other  [non-health]")
b = collections.defaultdict(lambda: [0, 0, 0])
for r in rows:
    if r["kind"] == "health_probe":
        continue
    k = int((r["te"] - L0) // 5) * 5
    b[k][0 if r["status"] == "200" else 1 if r["status"] == "503" else 2] += 1
print("  " + "  ".join(f"{k:+d}:{v[0]}/{v[1]}/{v[2]}" for k, v in sorted(b.items())))

# estimate value consistency vs data version
print("\nestimate values by (phase, x_cache, value):")
oc = collections.Counter((phase(r), r["kind"], r["x_cache"], r["value"]) for r in rows
                         if r["kind"].startswith("est") and r["status"] == "200")
for k, v in sorted(oc.items()):
    print(f"  {k} x{v}")

# lock sampler summary
lk = d / "locks.jsonl"
if lk.exists():
    samples = [json.loads(line) for line in open(lk)]
    maxwait, maxapi = 0, 0
    waits = collections.Counter()
    for s in samples:
        api = [b for b in s["backends"] if b["app"] in ("", None)]
        w = [b for b in api if b["wtype"] == "Lock"]
        maxwait = max(maxwait, len(w)); maxapi = max(maxapi, len(api))
        for b in w:
            waits[tuple(b["waiting_for"] or [])] += 1
    print(f"\nlock sampler: {len(samples)} samples; max API backends {maxapi}; max API backends waiting on a Lock {maxwait}")
    print("  API lock waits by relation (sample-count):", dict(waits.most_common(8)))
    for s in samples:
        ld = [b for b in s["backends"] if b["app"] == "atus_loader"]
        if ld and ld[0]["ae_held"]:
            print(f"  loader AE-held sample at {s['t'] - L0:+.2f}s: {ld[0]['ae_held']} waiting_for={ld[0]['waiting_for']}")
            break
