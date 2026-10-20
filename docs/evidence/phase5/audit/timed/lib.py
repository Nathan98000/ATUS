"""Shared helpers for the timed free-tier-fit audit (7.9 / 7.3).

Run with the repo venv python. Never touches database "atus" on 5434 except
through read-only sessions (default_transaction_read_only=on); the only
container this module (re)configures is atus-emu-pg and atus-audit-* ones.
"""
from __future__ import annotations

import datetime as dt
import gzip
import http.client as httpclient
import json
import re
import statistics
import subprocess
import time
import urllib.parse
from pathlib import Path

import psycopg

D = Path(__file__).resolve().parent
IMAGE = "atus-audit-api:proto"
EMU = "atus-emu-pg"
FULL = "atus-postgres"
API_APP = "atus-api-proto"


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed: {r.stdout}{r.stderr}")
    return (r.stdout + r.stderr).strip()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def container_ip(name: str) -> str:
    return sh(f"docker inspect {name} --format '{{{{.NetworkSettings.Networks.bridge.IPAddress}}}}'")


def api_db_url(db_container: str, *, jit: str | None, log_all: bool = True,
               read_only: bool = False) -> str:
    opts = []
    if jit is not None:
        opts.append(f"-c jit={jit}")
    if log_all:
        opts.append("-c log_min_duration_statement=0")
    if read_only:
        opts.append("-c default_transaction_read_only=on")
    q = {"application_name": API_APP}
    if opts:
        q["options"] = " ".join(opts)
    return (f"postgresql://atus:atus@{container_ip(db_container)}:5432/atus?"
            + urllib.parse.urlencode(q, quote_via=urllib.parse.quote))


def start_api(tag: str, port: int, db_url: str, *, cpus: str | None = "0.1",
              mem: str | None = "512m", extra_env: dict | None = None) -> tuple[str, float]:
    """docker run -d; returns (container name, perf_counter just before the run call)."""
    name = f"atus-audit-api-{tag}"
    sh(f"docker rm -f {name}", check=False)
    limits = ""
    if cpus:
        limits += f" --cpus={cpus}"
    if mem:
        limits += f" --memory={mem} --memory-swap={mem}"
    env = {"ATUS_DATABASE_URL": db_url, "ATUS_LOG_LEVEL": "INFO", **(extra_env or {})}
    envs = " ".join(f"-e {k}='{v}'" for k, v in env.items())
    t0 = time.perf_counter()
    sh(f"docker run -d --name {name} -p 127.0.0.1:{port}:8000{limits} {envs} {IMAGE}")
    return name, t0


def stop_api(name: str) -> None:
    sh(f"docker rm -f {name}", check=False)


def req(port: int, method: str, path: str, body: dict | None = None,
         timeout: float = 900) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    t0 = time.perf_counter()
    try:
        c = httpclient.HTTPConnection("127.0.0.1", port, timeout=timeout)
        hdrs = {"Content-Type": "application/json"} if data else {}
        c.request(method, path, body=data, headers=hdrs)
        r = c.getresponse()
        raw = r.read()
        t1 = time.perf_counter()
        out = {
            "status": r.status, "wall_s": t1 - t0, "bytes": len(raw),
            "x_cache": r.getheader("X-Cache"), "request_id": r.getheader("X-Request-ID"),
            "t_start": t0, "t_end": t1,
        }
        if r.status >= 400:
            out["error_body"] = raw[:500].decode(errors="replace")
        c.close()
        return out
    except Exception as exc:  # connection refused / reset / timeout
        return {"status": None, "wall_s": time.perf_counter() - t0, "error": repr(exc),
                "t_start": t0, "t_end": time.perf_counter()}



def wait_ok(port: int, method: str, path: str, body=None, deadline_s: float = 300,
            poll_s: float = 0.05) -> tuple[float | None, int]:
    """Poll until 200; return (perf_counter at first 200, attempts)."""
    end = time.perf_counter() + deadline_s
    n = 0
    while time.perf_counter() < end:
        n += 1
        r = req(port, method, path, body, timeout=60)
        if r.get("status") == 200:
            return r["t_end"], n
        time.sleep(poll_s)
    return None, n


# ---------------------------------------------------------------- markers & pg logs
class Marker:
    """Writes MARK statements into a server's log from a separate session so the
    API's statements of one request can be attributed by log order."""

    def __init__(self, port: int):
        self.port = port
        self.conn = None

    def _c(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(
                f"postgresql://atus:atus@localhost:{self.port}/atus", autocommit=True,
                application_name="audit-marker",
                options="-c log_min_duration_statement=0 -c default_transaction_read_only=on",
            )
        return self.conn

    def mark(self, text: str) -> None:
        for _ in range(3):
            try:
                self._c().execute(f"SELECT 'MARK {text}'")
                return
            except psycopg.OperationalError:
                self.conn = None
                time.sleep(1)

    def close(self):
        if self.conn is not None:
            self.conn.close()


_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ \w+ \[(\d+)\] (\w+):  (.*)$")
_DUR = re.compile(r"^duration: ([\d.]+) ms  (\w+)(?: ([^:]+))?: ?(.*)$")


def fetch_pg_log(container: str, since: str, save_as: Path) -> list[str]:
    txt = subprocess.run(f"docker logs --since {since} {container}", shell=True,
                         capture_output=True, text=True)
    out = txt.stdout + txt.stderr
    with gzip.open(save_as, "wt") as f:
        f.write(out)
    return out.splitlines()


def parse_pg_log(lines: list[str]) -> list[dict]:
    """Return log records {pid, level, kind, ms, text} with continuation lines
    folded into text (first 300 chars)."""
    recs: list[dict] = []
    for line in lines:
        m = _PREFIX.match(line)
        if m:
            pid, level, msg = m.groups()
            rec = {"pid": int(pid), "level": level, "msg": msg, "ms": None,
                   "kind": None, "text": ""}
            d = _DUR.match(msg)
            if d:
                rec["ms"] = float(d.group(1))
                rec["kind"] = d.group(2)
                rec["text"] = d.group(4)
            recs.append(rec)
        elif recs and len(recs[-1]["text"]) < 300:
            recs[-1]["text"] = (recs[-1]["text"] + " " + line.strip()).strip()
    return recs


def attribute_db_time(recs: list[dict]) -> dict[str, dict]:
    """Per marker tag: total DB ms of non-marker sessions between MARK <tag> BEGIN
    and MARK <tag> END, the analysis-SQL ms, and statement counts."""
    out: dict[str, dict] = {}
    cur = None
    marker_pids: set[int] = set()
    for r in recs:
        t = r["text"]
        if r["kind"] == "statement" and t.startswith("SELECT 'MARK "):
            marker_pids.add(r["pid"])
            tag, _, edge = t[len("SELECT 'MARK "):].rstrip("'").rpartition(" ")
            if edge == "BEGIN":
                cur = tag
                out[cur] = {"db_ms": 0.0, "analysis_ms": 0.0, "n_lines": 0,
                            "n_statements": 0, "pids": set(), "errors": []}
            elif edge == "END":
                cur = None
            continue
        if cur is None or r["pid"] in marker_pids:
            continue
        o = out[cur]
        if r["level"] in ("ERROR", "FATAL"):
            o["errors"].append(r["msg"][:200])
        if r["ms"] is None:
            continue
        o["db_ms"] += r["ms"]
        o["n_lines"] += 1
        o["pids"].add(r["pid"])
        if r["kind"] in ("statement", "execute"):
            o["n_statements"] += 1
        if "activity_minutes" in t[:80]:
            o["analysis_ms"] += r["ms"]
    for o in out.values():
        o["pids"] = sorted(o["pids"])
    return out


def api_durations(container: str) -> dict[str, float]:
    """request_id -> server-side ms from the API's own middleware log line."""
    txt = sh(f"docker logs {container}", check=False)
    out = {}
    for m in re.finditer(r"request_id=(\w+) \S+ \S+ -> \d+ in ([\d.]+)ms", txt):
        out[m.group(1)] = float(m.group(2))
    return out


# ---------------------------------------------------------------- cgroup probes
def cg(container: str) -> dict:
    """Memory/cpu cgroup v2 counters + PID-1 RSS from inside the container."""
    script = (
        "cat /sys/fs/cgroup/memory.current; echo ===; cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo NA; "
        "echo ===; grep -E '^(anon|file|kernel|sock|shmem) ' /sys/fs/cgroup/memory.stat; echo ===; "
        "grep -E '^(VmRSS|VmHWM|RssAnon|RssFile|Threads)' /proc/1/status; echo ===; "
        "ps -o pid,rss,vsz,nlwp,args -p 1 --no-headers 2>/dev/null; echo ===; cat /sys/fs/cgroup/cpu.stat"
    )
    raw = sh(f"docker exec {container} sh -c \"{script}\"", check=False)
    parts = [p.strip() for p in raw.split("===")]
    res: dict = {"raw": raw}
    try:
        res["memory_current"] = int(parts[0])
        res["memory_peak"] = int(parts[1]) if parts[1] != "NA" else None
        res["memory_stat"] = {l.split()[0]: int(l.split()[1]) for l in parts[2].splitlines()}
        st = {}
        for l in parts[3].splitlines():
            k, v = l.split(":", 1)
            st[k] = v.strip()
        res["proc1"] = st
        res["ps"] = parts[4]
        res["cpu_stat"] = {l.split()[0]: int(l.split()[1]) for l in parts[5].splitlines()}
    except Exception as exc:  # keep raw
        res["parse_error"] = repr(exc)
    return res


def docker_stats(names: list[str]) -> str:
    return sh("docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' "
              + " ".join(names), check=False)


def pctl(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summary(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "min": min(xs), "p50": statistics.median(xs),
            "p95": pctl(xs, 0.95), "max": max(xs)}


def header(label: str) -> dict:
    return {
        "label": label,
        "date_utc": now_utc(),
        "ran_alone": "yes - sole ATUS audit workload; other projects' containers "
                     "(supabase_*, timeblock-postgres) and host apps kept running (see env_snapshot_*.txt)",
        "machine": "Apple M4, 10-core, 24 GB RAM, macOS 27.0.1; Docker Desktop VM 10 vCPU / 7.75 GiB",
        "docker": sh("docker version --format 'server {{.Server.Version}} client {{.Client.Version}}'"),
    }
