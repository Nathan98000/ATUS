"""Shared helpers for the load-test tools (loadtests/README.md).

Standard library only, so run.py, cold_start.py and analyze.py work with any
Python >= 3.11 on the host. Nothing here prints or writes a secret: secret
values live in the stage's private env files (mode 600, outside the
repository), reach containers through --env-file and k6 through its process
environment, and every bundle is scanned for them before it is reported.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

LT_DIR = Path(__file__).resolve().parent
REPO_ROOT = LT_DIR.parent
K6_DIR = LT_DIR / "k6"
K6_BIN = os.environ.get("ATUS_LT_K6", shutil.which("k6") or "/opt/homebrew/bin/k6")

# Paths whose changes alter the API image (Dockerfile COPY sources).
IMAGE_INPUTS = ("Dockerfile", ".dockerignore", "src", "pyproject.toml", "requirements",
                "README.md", "LICENSE")
# Environment keys whose values are secrets (or contain one).
SECRET_KEY = re.compile(r"PASSWORD|SECRET|TOKEN|DATABASE_URL|HMAC|_KEY$")


class LoadTestError(RuntimeError):
    """A precondition or step failed; the message is safe to print."""


# --------------------------------------------------------------------------- processes
def run(cmd: list[str], *, check: bool = True, env: dict | None = None,
        timeout: float | None = None, cwd: Path = REPO_ROOT,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run a command (never through a shell) and capture its output."""
    result = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, input=input_text,
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-15:]
        raise LoadTestError(f"{' '.join(cmd[:4])} ... failed ({result.returncode}):\n"
                            + "\n".join(tail))
    return result


def out(cmd: list[str], **kwargs) -> str:
    return run(cmd, **kwargs).stdout.strip()


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def rfc3339(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=False, default=str) + "\n")


def write_private(path: Path, text: str) -> None:
    """A file only the owner can read (secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def gzip_file(path: Path) -> Path:
    target = path.with_name(path.name + ".gz")
    with open(path, "rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return target


# --------------------------------------------------------------------------- statistics
def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (the audit's method)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo = math.floor(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values: Iterable[float | None], digits: int = 1) -> dict:
    xs = [v for v in values if v is not None]
    if not xs:
        return {"n": 0}
    r = lambda v: None if v is None else round(v, digits)  # noqa: E731
    return {"n": len(xs), "min": r(min(xs)), "p50": r(percentile(xs, 0.5)),
            "p95": r(percentile(xs, 0.95)), "p99": r(percentile(xs, 0.99)),
            "max": r(max(xs)), "mean": r(sum(xs) / len(xs))}


# --------------------------------------------------------------------------- HTTP
def http_get(url: str, headers: dict | None = None, timeout: float = 30.0
             ) -> tuple[int | None, dict, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, {}, b""


def wait_http(url: str, headers: dict | None = None, *, deadline_s: float = 300,
              poll_s: float = 0.25) -> tuple[float | None, int, list]:
    """Poll until 200. Returns (time.time() at the first 200, polls, statuses seen)."""
    end = time.time() + deadline_s
    polls = 0
    seen: list = []
    while time.time() < end:
        polls += 1
        status, _, _ = http_get(url, headers, timeout=60)
        if status == 200:
            return time.time(), polls, seen
        if not seen or seen[-1] != status:
            seen.append(status)
        time.sleep(poll_s)
    return None, polls, seen


# --------------------------------------------------------------------------- docker
def docker_inspect(name: str) -> dict:
    return json.loads(out(["docker", "inspect", name]))[0]


def container_limits(name: str) -> dict:
    host = docker_inspect(name)["HostConfig"]
    return {"cpus": (host.get("NanoCpus") or 0) / 1e9 or "unlimited",
            "memory_bytes": host.get("Memory") or "unlimited",
            "memory_swap_bytes": host.get("MemorySwap") or "unlimited",
            "shm_size_bytes": host.get("ShmSize")}


def image_info(tag: str) -> dict:
    data = json.loads(out(["docker", "image", "inspect", tag]))[0]
    labels = (data.get("Config") or {}).get("Labels") or {}
    return {"tag": tag, "id": data.get("Id"), "repo_digests": data.get("RepoDigests") or [],
            "architecture": data.get("Architecture"), "os": data.get("Os"),
            "size_bytes": data.get("Size"), "created": data.get("Created"),
            "revision": labels.get("org.opencontainers.image.revision"),
            "version": labels.get("org.opencontainers.image.version")}


def cgroup_snapshot(container: str, *, proc: bool = False) -> dict:
    """cgroup v2 CPU and memory counters (and PID 1's VmRSS/VmHWM) via one exec.

    Taken outside the measured window: the exec runs inside the container's
    cgroup and so spends a little of its CPU quota.
    """
    script = ("cat /sys/fs/cgroup/cpu.stat; echo ===; cat /sys/fs/cgroup/memory.current; "
              "echo ===; cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo NA")
    if proc:
        script += "; echo ===; grep -E '^(VmRSS|VmHWM|Threads)' /proc/1/status"
    result = run(["docker", "exec", container, "sh", "-c", script], check=False)
    snap: dict[str, Any] = {"at": now_utc()}
    try:
        parts = [part.strip() for part in result.stdout.split("===")]
        snap["cpu_stat"] = {line.split()[0]: int(line.split()[1])
                            for line in parts[0].splitlines() if line.strip()}
        snap["memory_current"] = int(parts[1])
        snap["memory_peak"] = None if parts[2] == "NA" else int(parts[2])
        if proc:
            snap["proc1"] = {k.strip(): v.strip() for k, v in
                             (line.split(":", 1) for line in parts[3].splitlines())}
    except (IndexError, ValueError) as error:
        snap["error"] = f"could not parse ({error.__class__.__name__})"
    return snap


def cgroup_delta(before: dict, after: dict) -> dict:
    b, a = before.get("cpu_stat") or {}, after.get("cpu_stat") or {}
    delta = {key: a[key] - b.get(key, 0) for key in a if isinstance(a[key], int)}
    return {"cpu_usage_s": round(delta.get("usage_usec", 0) / 1e6, 3),
            "nr_periods": delta.get("nr_periods"), "nr_throttled": delta.get("nr_throttled"),
            "throttled_s": round(delta.get("throttled_usec", 0) / 1e6, 3),
            "memory_peak_after": after.get("memory_peak"),
            "proc1_after": after.get("proc1")}


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text: str) -> float | None:
    match = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$", text or "")
    if not match:
        return None
    factor = _UNITS.get(match.group(2).lower())
    return None if factor is None else float(match.group(1)) * factor


class StatsSampler:
    """Streams `docker stats` (about one sample per second per container) to JSONL.

    Read by the Docker daemon from outside the containers, so sampling costs
    the 0.1-CPU API nothing.
    """

    def __init__(self, containers: list[str], path: Path):
        self.containers = containers
        self.path = path
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["docker", "stats", "--format", "{{json .}}", *self.containers],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with open(self.path, "w") as sink:
            for raw in self.proc.stdout:
                line = _ANSI.sub("", raw).strip()
                if not line.startswith("{"):
                    continue
                try:
                    sample = json.loads(line)
                except ValueError:
                    continue
                used, _, limit = (sample.get("MemUsage") or "").partition("/")
                sink.write(json.dumps({
                    "ts": round(time.time(), 3), "name": sample.get("Name"),
                    "cpu_pct": float((sample.get("CPUPerc") or "0%").rstrip("%") or 0),
                    "mem_bytes": parse_size(used), "mem_limit_bytes": parse_size(limit),
                    "pids": sample.get("PIDs")}) + "\n")
                sink.flush()

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread:
            self.thread.join(timeout=10)


def summarize_stats(path: Path, t0: float, t1: float) -> dict:
    """Per container, inside [t0, t1]: CPU % (100 = one core) and memory."""
    per: dict[str, dict[str, list]] = {}
    if not path.exists():
        return {}
    for line in path.read_text().splitlines():
        sample = json.loads(line)
        if not t0 <= sample["ts"] <= t1:
            continue
        slot = per.setdefault(sample["name"], {"cpu": [], "mem": []})
        slot["cpu"].append(sample["cpu_pct"])
        slot["mem"].append(sample["mem_bytes"])
    return {name: {"samples": len(v["cpu"]), "cpu_pct": summarize(v["cpu"]),
                   "mem_mib": summarize([m / 2 ** 20 for m in v["mem"] if m is not None])}
            for name, v in per.items()}


# --------------------------------------------------------------------------- postgres log
# log_line_prefix: the stack sets '%m [%p] [%a] '; the stock image uses '%m [%p] '.
_PREFIX = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)? \S+) \[(\d+)\] "
                     r"(?:\[([^\]]*)\] )?([A-Z]+):  (.*)$")
_DURATION = re.compile(r"^duration: ([\d.]+) ms  (statement|parse|bind|execute)"
                       r"(?: ([^:]+))?: ?(.*)$")
MARKER_APP = "atus-lt-marker"


def parse_pg_log(lines: Iterable[str]) -> Iterator[dict]:
    """Log records {ts, pid, app, level, kind, ms, text}; continuation lines are
    folded into the previous record's text (first 200 characters kept)."""
    current: dict | None = None
    for line in lines:
        match = _PREFIX.match(line)
        if match:
            if current is not None:
                yield current
            ts, pid, app, level, message = match.groups()
            current = {"ts": ts, "pid": int(pid), "app": app, "level": level,
                       "kind": None, "ms": None, "text": message[:200]}
            duration = _DURATION.match(message)
            if duration:
                current["ms"] = float(duration.group(1))
                current["kind"] = duration.group(2)
                current["text"] = duration.group(4)[:200]
        elif current is not None and len(current["text"]) < 200:
            current["text"] = (current["text"] + " " + line.strip())[:200]
    if current is not None:
        yield current


_LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) (UTC|GMT)$")


def log_epoch(ts: str) -> float | None:
    """A log line's timestamp (log_timezone UTC/GMT) as epoch seconds."""
    match = _LOG_TS.match(ts or "")
    if not match:
        return None
    stamp_text = match.group(1)
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in stamp_text else "%Y-%m-%d %H:%M:%S"
    return dt.datetime.strptime(stamp_text, fmt).replace(tzinfo=dt.UTC).timestamp()


def attribute_db_time(records: Iterable[dict], tag: str, *, app: str | None = "atus-api",
                      windows: dict[str, tuple[float, float]] | None = None) -> dict:
    """DB time of the API's sessions between `MARK <tag> BEGIN` and `MARK <tag> END`.

    The audit's method (docs/evidence/phase5/01-audit-timed.md): the sum of the
    API backends' logged parse, bind and execute (or simple statement)
    durations between two marker statements. With an application name in the
    log prefix only `app` sessions count; without one, every session except the
    marker's.

    `windows` ({name: (epoch_from, epoch_to)}) splits the same sum by when each
    statement finished (its log time), for example k6's setup phase versus the
    measured phase. The host and the Docker VM clocks are compared, so a window
    edge is good to a few milliseconds.
    """
    windows = windows or {}
    split = {name: {"db_ms": 0.0, "statements": 0, "from": rfc3339(a), "to": rfc3339(b)}
             for name, (a, b) in windows.items()}
    begin, end = f"SELECT 'MARK {tag} BEGIN'", f"SELECT 'MARK {tag} END'"
    inside = False
    seen_end = False
    marker_pids: set[int] = set()
    result: dict[str, Any] = {"tag": tag, "db_ms": 0.0, "by_kind_ms": Counter(),
                              "statements": 0, "lines": 0, "pids": set(), "errors": Counter(),
                              "statement_timeouts": 0, "analysis_sql_ms": 0.0,
                              "first_ts": None, "last_ts": None}
    for record in records:
        text = record["text"]
        if record["kind"] == "statement" and text.startswith("SELECT 'MARK "):
            marker_pids.add(record["pid"])
            if text.startswith(begin):
                inside = True
            elif text.startswith(end) and inside:
                inside = False
                seen_end = True
                break
            continue
        if not inside or record["pid"] in marker_pids or record.get("app") == MARKER_APP:
            continue
        if app is not None and record.get("app") is not None and record["app"] != app:
            continue
        if record["level"] in ("ERROR", "FATAL"):
            message = re.sub(r"\d+", "N", text)[:120]
            result["errors"][message] += 1
            if "statement timeout" in text:
                result["statement_timeouts"] += 1
        if record["ms"] is None:
            continue
        result["db_ms"] += record["ms"]
        result["by_kind_ms"][record["kind"]] += record["ms"]
        result["lines"] += 1
        result["pids"].add(record["pid"])
        result["first_ts"] = result["first_ts"] or record["ts"]
        result["last_ts"] = record["ts"]
        if record["kind"] in ("statement", "execute"):
            result["statements"] += 1
        if "activity_minutes" in text:
            result["analysis_sql_ms"] += record["ms"]
        if windows:
            epoch = log_epoch(record["ts"])
            for name, (start, stop) in windows.items():
                if epoch is not None and start <= epoch <= stop:
                    split[name]["db_ms"] += record["ms"]
                    split[name]["statements"] += record["kind"] in ("statement", "execute")
    for item in split.values():
        item["db_ms"] = round(item["db_ms"], 1)
    result["windows"] = split
    result["complete"] = seen_end
    result["db_ms"] = round(result["db_ms"], 1)
    result["analysis_sql_ms"] = round(result["analysis_sql_ms"], 1)
    result["by_kind_ms"] = {k: round(v, 1) for k, v in result["by_kind_ms"].items()}
    result["backends"] = len(result.pop("pids"))
    result["errors"] = dict(result["errors"])
    return result


# --------------------------------------------------------------------------- k6 records
def load_requests(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    records = []
    with opener(path, "rt") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("t") == "req":
                records.append(record)
    return records


def _group(records: list[dict], with_latency: bool) -> dict:
    statuses = Counter(str(r["st"]) for r in records)
    codes = Counter(r["code"] for r in records if r["code"])
    caches = Counter(r["cache"] or "none" for r in records)
    group: dict[str, Any] = {"requests": len(records), "status": dict(statuses),
                             "error_codes": dict(codes), "x_cache": dict(caches),
                             "expected_outcome": sum(1 for r in records if r.get("ok"))}
    if with_latency and records:
        start = min(r["ts"] for r in records)
        stop = max(r["ts"] + r["dur"] for r in records)
        window = max((stop - start) / 1000, 1e-9)
        ok = [r for r in records if r["st"] == 200]
        group.update({
            "window_s": round(window, 3),
            "throughput_rps": round(len(records) / window, 3),
            "throughput_200_rps": round(len(ok) / window, 3),
            "latency_ms": summarize(r["dur"] for r in records),
            "latency_200_ms": summarize(r["dur"] for r in ok),
            "server_db_ms": summarize(r["db"] for r in records),
            "server_app_ms": summarize(r["app"] for r in records),
        })
        total = sum(r["dur"] for r in records)
        db_total = sum(r["db"] or 0 for r in records)
        group["server_db_share_of_latency"] = round(db_total / total, 4) if total else None
    return group


def analyze_requests(records: list[dict], *, with_latency: bool) -> dict:
    """Per-scenario statistics from the k6 per-request records (phase "main")."""
    main = [r for r in records if r.get("ph") == "main"]
    setup = [r for r in records if r.get("ph") == "setup"]
    analysis: dict[str, Any] = {
        "timed": with_latency,
        "setup": {"requests": len(setup),
                  "status": dict(Counter(str(r["st"]) for r in setup)),
                  "error_codes": dict(Counter(r["code"] for r in setup if r["code"]))},
        "overall": _group(main, with_latency),
        "by_endpoint": {}, "by_endpoint_cache": {}, "by_kind": {},
    }
    for key, field in (("by_endpoint", "ep"), ("by_kind", "kind")):
        for name in sorted({r[field] for r in main}):
            analysis[key][name] = _group([r for r in main if r[field] == name], with_latency)
    for ep, cache in sorted({(r["ep"], r["cache"] or "none") for r in main}):
        analysis["by_endpoint_cache"][f"{ep}|{cache}"] = _group(
            [r for r in main if r["ep"] == ep and (r["cache"] or "none") == cache], with_latency)
    return analysis


def strip_latency(analysis: dict) -> dict:
    """A functional record: counts, statuses, codes and cache outcomes only."""
    keep = ("requests", "status", "error_codes", "x_cache", "expected_outcome")

    def strip(group: dict) -> dict:
        return {k: v for k, v in group.items() if k in keep}

    result = {"timed": False, "setup": analysis["setup"], "overall": strip(analysis["overall"])}
    for key in ("by_endpoint", "by_endpoint_cache", "by_kind"):
        result[key] = {name: strip(group) for name, group in analysis[key].items()}
    return result


# --------------------------------------------------------------------------- environment
def host_snapshot() -> dict:
    def sysctl(name: str) -> str | None:
        result = run(["sysctl", "-n", name], check=False)
        return result.stdout.strip() or None

    info = json.loads(out(["docker", "info", "--format", "{{json .}}"]))
    load1, load5, load15 = os.getloadavg()
    return {
        "date_utc": now_utc(),
        "platform": platform.platform(),
        "machine_model": sysctl("hw.model"),
        "cpu": sysctl("machdep.cpu.brand_string"),
        "host_cpus": os.cpu_count(),
        "host_memory_bytes": int(sysctl("hw.memsize") or 0) or None,
        "load_average_1_5_15": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "docker_server": info.get("ServerVersion"),
        "docker_vm_cpus": info.get("NCPU"),
        "docker_vm_memory_bytes": info.get("MemTotal"),
        "docker_compose": out(["docker", "compose", "version", "--short"], check=False),
        "k6": out([K6_BIN, "version"], check=False),
        "python": sys.version.split()[0],
    }


def git_snapshot() -> dict:
    dirty_image = out(["git", "status", "--porcelain", "--", *IMAGE_INPUTS], check=False)
    return {"commit": out(["git", "rev-parse", "HEAD"], check=False),
            "branch": out(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False),
            "image_inputs_dirty": bool(dirty_image),
            "worktree_dirty": bool(out(["git", "status", "--porcelain"], check=False))}


def docker_ps() -> list[dict]:
    rows = out(["docker", "ps", "--format", "{{json .}}"], check=False).splitlines()
    result = []
    for row in rows:
        data = json.loads(row)
        result.append({"name": data.get("Names"), "image": data.get("Image"),
                       "status": data.get("Status")})
    return result


def safe_env(env_list: list[str]) -> dict:
    """Container environment with every secret-bearing value redacted."""
    result = {}
    for item in env_list:
        key, _, value = item.partition("=")
        if not (key.startswith("ATUS_") or key in ("WEB_CONCURRENCY", "PORT")):
            continue
        result[key] = "<redacted>" if SECRET_KEY.search(key) else value
    return result


LOAD_GENERATORS = re.compile(r"(^|/)(k6|locust|wrk|wrk2|hey|vegeta|oha|bombardier|ab|jmeter|"
                             r"artillery|siege|gatling)$")

# Other ATUS workloads that must not overlap a timed run (the measurement
# agent's precondition): the equivalence replay, test suites, browsers driven
# by Playwright, another k6, the Worker's dev server.
OTHER_WORKLOADS = re.compile(r"scripts/equivalence/replay\.py|pytest|playwright|"
                             r"(^|[\s/])k6(\s|$)|wrangler")
# The project's long-lived databases: never started, stopped or written by
# these tools; a timed run needs them idle (no active query but our own).
PROTECTED_DBS = {"atus-postgres": "atus", "atus-wsb-drill": "postgres",
                 "atus-wsb-build": "postgres", "atus-emu-pg": "atus"}
OWN_DB_APPS = ("atus-api", "atus-lt-marker")


def _protected_db_activity(own_apps: tuple[str, ...]) -> dict:
    """Client backends of each protected database (no query text is recorded)."""
    sql = ("SELECT coalesce(json_agg(json_build_object("
           "'pid', pid, 'user', usename, 'db', datname, 'app', application_name, "
           "'state', state, 'wait', wait_event_type, "
           "'query_age_s', round(extract(epoch FROM now() - query_start)::numeric, 1), "
           "'xact_age_s', round(extract(epoch FROM now() - xact_start)::numeric, 1))), '[]') "
           "FROM pg_stat_activity WHERE backend_type = 'client backend' "
           "AND pid <> pg_backend_pid()")
    result = {}
    for container, user in PROTECTED_DBS.items():
        proc = run(["docker", "exec", "-e", f"PGAPPNAME={MARKER_APP}", container, "psql", "-X",
                    "-q", "-U", user, "-d", "postgres", "-Atc", sql], check=False, timeout=60)
        if proc.returncode != 0:
            result[container] = {"error": f"psql exited {proc.returncode}"}
            continue
        try:
            backends = json.loads(proc.stdout.strip() or "[]")
        except ValueError:
            result[container] = {"error": "unparsable"}
            continue
        busy = [b for b in backends if b.get("state") not in ("idle", None)
                and b.get("app") not in own_apps]
        result[container] = {"client_backends": len(backends),
                             "by_state": dict(Counter(str(b.get("state")) for b in backends)),
                             "active_not_ours": busy}
    return result


def docker_stats_all() -> list[dict]:
    """One `docker stats --no-stream` sample of every running container."""
    rows = []
    for line in out(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                    check=False).splitlines():
        try:
            sample = json.loads(line)
        except ValueError:
            continue
        used, _, _ = (sample.get("MemUsage") or "").partition("/")
        rows.append({"name": sample.get("Name"),
                     "cpu_pct": float((sample.get("CPUPerc") or "0%").rstrip("%") or 0),
                     "mem_mib": None if parse_size(used) is None
                     else round(parse_size(used) / 2 ** 20, 1)})
    return rows


def precondition_snapshot(own_containers: set[str],
                          own_apps: tuple[str, ...] = OWN_DB_APPS) -> dict:
    """The measurement precondition, as evidence.

    ok requires: no process matching OTHER_WORKLOADS (the replay, pytest,
    Playwright, another k6, wrangler); no running atus-* container other than
    the four protected databases and this run's own atus-lt-* containers; and
    no active query in a protected database except ours. Processes are
    recorded by pid, age and a truncated command line only when they match;
    no query text is recorded.
    """
    table: dict[int, tuple[int, str, str]] = {}
    for line in run(["ps", "-Ao", "pid=,ppid=,etime=,command="], check=False).stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    ours = set()  # this process and its ancestors (the shell that started it)
    pid = os.getpid()
    while pid and pid not in ours:
        ours.add(pid)
        pid = table.get(pid, (0, "", ""))[0]
    matches = [{"pid": pid, "etime": etime, "command": command[:120]}
               for pid, (_ppid, etime, command) in sorted(table.items())
               if pid not in ours and OTHER_WORKLOADS.search(command)]
    containers = docker_ps()
    unexpected = [c["name"] for c in containers
                  if c["name"].startswith("atus") and c["name"] not in PROTECTED_DBS
                  and c["name"] not in own_containers and not c["name"].startswith("atus-lt-")]
    other_lt = [c["name"] for c in containers
                if c["name"].startswith("atus-lt-") and c["name"] not in own_containers]
    activity = _protected_db_activity(own_apps)
    busy_dbs = {name: item["active_not_ours"] for name, item in activity.items()
                if item.get("active_not_ours")}
    reasons = []
    if matches:
        reasons.append(f"other workload processes: {[m['pid'] for m in matches]}")
    if unexpected:
        reasons.append(f"other atus containers running: {unexpected}")
    if busy_dbs:
        reasons.append(f"active queries in protected databases: {sorted(busy_dbs)}")
    return {
        "checked_at": now_utc(),
        "ok": not reasons,
        "reasons": reasons,
        "criteria": "no process matching scripts/equivalence/replay.py|pytest|playwright|k6|"
                    "wrangler (other than this run's); no atus-* container besides "
                    f"{sorted(PROTECTED_DBS)} and this run's own; no active query in those "
                    "databases except this run's",
        "workload_processes": matches,
        "other_atus_lt_containers": other_lt,
        "protected_db_activity": activity,
        "load_average_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "docker_ps": containers,
        "docker_stats": docker_stats_all(),
    }


class BackgroundSampler:
    """Every `period_s`, one `docker stats --no-stream` sample of the containers
    that are not ours (other projects' idle services): background noise."""

    def __init__(self, own: set[str], path: Path, period_s: float = 60.0):
        self.own = own
        self.path = path
        self.period_s = period_s
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        with open(self.path, "w") as sink:
            while True:
                now = round(time.time(), 3)
                for row in docker_stats_all():
                    if row["name"] not in self.own:
                        sink.write(json.dumps({"ts": now, **row}) + "\n")
                sink.flush()
                if self.stop_event.wait(self.period_s):
                    return

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)


def summarize_background(path: Path) -> dict:
    per: dict[str, list[float]] = {}
    if not path.exists():
        return {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        per.setdefault(row["name"], []).append(row["cpu_pct"])
    return {"samples_per_container": max((len(v) for v in per.values()), default=0),
            "cpu_pct_max": {name: max(v) for name, v in sorted(per.items())},
            "cpu_pct_sum_of_means": round(sum(sum(v) / len(v) for v in per.values()), 2)}


def ran_alone_check(own_projects: set[str], own_containers: set[str]) -> dict:
    """Is this the only load workload? Checked before (and after) a run.

    "yes" requires: no other load generator process on the host, no other
    atus-lt-* compose project, no other container above 5% of one core in
    a `docker stats` sample, and the measurement precondition
    (precondition_snapshot: no other ATUS workload process or container, the
    protected databases idle). Other containers that are merely running (idle
    databases of other projects) and host apps are recorded, not disqualifying.
    """
    reasons = []
    procs = run(["ps", "-Ao", "pid=,pcpu=,comm="], check=False).stdout.splitlines()
    generators, top = [], []
    for line in procs:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, pcpu, comm = parts
        if LOAD_GENERATORS.search(comm.strip()) and int(pid) != os.getpid():
            generators.append({"pid": int(pid), "comm": os.path.basename(comm.strip())})
        try:
            top.append((float(pcpu), os.path.basename(comm.strip())))
        except ValueError:
            pass
    if generators:
        reasons.append(f"other load generator processes: {generators}")
    rows = out(["docker", "ps", "--format",
                '{{.Names}}\t{{.Label "com.docker.compose.project"}}'], check=False).splitlines()
    others = sorted({project for name, _, project in (row.partition("\t") for row in rows)
                     if project.startswith("atus-lt-") and project not in own_projects
                     and name not in own_containers})
    if others:
        reasons.append(f"other load-test stacks running: {others}")
    samples: list[dict[str, float]] = []
    for attempt in range(2):
        if attempt:
            time.sleep(3)
        cpu_by_name = {}
        stats = out(["docker", "stats", "--no-stream", "--format", "{{json .}}"], check=False)
        for line in stats.splitlines():
            try:
                sample = json.loads(line)
            except ValueError:
                continue
            cpu_by_name[sample.get("Name")] = float(
                (sample.get("CPUPerc") or "0%").rstrip("%") or 0)
        samples.append(cpu_by_name)
    busy = [{"name": name, "cpu_pct": [samples[0][name], samples[1].get(name)]}
            for name in samples[0]
            if name not in own_containers and samples[0][name] > 5.0
            and (samples[1].get(name) or 0) > 5.0]
    blips = [{"name": name, "cpu_pct": [cpu, samples[1].get(name)]}
             for name, cpu in samples[0].items()
             if name not in own_containers and max(cpu, samples[1].get(name) or 0) > 5.0
             and name not in {b["name"] for b in busy}]
    if busy:
        reasons.append(f"other containers busy (> 5% of a core in two samples 3 s apart): "
                       f"{busy}")
    precondition = precondition_snapshot(own_containers)
    reasons.extend(f"precondition: {reason}" for reason in precondition["reasons"])
    top.sort(reverse=True)
    return {
        "checked_at": now_utc(),
        "ran_alone": "no" if reasons else "yes",
        "reasons": reasons,
        "criteria": "no other load generator process; no other atus-lt-* stack; no other "
                    "container above 5% of one core in two docker stats samples 3 s apart; "
                    "the measurement precondition (see precondition.criteria)",
        "precondition": precondition,
        "other_container_blips": blips,
        "load_average_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "top_host_processes_pcpu": [{"pcpu": p, "comm": c} for p, c in top[:8]],
        "running_containers": [row["name"] for row in docker_ps()],
    }


def secret_scan(root: Path, secrets: Iterable[str]) -> list[str]:
    """Files under `root` that contain any secret value (plain or gzipped)."""
    values = [s for s in secrets if s and len(s) >= 8]
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".env"):
            continue
        try:
            data = gzip.open(path).read() if path.suffix == ".gz" else path.read_bytes()
        except OSError:
            continue
        if any(value.encode() in data for value in values):
            hits.append(str(path))
    return hits
