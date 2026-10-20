#!/usr/bin/env python3
"""3-way HTTP equivalence replay (brief §9): the same requests, three APIs.

    a  the Phase 1-4 API (a checkout of the baseline commit, 387fc2d) on the
       full database
    b  the Phase 5 API (this checkout) on the full database
    c  the Phase 5 API on the serving database (a restored release artifact)

The harness starts the three servers itself (``servers.py``: clean
environment, loopback ports, read-only database sessions, development mode
without edge authentication and with generous protection limits), POSTs
every spec's exact body to each, GETs the metadata endpoints, compares the
answers pairwise (``httpdiff.py``: b vs c under the §9 criterion with an
explicit field whitelist; a vs b likewise, accepting only the intended
Phase 5 differences it lists), stops the servers and writes:

    <out>/report.json      machine-readable: every spec's status, answers,
                           per-pair outcome, max relative/scaled SE-CI
                           difference and mismatches with JSON paths
    <out>/summary.md       the human-readable summary
    <out>/logs/<a|b|c>.log each server's log (connection secrets redacted)
    <out>/responses/       the three answers of every failed spec
                           (--save-responses)

Specs: every ``*.jsonl`` file in ``--specs``; one JSON object per line:

    {"id": "sleep-2024", "endpoint": "estimate", "category": "smoke",
     "expect": "ok", "body": {"measure": "average_minutes_per_day",
     "activity": {"include": ["0101"]}, "years": [2024]}}

``endpoint`` is estimate, trend or compare; ``body`` is serialized compactly
(or sent verbatim when it is a string); ``expect`` is ok (2xx) or error, as
answered by b; an optional ``content_type`` overrides application/json.

Databases (connection strings are never accepted on the command line, never
printed and never written; reports name ``user@host:port/dbname``):

* full: ``$ATUS_EQ_FULL_DATABASE_URL``, default the local development DB;
* serving: ``--serving-secrets-file`` (the KEY=value file the restore
  writes: ``ATUS_API_DATABASE_URL``, the read-only API role, else
  ``ATUS_OWNER_DATABASE_URL``) or ``$ATUS_EQ_SERVING_DATABASE_URL``.

Usage (``scripts/equivalence/run.sh`` also prepares the baseline checkout):

    python scripts/equivalence/replay.py --specs DIR --out DIR \\
        --baseline-dir /path/to/checkout-of-387fc2d \\
        --serving-secrets-file ~/.config/atus/neon.env

Nothing is timed. Exit status: 0 every spec and GET passed, 1 a failure
(see summary.md), 2 a usage, input or start-up error.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpdiff
import psycopg
from httpdiff import (
    HARNESS_VERSION,
    INTENDED_DIFFERENCES,
    PAIRS,
    TARGET_LABELS,
    WHITELIST,
    WHITELISTED_HEADERS,
    RequestInfo,
    Response,
    Spec,
    SpecError,
)
from servers import (
    DEFAULT_MAX_BODY_BYTES,
    ManagedServer,
    ServerConfig,
    ServerError,
    ServerGroup,
    baseline_command,
    baseline_env,
    clean_env,
    dsn_secrets,
    phase5_command,
    phase5_env,
    read_only_dsn,
    stray_dotenv,
)

from atus_pipeline.config import DEFAULT_DATABASE_URL
from atus_pipeline.database.dsn import describe, redact
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.serving.secrets_file import (
    API_URL_KEY,
    OWNER_URL_KEY,
    SecretsFileError,
    read_env_file,
)

REPO = Path(__file__).resolve().parents[2]
FULL_URL_ENV = "ATUS_EQ_FULL_DATABASE_URL"
SERVING_URL_ENV = "ATUS_EQ_SERVING_DATABASE_URL"
BASELINE_DIR_ENV = "ATUS_EQ_BASELINE_DIR"
DEFAULT_BASELINE_COMMIT = "387fc2d"
DEFAULT_PORTS = "8641,8642,8643"
USER_AGENT = f"atus-equivalence/{HARNESS_VERSION}"
PACKAGES = ("fastapi", "starlette", "pydantic", "pydantic-core", "uvicorn", "psycopg",
            "psycopg-pool")


class SetupError(RuntimeError):
    """Something prevents the run from starting (exit status 2)."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# inputs                                                                       #
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="3-way HTTP equivalence replay: Phase 1-4 API and Phase 5 API on the "
                    "full database, Phase 5 API on the serving database.",
    )
    parser.add_argument("--specs", type=Path, required=True,
                        help="Directory of *.jsonl spec files.")
    parser.add_argument("--out", type=Path, required=True,
                        help="Output directory (report.json, summary.md, logs/, responses/).")
    parser.add_argument("--targets", default="abc",
                        help="Servers to run: abc (default), bc (full vs serving only) or ab.")
    parser.add_argument("--baseline-dir", type=Path, default=os.environ.get(BASELINE_DIR_ENV),
                        help=f"Checkout of the baseline commit (default ${BASELINE_DIR_ENV}).")
    parser.add_argument("--baseline-python", type=Path, default=None,
                        help="Interpreter of the baseline (default <baseline-dir>/.venv/bin/"
                             "python).")
    parser.add_argument("--baseline-commit", default=DEFAULT_BASELINE_COMMIT,
                        help="The baseline checkout's HEAD must start with this "
                             f"(default {DEFAULT_BASELINE_COMMIT}; '' skips the check).")
    parser.add_argument("--phase5-python", type=Path, default=Path(sys.executable),
                        help="Interpreter with this checkout installed (default: this one).")
    parser.add_argument("--serving-secrets-file", type=Path, default=None,
                        help=f"KEY=value file with {API_URL_KEY} (or {OWNER_URL_KEY}) of the "
                             f"serving database (default ${SERVING_URL_ENV}).")
    parser.add_argument("--ports", default=DEFAULT_PORTS,
                        help=f"Loopback ports of a,b,c (default {DEFAULT_PORTS}).")
    parser.add_argument("--jobs", type=int, default=2,
                        help="Specs in flight at once (each goes to every server at once).")
    parser.add_argument("--retries", type=int, default=3,
                        help="Retries of a 429/503 capacity answer or a lost connection.")
    parser.add_argument("--request-timeout", type=float, default=900.0,
                        help="Seconds to wait for one answer.")
    parser.add_argument("--activity-sample", type=int, default=40,
                        help="Tier-2 and 6-digit codes to GET /activities/<code> for (every "
                             "tier-1 code is always included).")
    parser.add_argument("--seed", type=int, default=20261020, help="Seed of that sample.")
    parser.add_argument("--no-gets", action="store_true", help="Skip the GET endpoints.")
    parser.add_argument("--category", action="append", default=[],
                        help="Replay only specs of this category (repeatable).")
    parser.add_argument("--limit", type=int, default=None, help="Replay only the first N specs.")
    parser.add_argument("--save-responses", choices=("none", "failures", "all"),
                        default="failures", help="Which answers to keep under responses/.")
    parser.add_argument("--fail-on-unexpected", action="store_true",
                        help="Also fail when b's answer is not what a spec expects.")
    parser.add_argument("--server-log-level", default="WARNING",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    try:
        args.targets = list(httpdiff.iter_targets(args.targets))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        ports = [int(p) for p in args.ports.split(",")]
    except ValueError:
        parser.error("--ports must be three integers, e.g. 8641,8642,8643")
    if len(ports) != 3 or len(set(ports)) != 3 or not all(0 < p < 65536 for p in ports):
        parser.error("--ports must be three distinct ports for a,b,c")
    args.port_of = dict(zip(httpdiff.TARGETS, ports, strict=True))
    if args.jobs < 1 or args.retries < 0 or args.activity_sample < 0:
        parser.error("--jobs must be >= 1, --retries and --activity-sample >= 0")
    if "a" in args.targets:
        if args.baseline_dir is None:
            parser.error(f"target a needs --baseline-dir (or ${BASELINE_DIR_ENV})")
        args.baseline_dir = Path(args.baseline_dir)
        if args.baseline_python is None:
            args.baseline_python = args.baseline_dir / ".venv" / "bin" / "python"
    return args


def database_urls(args: argparse.Namespace) -> dict[str, tuple[str, str]]:
    """target -> (connection string, where it came from)."""
    full = os.environ.get(FULL_URL_ENV) or DEFAULT_DATABASE_URL
    source = f"${FULL_URL_ENV}" if os.environ.get(FULL_URL_ENV) else "development default"
    urls = {"a": (full, source), "b": (full, source)}
    if "c" in args.targets:
        if args.serving_secrets_file is not None:
            path = args.serving_secrets_file.expanduser()
            try:
                values = read_env_file(path)
            except (OSError, SecretsFileError) as exc:
                raise SetupError(f"cannot read {path}: {type(exc).__name__}") from None
            key = API_URL_KEY if values.get(API_URL_KEY) else OWNER_URL_KEY
            if not values.get(key):
                raise SetupError(f"{path} has neither {API_URL_KEY} nor {OWNER_URL_KEY}")
            urls["c"] = (values[key], f"{path.name}:{key}")
        elif os.environ.get(SERVING_URL_ENV):
            urls["c"] = (os.environ[SERVING_URL_ENV], f"${SERVING_URL_ENV}")
        else:
            raise SetupError(f"target c needs --serving-secrets-file or ${SERVING_URL_ENV}")
    return {key: urls[key] for key in args.targets}


def database_facts(url: str) -> dict:
    """What the harness reads itself, read-only, about a database."""
    try:
        with psycopg.connect(read_only_dsn(url), autocommit=True, connect_timeout=10) as conn:
            return {
                "database": describe(url),
                "data_version": read_data_version(conn),
                "server_version": conn.execute("SHOW server_version").fetchone()[0],
                "timezone": conn.execute("SHOW TimeZone").fetchone()[0],
                "read_only": conn.execute(
                    "SHOW default_transaction_read_only").fetchone()[0],
            }
    except psycopg.Error as exc:
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        raise SetupError(f"{describe(url)}: {redact(first, *dsn_secrets(url))}") from None


def git_facts(directory: Path) -> dict:
    def git(*command: str) -> str:
        return subprocess.run(["git", *command], cwd=directory, capture_output=True, text=True,
                              check=True).stdout.strip()

    try:
        return {"commit": git("rev-parse", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": None}


def package_versions(python: Path) -> dict:
    code = ("import importlib.metadata as m, json\n"
            f"print(json.dumps({{p: m.version(p) for p in {list(PACKAGES)!r}}}))")
    try:
        out = subprocess.run([str(python), "-c", code], capture_output=True, text=True,
                             check=True, env=clean_env({}), timeout=60).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def send(port: int, request: RequestInfo, *, timeout: float) -> Response:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity",
               "User-Agent": USER_AGENT}
    if request.method == "POST" and request.content_type:
        headers["Content-Type"] = request.content_type
    try:
        connection.request(request.method, request.path,
                           body=request.body if request.method == "POST" else None,
                           headers=headers)
        answer = connection.getresponse()
        body = answer.read()
        received: dict[str, str] = {}
        for name, value in answer.getheaders():
            name = name.lower()
            received[name] = f"{received[name]}, {value}" if name in received else value
        return Response(answer.status, received, body)
    except (OSError, http.client.HTTPException) as exc:
        return Response(None, transport_error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        connection.close()


def _retry_delay(response: Response, attempt: int) -> float:
    try:
        hinted = float(response.headers.get("retry-after", ""))
    except ValueError:
        hinted = 0.0
    return min(max(hinted, 2.0 ** attempt), 30.0)


def send_with_retries(server: ManagedServer, request: RequestInfo, *, retries: int,
                      timeout: float) -> Response:
    attempt = 0
    while True:
        attempt += 1
        if not server.alive():
            return Response(None, transport_error="server process exited", attempts=attempt)
        response = send(server.port, request, timeout=timeout)
        response.attempts = attempt
        if not httpdiff.is_retryable(response) or attempt > retries:
            return response
        time.sleep(_retry_delay(response, attempt))


# --------------------------------------------------------------------------- #
# the run                                                                      #
# --------------------------------------------------------------------------- #


def _safe_name(text: str) -> str:
    """A file name for a spec id or path: readable, never hidden or
    traversing, unique by its hash suffix."""
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", text)[:80].lstrip("._-") or "request"
    return f"{readable}-{digest}"


def save_responses(directory: Path, name: str, responses: dict[str, Response]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for key, response in responses.items():
        kept = {k: v for k, v in response.headers.items()
                if k.startswith("x-") or k in ("content-type", "retry-after", "cache-control")}
        payload = response.payload if response.is_json else response.body.decode(
            "utf-8", "replace")
        (directory / f"{_safe_name(name)}.{key}.json").write_text(json.dumps({
            "status": response.status, "transport_error": response.transport_error,
            "attempts": response.attempts, "headers": kept, "body": payload,
        }, indent=1, ensure_ascii=False) + "\n")


def server_configs(args: argparse.Namespace, urls: dict[str, tuple[str, str]],
                   out: Path, build_sha: str) -> list[ServerConfig]:
    configs = []
    for key in args.targets:
        url = read_only_dsn(urls[key][0])
        port = args.port_of[key]
        if key == "a":
            python = str(args.baseline_python)
            command = baseline_command(python, port)
            env = clean_env(baseline_env(url, log_level=args.server_log_level))
            # The baseline's load_dotenv() searches upwards from its package.
            package_dir = args.baseline_dir / "src" / "atus_pipeline"
        else:
            python = str(args.phase5_python)
            command = phase5_command(python, port)
            env = clean_env(phase5_env(url, port, build_sha=build_sha,
                                       log_level=args.server_log_level))
            package_dir = None  # Phase 5 searches upwards from the working directory only
        configs.append(ServerConfig(
            key=key, label=TARGET_LABELS[key], command=command, cwd=out / "run" / key,
            env=env, port=port, log_path=out / "logs" / f"{key}.log",
            secrets=dsn_secrets(urls[key][0]), package_dir=package_dir,
        ))
    return configs


def check_setup(args: argparse.Namespace, configs: list[ServerConfig]) -> None:
    for config in configs:
        python = Path(config.command[0])
        if not python.exists():
            raise SetupError(f"server {config.key}: interpreter {python} does not exist")
    if "a" in args.targets:
        if not (args.baseline_dir / "src" / "atus_pipeline" / "cli.py").is_file():
            raise SetupError(f"{args.baseline_dir} is not a checkout of this repository")
        head = git_facts(args.baseline_dir)["commit"]
        if args.baseline_commit and not head.startswith(args.baseline_commit):
            raise SetupError(f"{args.baseline_dir} is at {head[:12]}, not "
                             f"{args.baseline_commit}")
    for config in configs:
        config.cwd.mkdir(parents=True, exist_ok=True)
    places = [c.cwd for c in configs] + [c.package_dir for c in configs if c.package_dir]
    found = stray_dotenv(places)
    if found:
        raise SetupError("a .env file would configure a server: "
                         + ", ".join(str(p) for p in found)
                         + " (move it away or choose another --out)")


def warm_up(group: ServerGroup, args: argparse.Namespace, facts: dict[str, dict]
            ) -> tuple[dict[str, str], dict[str, Response]]:
    """GET /api/v1/meta on every server (loads the Phase 5 data snapshot, so
    every later answer is from a warm process) and check each Phase 5 server
    serves the data version its database holds."""
    expected: dict[str, str] = {}
    metas: dict[str, Response] = {}
    for key, server in group.servers.items():
        response = send_with_retries(server, RequestInfo("GET", "/api/v1/meta"),
                                     retries=args.retries, timeout=args.request_timeout)
        if response.status != 200:
            raise ServerError(f"server {key}: GET /api/v1/meta answered "
                              f"{response.describe()}:\n{server.log_tail()}")
        metas[key] = response
        served = response.headers.get("x-data-version")
        if key != "a":
            if served != facts[key]["data_version"]:
                raise ServerError(f"server {key} serves data version {served!r}, but its "
                                  f"database holds {facts[key]['data_version']!r}")
            expected[key] = served
    return expected, metas


def fetch_all(group: ServerGroup, pool: ThreadPoolExecutor, request: RequestInfo,
              args: argparse.Namespace) -> dict[str, Response]:
    futures = {
        key: pool.submit(send_with_retries, server, request, retries=args.retries,
                         timeout=args.request_timeout)
        for key, server in group.servers.items()
    }
    return {key: future.result() for key, future in futures.items()}


def run(args: argparse.Namespace) -> int:
    specs, spec_files = httpdiff.load_specs(args.specs)
    if args.category:
        specs = [s for s in specs if s.category in set(args.category)]
    if args.limit is not None:
        specs = specs[: args.limit]
    if not specs:
        raise SetupError("no specs left after --category/--limit")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    urls = database_urls(args)
    phase5_git = git_facts(REPO)
    build_sha = phase5_git["commit"][:12] if phase5_git["commit"] != "unknown" else "dev"
    configs = server_configs(args, urls, out, build_sha)
    check_setup(args, configs)

    facts = {key: database_facts(url) for key, (url, _) in urls.items()}
    servers_info: dict[str, dict] = {}
    for config in configs:
        code_dir = args.baseline_dir if config.key == "a" else REPO
        python = Path(config.command[0])
        servers_info[config.key] = {
            "label": config.label,
            "code_dir": str(code_dir),
            **git_facts(code_dir),
            "python": str(python),
            "packages": package_versions(python),
            "port": config.port,
            **facts[config.key],
            "database_source": urls[config.key][1],
            "settings": {k: v for k, v in config.env.items()
                         if (k.startswith("ATUS_") or k == "PORT")
                         and k != "ATUS_DATABASE_URL"},
        }
    log(f"{len(specs)} specs from {args.specs}; servers: "
        + ", ".join(f"{k}={servers_info[k]['database']} :{servers_info[k]['port']}"
                    for k in args.targets))

    spec_results: list[dict | None] = [None] * len(specs)
    get_results: list[dict] = []
    responses_dir = out / "responses"
    with ServerGroup(configs) as group:
        log("servers ready; warming up")
        expected, metas = warm_up(group, args, facts)
        for key, response in metas.items():
            servers_info[key]["served_data_version"] = response.headers.get("x-data-version")
            meta = response.payload if response.is_json else {}
            servers_info[key]["meta_data"] = meta.get("data") if isinstance(meta, dict) else None
        inner = ThreadPoolExecutor(max_workers=args.jobs * len(group.servers),
                                   thread_name_prefix="send")
        try:
            if not args.no_gets:
                # The detail sample comes from b's lexicon (b is in every pair).
                listing = send_with_retries(group.servers["b"],
                                            RequestInfo("GET", "/api/v1/activities"),
                                            retries=args.retries, timeout=args.request_timeout)
                paths = list(httpdiff.FIXED_GETS) + httpdiff.activity_detail_paths(
                    listing.payload if listing.is_json else None,
                    sample=args.activity_sample, seed=args.seed)
                log(f"GET: {len(paths)} requests")
                for path in paths:
                    answers = fetch_all(group, inner, RequestInfo("GET", path), args)
                    try:
                        entry = httpdiff.evaluate_get(path, answers,
                                                      expected_versions=expected)
                    except Exception as exc:  # a harness bug must not lose the run
                        entry = httpdiff.harness_error_entry({"path": path}, answers, exc)
                    get_results.append(entry)
                    if args.save_responses == "all" or (
                            args.save_responses == "failures" and not entry["passed"]):
                        save_responses(responses_dir / "get", path, answers)

            def replay_one(index: int, spec: Spec) -> None:
                request = spec.request(DEFAULT_MAX_BODY_BYTES)
                answers = fetch_all(group, inner, request, args)
                try:
                    entry = httpdiff.evaluate_spec(spec, answers,
                                                   max_body_bytes=DEFAULT_MAX_BODY_BYTES,
                                                   expected_versions=expected)
                except Exception as exc:  # a harness bug must not lose the run
                    entry = httpdiff.harness_error_entry(
                        {"id": spec.id, "source": spec.source, "endpoint": spec.endpoint,
                         "category": spec.category, "expect": spec.expect,
                         "expect_met": None}, answers, exc)
                spec_results[index] = entry
                if args.save_responses == "all" or (
                        args.save_responses == "failures" and not entry["passed"]):
                    save_responses(responses_dir / "specs", spec.id, answers)

            log(f"POST: {len(specs)} specs, {args.jobs} at a time")
            with ThreadPoolExecutor(max_workers=args.jobs, thread_name_prefix="spec") as outer:
                futures = [outer.submit(replay_one, i, spec) for i, spec in enumerate(specs)]
                for done, future in enumerate(futures, 1):
                    future.result()
                    if done % 25 == 0 or done == len(futures):
                        failed = sum(1 for r in spec_results if r and not r["passed"])
                        log(f"  {done}/{len(futures)} replayed, {failed} failed so far")
        finally:
            inner.shutdown(wait=True)

    results = [r for r in spec_results if r is not None]
    report = {
        "harness_version": HARNESS_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {
            "spec_dir": str(args.specs.resolve()),
            "spec_files": spec_files,
            "categories": args.category or None,
            "limit": args.limit,
            "targets": "".join(args.targets),
            "jobs": args.jobs,
            "retries": args.retries,
            "activity_sample": args.activity_sample,
            "seed": args.seed,
            "fail_on_unexpected": args.fail_on_unexpected,
        },
        "servers": servers_info,
        "pairs": {pair: httpdiff.PAIR_LABELS[pair] for pair, (x, y) in PAIRS.items()
                  if x in args.targets and y in args.targets},
        "criterion": httpdiff.criterion_description(),
        "whitelist": {"body": [rule.as_dict() for rule in WHITELIST],
                      "headers": WHITELISTED_HEADERS,
                      "matched_headers_b_c": list(httpdiff.MATCHED_HEADERS)},
        "intended_differences": [d.as_dict() for d in INTENDED_DIFFERENCES],
        "summary": httpdiff.summarize(results, get_results),
        "specs": results,
        "gets": get_results,
    }
    (out / "report.json").write_text(json.dumps(report, indent=1, allow_nan=False,
                                                ensure_ascii=False) + "\n")
    (out / "summary.md").write_text(httpdiff.render_markdown(report))
    passed = httpdiff.report_passed(report, fail_on_unexpected=args.fail_on_unexpected)
    summary = report["summary"]
    print(f"{'PASS' if passed else 'FAIL'}: {summary['specs_passed']}/{summary['specs']} specs, "
          f"{summary['gets'] - len(summary['gets_failed'])}/{summary['gets']} GETs; "
          f"report {out / 'report.json'}")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (SpecError, SetupError, ServerError) as exc:
        log(f"equivalence: {exc}")
        return 2
    except KeyboardInterrupt:
        log("equivalence: interrupted; servers stopped")
        return 2


if __name__ == "__main__":
    sys.exit(main())
