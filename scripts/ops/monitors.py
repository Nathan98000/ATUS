#!/usr/bin/env python3
"""The $0 uptime monitors and the daily quota report (ADR-011, ADR-014, brief §12).

Run by the scheduled GitHub workflows; standard library only:

    python scripts/ops/monitors.py edge  --base-url https://atus.<sub>.workers.dev
    python scripts/ops/monitors.py deep  --base-url https://atus.<sub>.workers.dev
    EDGE_OPS_TOKEN=... python scripts/ops/monitors.py quota --base-url https://...

``edge`` (every 30 min, ``.github/workflows/edge-check.yml``): the static SPA
shell with its security headers, the Worker's own ``/api/_edge/health`` and
the static result snapshot (pointer and manifest). Four requests, one of
which invokes the Worker; nothing reaches Render or Neon.

``deep`` (every 6 h, ``deep-check.yml``): ``GET /api/health`` through the
Worker to Render's ``/health``, which never touches the database (it may
wake Render, never Neon). Tolerates a cold start.

While the owner's pause switch is on (deploy.yml ``pause-api``;
edge/src/pause.ts), ``/api/_edge/health`` reports it and ``/api/health``
answers 503 ``api_paused`` at the edge: ``edge`` and ``deep`` say so as a
WARN, not a failure, and ``deep`` stops after that one request. The owner
set it on purpose; it ends at its date or with ``resume-api``.

``quota`` (daily, ``quota-report.yml``): reads the governor's budget state
from ``GET /api/_ops/budget`` (bearer ``EDGE_OPS_TOKEN``, never printed) and
fails when any provider budget is above 70% of its pro-rated value. One
Worker request; the governor answers from its own storage. With
``CLOUDFLARE_ANALYTICS_TOKEN`` (a read-only "Account Analytics: Read" token)
and ``CLOUDFLARE_ANALYTICS_ACCOUNT`` (the account id) it also judges the
account's Worker requests today against Workers Free's 100,000 a day, from
one query to Cloudflare's GraphQL Analytics API (``workersInvocationsAdaptive``;
whether that dataset answers on Workers Free is not documented). Render's
outbound bandwidth has no monitor: reading it needs an account-wide Render
API key, which the owner never creates (docs/deployment.md, owner step 2.6).

No monitor calls a path that touches the database; tests/deploy/test_workflows.py
holds the workflows to that. Exit status: 0 healthy, 1 a check failed, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from httpcheck import (  # noqa: E402
    API_HEADERS,
    SITE_HEADERS,
    Client,
    OpsError,
    Report,
    header_problems,
)

SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: The paths each monitor may request. None of them reaches the database.
EDGE_PATHS = ("/", "/api/_edge/health", "/snap/current.json", "/snap/<id>/manifest.json")
DEEP_PATHS = ("/api/health",)
QUOTA_PATHS = ("/api/_ops/budget",)

# Provider quotas the report pro-rates against (ADR-011 "Cited free-tier terms").
NEON_MONTHLY_CU_HOURS = 100.0
NEON_MONTHLY_EGRESS_BYTES = 5e9
ALERT_THRESHOLD = 0.70
#: Pro-rating floors, so a report right after a reset does not alert on noise.
MIN_DAY_SHARE = 1 / 24
MIN_MONTH_SHARE = 1 / 31
#: Workers Free: 100,000 requests a day, account-wide, reset at 00:00 UTC.
WORKER_DAILY_REQUESTS = 100_000
CLOUDFLARE_API = "https://api.cloudflare.com"
GRAPHQL_PATH = "/client/v4/graphql"
#: The account's Worker invocations over a window (Cloudflare's "Querying
#: Workers metrics with GraphQL" tutorial, without the per-script filter).
WORKER_REQUESTS_QUERY = (
    "query WorkerRequests($accountTag: string, $datetimeStart: string, $datetimeEnd: string) {"
    " viewer { accounts(filter: {accountTag: $accountTag}) {"
    " workersInvocationsAdaptive(limit: 10000, filter: {"
    "datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd}) { sum { requests } } } } }"
)
ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# edge                                                                         #
# --------------------------------------------------------------------------- #

def check_edge(client: Client, report: Report) -> None:
    shell = client.get("/")
    problems = header_problems(shell, SITE_HEADERS)
    if shell.status != 200 or shell.content_type != "text/html":
        report.fail("spa_shell", f"expected 200 text/html, got {shell.describe()} "
                                 f"{shell.content_type or 'no content type'}")
    elif b'id="root"' not in shell.body:
        report.fail("spa_shell", "the page has no #root element")
    elif problems:
        report.fail("spa_shell", "security headers: " + ", ".join(problems))
    else:
        report.ok("spa_shell", "200 text/html with the site security headers")

    health = client.get("/api/_edge/health")
    edge_snapshot = None
    if health.status != 200 or not health.is_json:
        report.fail("edge_health", f"expected a 200 JSON body, got {health.describe()}")
    else:
        body = health.json()
        edge_snapshot = body.get("snapshot_id")
        problems = header_problems(health, API_HEADERS)
        if body.get("status") != "ok":
            report.fail("edge_health", f"status {body.get('status')!r}")
        elif problems:
            report.fail("edge_health", "security headers: " + ", ".join(problems))
        else:
            report.ok("edge_health", f"edge {body.get('edge_version')}, "
                                     f"data {body.get('data_version')}, "
                                     f"snapshot {edge_snapshot}")
        if isinstance(body.get("api_paused"), dict):
            report.add("api_paused", "WARN", _pause_note(body["api_paused"]))

    pointer = client.get("/snap/current.json")
    if pointer.status != 200 or not pointer.is_json:
        report.fail("snapshot", "/snap/current.json is not served as JSON "
                                f"({pointer.describe()} {pointer.content_type}): no snapshot "
                                "deployed, or the SPA fallback answered")
        return
    snapshot_id = pointer.json().get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.match(snapshot_id):
        report.fail("snapshot", "the pointer names no valid snapshot_id")
        return
    if edge_snapshot is not None and edge_snapshot != snapshot_id:
        report.fail("snapshot", f"the Worker's SNAPSHOT_ID {edge_snapshot} differs from the "
                                f"static pointer {snapshot_id} (half-finished deploy?)")
        return
    manifest = client.get(f"/snap/{snapshot_id}/manifest.json")
    if manifest.status != 200 or not manifest.is_json:
        report.fail("snapshot", f"manifest of {snapshot_id}: {manifest.describe()}")
    elif manifest.json().get("snapshot_id") != snapshot_id:
        report.fail("snapshot", f"manifest of {snapshot_id} names another snapshot")
    else:
        report.ok("snapshot", f"{snapshot_id}: pointer and manifest served")


# --------------------------------------------------------------------------- #
# deep                                                                         #
# --------------------------------------------------------------------------- #

def _pause_note(paused: dict) -> str:
    until = paused.get("until") or "unknown"
    return (f"the owner's pause switch is on (until {until}, reason "
            f"{paused.get('reason') or 'none'}): requests that would reach Render get "
            "503 api_paused; clear it with deploy.yml operation resume-api")


def check_deep(client: Client, report: Report, *, wake_timeout_s: float,
               expect_build: str | None = None) -> None:
    def healthy(reply) -> bool:
        if reply.error_code == "api_paused":
            return True  # the owner's switch: retrying cannot change it
        if reply.status != 200 or not reply.is_json:
            return False
        try:
            return reply.json().get("status") == "ok"
        except OpsError:
            return False

    try:
        reply = client.wait_for(
            "GET", "/api/health", healthy, deadline_s=wake_timeout_s,
            min_interval_s=15.0, max_attempts=client.remaining, timeout=120.0,
        )
    except OpsError as exc:
        report.fail("api_health", str(exc))
        return
    if reply.error_code == "api_paused":
        details = (reply.json().get("error") or {}).get("details")
        report.add("api_paused", "WARN",
                   _pause_note(details if isinstance(details, dict) else {}) +
                   "; Render not checked")
        return
    build = reply.json().get("build")
    problems = header_problems(reply, API_HEADERS)
    if problems:
        report.fail("api_health", "security headers: " + ", ".join(problems))
    elif expect_build and build != expect_build:
        report.fail("api_health", f"build {build}, expected {expect_build}")
    else:
        report.ok("api_health", f"Render answered /health through the Worker (build {build})")


# --------------------------------------------------------------------------- #
# quota                                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class BudgetLine:
    name: str
    used: float
    budget: float | None
    ratio: float | None
    alert: bool
    note: str = ""


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def _day_share(generated: datetime) -> float:
    midnight = generated.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(MIN_DAY_SHARE, (generated - midnight).total_seconds() / 86_400)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def worker_requests_line(client: Client, *, token: str, account: str, generated: datetime,
                         threshold: float) -> BudgetLine:
    """The account's Worker requests since 00:00 UTC against the pro-rated
    100,000 a day (one GraphQL query; raises OpsError on any problem)."""
    midnight = generated.replace(hour=0, minute=0, second=0, microsecond=0)
    body = json.dumps({"query": WORKER_REQUESTS_QUERY, "variables": {
        "accountTag": account, "datetimeStart": _iso(midnight),
        "datetimeEnd": _iso(generated)}}).encode()
    reply = client.post_json(GRAPHQL_PATH, body, headers={"Authorization": f"Bearer {token}"})
    if reply.status != 200 or not reply.is_json:
        raise OpsError(f"Cloudflare GraphQL Analytics API: {reply.describe()}")
    payload = reply.json()
    if payload.get("errors"):
        messages = "; ".join(str((e or {}).get("message", "?"))[:160]
                             for e in payload["errors"][:3])
        raise OpsError(f"Cloudflare GraphQL Analytics API: {messages}")
    accounts = (((payload.get("data") or {}).get("viewer") or {}).get("accounts")) or []
    if not accounts:
        raise OpsError("Cloudflare returned no account: check CLOUDFLARE_ANALYTICS_ACCOUNT and "
                       "that the token has Account Analytics: Read on it")
    rows = accounts[0].get("workersInvocationsAdaptive") or []
    used = sum(_num((row.get("sum") or {}).get("requests")) or 0.0 for row in rows)
    return _line("Worker requests today (account)", used,
                 WORKER_DAILY_REQUESTS * _day_share(generated), threshold,
                 f"{WORKER_DAILY_REQUESTS:,}/day account-wide (Workers Free), "
                 "Cloudflare's analytics")


def _line(name: str, used: float, budget: float | None, threshold: float,
          note: str = "", informational: bool = False) -> BudgetLine:
    ratio = used / budget if budget else None
    alert = (not informational) and ratio is not None and ratio > threshold
    return BudgetLine(name, used, budget, ratio, alert, note)


def evaluate_budget(report: dict, *, threshold: float = ALERT_THRESHOLD) -> list[BudgetLine]:
    """Every governor budget against its pro-rated value (pure; tested directly).

    Daily budgets (the governor's counters, which reset at 00:00 UTC) are
    pro-rated by the share of the UTC day elapsed at ``generated_at``; monthly
    provider quotas (Neon CU-hours and egress) by the share of Neon's
    consumption period. The 31-day rolling CU total is compared with the whole
    monthly quota. The per-network cap is reported but never alerts: hitting
    it is the protection working, not a quota at risk.
    """
    generated = _parse_time(report.get("generated_at")) or datetime.now(UTC)
    day_share = _day_share(generated)
    lines: list[BudgetLine] = []

    analyses = report.get("analyses") or {}
    for name, used_key, limit_key in (("global analyses/day", "admitted", "limit"),
                                      ("heavy analyses/day", "heavy", "heavy_limit")):
        used, limit = _num(analyses.get(used_key)), _num(analyses.get(limit_key))
        if used is not None and limit:
            lines.append(_line(name, used, limit * day_share, threshold,
                               f"cap {limit:g}/day, {day_share:.0%} of the UTC day elapsed"))

    compute = report.get("compute_today") or {}
    settled, provisional = _num(compute.get("settled_cu_hours")), \
        _num(compute.get("provisional_cu_hours"))
    ceiling = _num(compute.get("ceiling_cu_hours"))
    if settled is not None and ceiling:
        lines.append(_line("Neon CU-h today", settled + (provisional or 0.0),
                           ceiling * day_share, threshold,
                           f"daily ceiling {ceiling:g} CU-h ({report.get('mode', '?')} mode)"))

    rolling = _num(report.get("compute_rolling_31d_cu_hours"))
    if rolling is not None:
        lines.append(_line("Neon CU-h, last 31 days", rolling, NEON_MONTHLY_CU_HOURS,
                           threshold, f"monthly quota {NEON_MONTHLY_CU_HOURS:g} CU-h"))

    month = report.get("month") or {}
    share = _num(month.get("elapsed_share"))
    if share is not None:
        share = max(MIN_MONTH_SHARE, share)
        cu = _num(month.get("cu_hours"))
        if cu is not None:
            used = cu + (_num(month.get("bridge_cu_hours")) or 0.0)
            lines.append(_line("Neon CU-h, month to date", used, NEON_MONTHLY_CU_HOURS * share,
                               threshold, f"{share:.0%} of Neon's period elapsed"))
        egress = _num(month.get("egress_bytes"))
        if egress is not None:
            used = egress + (_num(month.get("bridge_egress_bytes")) or 0.0)
            lines.append(_line("Neon egress, month to date (bytes)", used,
                               NEON_MONTHLY_EGRESS_BYTES * share, threshold,
                               f"{share:.0%} of Neon's period elapsed"))

    prefixes = report.get("prefixes") or {}
    top, limit = _num(prefixes.get("max_admitted")), _num(prefixes.get("limit"))
    if top is not None and limit:
        lines.append(_line("busiest client network today", top, limit, threshold,
                           "per-network cap; informational", informational=True))
    return lines


def check_quota(client: Client, report: Report, *, token: str, threshold: float,
                summary: list[str], cloudflare: Client | None = None,
                cloudflare_token: str = "", cloudflare_account: str = "") -> None:
    reply = client.get("/api/_ops/budget", headers={"Authorization": f"Bearer {token}"})
    if reply.status == 404:
        report.fail("budget", "404: the Worker rejected EDGE_OPS_TOKEN (or has no OPS_TOKEN)")
        return
    if reply.status != 200 or not reply.is_json:
        report.fail("budget", f"expected a 200 JSON body, got {reply.describe()}")
        return
    body = reply.json()
    governor = body.get("governor")
    if not isinstance(governor, dict):
        report.fail("budget", "the reply has no governor section")
        return
    lines = evaluate_budget(governor, threshold=threshold)
    if not lines:
        report.fail("budget", "the governor reported no budgets")
        return
    if cloudflare is not None:
        generated = _parse_time(governor.get("generated_at")) or datetime.now(UTC)
        try:
            lines.append(worker_requests_line(cloudflare, token=cloudflare_token,
                                              account=cloudflare_account, generated=generated,
                                              threshold=threshold))
        except OpsError as exc:
            report.fail("worker_requests", str(exc))
    else:
        report.add("worker_requests", "SKIP",
                   "not monitored: set CLOUDFLARE_ANALYTICS_TOKEN and "
                   "CLOUDFLARE_ANALYTICS_ACCOUNT (docs/deployment.md, owner step 4.3)")
    summary += [f"Governor mode: **{governor.get('mode', '?')}**, "
                f"day {governor.get('day', '?')}, generated {governor.get('generated_at', '?')}",
                "", "| Budget | Used | Pro-rated budget | Share | Alert |", "|---|---|---|---|---|"]
    for line in lines:
        share = "n/a" if line.ratio is None else f"{line.ratio:.0%}"
        budget = "n/a" if line.budget is None else f"{line.budget:,.4g}"
        summary.append(f"| {line.name} | {line.used:,.4g} | {budget} | {share} | "
                       f"{'**ALERT**' if line.alert else 'ok'} |")
        status = "FAIL" if line.alert else "PASS"
        report.add(line.name, status, f"{line.used:,.4g} of {budget} ({share}); {line.note}")
    if (governor.get("month") or {}).get("cu_hours") is None:
        report.add("neon_ground_truth", "WARN",
                   "no Neon usage poll (conservative mode): month-to-date CU-hours and "
                   "egress are not reported; the rolling 31-day total stands in")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("edge", "static shell, edge health and the snapshot"),
                            ("deep", "/api/health through the Worker to Render"),
                            ("quota", "governor budgets from /api/_ops/budget")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--base-url", default=os.environ.get("PUBLIC_BASE_URL"),
                         help="the public origin (default: $PUBLIC_BASE_URL)")
        if name == "deep":
            cmd.add_argument("--wake-timeout", type=float, default=240.0,
                             help="seconds to wait for a sleeping Render instance")
            cmd.add_argument("--max-requests", type=int, default=8)
            cmd.add_argument("--expect-build", default=None)
        if name == "quota":
            cmd.add_argument("--threshold", type=float, default=ALERT_THRESHOLD)
            cmd.add_argument("--cloudflare-api-url", default=CLOUDFLARE_API,
                             help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: pass --base-url or set PUBLIC_BASE_URL", file=sys.stderr)
        return 2
    titles = {"edge": "Edge check", "deep": "Deep check", "quota": "Quota report"}
    report = Report(titles[args.command])
    summary: list[str] = []
    try:
        if args.command == "edge":
            check_edge(Client(args.base_url, label="site", max_requests=len(EDGE_PATHS)),
                       report)
        elif args.command == "deep":
            if not 1 <= args.max_requests <= 12:
                print("error: --max-requests must be 1-12", file=sys.stderr)
                return 2
            check_deep(Client(args.base_url, label="site", max_requests=args.max_requests),
                       report, wake_timeout_s=args.wake_timeout, expect_build=args.expect_build)
        else:
            token = os.environ.get("EDGE_OPS_TOKEN", "").strip()
            if not token:
                print("error: set EDGE_OPS_TOKEN (the Worker's OPS_TOKEN)", file=sys.stderr)
                return 2
            cf_token = os.environ.get("CLOUDFLARE_ANALYTICS_TOKEN", "").strip()
            cf_account = os.environ.get("CLOUDFLARE_ANALYTICS_ACCOUNT", "").strip()
            if cf_account and not ACCOUNT_ID.match(cf_account):
                print("error: CLOUDFLARE_ANALYTICS_ACCOUNT is not a 32-hex account id",
                      file=sys.stderr)
                return 2
            cloudflare = Client(args.cloudflare_api_url, label="cloudflare", max_requests=1) \
                if cf_token and cf_account else None
            check_quota(Client(args.base_url, label="site", max_requests=len(QUOTA_PATHS)),
                        report, token=token, threshold=args.threshold, summary=summary,
                        cloudflare=cloudflare, cloudflare_token=cf_token,
                        cloudflare_account=cf_account)
    except OpsError as exc:
        report.fail(args.command, str(exc))
    report.write_summary(os.environ.get("GITHUB_STEP_SUMMARY"))
    if summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    print(f"{args.command}: {report.failed} failed", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
