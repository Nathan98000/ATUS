"""Q7.9 latency matrix for one (DB profile, API profile).
usage: python matrix.py <label> <db_container> <marker_port> <api_cpus|none> <api_mem|none>
                        <jit on|off|default> <api_port> [only=t1,t2] [n_scale=1.0] [ro]
Requests are strictly sequential, round-robin interleaved across request types;
round 0 is a warm-up (reported separately). Uncached requests get a unique
confidence_level so every one is a cache miss with identical SQL.
DB time = sum of Postgres-logged durations (log_min_duration_statement=0 set
per API session) for the API's backends between MARK BEGIN/END statements
issued from a separate session."""
import itertools
import json
import sys
import time

import lib

args = sys.argv[1:]
label, dbc, mport, cpus, mem, jit, port = args[:7]
extra = dict(a.split("=", 1) for a in args[7:] if "=" in a)
ro = "ro" in args[7:]
only = set(extra["only"].split(",")) if "only" in extra else None
n_scale = float(extra.get("n_scale", "1"))
cpus = None if cpus == "none" else cpus
mem = None if mem == "none" else mem
jit_opt = None if jit == "default" else jit
port = int(port)
mport = int(mport)

YEARS_ALL = list(range(2003, 2026))
P_YEARS = [y for y in YEARS_ALL if y != 2020]
_cl = itertools.count()


def cl():
    return round(0.95 - 0.0001 * next(_cl), 6)


TYPES = {
    # name: (n measured, method, path, body factory, uncached?)
    "meta": (10, "GET", "/api/v1/meta", None, True),
    "activities": (10, "GET", "/api/v1/activities", None, True),
    "cached_estimate": (10, "POST", "/api/v1/analysis/estimate",
                        lambda: {"activity": {"preset": "sleep"}, "years": [2025]}, False),
    "est_1y_010101_2025": (7, "POST", "/api/v1/analysis/estimate",
                           lambda: {"activity": {"include": ["010101"]}, "years": [2025],
                                    "confidence_level": cl()}, True),
    "cmp_1y_sex_010101_2025": (7, "POST", "/api/v1/analysis/compare",
                               lambda: {"activity": {"include": ["010101"]}, "years": [2025],
                                        "group_a": {"sex": "male"}, "group_b": {"sex": "female"},
                                        "confidence_level": cl()}, True),
    "pooled22_leisure_bls": (5, "POST", "/api/v1/analysis/estimate",
                             lambda: {"activity": {"preset": "leisure_and_sports_bls_table"},
                                      "years": P_YEARS, "confidence_level": cl()}, True),
    "trend23_tier1_12": (5, "POST", "/api/v1/analysis/trend",
                         lambda: {"activity": {"include": ["12"]}, "years": YEARS_ALL,
                                  "confidence_level": cl()}, True),
    "trend23_6digit_010101": (5, "POST", "/api/v1/analysis/trend",
                              lambda: {"activity": {"include": ["010101"]}, "years": YEARS_ALL,
                                       "confidence_level": cl()}, True),
    "pandemic_2019_2020_sleep": (7, "POST", "/api/v1/analysis/estimate",
                                 lambda: {"activity": {"preset": "sleep"}, "years": [2019, 2020],
                                          "weights": "pandemic", "confidence_level": cl()}, True),
}
if only:
    TYPES = {k: v for k, v in TYPES.items() if k in only}
TYPES = {k: (max(3, round(v[0] * n_scale)),) + v[1:] for k, v in TYPES.items()}

res = {**lib.header(f"matrix-{label}"),
       "db_container": dbc,
       "db_limits": lib.sh(f"docker inspect {dbc} --format 'NanoCpus={{{{.HostConfig.NanoCpus}}}} Memory={{{{.HostConfig.Memory}}}}'"),
       "api_limits": f"cpus={cpus} mem={mem}", "session_jit": jit, "read_only_session": ro,
       "requests": []}
since = lib.now_utc()
db_url = lib.api_db_url(dbc, jit=jit_opt, log_all=True, read_only=ro)
name, t0 = lib.start_api(f"mx-{label}", port, db_url, cpus=cpus, mem=mem)
lib.wait_ok(port, "GET", "/health", poll_s=0.1)
time.sleep(3)
mk = lib.Marker(mport)
res["db_cpu_before"] = lib.sh(f"docker exec {dbc} cat /sys/fs/cgroup/cpu.stat", check=False)
res["api_cpu_before"] = lib.sh(f"docker exec {name} cat /sys/fs/cgroup/cpu.stat", check=False)
# prime the cached request outside the measured rounds
if "cached_estimate" in TYPES:
    b = TYPES["cached_estimate"][3]()
    mk.mark("prime BEGIN")
    r = lib.req(port, "POST", TYPES["cached_estimate"][2], b)
    mk.mark("prime END")
    res["requests"].append({"type": "cached_estimate", "round": -1, "tag": "prime", **r})
max_n = max(v[0] for v in TYPES.values())
seq = 0
for rnd in range(0, max_n + 1):
    for t, (n, method, path, factory, uncached) in TYPES.items():
        if rnd > n:
            continue
        body = factory() if factory else None
        seq += 1
        tag = f"{t}#{rnd}#{seq}"
        mk.mark(f"{tag} BEGIN")
        r = lib.req(port, method, path, body)
        mk.mark(f"{tag} END")
        rec = {"type": t, "round": rnd, "tag": tag, "uncached_expected": uncached, **r}
        res["requests"].append(rec)
        print(f"{tag:45s} status={r.get('status')} wall={r['wall_s']:.3f}s cache={r.get('x_cache')} "
              f"{r.get('error', '')}{r.get('error_body', '')[:120] if r.get('error_body') else ''}", flush=True)
        time.sleep(0.3)
res["db_cpu_after"] = lib.sh(f"docker exec {dbc} cat /sys/fs/cgroup/cpu.stat", check=False)
res["api_cpu_after"] = lib.sh(f"docker exec {name} cat /sys/fs/cgroup/cpu.stat", check=False)
res["api_mem_after"] = lib.cg(name)
time.sleep(1)
mk.close()
recs = lib.parse_pg_log(lib.fetch_pg_log(dbc, since, lib.D / f"matrix_{label}_pglog.txt.gz"))
att = lib.attribute_db_time(recs)
srv = lib.api_durations(name)
(lib.D / f"matrix_{label}_apilog.txt").write_text(lib.sh(f"docker logs {name}", check=False))
lib.stop_api(name)
for r in res["requests"]:
    a = att.get(r["tag"], {})
    r["db_ms"] = a.get("db_ms")
    r["analysis_sql_ms"] = a.get("analysis_ms")
    r["db_statements"] = a.get("n_statements")
    r["db_errors"] = a.get("errors")
    r["server_ms"] = srv.get(r.get("request_id"))
    r["api_ms_total_minus_db"] = (r["wall_s"] * 1000 - r["db_ms"]) if r.get("db_ms") is not None else None
summ = {}
for t in TYPES:
    rows = [r for r in res["requests"] if r["type"] == t and r["round"] >= 1]
    warm = [r for r in res["requests"] if r["type"] == t and r["round"] == 0]
    summ[t] = {
        "n": len(rows),
        "statuses": sorted({str(r.get("status")) for r in rows}),
        "x_cache": sorted({str(r.get("x_cache")) for r in rows}),
        "wall_s": lib.summary([r["wall_s"] for r in rows]),
        "server_ms": lib.summary([r["server_ms"] for r in rows]),
        "db_ms": lib.summary([r["db_ms"] for r in rows]),
        "analysis_sql_ms": lib.summary([r["analysis_sql_ms"] for r in rows]),
        "api_ms_total_minus_db": lib.summary([r["api_ms_total_minus_db"] for r in rows]),
        "db_statements": sorted({r["db_statements"] for r in rows if r["db_statements"] is not None}),
        "warmup_round0": [{"wall_s": w["wall_s"], "db_ms": w["db_ms"], "status": w.get("status")} for w in warm],
    }
res["summary"] = summ
res["date_end_utc"] = lib.now_utc()
(lib.D / f"matrix_{label}.json").write_text(json.dumps(res, indent=1, default=str))
# CSV of raw rows
import csv
with open(lib.D / f"matrix_{label}.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["type", "round", "status", "x_cache", "wall_s", "server_ms", "db_ms",
                "analysis_sql_ms", "api_ms_total_minus_db", "db_statements", "bytes"])
    for r in res["requests"]:
        w.writerow([r["type"], r["round"], r.get("status"), r.get("x_cache"), round(r["wall_s"], 4),
                    r["server_ms"], None if r["db_ms"] is None else round(r["db_ms"], 2),
                    None if r["analysis_sql_ms"] is None else round(r["analysis_sql_ms"], 2),
                    None if r["api_ms_total_minus_db"] is None else round(r["api_ms_total_minus_db"], 2),
                    r["db_statements"], r.get("bytes")])
for t, s in summ.items():
    f = lambda d: "n/a" if not d or d.get("n", 0) == 0 else f"p50={d['p50']:.1f} p95={d['p95']:.1f} max={d['max']:.1f}"
    print(f"{t:28s} n={s['n']} st={s['statuses']} cache={s['x_cache']} wall_s[{f(s['wall_s'])}] "
          f"db_ms[{f(s['db_ms'])}] api_ms[{f(s['api_ms_total_minus_db'])}] stmts={s['db_statements']}")
