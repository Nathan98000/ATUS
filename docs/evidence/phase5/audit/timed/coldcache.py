"""Q7.9 first request after a DB restart (cold buffer cache) for the worst case,
under the currently applied atus-emu-pg profile.
Procedure per repetition:
  1. docker restart atus-emu-pg (shared_buffers empty)
  2. [variant 'oscold'] drop the kernel page cache for the DB's data files only
     (posix_fadvise DONTNEED from a helper container with the volume mounted
     read-only) and verify residency with mincore before/after
  3. fresh API container (0.1 CPU / 512 MB, jit=off), wait for /health, 3 s
  4. worst-case request (cold), then the same request with a new
     confidence_level (warm), with MARK statements for DB time attribution
usage: python coldcache.py <label> <api_port> <type> <reps> <variant: pgonly|oscold>"""
import json
import sys
import time

import lib

label, port, typ, reps, variant = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
YEARS_ALL = list(range(2003, 2026))
P_YEARS = [y for y in YEARS_ALL if y != 2020]
BODIES = {
    "pooled22_leisure_bls": ("/api/v1/analysis/estimate",
                             lambda cl: {"activity": {"preset": "leisure_and_sports_bls_table"},
                                         "years": P_YEARS, "confidence_level": cl}),
    "trend23_tier1_12": ("/api/v1/analysis/trend",
                         lambda cl: {"activity": {"include": ["12"]}, "years": YEARS_ALL,
                                     "confidence_level": cl}),
}
path, factory = BODIES[typ]
cache_cmd = (f"docker run --rm --name atus-audit-pgcache -v atus-emu-pgdata:/pgdata:ro "
             f"-v {lib.D}:/s:ro {lib.IMAGE} python /s/pgcache.py")
res = {**lib.header(f"coldcache-{label}"),
       "db_limits": lib.sh("docker inspect atus-emu-pg --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'"),
       "variant": variant, "type": typ, "reps": []}
for i in range(reps):
    rep = {"i": i}
    since = lib.now_utc()
    t = time.perf_counter()
    lib.sh("docker restart atus-emu-pg")
    rep["db_restart_s"] = time.perf_counter() - t
    # wait until postgres accepts connections
    for _ in range(120):
        if "accepting" in lib.sh("docker exec atus-emu-pg pg_isready -U atus -d atus", check=False):
            break
        time.sleep(0.5)
    if variant == "oscold":
        rep["page_cache_before_drop"] = lib.sh(cache_cmd, check=False)
        rep["page_cache_drop"] = lib.sh(cache_cmd + " --drop", check=False)
        rep["page_cache_after_drop"] = lib.sh(cache_cmd, check=False)
    else:
        rep["page_cache_at_start"] = lib.sh(cache_cmd, check=False)
    name, _ = lib.start_api(f"cc-{label}", port, lib.api_db_url("atus-emu-pg", jit="off", log_all=True),
                            cpus="0.1", mem="512m")
    lib.wait_ok(port, "GET", "/health", poll_s=0.1)
    time.sleep(3)
    mk = lib.Marker(5441)
    for phase, cl in (("cold", 0.9 - 0.001 * i), ("warm", 0.8 - 0.001 * i)):
        tag = f"cc{label}#{i}#{phase}"
        mk.mark(f"{tag} BEGIN")
        r = lib.req(port, "POST", path, factory(cl))
        mk.mark(f"{tag} END")
        rep[phase] = r
        print(f"rep{i} {phase}: status={r.get('status')} wall={r['wall_s']:.2f}s cache={r.get('x_cache')}", flush=True)
    rep["page_cache_after_requests"] = lib.sh(cache_cmd, check=False)
    mk.close()
    time.sleep(1)
    recs = lib.parse_pg_log(lib.fetch_pg_log("atus-emu-pg", since, lib.D / f"coldcache_{label}_{i}_pglog.txt.gz"))
    att = lib.attribute_db_time(recs)
    for phase in ("cold", "warm"):
        a = att.get(f"cc{label}#{i}#{phase}", {})
        rep[phase]["db_ms"] = a.get("db_ms")
        rep[phase]["analysis_sql_ms"] = a.get("analysis_ms")
        rep[phase]["db_errors"] = a.get("errors")
    print(f"rep{i}: cold wall={rep['cold']['wall_s']:.2f}s db={rep['cold']['db_ms']}ms | warm wall="
          f"{rep['warm']['wall_s']:.2f}s db={rep['warm']['db_ms']}ms | cache: "
          f"{rep.get('page_cache_after_drop') or rep.get('page_cache_at_start')}", flush=True)
    lib.stop_api(name)
    res["reps"].append(rep)
res["date_end_utc"] = lib.now_utc()
(lib.D / f"coldcache_{label}.json").write_text(json.dumps(res, indent=1, default=str))
