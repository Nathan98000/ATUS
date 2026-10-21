#!/usr/bin/env python3
"""Run the ATUS load tests against the production-mode stack (brief §13).

Every run is local: the production compose stack (docker-compose.prod.yml)
with the free-tier emulation override (docker-compose.emulation.yml), the real
serving dump restored into its database, and k6 on the host sending the
Worker's headers straight to the API. No hosting provider is ever contacted.

Commands (all take --stage NAME and --out-root DIR; raw output goes to
DIR/NAME/, which must be outside the repository or in the ignored
loadtests/out/):

  up        build the API image from this checkout, start the stack, restore
            the serving dump once (VACUUM ANALYZE included), apply a DB profile
  profile   up + a list of scenarios (+ optional cold starts) into one bundle
  scenario  one scenario (or a --sweep of it) against the running stack
  baseline  the laptop baseline: the same image, unconstrained, against the
            FULL database on 127.0.0.1:5434 (read-only transactions)
  export    copy a bundle's compact results into loadtests/results/<name>/
  status    what the stage has running
  down      stop the stack (--purge also deletes its database volume)

DB profiles (docs/adr/ADR-011-hosting.md; Neon: 1 CU = 1 vCPU + 4 GB):
  0.25cu    0.25 vCPU / 1 GB   Neon Free's autoscaling floor
  2cu       2 vCPU / 7 GB      Neon Free's ceiling (8 GB by Neon's ratio; the
                               Docker VM has 7.75 GiB, so 7 GB is the cap)
  laptop    unconstrained      the Docker VM (10 vCPU / 7.75 GiB here)
The API always runs as on Render Free (0.1 CPU / 512 MB, no swap) unless
--api-cpus/--api-memory say otherwise.

Load-run settings that differ from production, and why:
  ATUS_API_DAILY_ANALYSIS_CAP=10000000  the 600/day backstop would end a
      capacity run after 600 analyses; the cap itself is functionally tested in
      tests/unit/api/test_protection.py and the integration suite.
  Postgres log_min_duration_statement=0 and log_line_prefix with %a, set on
      the stack's own throwaway database container (never ALTER SYSTEM, never
      on a shared server): every statement's duration is logged, and DB time is
      attributed between marker statements as in the audit
      (docs/evidence/phase5/01-audit-timed.md). Logging every statement costs
      the database some CPU; the audit's numbers were taken the same way.
  The API's Docker HEALTHCHECK is disabled: it starts a Python process inside
      the 0.1-CPU container every 30 s; Render ignores it and probes /health
      over HTTP, which run.py also does.
  Optional: --max-parallel-workers (ATUS_API_MAX_PARALLEL_WORKERS),
      --statement-timeout-ms (to observe latencies beyond the production 85 s),
      --api-env ATUS_API_*=VALUE (for example the lane sizes when calibrating).

Examples:
  python loadtests/run.py profile --stage s1 --out-root /tmp/lt --profile 0.25cu \\
      --scenarios metadata,cached,uncached,worstcase,mix --cold-start 5
  python loadtests/run.py scenario --stage s1 --out-root /tmp/lt uncached \\
      --set EXECUTOR=duration --set DURATION=120s --sweep VUS=1,2,4,8
  python loadtests/run.py baseline --stage s1 --out-root /tmp/lt --scenarios worstcase
  python loadtests/run.py profile ... --dry-run    # tiny scale, functional only
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402

PROFILES = {
    "0.25cu": {"db_cpus": 0.25, "db_memory": "1g",
               "label": "Neon Free floor 0.25 CU: 0.25 vCPU / 1 GB"},
    "2cu": {"db_cpus": 2.0, "db_memory": "7g",
            "label": "Neon Free ceiling 2 CU: 2 vCPU / 7 GB (Neon's ratio gives 8 GB; the "
                     "Docker VM has 7.75 GiB)"},
    "laptop": {"db_cpus": 0, "db_memory": 0,
               "label": "unconstrained: the Docker VM's CPUs and memory"},
}
SCENARIOS = {"metadata": "metadata.js", "cached": "cached.js", "uncached": "uncached.js",
             "worstcase": "worstcase.js", "mix": "mix.js"}
DEFAULT_SCENARIOS = "metadata,cached,uncached,worstcase,mix"
# Tiny functional runs: one VU, a few iterations; every scenario's code path.
DRY_RUN = {
    "metadata": {"EXECUTOR": "iterations", "VUS": "1", "ITERATIONS": "3"},
    "cached": {"EXECUTOR": "iterations", "VUS": "1", "ITERATIONS": "4", "WARM_SET": "starters"},
    "uncached": {"EXECUTOR": "iterations", "VUS": "1", "ITERATIONS": "3"},
    "worstcase": {"EXECUTOR": "iterations", "VUS": "1", "ITERATIONS": "6", "WORST_SET": "all"},
    "mix": {"EXECUTOR": "iterations", "VUS": "1", "ITERATIONS": "4", "THINK_S": "0.2"},
}
LOAD_CAP = 10_000_000
DEFAULT_DUMP = os.environ.get("ATUS_LT_DUMP", "")
API_ENV_KEY = re.compile(r"^ATUS_API_[A-Z0-9_]+$")
K6_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
FULL_DB = {"container": "atus-postgres", "host_port": 5434, "user": "atus", "db": "atus"}


# =========================================================================== stage
class Stage:
    """A named stage: its private files, state and the compose project."""

    def __init__(self, out_root: Path, name: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
            raise lt.LoadTestError("--stage must be lowercase letters, digits, '-' or '_'")
        out_root = out_root.resolve()
        if lt.REPO_ROOT in out_root.parents or out_root == lt.REPO_ROOT:
            if not (out_root == lt.LT_DIR / "out" or (lt.LT_DIR / "out") in out_root.parents):
                raise lt.LoadTestError("--out-root inside the repository must be loadtests/out "
                                       "(ignored by git): raw outputs are never committed")
        self.name = name
        self.dir = out_root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.state_path = self.dir / "state.json"
        self.env_path = self.dir / "stack.env"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save(self) -> None:
        lt.write_json(self.state_path, self.state)

    @contextmanager
    def lock(self):
        handle = open(self.dir / ".lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise lt.LoadTestError(
                f"stage {self.name} is busy (another run.py holds its lock)") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def secrets(self) -> list[str]:
        values = []
        for path in (self.env_path, self.dir / "cold.env", self.dir / "baseline.env"):
            if path.exists():
                for key, value in lt.read_env_file(path).items():
                    if lt.SECRET_KEY.search(key) and value:
                        values.append(value)
                        if key == "ATUS_DATABASE_URL" and "@" in value:
                            values.append(value.split("://", 1)[1].split("@", 1)[0].split(":")[-1])
        return values


def image_tag() -> tuple[str, str]:
    sha = lt.out(["git", "rev-parse", "HEAD"])
    dirty = lt.git_snapshot()["image_inputs_dirty"]
    return f"atus-api:lt-{sha[:12]}{'-dirty' if dirty else ''}", sha


def api_version() -> str:
    text = (lt.REPO_ROOT / "src" / "atus_pipeline" / "__init__.py").read_text()
    match = re.search(r'^__version__ = "(.+)"$', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


# =========================================================================== API settings
def api_settings(args) -> dict:
    env = {"ATUS_API_DAILY_ANALYSIS_CAP": str(args.daily_cap)}
    if args.max_parallel_workers is not None:
        env["ATUS_API_MAX_PARALLEL_WORKERS"] = str(args.max_parallel_workers)
    if args.statement_timeout_ms is not None:
        env["ATUS_API_STATEMENT_TIMEOUT_MS"] = str(args.statement_timeout_ms)
    for item in args.api_env or []:
        key, _, value = item.partition("=")
        if not API_ENV_KEY.match(key) or lt.SECRET_KEY.search(key):
            raise lt.LoadTestError(f"--api-env takes ATUS_API_* settings only, not {key}")
        env[key] = value
    return {"cpus": args.api_cpus, "memory": args.api_memory, "env": env}


# =========================================================================== targets
class Target:
    """What a scenario runs against: an API container and its database container."""

    kind = "?"
    app_filter: str | None = "atus-api"
    profile = "?"

    def __init__(self, stage: Stage):
        self.stage = stage

    # subclasses: api_container, db_container, base_url, edge_secret, ops_token,
    # restart_api, mark, describe
    def headers(self, ip: str = "198.18.255.254") -> dict:
        return {"X-ATUS-Edge-Auth": self.edge_secret, "X-ATUS-Client-IP": ip,
                "Accept": "application/json"}

    def wait_ready(self, expected_version: str | None) -> dict:
        t_health, _, seen = lt.wait_http(f"{self.base_url}/health", deadline_s=300)
        if t_health is None:
            raise lt.LoadTestError(f"the API never answered /health (statuses seen: {seen})")
        status, headers, _ = lt.http_get(f"{self.base_url}/api/v1/meta", self.headers(), 120)
        if status != 200:
            raise lt.LoadTestError(f"/api/v1/meta answered {status}")
        served = {k.lower(): v for k, v in headers.items()}.get("x-data-version")
        if expected_version and served != expected_version:
            raise lt.LoadTestError(f"the API serves data version {served}, "
                                   f"expected {expected_version}")
        return {"data_version": served}

    def api_metrics(self) -> str:
        status, _, body = lt.http_get(f"{self.base_url}/metrics",
                                      {"Authorization": f"Bearer {self.ops_token}"}, 30)
        return body.decode(errors="replace") if status == 200 else f"# /metrics answered {status}\n"

    def pg_log(self, since: float, tag: str, out_path: Path,
               windows: dict[str, tuple[float, float]] | None = None) -> dict:
        """Extract the database log between the tag's markers and attribute DB time."""
        proc = subprocess.Popen(["docker", "logs", "--since", lt.rfc3339(since),
                                 self.db_container], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        begin, end = f"MARK {tag} BEGIN", f"MARK {tag} END"

        def lines(sink):
            writing = False
            assert proc.stdout
            for line in proc.stdout:
                if begin in line:
                    writing = True
                if writing:
                    sink.write(line)
                yield line.rstrip("\n")
                if end in line and writing:
                    return

        with gzip.open(out_path, "wt") as sink:
            result = lt.attribute_db_time(lt.parse_pg_log(lines(sink)), tag,
                                          app=self.app_filter, windows=windows)
        proc.kill()
        proc.wait()
        return result

    def api_log(self, since: float, out_path: Path) -> None:
        result = lt.run(["docker", "logs", "--since", lt.rfc3339(since), self.api_container],
                        check=False)
        with gzip.open(out_path, "wt") as sink:
            sink.write(result.stdout)
            sink.write(result.stderr)


class StackTarget(Target):
    kind = "stack"

    def __init__(self, stage: Stage, args):
        super().__init__(stage)
        self.project = args.project or f"atus-lt-{stage.name}"
        self.api_port = args.api_port
        self.db_port = args.db_port
        self.base_url = f"http://127.0.0.1:{self.api_port}"
        self.override = stage.dir / "lt-override.yml"

    # ---- compose
    def compose(self, *args: str, profiles: tuple = (), files: tuple | None = None,
                check: bool = True, timeout: float | None = None):
        cmd = ["docker", "compose", "-p", self.project, "--env-file", str(self.stage.env_path)]
        for path in files or ("docker-compose.prod.yml", "docker-compose.emulation.yml",
                              str(self.override)):
            cmd += ["-f", path]
        for profile in profiles:
            cmd += ["--profile", profile]
        return lt.run([*cmd, *args], check=check, timeout=timeout)

    @property
    def api_container(self) -> str:
        return f"{self.project}-api-1"

    @property
    def db_container(self) -> str:
        return f"{self.project}-db-1"

    @property
    def env(self) -> dict:
        return lt.read_env_file(self.stage.env_path)

    @property
    def edge_secret(self) -> str:
        return self.env["ATUS_STACK_EDGE_SECRET"]

    @property
    def ops_token(self) -> str:
        return self.env["ATUS_STACK_OPS_TOKEN"]

    # ---- preparation
    def write_env(self, dump: Path, data_version: str, tag: str, sha: str) -> None:
        existing = lt.read_env_file(self.stage.env_path) if self.stage.env_path.exists() else {}
        values = {}
        for key in ("ATUS_STACK_PG_SUPERUSER_PASSWORD", "ATUS_STACK_OWNER_PASSWORD",
                    "ATUS_STACK_API_DB_PASSWORD", "ATUS_STACK_EDGE_SECRET",
                    "ATUS_STACK_OPS_TOKEN"):
            values[key] = existing.get(key) or secrets.token_urlsafe(32)
        values.update({
            "ATUS_STACK_DB_PORT": str(self.db_port),
            "ATUS_STACK_DUMP": str(dump),
            "ATUS_STACK_IMAGE": tag,
            "ATUS_STACK_PULL_POLICY": "missing",
            "ATUS_BUILD_SHA": sha,
            "ATUS_VERSION": api_version(),
            "ATUS_EXPECTED_DATA_VERSION": data_version,
        })
        lt.write_private(self.stage.env_path, "".join(f"{k}={v}\n" for k, v in values.items())
                         + "# written by loadtests/run.py; throwaway local secrets\n")

    def base_db_command(self) -> list[str]:
        result = self.compose("config", "--format", "json",
                              files=("docker-compose.prod.yml", "docker-compose.emulation.yml"))
        return json.loads(result.stdout)["services"]["db"]["command"]

    def write_override(self, profile: str, api: dict, *, pg_log: bool) -> None:
        prof = PROFILES[profile]
        command = self.base_db_command()
        if pg_log:
            command += ["-c", "log_min_duration_statement=0",
                        "-c", "log_line_prefix=%m [%p] [%a] "]
        memory = api["memory"] or 0
        doc = {"services": {
            "api": {"cpus": api["cpus"], "mem_limit": memory, "memswap_limit": memory,
                    "ports": [f"127.0.0.1:{self.api_port}:10000"],
                    "healthcheck": {"disable": True},
                    "environment": api["env"]},
            "db": {"cpus": prof["db_cpus"], "mem_limit": prof["db_memory"],
                   "memswap_limit": prof["db_memory"], "command": command},
        }}
        self.override.write_text("# generated by loadtests/run.py; JSON is YAML\n"
                                 + json.dumps(doc, indent=1) + "\n")

    def volume_exists(self) -> bool:
        return lt.run(["docker", "volume", "inspect", f"{self.project}_pgdata"],
                      check=False).returncode == 0

    def ensure(self, args, profile: str, api: dict) -> None:
        """Image, stack, one restore, then the requested profile."""
        dump = Path(args.dump or self.stage.state.get("dump") or "").resolve()
        if not dump.is_file():
            raise lt.LoadTestError("--dump must name the serving dump (or set ATUS_LT_DUMP)")
        manifest_path = dump.with_name(dump.name.removesuffix(".dump") + ".manifest.json")
        sha_path = dump.with_name(dump.name + ".sha256")
        if self.stage.state.get("dump") != str(dump) or not self.stage.state.get("dump_verified"):
            print(f"== verify {dump.name} against its .sha256 and manifest")
            actual = lt.sha256_file(dump)
            listed = sha_path.read_text().split()[0]
            manifest = json.loads(manifest_path.read_text())
            if not (actual == listed == manifest["artifact"]["sha256"]):
                raise lt.LoadTestError("the dump's sha256 does not match its .sha256/manifest")
            self.stage.state.update({"dump": str(dump), "dump_sha256": actual,
                                     "dump_verified": True,
                                     "data_version": manifest["data_version"],
                                     "manifest": {k: manifest.get(k) for k in (
                                         "build_id", "content_hash", "data_version",
                                         "release", "analytics_version", "sizes")}})
            self.stage.save()
        data_version = self.stage.state["data_version"]
        tag, sha = image_tag()
        if self.volume_exists() and not self.stage.env_path.exists():
            raise lt.LoadTestError(f"volume {self.project}_pgdata exists without this stage's "
                                   "env file; run `down --purge` first")
        self.write_env(dump, data_version, tag, sha)
        if args.rebuild or lt.run(["docker", "image", "inspect", tag], check=False).returncode:
            # `docker build`, not `compose build`: compose would label the image with
            # this project, and containers started from it outside compose (cold
            # starts, the baseline) would inherit that label.
            print(f"== build {tag} from this checkout")
            lt.run(["docker", "build", "-t", tag, "--build-arg", f"ATUS_BUILD_SHA={sha}",
                    "--build-arg", f"ATUS_VERSION={api_version()}", "."], timeout=3600)
        self.stage.state.update({"project": self.project, "image": tag, "api_port": self.api_port,
                                 "db_port": self.db_port})
        self.stage.save()
        if not self.stage.state.get("restored") or not self.volume_exists():
            # Restore unconstrained and without statement logging (roles.sql sets
            # a password; it must never reach a statement log).
            print("== restore the serving dump (pg_restore --no-owner --no-acl, VACUUM ANALYZE)")
            self.write_override("laptop", api, pg_log=False)
            self.compose("up", "-d", "--wait", "--wait-timeout", "300", "db")
            started = time.time()
            self.compose("run", "--rm", "--no-deps", "restore", profiles=("dump",), timeout=7200)
            self.compose("run", "--rm", "--no-deps", "roles", timeout=600)
            size = self.psql("SELECT sum(pg_database_size(datname)) FROM pg_database")
            self.stage.state.update({"restored": True, "restored_at": lt.now_utc(),
                                     "restore_wall_s_untimed": round(time.time() - started, 1),
                                     "cluster_bytes_after_restore": int(size)})
            self.stage.save()
        self.apply_profile(profile, api, pg_log=not args.no_pg_log)

    def apply_profile(self, profile: str, api: dict, *, pg_log: bool) -> None:
        print(f"== DB profile {profile}: {PROFILES[profile]['label']}")
        self.write_override(profile, api, pg_log=pg_log)
        self.compose("up", "-d", "--no-deps", "--force-recreate", "--wait",
                     "--wait-timeout", "300", "db")
        self.profile = profile
        self.stage.state.update({"profile": profile, "api": api, "pg_log": pg_log})
        self.stage.save()
        self.restart_api()

    def restart_api(self) -> None:
        self.compose("up", "-d", "--no-deps", "--force-recreate", "--no-build", "api")
        self.wait_ready(self.stage.state.get("data_version"))

    def psql(self, sql: str, *, options: str = "") -> str:
        cmd = ["docker", "exec", "-e", f"PGAPPNAME={lt.MARKER_APP}"]
        if options:
            cmd += ["-e", f"PGOPTIONS={options}"]
        cmd += [self.db_container, "psql", "-X", "-q", "-U", "neondb_owner", "-d", "atus",
                "-Atc", sql]
        return lt.out(cmd)

    def mark(self, text: str) -> None:
        self.psql(f"SELECT 'MARK {text}'")

    def describe(self) -> dict:
        settings = self.psql(
            "SELECT string_agg(name || '=' || current_setting(name), '; ' ORDER BY name) "
            "FROM pg_settings WHERE name IN ('server_version', 'shared_buffers', 'max_connections',"
            " 'max_worker_processes', 'max_parallel_workers', 'max_parallel_workers_per_gather', "
            "'jit', 'work_mem', 'effective_cache_size', 'log_min_duration_statement')")
        env = lt.docker_inspect(self.api_container)["Config"]["Env"]
        return {
            "target": "stack", "project": self.project, "profile": self.stage.state.get("profile"),
            "profile_label": PROFILES[self.stage.state["profile"]]["label"],
            "api_container_limits": lt.container_limits(self.api_container),
            "db_container_limits": lt.container_limits(self.db_container),
            "db_settings": settings,
            "cluster_bytes": int(self.psql(
                "SELECT sum(pg_database_size(datname)) FROM pg_database")),
            "api_environment": lt.safe_env(env),
            "image": lt.image_info(self.stage.state["image"]),
            "dump": {k: self.stage.state.get(k) for k in ("dump", "dump_sha256", "dump_verified",
                                                           "manifest")},
            "expected_data_version": self.stage.state.get("data_version"),
            "pg_statement_logging": self.stage.state.get("pg_log"),
        }


class BaselineTarget(Target):
    """The laptop baseline: the API image unconstrained against the full DB."""

    kind = "baseline"
    app_filter = None  # the full DB's log prefix has no application name
    profile = "laptop-fulldb"

    def __init__(self, stage: Stage, args):
        super().__init__(stage)
        self.port = args.baseline_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.api_container = f"atus-lt-{stage.name}-baseline-api"
        self.db_container = FULL_DB["container"]
        self.env_path = stage.dir / "baseline.env"
        self.api = api_settings(args)
        self.pg_log_enabled = not args.no_pg_log
        self.tag = None

    @property
    def edge_secret(self) -> str:
        return lt.read_env_file(self.env_path)["ATUS_EDGE_SECRETS"]

    @property
    def ops_token(self) -> str:
        return lt.read_env_file(self.env_path)["ATUS_OPS_TOKEN"]

    def full_db_psql(self, sql: str, *, log: bool = False) -> str:
        options = "-c default_transaction_read_only=on"
        if log:
            options += " -c log_min_duration_statement=0"
        return lt.out(["docker", "exec", "-e", f"PGAPPNAME={lt.MARKER_APP}",
                       "-e", f"PGOPTIONS={options}", self.db_container, "psql", "-X", "-q",
                       "-U", FULL_DB["user"], "-d", FULL_DB["db"], "-Atc", sql])

    def write_env(self, expected_version: str | None, superuser: bool) -> None:
        existing = lt.read_env_file(self.env_path) if self.env_path.exists() else {}
        options = "-c default_transaction_read_only=on"
        if self.pg_log_enabled and superuser:
            options += " -c log_min_duration_statement=0"
        url = (f"postgresql://{FULL_DB['user']}:{FULL_DB['user']}@host.docker.internal:"
               f"{FULL_DB['host_port']}/{FULL_DB['db']}?options={quote(options)}")
        values = {
            "ATUS_ENV": "production", "ATUS_DATABASE_URL": url,
            "ATUS_EDGE_SECRETS": existing.get("ATUS_EDGE_SECRETS") or secrets.token_urlsafe(32),
            "ATUS_OPS_TOKEN": existing.get("ATUS_OPS_TOKEN") or secrets.token_urlsafe(32),
            "WEB_CONCURRENCY": "1", "PORT": "10000",
            **self.api["env"],
        }
        if expected_version:
            values["ATUS_EXPECTED_DATA_VERSION"] = expected_version
        lt.write_private(self.env_path, "".join(f"{k}={v}\n" for k, v in values.items()))

    def ensure(self, args) -> None:
        state = lt.docker_inspect(self.db_container)["State"]
        if not state.get("Running"):
            raise lt.LoadTestError(f"{self.db_container} is not running (it is never started "
                                   "or restarted by these tools)")
        self.superuser = self.full_db_psql(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user") == "t"
        self.tag, sha = image_tag()
        if lt.run(["docker", "image", "inspect", self.tag], check=False).returncode:
            raise lt.LoadTestError(f"image {self.tag} is missing: run `up` (or `profile`) once "
                                   "to build it")
        self.write_env(None, self.superuser)
        self._start()
        version = self.wait_ready(None)["data_version"]
        self.write_env(version, self.superuser)  # pin it, as production does
        self.data_version = version
        self.restart_api()

    def _start(self) -> None:
        lt.run(["docker", "rm", "-f", self.api_container], check=False)
        cmd = ["docker", "run", "-d", "--name", self.api_container,
               "--label", f"atus-lt-stage={self.stage.name}",
               "-p", f"127.0.0.1:{self.port}:10000", "--env-file", str(self.env_path),
               "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--no-healthcheck"]
        if self.api["cpus"]:
            cmd += ["--cpus", str(self.api["cpus"])]
        if self.api["memory"]:
            cmd += ["--memory", self.api["memory"], "--memory-swap", self.api["memory"]]
        lt.run([*cmd, self.tag])

    def restart_api(self) -> None:
        self._start()
        self.wait_ready(getattr(self, "data_version", None))

    def mark(self, text: str) -> None:
        self.full_db_psql(f"SELECT 'MARK {text}'", log=True)

    def stop(self) -> None:
        lt.run(["docker", "rm", "-f", self.api_container], check=False)

    def describe(self) -> dict:
        env = lt.docker_inspect(self.api_container)["Config"]["Env"]
        settings = self.full_db_psql(
            "SELECT string_agg(name || '=' || current_setting(name), '; ' ORDER BY name) "
            "FROM pg_settings WHERE name IN ('server_version', 'shared_buffers', 'max_connections',"
            " 'max_worker_processes', 'max_parallel_workers', 'max_parallel_workers_per_gather', "
            "'jit', 'work_mem', 'effective_cache_size')")
        return {
            "target": "baseline", "profile": self.profile,
            "profile_label": "laptop baseline: the API image (limits below) against the FULL "
                             "database atus-postgres:5434; every API session read-only "
                             "(default_transaction_read_only=on, and the API's own "
                             "BEGIN READ ONLY)",
            "api_container_limits": lt.container_limits(self.api_container),
            "db_container_limits": lt.container_limits(self.db_container),
            "db_settings": settings,
            "db_note": "server-level settings of the full DB; the API sets jit and its timeouts "
                       "per transaction",
            "api_environment": lt.safe_env(env),
            "image": lt.image_info(self.tag),
            "served_data_version": getattr(self, "data_version", None),
            "pg_statement_logging": bool(self.pg_log_enabled and self.superuser),
        }


# =========================================================================== scenarios
def k6_env(target: Target, sdir: Path, label: str, sets: dict) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "K6_NO_USAGE_REPORT": "true",
           "BASE_URL": target.base_url, "SUMMARY_JSON": str(sdir / "summary.json"),
           "SCENARIO_LABEL": label, "ATUS_LT_EDGE_SECRET": target.edge_secret}
    env.update(sets)
    return env


def dry_env(sets: dict, dry_run: bool) -> dict:
    return {**sets, "DRY_RUN": "1"} if dry_run else sets


def parse_sets(items: list[str] | None) -> dict:
    sets = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not K6_ENV_KEY.match(key) or "SECRET" in key or key in (
                "BASE_URL", "SUMMARY_JSON"):
            raise lt.LoadTestError(f"--set takes KEY=VALUE for the k6 scripts, not {item!r}")
        sets[key] = value
    return sets


def run_scenario(target: Target, bundle: Path, name: str, label: str, sets: dict,
                 *, timed: bool, restart: bool) -> dict:
    sdir = bundle / label
    sdir.mkdir(parents=True)
    print(f"== scenario {label} ({target.profile})", flush=True)
    if restart:
        target.restart_api()  # a fresh process: empty result cache, snapshot read by /meta
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{bundle.name}.{label}")
    before = {"api": lt.cgroup_snapshot(target.api_container, proc=True),
              "db": lt.cgroup_snapshot(target.db_container)}
    (sdir / "api_metrics_before.txt").write_text(target.api_metrics())
    sampler = lt.StatsSampler([target.api_container, target.db_container],
                              sdir / "docker_stats.jsonl")
    sampler.start()
    time.sleep(2)
    since = time.time() - 2
    target.mark(f"{tag} BEGIN")
    t0 = time.time()
    with open(sdir / "k6.log", "w") as log:
        proc = subprocess.run(
            [lt.K6_BIN, "run", "--no-usage-report", "--quiet", "--log-format=raw",
             f"--console-output={sdir / 'requests.jsonl'}", str(lt.K6_DIR / SCENARIOS[name])],
            env=k6_env(target, sdir, label, dry_env(sets, not timed)), stdout=log,
            stderr=subprocess.STDOUT,
            cwd=lt.LT_DIR)
    t1 = time.time()
    target.mark(f"{tag} END")
    time.sleep(2)
    sampler.stop()
    after = {"api": lt.cgroup_snapshot(target.api_container, proc=True),
             "db": lt.cgroup_snapshot(target.db_container)}
    (sdir / "api_metrics_after.txt").write_text(target.api_metrics())
    records = lt.load_requests(sdir / "requests.jsonl") if (sdir / "requests.jsonl").exists() \
        else []
    windows = {}
    for phase in ("setup", "main"):
        chosen = [r for r in records if r.get("ph") == phase]
        if chosen:
            windows[phase] = (min(r["ts"] for r in chosen) / 1000,
                              max(r["ts"] + r["dur"] for r in chosen) / 1000 + 0.25)
    if "setup" in windows and "main" in windows:  # no statement counted twice
        start, stop = windows["setup"]
        windows["setup"] = (start, min(stop, windows["main"][0]))
    db_time = target.pg_log(since, tag, sdir / "pg_log.txt.gz", windows)
    target.api_log(since, sdir / "api_log.txt.gz")
    analysis = lt.analyze_requests(records, with_latency=timed)
    lt.write_json(sdir / "analysis.json", analysis)
    lt.write_json(sdir / "db_time.json", db_time)
    lt.write_json(sdir / "cgroup.json", {
        "before": before, "after": after,
        "api_delta": lt.cgroup_delta(before["api"], after["api"]),
        "db_delta": lt.cgroup_delta(before["db"], after["db"])})
    stats = lt.summarize_stats(sdir / "docker_stats.jsonl", t0, t1)
    lt.write_json(sdir / "stats_summary.json", stats)
    if (sdir / "requests.jsonl").exists():
        lt.gzip_file(sdir / "requests.jsonl")
    lt.gzip_file(sdir / "docker_stats.jsonl")
    record = {"scenario": name, "label": label, "settings": sets, "k6_exit": proc.returncode,
              "started_utc": lt.rfc3339(t0), "wall_s": round(t1 - t0, 1),
              "requests": analysis["overall"]["requests"],
              "status": analysis["overall"]["status"],
              "error_codes": analysis["overall"]["error_codes"],
              "x_cache": analysis["overall"]["x_cache"],
              "db_attribution_complete": db_time.get("complete"),
              "db_statements": db_time.get("statements")}
    if not timed:
        record.pop("wall_s")
    print(f"   k6 exit {proc.returncode}; {record['requests']} requests; status "
          f"{record['status']}; codes {record['error_codes']}; X-Cache {record['x_cache']}; "
          f"DB statements logged {record['db_statements']}", flush=True)
    return record


def scenario_plan(args) -> list[tuple[str, str, dict]]:
    """(scenario, label, k6 settings) for each requested run, sweeps expanded."""
    names = args.scenarios.split(",") if getattr(args, "scenarios", None) else [args.name]
    plan = []
    for name in names:
        name = name.strip()
        if name not in SCENARIOS:
            raise lt.LoadTestError(f"unknown scenario {name!r}; one of {', '.join(SCENARIOS)}")
        sets = {**(DRY_RUN[name] if args.dry_run else {}), **parse_sets(args.set)}
        if args.sweep:
            key, _, values = args.sweep.partition("=")
            if not K6_ENV_KEY.match(key) or not values:
                raise lt.LoadTestError("--sweep takes KEY=v1,v2,...")
            for value in values.split(","):
                plan.append((name, f"{name}-{key.lower()}{value}", {**sets, key: value}))
        else:
            plan.append((name, name, sets))
    return plan


def new_bundle(stage: Stage, profile: str, dry_run: bool) -> Path:
    bundle = stage.dir / f"{lt.stamp()}-{profile}{'-dry' if dry_run else ''}"
    bundle.mkdir()
    return bundle


def run_bundle(target: Target, stage: Stage, args, *, cold_start_runs: int = 0) -> Path:
    timed = not args.dry_run
    bundle = new_bundle(stage, target.profile, args.dry_run)
    # This stage's own stack counts as ours even when idle beside a baseline run;
    # its containers are still checked for CPU use like any other.
    own_projects = {getattr(target, "project", None) or f"atus-lt-{stage.name}"}
    own_containers = {target.api_container, target.db_container}
    meta = {"bundle": bundle.name, "stage": stage.name, "timed": timed,
            "dry_run": args.dry_run, "profile": target.profile,
            "ran_alone_before": lt.ran_alone_check(own_projects, own_containers),
            "host_before": lt.host_snapshot(), "git": lt.git_snapshot(),
            "target": target.describe(), "docker_ps_before": lt.docker_ps(),
            "scenarios": []}
    lt.write_json(bundle / "environment.json", meta)
    print(f"== bundle {bundle} (ran alone: {meta['ran_alone_before']['ran_alone']})")
    if cold_start_runs:
        import cold_start
        meta["cold_start"] = cold_start.run_cold_start(
            stage, runs=cold_start_runs, port=args.cold_port, out_dir=bundle / "cold_start",
            timed=timed)
    try:
        for name, label, sets in scenario_plan(args):
            meta["scenarios"].append(run_scenario(target, bundle, name, label, sets, timed=timed,
                                                  restart=not args.no_api_restart))
            lt.write_json(bundle / "environment.json", meta)
    finally:
        meta["ran_alone_after"] = lt.ran_alone_check(own_projects, own_containers)
        meta["host_after"] = lt.host_snapshot()
        meta["docker_ps_after"] = lt.docker_ps()
        meta["finished_utc"] = lt.now_utc()
        lt.write_json(bundle / "environment.json", meta)
    hits = lt.secret_scan(bundle, stage.secrets())
    if hits:
        raise lt.LoadTestError(f"a secret value appears in {hits}; the bundle must be deleted")
    print_bundle(bundle)
    return bundle


def print_bundle(bundle: Path) -> None:
    meta = json.loads((bundle / "environment.json").read_text())
    after = meta.get("ran_alone_after", {}).get("ran_alone")
    print(f"\nbundle {bundle.name}: timed={meta['timed']} ran_alone="
          f"{meta['ran_alone_before']['ran_alone']}/{after}")
    for record in meta["scenarios"]:
        analysis = json.loads((bundle / record["label"] / "analysis.json").read_text())
        line = (f"  {record['label']:<24} n={record['requests']:<6} status={record['status']} "
                f"codes={record['error_codes']} cache={record['x_cache']}")
        if meta["timed"]:
            overall = analysis["overall"]
            lat = overall.get("latency_ms", {})
            line += (f" p50={lat.get('p50')} p95={lat.get('p95')} p99={lat.get('p99')} ms"
                     f" rps={overall.get('throughput_rps')}")
        print(line)
    if not meta["timed"]:
        print("  (dry run: functional only; no latency is reported)")


# =========================================================================== export
def export(bundle: Path, dest: Path, secrets_from: Stage | None) -> None:
    meta = json.loads((bundle / "environment.json").read_text())
    dest.mkdir(parents=True, exist_ok=False)
    lt.write_json(dest / "environment.json", meta)
    for record in meta["scenarios"]:
        src, target = bundle / record["label"], dest / record["label"]
        target.mkdir()
        analysis = json.loads((src / "analysis.json").read_text())
        if meta["timed"]:
            for name in ("analysis.json", "db_time.json", "stats_summary.json", "cgroup.json",
                         "summary.json"):
                if (src / name).exists():
                    shutil.copy2(src / name, target / name)
        else:
            lt.write_json(target / "functional.json", {
                "note": "DRY RUN: tiny scale, not timed. Status codes, error codes and cache "
                        "outcomes only; no latency is recorded here.",
                "k6_exit": record["k6_exit"],
                "db_statements_logged": record.get("db_statements"),
                "db_attribution_complete": record.get("db_attribution_complete"),
                **lt.strip_latency(analysis)})
    if "cold_start" in meta and (bundle / "cold_start" / "cold_start.json").exists():
        (dest / "cold_start").mkdir()
        data = json.loads((bundle / "cold_start" / "cold_start.json").read_text())
        if not meta["timed"]:
            data = {"note": "DRY RUN: functional only", "runs": len(data.get("runs", [])),
                    "all_reached_meta": data.get("all_reached_meta"),
                    "image": data.get("image")}
        lt.write_json(dest / "cold_start" / "cold_start.json", data)
    if not meta["timed"]:
        meta_view = {k: v for k, v in meta.items() if k != "scenarios"}
        meta_view["scenarios"] = [{k: v for k, v in r.items() if k not in ("wall_s",)}
                                  for r in meta["scenarios"]]
        lt.write_json(dest / "environment.json", meta_view)
    if secrets_from is not None:
        hits = lt.secret_scan(dest, secrets_from.secrets())
        if hits:
            shutil.rmtree(dest)
            raise lt.LoadTestError(f"export refused: a secret value appears in {hits}")
    print(f"exported {bundle.name} -> {dest}")


# =========================================================================== CLI
def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stage", required=True, help="stage name (raw outputs: OUT_ROOT/STAGE)")
    parser.add_argument("--out-root", type=Path,
                        default=Path(os.environ.get("ATUS_LT_OUT_ROOT", lt.LT_DIR / "out")))
    parser.add_argument("--project", help="compose project (default atus-lt-<stage>)")
    parser.add_argument("--api-port", type=int, default=8701)
    parser.add_argument("--db-port", type=int, default=5501)
    parser.add_argument("--cold-port", type=int, default=8702)
    parser.add_argument("--baseline-port", type=int, default=8703)


def add_api(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dump", default=DEFAULT_DUMP, help="serving dump (.dump next to its "
                        ".sha256 and .manifest.json); default $ATUS_LT_DUMP")
    parser.add_argument("--api-cpus", type=float, default=0.1)
    parser.add_argument("--api-memory", default="512m")
    parser.add_argument("--daily-cap", type=int, default=LOAD_CAP)
    parser.add_argument("--max-parallel-workers", type=int)
    parser.add_argument("--statement-timeout-ms", type=int)
    parser.add_argument("--api-env", action="append", metavar="ATUS_API_X=VALUE")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the API image")
    parser.add_argument("--no-pg-log", action="store_true",
                        help="no statement-duration logging (no DB-time attribution)")


def add_runs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="k6 setting (VUS, ITERATIONS, DURATION, EXECUTOR, RATE, ...)")
    parser.add_argument("--sweep", metavar="KEY=v1,v2", help="repeat each scenario per value")
    parser.add_argument("--dry-run", action="store_true",
                        help="tiny scale, functional only: no latency is reported")
    parser.add_argument("--no-api-restart", action="store_true",
                        help="keep the API process (and its cache) between scenarios")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_up = sub.add_parser("up")
    add_common(p_up)
    add_api(p_up)
    p_up.add_argument("--profile", choices=PROFILES, default="0.25cu")
    p_prof = sub.add_parser("profile")
    add_common(p_prof)
    add_api(p_prof)
    add_runs(p_prof)
    p_prof.add_argument("--profile", choices=PROFILES, required=True)
    p_prof.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    p_prof.add_argument("--cold-start", type=int, default=0, metavar="N",
                        help="also N cold starts (cold_start.py) at this profile")
    p_sc = sub.add_parser("scenario")
    add_common(p_sc)
    add_runs(p_sc)
    p_sc.add_argument("name", choices=SCENARIOS)
    p_base = sub.add_parser("baseline")
    add_common(p_base)
    add_api(p_base)
    add_runs(p_base)
    p_base.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    # The baseline is the laptop's speed: the API unconstrained unless asked otherwise.
    p_base.set_defaults(api_cpus=0.0, api_memory="")
    p_exp = sub.add_parser("export")
    p_exp.add_argument("bundle", type=Path)
    p_exp.add_argument("--to", type=Path, required=True)
    p_st = sub.add_parser("status")
    add_common(p_st)
    p_down = sub.add_parser("down")
    add_common(p_down)
    p_down.add_argument("--purge", action="store_true", help="also delete the database volume")
    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            stage = Stage(args.bundle.resolve().parent.parent, args.bundle.resolve().parent.name)
            export(args.bundle.resolve(), args.to.resolve(), stage)
            return 0
        stage = Stage(args.out_root, args.stage)
        with stage.lock():
            if args.command == "up":
                target = StackTarget(stage, args)
                target.ensure(args, args.profile, api_settings(args))
                print(f"API: {target.base_url}  (profile {args.profile})")
            elif args.command == "profile":
                target = StackTarget(stage, args)
                target.ensure(args, args.profile, api_settings(args))
                target.profile = args.profile
                run_bundle(target, stage, args, cold_start_runs=args.cold_start)
            elif args.command == "scenario":
                target = StackTarget(stage, args)
                if not stage.state.get("profile"):
                    raise lt.LoadTestError("no stack for this stage yet: run `up` first")
                target.profile = stage.state["profile"]
                run_bundle(target, stage, args)
            elif args.command == "baseline":
                target = BaselineTarget(stage, args)
                target.ensure(args)
                try:
                    run_bundle(target, stage, args)
                finally:
                    target.stop()
            elif args.command == "status":
                print(json.dumps({k: v for k, v in stage.state.items() if k != "api"}, indent=1))
                print(lt.out(["docker", "ps", "--filter",
                              f"label=com.docker.compose.project=atus-lt-{stage.name}",
                              "--format", "{{.Names}}\t{{.Status}}"], check=False))
            elif args.command == "down":
                target = StackTarget(stage, args)
                if stage.env_path.exists():
                    target.write_override(stage.state.get("profile", "laptop"),
                                          stage.state.get("api") or api_settings(
                                              argparse.Namespace(
                                                  daily_cap=LOAD_CAP, max_parallel_workers=None,
                                                  statement_timeout_ms=None, api_env=None,
                                                  api_cpus=0.1, api_memory="512m")),
                                          pg_log=False)
                    target.compose("--profile", "dump", "down", "--remove-orphans",
                                   *(["-v"] if args.purge else []))
                lt.run(["docker", "rm", "-f", f"atus-lt-{stage.name}-baseline-api",
                        f"atus-lt-{stage.name}-cold"], check=False)
                if args.purge:
                    stage.state.pop("restored", None)
                    stage.save()
                print("stack stopped" + (" and its volume deleted" if args.purge else ""))
    except lt.LoadTestError as error:
        print(f"run.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
