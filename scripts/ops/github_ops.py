#!/usr/bin/env python3
"""GitHub-side housekeeping for the $0 stack: schedule health and GHCR retention.

Standard library only; the token comes from ``GH_TOKEN`` (or ``GITHUB_TOKEN``)
and is sent only to the GitHub REST API (the endpoints ``gh api`` calls),
never printed.

    github_ops.py schedules --repo Nathan98000/ATUS
        Fails loudly when monitoring could be silently off (ADR-011 "Risks";
        GitHub disables scheduled workflows "when no repository activity has
        occurred in 60 days", and scheduled runs "may be delayed or dropped"):
          * a scheduled workflow is not ``active`` (disabled manually or for
            inactivity);
          * a monitor has had no scheduled run for much longer than its cadence;
          * the repository has had no push for 28 days or more, so the next
            monthly check could come after the 60-day cutoff.
        Run monthly by .github/workflows/schedules-check.yml.

    github_ops.py ghcr-cleanup --owner nathan98000 --package atus-api --keep 20 [--dry-run]
        Deletes all but the newest ``--keep`` versions (digests) of the image.
        Render keeps no images, so these are the API's rollback targets
        (ADR-011 "Rollback"). Never deletes when there are ``--keep`` or fewer,
        and at most ``--max-delete`` per run. Weekly: ghcr-cleanup.yml.

Exit status: 0 healthy / done, 1 a check or deletion failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

API_URL = "https://api.github.com"
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Every scheduled workflow, with the longest acceptable gap since its last
#: scheduled run (cadence plus GitHub's delays; None: state only).
#: tests/deploy/test_workflows.py keeps this in step with the workflow files.
SCHEDULED_WORKFLOWS: dict[str, timedelta | None] = {
    ".github/workflows/edge-check.yml": timedelta(hours=3),        # every 30 min
    ".github/workflows/deep-check.yml": timedelta(hours=24),       # every 6 h
    ".github/workflows/quota-report.yml": timedelta(days=3),       # daily
    ".github/workflows/ghcr-cleanup.yml": timedelta(days=15),      # weekly
    ".github/workflows/schedules-check.yml": None,                 # this check, monthly
}
#: Alert this long after the last push: a monthly check then still runs
#: before GitHub's 60-day inactivity cutoff (28 + 31 < 60).
INACTIVITY_ALERT = timedelta(days=28)
INACTIVITY_CUTOFF = timedelta(days=60)


class GitHubError(Exception):
    """The API call failed (message safe to print)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, *, api_url: str = API_URL, timeout: float = 30.0) -> None:
        parts = urlsplit(api_url)
        loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
        if not (parts.scheme == "https" or (parts.scheme == "http" and loopback)):
            raise GitHubError("the API URL must be https")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str) -> Any:
        request = urllib.request.Request(self.api_url + path, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "atus-github-ops/1",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{method} {path.split('?')[0]}: HTTP {exc.code}",
                              exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(f"{method} {path.split('?')[0]}: no reply "
                              f"({type(exc).__name__})") from None
        return json.loads(body) if body else None

    def paged(self, path: str, key: str | None = None, *, max_pages: int = 20) -> list[dict]:
        items: list[dict] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            data = self.call("GET", f"{path}{sep}per_page=100&page={page}")
            batch = data.get(key, []) if key else data
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise GitHubError(f"GET {path}: more than {max_pages} pages")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Finding:
    name: str
    ok: bool
    detail: str


def check_schedules(gh: GitHub, repo: str, *, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    workflows = {w.get("path"): w for w in gh.paged(f"/repos/{repo}/actions/workflows",
                                                     "workflows")}
    for path, max_gap in SCHEDULED_WORKFLOWS.items():
        name = path.rsplit("/", 1)[-1]
        workflow = workflows.get(path)
        if workflow is None:
            findings.append(Finding(name, False, "not found in the repository's workflows"))
            continue
        state = workflow.get("state")
        if state != "active":
            findings.append(Finding(name, False, f"state is {state!r}: re-enable it (Actions "
                                                 f"-> {workflow.get('name', name)} -> Enable)"))
            continue
        if max_gap is None:
            findings.append(Finding(name, True, "active"))
            continue
        runs = gh.call("GET", f"/repos/{repo}/actions/workflows/{workflow['id']}/runs"
                              "?event=schedule&per_page=1").get("workflow_runs", [])
        last = _time(runs[0].get("created_at")) if runs else None
        created = _time(workflow.get("created_at")) or now
        if last is None:
            young = now - created < max_gap
            findings.append(Finding(name, young, "active; no scheduled run yet" +
                                    ("" if young else f" in {now - created}")))
        elif now - last > max_gap:
            findings.append(Finding(name, False, f"active, but the last scheduled run was "
                                                 f"{_ago(now - last)} ago (expected within "
                                                 f"{_ago(max_gap)}): runs are being dropped"))
        else:
            findings.append(Finding(name, True, f"active; last scheduled run {_ago(now - last)} "
                                                "ago"))
    pushed = _time(gh.call("GET", f"/repos/{repo}").get("pushed_at"))
    if pushed is None:
        findings.append(Finding("repository activity", False, "no pushed_at reported"))
    else:
        idle = now - pushed
        left = INACTIVITY_CUTOFF - idle
        if idle >= INACTIVITY_ALERT:
            findings.append(Finding("repository activity", False,
                                    f"last push {_ago(idle)} ago: GitHub disables scheduled "
                                    f"workflows after 60 days without activity "
                                    f"(about {max(0, left.days)} days left). Push a commit."))
        else:
            findings.append(Finding("repository activity", True, f"last push {_ago(idle)} ago"))
    return findings


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


@dataclass
class Version:
    id: int
    name: str
    created_at: datetime
    tags: list[str]


def plan_cleanup(versions: list[Version], *, keep: int) -> list[Version]:
    """The versions to delete: everything but the newest ``keep`` (pure; tested)."""
    newest_first = sorted(versions, key=lambda v: (v.created_at, v.id), reverse=True)
    return newest_first[keep:]


def ghcr_versions(gh: GitHub, owner_kind: str, owner: str, package: str) -> list[Version]:
    out = []
    for item in gh.paged(f"/{owner_kind}/{owner}/packages/container/{package}/versions"):
        created = _time(item.get("created_at"))
        if not isinstance(item.get("id"), int) or created is None:
            raise GitHubError("a package version has no id or created_at")
        tags = ((item.get("metadata") or {}).get("container") or {}).get("tags") or []
        out.append(Version(item["id"], str(item.get("name", "")), created, list(tags)))
    return out


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-url", default=API_URL, help=argparse.SUPPRESS)  # tests only
    sub = parser.add_subparsers(dest="command", required=True)
    sch = sub.add_parser("schedules", help="scheduled workflows enabled and running")
    sch.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    sch.add_argument("--now", default=None, help=argparse.SUPPRESS)  # tests only
    gc = sub.add_parser("ghcr-cleanup", help="keep only the newest image versions")
    gc.add_argument("--owner", required=True)
    gc.add_argument("--owner-kind", choices=("users", "orgs"), default="users")
    gc.add_argument("--package", required=True)
    gc.add_argument("--keep", type=int, default=20)
    gc.add_argument("--max-delete", type=int, default=50)
    gc.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("error: set GH_TOKEN (or GITHUB_TOKEN)", file=sys.stderr)
        return 2
    try:
        gh = GitHub(token, api_url=args.api_url)
        if args.command == "schedules":
            if not args.repo or not REPOSITORY.match(args.repo):
                print("error: pass --repo <owner>/<name>", file=sys.stderr)
                return 2
            now = _time(args.now) if args.now else datetime.now(UTC)
            findings = check_schedules(gh, args.repo, now=now)
            lines = ["### Scheduled workflows", "", "| Check | Result | Detail |",
                     "|---|---|---|"]
            for f in findings:
                print(f"[{'PASS' if f.ok else 'FAIL'}] {f.name}: {f.detail}", flush=True)
                lines.append(f"| {f.name} | {'ok' if f.ok else '**FAIL**'} | {f.detail} |")
            _summary(lines)
            failed = sum(1 for f in findings if not f.ok)
            print(f"schedules: {failed} failed")
            return 1 if failed else 0

        if not (NAME.match(args.owner) and NAME.match(args.package)):
            print("error: malformed --owner or --package", file=sys.stderr)
            return 2
        if args.keep < 1 or args.max_delete < 0:
            print("error: --keep must be >= 1 and --max-delete >= 0", file=sys.stderr)
            return 2
        try:
            versions = ghcr_versions(gh, args.owner_kind, args.owner, args.package)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            print(f"no package {args.owner}/{args.package} yet (nothing pushed): nothing to do")
            return 0
        doomed = plan_cleanup(versions, keep=args.keep)
        print(f"{len(versions)} versions; keeping the newest {min(args.keep, len(versions))}; "
              f"{len(doomed)} to delete" + (" (dry run)" if args.dry_run else ""))
        if len(doomed) > args.max_delete:
            print(f"error: {len(doomed)} deletions exceed --max-delete {args.max_delete}; "
                  "refusing (raise it deliberately)", file=sys.stderr)
            return 1
        lines = ["### GHCR retention", "", f"{len(versions)} versions, keep {args.keep}, "
                 f"delete {len(doomed)}{' (dry run)' if args.dry_run else ''}", ""]
        for version in doomed:
            label = f"{version.name} {','.join(version.tags) or '(untagged)'} " \
                    f"{version.created_at:%Y-%m-%d}"
            if not args.dry_run:
                gh.call("DELETE", f"/{args.owner_kind}/{args.owner}/packages/container/"
                                  f"{args.package}/versions/{version.id}")
            print(("would delete " if args.dry_run else "deleted ") + label, flush=True)
            lines.append(f"- {'would delete' if args.dry_run else 'deleted'} `{label}`")
        _summary(lines)
        return 0
    except (GitHubError, ValueError, KeyError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
