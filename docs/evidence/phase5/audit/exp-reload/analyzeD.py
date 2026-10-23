import csv, json, collections, sys
d = sys.argv[1]
ev = []
for l in open(f"{d}/events.jsonl"):
    try: ev.append(json.loads(l))
    except Exception: pass
T = {e["event"]: e["t"] for e in ev}
t2 = T["T2_committed_T3_blocked"]; kill = T["loader_backend_terminated"]; hs = T.get("hold_start") or T["rowlock_taken"]
rows = list(csv.DictReader(open(f"{d}/requests.csv")))
def ph(r):
    ts, te = float(r["t_start"]), float(r["t_end"])
    if te < hs: return "1_before_load"
    if ts < t2: return "2_during_hold_or_T2"
    if ts < kill: return "3_T2_committed_version_not_bumped"
    return "4_after_loader_died(run stays 'running')"
c = collections.Counter((ph(r), r["kind"], r["status"], r["x_cache"], r["value"], r["run_id"]) for r in rows if r["kind"] not in ("health_probe","threads_probe"))
for k, v in sorted(c.items()): print(k, v)
