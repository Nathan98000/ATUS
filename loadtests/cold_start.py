#!/usr/bin/env python3
"""Cold start of the API container (brief §13 "cold start"; audit 7.9).

For each of N repetitions: `docker run` a fresh API container exactly as the
production stack runs it (the stage's image and production environment, the
read-only root filesystem, all capabilities dropped) with Render Free's limits
(--cpus=0.1 --memory=512m, no swap), attached to the stage's compose network
so it reaches the stack database at whatever DB profile is applied, and time:

  docker run -> first 200 from GET /health          (no database involved)
  docker run -> first 200 from GET /api/v1/meta     (first data request: the
                                                     process reads its data
                                                     snapshot, 11 statements)

The clock starts just before `docker run` with the image already in the
Docker VM, as in the audit (docs/evidence/phase5/01-audit-timed.md). It
excludes Render's own spin-up (image pull, scheduling, routing), Neon's wake
from suspend and any network round trip between providers: none of those is
emulated (audit F41). The image's Docker HEALTHCHECK is disabled
(--no-healthcheck), as Render ignores it; polling /health from the host is
the platform's probe.

Records the image (tag, local image id, repo digests, revision label), the DB
container's limits, every repetition's timings and polls, the container's
cgroup memory peak, CPU time and throttling after start-up, and PID 1's
VmHWM; then p50/p95/max over the repetitions.

  python loadtests/cold_start.py --stage s1 --out-root /tmp/lt --runs 5
  (run.py profile ... --cold-start 5 runs it inside a bundle)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ltcommon as lt  # noqa: E402


def container_env(stage, state: dict) -> Path:
    """The production API environment for the cold-start container (mode 600)."""
    env = lt.read_env_file(stage.env_path)
    path = stage.dir / "cold.env"
    values = {
        "ATUS_ENV": "production",
        "ATUS_DATABASE_URL": f"postgresql://atus_api:{env['ATUS_STACK_API_DB_PASSWORD']}"
                             "@db:5432/atus",
        "ATUS_EDGE_SECRETS": env["ATUS_STACK_EDGE_SECRET"],
        "ATUS_OPS_TOKEN": env["ATUS_STACK_OPS_TOKEN"],
        "ATUS_EXPECTED_DATA_VERSION": state["data_version"],
        "WEB_CONCURRENCY": "1",
        "PORT": "10000",
    }
    lt.write_private(path, "".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def run_cold_start(stage, *, runs: int, port: int, out_dir: Path, timed: bool = True,
                   cpus: str = "0.1", memory: str = "512m") -> dict:
    state = stage.state
    if not state.get("restored") or not state.get("project"):
        raise lt.LoadTestError("no restored stack for this stage: run `run.py up` first")
    project, image = state["project"], state["image"]
    db = f"{project}-db-1"
    network = f"{project}_default"
    env_file = container_env(stage, state)
    secret = lt.read_env_file(stage.env_path)["ATUS_STACK_EDGE_SECRET"]
    headers = {"X-ATUS-Edge-Auth": secret, "X-ATUS-Client-IP": "198.18.255.253",
               "Accept": "application/json"}
    name = f"atus-lt-{stage.name}-cold"
    base = f"http://127.0.0.1:{port}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "timed": timed,
        "started_utc": lt.now_utc(),
        "image": lt.image_info(image),
        "api_limits": f"--cpus={cpus} --memory={memory} --memory-swap={memory}",
        "db_container_limits": lt.container_limits(db),
        "db_profile": state.get("profile"),
        "clock": "perf_counter immediately before `docker run`; image already in the Docker VM",
        "not_emulated": "Render spin-up (pull, scheduling, routing), Neon wake from suspend, "
                        "inter-provider round trips",
        "runs": [],
    }
    for index in range(runs):
        lt.run(["docker", "rm", "-f", name], check=False)
        cmd = ["docker", "run", "-d", "--name", name, "--label", f"atus-lt-stage={stage.name}",
               "--network", network, "--cpus", cpus, "--memory", memory,
               "--memory-swap", memory, "-p", f"127.0.0.1:{port}:10000",
               "--env-file", str(env_file), "--read-only", "--tmpfs", "/tmp",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--no-healthcheck", image]
        t0 = time.perf_counter()
        wall0 = time.time()
        lt.run(cmd)
        t_returned = time.perf_counter()
        health_at, health_polls, health_seen = lt.wait_http(f"{base}/health", poll_s=0.1)
        meta_at, meta_polls, meta_seen = lt.wait_http(f"{base}/api/v1/meta", headers,
                                                      poll_s=0.05)
        status, _, _ = lt.http_get(f"{base}/api/v1/meta", headers)
        snapshot = lt.cgroup_snapshot(name, proc=True)
        logs = lt.run(["docker", "logs", name], check=False)
        run = {
            "i": index,
            "docker_run_returned_s": round(t_returned - t0, 3),
            "to_first_health_s": None if health_at is None else round(health_at - wall0, 3),
            "to_first_meta_s": None if meta_at is None else round(meta_at - wall0, 3),
            "health_polls": health_polls, "health_statuses_before_200": health_seen,
            "meta_polls": meta_polls, "meta_statuses_before_200": meta_seen,
            "second_meta_status": status,
            "cgroup_after": snapshot,
            "log_head": (logs.stdout + logs.stderr).splitlines()[:6],
        }
        result["runs"].append(run)
        shown = {k: run[k] for k in ("i", "health_polls", "meta_polls", "second_meta_status")}
        if timed:
            shown.update({k: run[k] for k in ("to_first_health_s", "to_first_meta_s")})
        print(f"   cold start {json.dumps(shown)}", flush=True)
        lt.run(["docker", "rm", "-f", name], check=False)
        time.sleep(2)
    result["all_reached_meta"] = all(r["to_first_meta_s"] is not None for r in result["runs"])
    if timed:
        for key in ("to_first_health_s", "to_first_meta_s", "docker_run_returned_s"):
            result[f"summary_{key}"] = lt.summarize((r[key] for r in result["runs"]), digits=3)
    result["finished_utc"] = lt.now_utc()
    lt.write_json(out_dir / "cold_start.json", result)
    hits = lt.secret_scan(out_dir, stage.secrets())
    if hits:
        raise lt.LoadTestError(f"a secret value appears in {hits}")
    return {k: v for k, v in result.items() if k != "runs"}


def main(argv: list[str] | None = None) -> int:
    import run as runner  # the stage layout lives in run.py

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--out-root", type=Path, default=lt.LT_DIR / "out")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--port", type=int, default=8702)
    parser.add_argument("--cpus", default="0.1")
    parser.add_argument("--memory", default="512m")
    parser.add_argument("--dry-run", action="store_true",
                        help="functional only: the timings are not printed")
    args = parser.parse_args(argv)
    try:
        stage = runner.Stage(args.out_root, args.stage)
        with stage.lock():
            out_dir = stage.dir / f"{lt.stamp()}-coldstart{'-dry' if args.dry_run else ''}"
            summary = run_cold_start(stage, runs=args.runs, port=args.port, out_dir=out_dir,
                                     timed=not args.dry_run, cpus=args.cpus, memory=args.memory)
    except lt.LoadTestError as error:
        print(f"cold_start.py: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=1, default=str))
    print(f"written: {out_dir / 'cold_start.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
