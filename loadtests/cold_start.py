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

A/B comparison: --image may be given more than once. Each repetition then
starts every image once, in the order given (A, B, A, B, ...), so slow drift
on the host spreads over both; the summaries are per image (`by_image`).
--warmup K first starts each image K times, recorded but left out of the
summaries, so every image's files are in the Docker VM's page cache before the
measured runs (the stack's own API container warms only its own image).
Run standalone, the tool also records the ran-alone check (run.py's) before
and after, and samples the other containers' CPU once a minute.

  python loadtests/cold_start.py --stage s1 --out-root /tmp/lt --runs 5
  python loadtests/cold_start.py --stage s1 --out-root /tmp/lt --runs 5 --warmup 1 \\
      --image atus-api:before --image atus-api:after
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
        # the stack's Postgres has no TLS: the escape hatch docker-compose.prod.yml sets
        "ATUS_DATABASE_TLS": "local-stack-only",
        "ATUS_EDGE_SECRETS": env["ATUS_STACK_EDGE_SECRET"],
        "ATUS_OPS_TOKEN": env["ATUS_STACK_OPS_TOKEN"],
        "ATUS_EXPECTED_DATA_VERSION": state["data_version"],
        "WEB_CONCURRENCY": "1",
        "PORT": "10000",
    }
    lt.write_private(path, "".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def run_plan(images: list[str], runs: int, warmup: int = 0) -> list[tuple[str, bool]]:
    """(image, is_warmup) in start order: the warm-ups, then `runs` rounds that
    start each image once, alternating A, B, A, B, ..."""
    return [(image, True) for _ in range(warmup) for image in images] + \
        [(image, False) for _ in range(runs) for image in images]


SUMMARY_KEYS = ("to_first_health_s", "to_first_meta_s", "docker_run_returned_s",
                "startup_cpu_s")


def summaries(runs: list[dict]) -> dict:
    measured = [r for r in runs if not r.get("warmup")]
    return {f"summary_{key}": lt.summarize((r.get(key) for r in measured), digits=3)
            for key in SUMMARY_KEYS}


def run_cold_start(stage, *, runs: int, port: int, out_dir: Path, timed: bool = True,
                   cpus: str = "0.1", memory: str = "512m", images: list[str] | None = None,
                   warmup: int = 0) -> dict:
    state = stage.state
    if not state.get("restored") or not state.get("project"):
        raise lt.LoadTestError("no restored stack for this stage: run `run.py up` first")
    project = state["project"]
    images = list(images or [state["image"]])
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
        **({"image": lt.image_info(images[0])} if len(images) == 1 else {
            "images": [lt.image_info(tag) for tag in images],
            "order": "each repetition starts every image once, in this order (A, B, A, B, ...)"}),
        "warmup_per_image": warmup,
        "api_limits": f"--cpus={cpus} --memory={memory} --memory-swap={memory}",
        "db_container_limits": lt.container_limits(db),
        "db_profile": state.get("profile"),
        "clock": "perf_counter immediately before `docker run`; image already in the Docker VM",
        "not_emulated": "Render spin-up (pull, scheduling, routing), Neon wake from suspend, "
                        "inter-provider round trips",
        "runs": [],
    }
    for index, (image, warming) in enumerate(run_plan(images, runs, warmup)):
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
        usage = (snapshot.get("cpu_stat") or {}).get("usage_usec")
        run = {
            "i": index,
            "image": image,
            "warmup": warming,
            "docker_run_returned_s": round(t_returned - t0, 3),
            "to_first_health_s": None if health_at is None else round(health_at - wall0, 3),
            "to_first_meta_s": None if meta_at is None else round(meta_at - wall0, 3),
            "health_polls": health_polls, "health_statuses_before_200": health_seen,
            "meta_polls": meta_polls, "meta_statuses_before_200": meta_seen,
            "second_meta_status": status,
            "startup_cpu_s": None if usage is None else round(usage / 1e6, 3),
            "cgroup_after": snapshot,
            "log_head": (logs.stdout + logs.stderr).splitlines()[:6],
        }
        result["runs"].append(run)
        shown = {k: run[k] for k in ("i", "image", "warmup", "health_polls", "meta_polls",
                                     "second_meta_status")}
        if timed:
            shown.update({k: run[k] for k in ("to_first_health_s", "to_first_meta_s",
                                              "startup_cpu_s")})
        print(f"   cold start {json.dumps(shown)}", flush=True)
        lt.run(["docker", "rm", "-f", name], check=False)
        time.sleep(2)
    result["all_reached_meta"] = all(r["to_first_meta_s"] is not None for r in result["runs"])
    if timed and len(images) == 1:
        result.update(summaries(result["runs"]))
    elif timed:
        result["by_image"] = {tag: summaries([r for r in result["runs"] if r["image"] == tag])
                              for tag in images}
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
    parser.add_argument("--image", action="append", metavar="TAG",
                        help="image to start (default: the stage's); repeat to alternate "
                             "A, B, A, B, ... with summaries per image")
    parser.add_argument("--warmup", type=int, default=0, metavar="K",
                        help="first start each image K times, recorded but not summarized")
    parser.add_argument("--dry-run", action="store_true",
                        help="functional only: the timings are not printed")
    args = parser.parse_args(argv)
    try:
        stage = runner.Stage(args.out_root, args.stage)
        with stage.lock():
            out_dir = stage.dir / f"{lt.stamp()}-coldstart{'-dry' if args.dry_run else ''}"
            project = stage.state.get("project") or f"atus-lt-{stage.name}"
            own = {f"{project}-db-1", f"{project}-api-1", f"atus-lt-{stage.name}-cold"}
            out_dir.mkdir(parents=True, exist_ok=True)
            evidence = {"ran_alone_before": lt.ran_alone_check({project}, own),
                        "host_before": lt.host_snapshot(), "git": lt.git_snapshot()}
            print(f"== ran alone before: {evidence['ran_alone_before']['ran_alone']} "
                  f"{evidence['ran_alone_before']['reasons']}", flush=True)
            background = lt.BackgroundSampler(own, out_dir / "background_stats.jsonl")
            background.start()
            try:
                summary = run_cold_start(stage, runs=args.runs, port=args.port, out_dir=out_dir,
                                         timed=not args.dry_run, cpus=args.cpus,
                                         memory=args.memory, images=args.image,
                                         warmup=args.warmup)
            finally:
                background.stop()
            evidence["background_noise"] = {
                "what": "docker stats --no-stream of every container that is not this run's, "
                        "once a minute during the cold starts (other projects' services)",
                **lt.summarize_background(out_dir / "background_stats.jsonl")}
            if (out_dir / "background_stats.jsonl").exists():
                lt.gzip_file(out_dir / "background_stats.jsonl")
            evidence["ran_alone_after"] = lt.ran_alone_check({project}, own)
            evidence["host_after"] = lt.host_snapshot()
            print(f"== ran alone after: {evidence['ran_alone_after']['ran_alone']} "
                  f"{evidence['ran_alone_after']['reasons']}", flush=True)
            lt.write_json(out_dir / "environment.json", evidence)
            hits = lt.secret_scan(out_dir, stage.secrets())
            if hits:
                raise lt.LoadTestError(f"a secret value appears in {hits}")
    except lt.LoadTestError as error:
        print(f"cold_start.py: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=1, default=str))
    print(f"written: {out_dir / 'cold_start.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
