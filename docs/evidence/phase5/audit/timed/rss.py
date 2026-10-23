"""Q7.9 peak RSS of the API process under --cpus=0.1 --memory=512m.
Snapshots: S0 first /health; S1 +5 s idle (pool open); S2 after metadata GETs;
then distinct uncached analyses filling the 256-entry LRU: snapshots after
50/100/150/200/256/300 analyses. DB = prototype serving DB (atus-emu-pg, jit=off).
usage: python rss.py <port>"""
import json
import random
import sys
import time

import lib

port = int(sys.argv[1])
YEARS_ALL = list(range(2003, 2026))
P_YEARS = [y for y in YEARS_ALL if y != 2020]
T1 = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16", "18", "50"]
T2 = ["0101", "0102", "0201", "0202", "0203", "0301", "0401", "0501", "0502", "0601", "0701", "1101", "1201", "1203", "1301", "1801", "1805", "1812", "0103", "0104"]
MEASURES = ["average_minutes_per_day", "participation_rate", "average_minutes_per_participant"]


def specs():
    out = []
    # 120 single-year estimates with replicate SE
    for c in T1[:12]:
        for y in [2003, 2005, 2008, 2010, 2012, 2014, 2016, 2018, 2022, 2024]:
            out.append(("estimate", {"activity": {"include": [c]}, "years": [y]}))
    # 60 trends with SE over all 23 years (largest payloads)
    for c in T2:
        for m in MEASURES:
            out.append(("trend", {"measure": m, "activity": {"include": [c]}, "years": YEARS_ALL}))
    # 60 compares (single year, SE)
    pairs = [({"sex": "male"}, {"sex": "female"}),
             ({"employment_status": "employed"}, {"employment_status": "not_in_labor_force"}),
             ({"has_household_children": True}, {"has_household_children": False})]
    for c in T2:
        for a, b in pairs:
            out.append(("compare", {"activity": {"include": [c]}, "years": [2024], "group_a": a, "group_b": b}))
    # 60 pooled multi-year estimates (SE)
    ranges = [list(range(2003, 2008)), list(range(2015, 2020)), [2021, 2022, 2023, 2024, 2025]]
    for c in T2:
        for r in ranges:
            out.append(("estimate", {"activity": {"include": [c]}, "years": r}))
    random.Random(20261020).shuffle(out)
    return out


res = {**lib.header("rss"), "api_limits": "--cpus=0.1 --memory=512m --memory-swap=512m",
       "db": "atus-emu-pg prototype serving DB, unconstrained, session jit=off",
       "snapshots": [], "requests": []}


def snap(name, name_c):
    s = lib.cg(name_c)
    s["docker_stats"] = lib.docker_stats([name_c])
    s["at"] = name
    s["t"] = time.perf_counter()
    res["snapshots"].append(s)
    print(name, "mem.current", s.get("memory_current"), "mem.peak", s.get("memory_peak"),
          "VmRSS", s.get("proc1", {}).get("VmRSS"), "VmHWM", s.get("proc1", {}).get("VmHWM"),
          "threads", s.get("proc1", {}).get("Threads"), "|", s["docker_stats"], flush=True)


name, t0 = lib.start_api("rss", port, lib.api_db_url("atus-emu-pg", jit="off", log_all=False),
                         cpus="0.1", mem="512m")
lib.wait_ok(port, "GET", "/health", poll_s=0.1)
snap("S0_first_health", name)
time.sleep(5)
snap("S1_idle_5s_pool_open", name)
for p in ["/api/v1/meta", "/api/v1/activities", "/api/v1/activities/presets",
          "/api/v1/population/metadata", "/api/v1/activities/0101"]:
    r = lib.req(port, "GET", p)
    res["requests"].append({"op": "GET " + p, **r})
snap("S2_after_metadata", name)
checkpoints = {50, 100, 150, 200, 256, 300}
for i, (op, body) in enumerate(specs(), start=1):
    r = lib.req(port, "POST", f"/api/v1/analysis/{op}", body)
    res["requests"].append({"op": op, "i": i, "spec": body, **r})
    if r["status"] != 200 or r.get("x_cache") != "miss":
        print("UNEXPECTED", i, op, r, flush=True)
    if i in checkpoints:
        snap(f"after_{i}_analyses", name)
time.sleep(5)
snap("final_idle_5s", name)
res["api_log_tail"] = lib.sh(f"docker logs --tail 20 {name}", check=False)
res["date_end_utc"] = lib.now_utc()
(lib.D / "rss.json").write_text(json.dumps(res, indent=1, default=str))
walls = [r["wall_s"] for r in res["requests"] if r.get("i")]
print("analyses", len(walls), "wall summary", lib.summary(walls))
print("bytes per response summary", lib.summary([r.get("bytes") for r in res["requests"] if r.get("i")]))
lib.stop_api(name)
