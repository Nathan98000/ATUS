import csv, json, sys
from collections import Counter
BASE = "docs/evidence/phase5/audit/exp-reload/"
for exp, old, new in (("expA", "75.0", "90.0"), ("expC", "90.0", "75.0")):
    ev = [json.loads(l) for l in open(BASE + exp + "/events.jsonl")]
    def t_of(pred):
        return [e["t"] for e in ev if pred(e)][0]
    trunc = t_of(lambda e: e.get("msg", "").startswith("Canonical tables truncated"))
    loader_start = t_of(lambda e: e["event"] == "loader_start")
    hold_end = t_of(lambda e: e["event"] == "hold_end")
    last_copy = t_of(lambda e: e.get("msg", "").startswith("pandemic_replicate_weights:") and "rows loaded" in e.get("msg", ""))
    succeeded = t_of(lambda e: "succeeded" in e.get("msg", ""))
    done = t_of(lambda e: e["event"] == "loader_done")
    print(f"=== {exp}: trunc_log={trunc-loader_start:+.3f} hold_end={hold_end-trunc:+.3f} last_copy={last_copy-trunc:+.3f} "
          f"succeeded_log={succeeded-trunc:+.3f} loader_done={done-trunc:+.3f} (rel. to truncate log)")
    print(f"   data commit lies in ({last_copy-trunc:+.4f}, {succeeded-trunc:+.4f}); version bump commit before {succeeded-trunc:+.4f}")
    rows = list(csv.DictReader(open(BASE + exp + "/requests.csv")))
    for r in rows:
        r["ts"] = float(r["t_start"]); r["te"] = float(r["t_end"])
    # requests started in the window [loader_start, loader_done) and completed after data-commit lower bound
    win = [r for r in rows if loader_start <= r["ts"] < done and r["kind"] != "health_probe"]
    print("  started in load window (non-health):", len(win))
    after = [r for r in win if r["te"] >= last_copy]
    c = Counter()
    for r in after:
        c[(r["kind"], r["status"], r["error_code"], r["x_cache"], r["value"], r["run_id"])] += 1
    print("  started in window, completed after last COPY:")
    for k, n in c.most_common():
        sel = [r for r in after if (r["kind"], r["status"], r["error_code"], r["x_cache"], r["value"], r["run_id"]) == k]
        te = [r["te"] - done for r in sel]
        dur = [float(r["dur_s"]) for r in sel]
        print(f"    {k} x{n}  t_end-loader_done: {min(te):+.3f}..{max(te):+.3f}s  dur {min(dur):.2f}..{max(dur):.2f}s")
    # bypass responses detail
    byp = [r for r in rows if r["x_cache"] == "bypass"]
    print("  ALL bypass responses:", len(byp), Counter(r["value"] for r in byp),
          "t_end-loader_done:", sorted(round(r["te"] - done, 3) for r in byp),
          "t_end-last_copy:", sorted(round(r["te"] - last_copy, 3) for r in byp))
    # estimates (any kind est_*) with old value completing after last_copy
    stale = [r for r in rows if r["kind"].startswith("est") and r["value"] == old and r["te"] >= last_copy]
    print(f"  estimates with PRE-reload value {old} completing after last COPY: {len(stale)}",
          [(r['kind'], r['x_cache'], round(r['ts']-trunc,3), round(r['te']-done,3)) for r in stale][:10])
    stale2 = [r for r in rows if r["kind"].startswith("est") and r["value"] == old and r["te"] >= done]
    print(f"  estimates with PRE-reload value {old} completing after loader_done: {len(stale2)}")
    # meta responses completing after loader_done with old run id
    metas = [r for r in rows if r["kind"] == "meta" and r["te"] >= last_copy and r["status"] == "200"]
    print("  meta 200 completing after last COPY by run_id:", Counter(r["run_id"] for r in metas),
          [(r['run_id'], round(r['ts']-trunc,3), round(r['te']-done,3)) for r in metas if r['ts'] < done])
    # 503 est_uncached that completed after last copy: their durations and start times
    e503 = [r for r in after if r["status"] == "503"]
    print("  503s completing after last COPY:", len(e503), Counter(r['kind'] for r in e503),
          "dur:", sorted(float(r['dur_s']) for r in e503)[:3], "...", sorted(float(r['dur_s']) for r in e503)[-3:],
          "start rel trunc:", sorted(round(r['ts']-trunc,2) for r in e503)[:3], "..", sorted(round(r['ts']-trunc,2) for r in e503)[-3:])
    eu = [r for r in win if r["kind"] == "est_uncached"]
    print("  est_uncached started in window:", len(eu), Counter((r['status'], r['x_cache']) for r in eu))
