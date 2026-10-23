"""Q7.9 cold start: `docker run --cpus=0.1 --memory=512m` of the prototype API image
-> first 200 from GET /health -> first 200 from GET /api/v1/meta.
usage: python coldstart.py <label> <runs> <port>
DB = prototype serving DB in atus-emu-pg (whatever profile is currently applied)."""
import json
import sys
import time

import lib

label, runs, port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
out = {**lib.header(f"coldstart-{label}"),
       "db_profile": lib.sh("docker inspect atus-emu-pg --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'"),
       "api_limits": "--cpus=0.1 --memory=512m --memory-swap=512m", "runs": []}
for i in range(runs):
    name, t0 = lib.start_api(f"cold{label}{i}", port, lib.api_db_url("atus-emu-pg", jit="off", log_all=False),
                             cpus="0.1", mem="512m")
    t_run_returned = time.perf_counter()
    t_health, n_h = lib.wait_ok(port, "GET", "/health", poll_s=0.1)
    t_meta, n_m = lib.wait_ok(port, "GET", "/api/v1/meta", poll_s=0.05)
    # one more /meta, now warm, for contrast
    warm = lib.req(port, "GET", "/api/v1/meta")
    time.sleep(1)
    mem = lib.cg(name)
    logs = lib.sh(f"docker logs {name}", check=False)
    run = {
        "i": i,
        "docker_run_cmd_s": t_run_returned - t0,
        "to_first_health_s": None if t_health is None else t_health - t0,
        "to_first_meta_s": None if t_meta is None else t_meta - t0,
        "health_polls": n_h, "meta_polls": n_m,
        "second_meta_wall_s": warm["wall_s"], "second_meta_status": warm["status"],
        "memory_current": mem.get("memory_current"), "memory_peak": mem.get("memory_peak"),
        "proc1": mem.get("proc1"), "cpu_stat": mem.get("cpu_stat"),
        "uvicorn_log_head": logs.splitlines()[:8],
    }
    out["runs"].append(run)
    print(json.dumps({k: v for k, v in run.items() if k not in ("uvicorn_log_head", "cpu_stat")}), flush=True)
    lib.stop_api(name)
    time.sleep(2)
for key in ("to_first_health_s", "to_first_meta_s", "docker_run_cmd_s", "second_meta_wall_s"):
    out[f"summary_{key}"] = lib.summary([r[key] for r in out["runs"]])
out["date_end_utc"] = lib.now_utc()
(lib.D / f"coldstart_{label}.json").write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k.startswith("summary")}, indent=1))
