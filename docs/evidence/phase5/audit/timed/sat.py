"""Q7.3 saturation: bursts of 1,2,4,8,16 concurrent DISTINCT uncached single-year
estimates with replicate SE against the 0.1-CPU / 512 MB API, DB = atus-emu-pg
under whatever profile is applied. One fresh API container per profile; one
burst per level (levels 1 and 2 repeated 2x). A helper container samples the
cgroup cpu.stat of both containers every 0.5 s (read-only mount of the VM's
cgroupfs, so sampling costs the API nothing); a separate DB session samples
pg_stat_activity every 0.5 s.
usage: python sat.py <label> <api_port> [levels=1,2,4,8,16]"""
import json
import subprocess
import sys
import threading
import time

import psycopg

import lib

label, port = sys.argv[1], int(sys.argv[2])
extra = dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a)
levels = [int(x) for x in extra.get("levels", "1,1,2,2,4,8,16").split(",")]
T1 = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16", "18", "50"]
YRS = [2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024, 2025]
counter = [0]


def spec():
    i = counter[0]
    counter[0] += 1
    return {"activity": {"include": [T1[i % len(T1)]]}, "years": [YRS[(i * 7) % len(YRS)]],
            "confidence_level": round(0.95 - 0.0001 * i, 6)}


res = {**lib.header(f"saturation-{label}"),
       "db_limits": lib.sh("docker inspect atus-emu-pg --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'"),
       "api_limits": "--cpus=0.1 --memory=512m --memory-swap=512m; pool max_size=10, acquire timeout 10 s (code)",
       "session_jit": "off", "bursts": []}
name, _ = lib.start_api(f"sat-{label}", port, lib.api_db_url("atus-emu-pg", jit="off", log_all=False),
                        cpus="0.1", mem="512m")
lib.wait_ok(port, "GET", "/health", poll_s=0.1)
time.sleep(3)
# warm the per-process paths once (not part of the bursts)
w = lib.req(port, "POST", "/api/v1/analysis/estimate", spec())
res["warmup"] = w
api_id = lib.sh(f"docker inspect {name} --format '{{{{.Id}}}}'")
db_id = lib.sh("docker inspect atus-emu-pg --format '{{.Id}}'")
mon = psycopg.connect("postgresql://atus:atus@localhost:5441/atus", autocommit=True,
                      application_name="audit-monitor", options="-c default_transaction_read_only=on")

for li, c in enumerate(levels):
    time.sleep(5)
    sampler = subprocess.Popen(
        ["docker", "run", "--rm", "--name", f"atus-audit-sampler-{label}-{li}", "-v", "/sys/fs/cgroup:/cg:ro",
         "--entrypoint", "sh", lib.IMAGE, "-c",
         f"while true; do t=$(date +%s.%N); a=$(tr '\\n' ' ' < /cg/docker/{api_id}/cpu.stat); "
         f"m=$(cat /cg/docker/{api_id}/memory.current); d=$(tr '\\n' ' ' < /cg/docker/{db_id}/cpu.stat); "
         f"echo \"$t|API $a|APIMEM $m|DB $d\"; sleep 0.5; done"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2)
    stop = threading.Event()
    act = []

    def poll_activity():
        while not stop.is_set():
            try:
                row = mon.execute(
                    "select count(*) filter (where state='active'), count(*), "
                    "count(*) filter (where wait_event_type is not null and state='active') "
                    "from pg_stat_activity where application_name='atus-api-proto'").fetchone()
                act.append((time.perf_counter(), *row))
            except Exception as exc:
                act.append((time.perf_counter(), repr(exc)))
            time.sleep(0.5)

    th = threading.Thread(target=poll_activity, daemon=True)
    th.start()
    results = [None] * c
    bodies = [spec() for _ in range(c)]
    barrier = threading.Barrier(c)

    def worker(k):
        barrier.wait()
        results[k] = lib.req(port, "POST", "/api/v1/analysis/estimate", bodies[k], timeout=900)

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(k,)) for k in range(c)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    t1 = time.perf_counter()
    time.sleep(1.5)
    stop.set()
    th.join()
    sampler.terminate()
    subprocess.run(["docker", "rm", "-f", f"atus-audit-sampler-{label}-{li}"], capture_output=True)
    samples = (sampler.communicate(timeout=10)[0] or "").splitlines()
    for r in results:
        r["rel_start_s"] = r["t_start"] - t0
        r["rel_end_s"] = r["t_end"] - t0
    statuses = {}
    for r in results:
        k = str(r.get("status"))
        statuses[k] = statuses.get(k, 0) + 1
    burst = {"concurrency": c, "burst_wall_s": t1 - t0, "statuses": statuses,
             "latency_s": lib.summary([r["wall_s"] for r in results]),
             "latency_200_s": lib.summary([r["wall_s"] for r in results if r.get("status") == 200]),
             "requests": results, "pg_activity": [(round(a[0] - t0, 2), *a[1:]) for a in act],
             "cgroup_samples": samples}
    # derive CPU used + throttling over the burst window from samples
    def parse(line):
        try:
            t, a, m, d = line.split("|")
            av = dict(zip(a.split()[1::2], map(int, a.split()[2::2])))
            dv = dict(zip(d.split()[1::2], map(int, d.split()[2::2])))
            return float(t), av, int(m.split()[1]), dv
        except Exception:
            return None
    ps = [p for p in map(parse, samples) if p]
    if len(ps) >= 2:
        dt = ps[-1][0] - ps[0][0]
        burst["sampled_window_s"] = dt
        burst["api_cpu_s"] = (ps[-1][1]["usage_usec"] - ps[0][1]["usage_usec"]) / 1e6
        burst["api_throttled_periods"] = ps[-1][1]["nr_throttled"] - ps[0][1]["nr_throttled"]
        burst["api_periods"] = ps[-1][1]["nr_periods"] - ps[0][1]["nr_periods"]
        burst["api_throttled_s"] = (ps[-1][1]["throttled_usec"] - ps[0][1]["throttled_usec"]) / 1e6
        burst["api_mem_max_bytes"] = max(p[2] for p in ps)
        burst["db_cpu_s"] = (ps[-1][3]["usage_usec"] - ps[0][3]["usage_usec"]) / 1e6
        burst["db_throttled_s"] = (ps[-1][3].get("throttled_usec", 0) - ps[0][3].get("throttled_usec", 0)) / 1e6
        burst["max_active_api_backends"] = max((a[1] for a in act if isinstance(a[1], int)), default=None)
        burst["max_api_backends"] = max((a[2] for a in act if isinstance(a[2], int)), default=None)
    res["bursts"].append(burst)
    errs = sorted({(r.get("error_body") or r.get("error") or "")[:160] for r in results if r.get("status") != 200})
    print(f"c={c:2d} wall={t1-t0:6.2f}s statuses={statuses} lat200={burst['latency_200_s']} "
          f"api_cpu={burst.get('api_cpu_s')} api_thr_s={burst.get('api_throttled_s')} db_cpu={burst.get('db_cpu_s')} "
          f"maxActive={burst.get('max_active_api_backends')} maxConns={burst.get('max_api_backends')} errs={errs}", flush=True)

mon.close()
res["api_log_tail"] = lib.sh(f"docker logs --tail 80 {name}", check=False)
lib.stop_api(name)
res["date_end_utc"] = lib.now_utc()
(lib.D / f"sat_{label}.json").write_text(json.dumps(res, indent=1, default=str))
