#!/usr/bin/env python3
"""GitHub-side housekeeping for the $0 stack: schedule health, the deploy gate and
GHCR retention.

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
          * the default branch has had no commit for 28 days or more (not
            the repository's pushed_at, which Dependabot's branch pushes
            keep fresh), leaving four weekly runs before the 60-day cutoff.
        Run weekly (Mondays, off the hour) by .github/workflows/schedules-check.yml.

    github_ops.py deploy-gate --repo Nathan98000/ATUS --sha "$GITHUB_SHA"
        Fails unless ci.yml succeeded on a push to main for exactly this
        commit, and the commit is on main (a vX.Y.Z tag can point anywhere).
        deploy.yml's preflight runs it: the deploy itself runs only the
        Worker's config tests and the SPA build, so the Worker tests,
        typecheck, lint and Playwright must have passed in CI.

    github_ops.py ghcr-cleanup --owner nathan98000 --package atus-api --keep 20 [--dry-run]
        Deletes all but the newest ``--keep`` versions (digests) of the image,
        and never the one tagged ``live`` (Render's configured image).
        Render keeps no images, so these are the API's rollback targets
        (ADR-011 "Rollback"). Never deletes when there are ``--keep`` or fewer,
        and at most ``--max-delete`` per run. Weekly: ghcr-cleanup.yml.

    github_ops.py live-build --owner nathan98000 --package atus-api [--require-live]
        Prints ``build=<commit>``: the commit of the digest tagged ``live``
        (empty before the first deploy). deep-check.yml expects /health to
        report it, with ``--require-live``: it runs only after the first deploy
        has tagged ``live``, so a missing package or tag fails. A refused
        lookup (401, 403, any other 4xx) always fails: it would switch the
        build comparison off for good. Only a transient failure (no reply,
        408, 429, 5xx) prints an empty ``build=`` and exits 0, so the health
        check still runs, without the build.

    github_ops.py deployed-image --owner nathan98000 --package atus-api --build <commit>
        Prints ``image_ref=`` and ``image_source=``: the digest ``<commit>``
        last ran as (its ``deployed-`` tag), for deploy.yml's rollback notes.
        Never this run's own ``sha-`` tag, which its push has just moved.

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
    ".github/workflows/schedules-check.yml": None,                 # this check, weekly
}
#: Alert this long after the default branch's newest commit: four more
#: weekly checks still run before GitHub's 60-day inactivity cutoff
#: (28 + 4 * 7 < 60), so a dropped run never costs the warning.
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
    findings.append(check_activity(gh, repo, now=now))
    return findings


def check_activity(gh: GitHub, repo: str, *, now: datetime) -> Finding:
    """Days since the newest commit on the default branch. Not ``pushed_at``:
    that counts pushes to any branch, Dependabot's weekly update branches
    included, and GitHub does not say whether those count as activity."""
    branch = gh.call("GET", f"/repos/{repo}").get("default_branch")
    if not isinstance(branch, str) or not NAME.match(branch):
        return Finding("repository activity", False, "no default branch reported")
    commits = gh.call("GET", f"/repos/{repo}/commits?sha={branch}&per_page=1")
    latest = commits[0] if isinstance(commits, list) and commits else {}
    committed = _time(((latest.get("commit") or {}).get("committer") or {}).get("date"))
    if committed is None:
        return Finding("repository activity", False, f"no commit date on {branch}")
    idle = now - committed
    left = INACTIVITY_CUTOFF - idle
    if idle >= INACTIVITY_ALERT:
        return Finding("repository activity", False,
                       f"last commit on {branch} {_ago(idle)} ago: GitHub disables scheduled "
                       f"workflows after 60 days without activity (about "
                       f"{max(0, left.days)} days left). Push a commit to {branch}.")
    return Finding("repository activity", True, f"last commit on {branch} {_ago(idle)} ago")


def check_deploy_gate(gh: GitHub, repo: str, sha: str, *, branch: str = "main",
                      workflow: str = "ci.yml") -> list[Finding]:
    """Whether ``sha`` may deploy: a successful ``workflow`` run on a push to
    ``branch`` for exactly this commit, and the commit is on ``branch``."""
    runs = gh.call("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}"
                          f"&event=push&branch={branch}&status=success&per_page=1")
    passed = [r for r in (runs or {}).get("workflow_runs", [])
              if r.get("head_sha") == sha and r.get("conclusion") == "success"]
    findings = [Finding("ci", True, f"{workflow} passed for {sha[:12]} on a push to {branch} "
                                    f"(run {passed[0].get('id')})") if passed else
                Finding("ci", False, f"no successful {workflow} run for {sha[:12]} on a push to "
                                     f"{branch}: wait until CI has passed for this commit (or "
                                     f"fix it), then re-run")]
    compare = gh.call("GET", f"/repos/{repo}/compare/{sha}...{branch}") or {}
    status, behind = compare.get("status"), compare.get("behind_by")
    if status in ("ahead", "identical") and behind == 0:
        findings.append(Finding("on_branch", True, f"{sha[:12]} is on {branch}"))
    else:
        findings.append(Finding("on_branch", False, f"{sha[:12]} is not on {branch} ({status}, "
                                                    f"{behind} commit(s) off it): tag only "
                                                    f"commits on {branch}"))
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


#: Tags deploy.yml's `promote` job moves to a digest once its deploy passed
#: the smoke: `live` (the Image URL Render's service is configured with, so a
#: deploy not made through the hook runs the last verified digest) and
#: `deployed-<commit>` (the digest that commit last ran as: the rollback
#: target, which no later push of the same commit can move).
LIVE_TAG = "live"
DEPLOYED_PREFIX = "deployed-"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
#: GitHub answers that say "try again later", not "this can never work".
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def plan_cleanup(versions: list[Version], *, keep: int) -> list[Version]:
    """The versions to delete: everything but the newest ``keep``, and never
    the one tagged ``live`` (pure; tested)."""
    newest_first = sorted(versions, key=lambda v: (v.created_at, v.id), reverse=True)
    return [v for v in newest_first[keep:] if LIVE_TAG not in v.tags]


def live_build(versions: list[Version]) -> str | None:
    """The commit the ``live`` digest was deployed from (its ``deployed-`` tag)."""
    for version in versions:
        if LIVE_TAG in version.tags:
            builds = [t.removeprefix(DEPLOYED_PREFIX) for t in version.tags
                      if t.startswith(DEPLOYED_PREFIX) and COMMIT.match(t[len(DEPLOYED_PREFIX):])]
            return builds[0] if builds else None
    return None


def deployed_image(versions: list[Version], build: str, this_build: str) -> tuple[str | None,
                                                                                     str]:
    """The digest ``build`` last ran as, and where that came from: its
    ``deployed-`` tag; else, for another commit than this run's, its
    ``sha-`` tag (the last push of that commit); else unknown. Never this
    run's own ``sha-`` tag, which this run's push has just moved."""
    for version in versions:
        if f"{DEPLOYED_PREFIX}{build}" in version.tags:
            return version.name, "deployed-tag"
    if build != this_build:
        for version in versions:
            if f"sha-{build}" in version.tags:
                return version.name, "sha-tag"
    return None, "unknown"


def ghcr_versions(gh: GitHub, owner_kind: str, owner: str, package: str) -> list[Version]:
    out = []
    for item in gh.paged(f"/{owner_kind}/{owner}/packages/container/{package}/versions"):
        created = _time(item.get("created_at"))
        if not isinstance(item.get("id"), int) or created is None:
            raise GitHubError("a package version has no id or created_at")
        tags = ((item.get("metadata") or {}).get("container") or {}).get("tags") or []
        out.append(Version(item["id"], str(item.get("name", "")), created, list(tags)))
    return out


def print_live_build(gh: GitHub, owner_kind: str, owner: str, package: str, *,
                     require: bool) -> int:
    """``live-build``: print ``build=<commit>`` and return the exit status (see
    the module docstring). deep-check.yml ran this with ``continue-on-error``,
    so a lasting 401/403 or a missing tag silently dropped the build comparison
    (review round 2, deep-check-build-expectation-silently-off)."""
    where = f"{owner_kind}/{owner}/packages/container/{package}"
    try:
        versions = ghcr_versions(gh, owner_kind, owner, package)
    except GitHubError as exc:
        if exc.status is None or exc.status in TRANSIENT_STATUSES:
            print(f"warning: {exc}: GitHub did not answer; the build is not compared this "
                  "time", file=sys.stderr)
            print("build=")
            return 0
        if exc.status in (401, 403):
            print(f"error: {exc}: the token may not read the versions of {where}. In the "
                  "package's settings, Manage Actions access must give this repository "
                  "read access (docs/operations.md, \"The deep check cannot read the live "
                  "tag\")", file=sys.stderr)
            return 1
        if exc.status != 404:
            raise
        versions = []
    build = live_build(versions)
    if build is None:
        missing = f"no version is tagged {LIVE_TAG} ({where})"
        if require:
            print(f"error: {missing}, although the monitors start only after the first "
                  "deploy's promote job tagged one (docs/operations.md, \"The deep check "
                  "cannot read the live tag\")", file=sys.stderr)
            return 1
        print(f"{missing} (no deploy has passed yet)", file=sys.stderr)
    print(f"build={build or ''}")
    return 0


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
    dg = sub.add_parser("deploy-gate", help="the commit passed CI and is on main")
    dg.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    dg.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    dg.add_argument("--branch", default="main")
    gc = sub.add_parser("ghcr-cleanup", help="keep only the newest image versions")
    lb = sub.add_parser("live-build", help="the commit the `live` image was deployed from")
    lb.add_argument("--require-live", action="store_true",
                    help="fail when no version is tagged live (after the first deploy)")
    di = sub.add_parser("deployed-image", help="the digest a commit last ran as")
    for cmd in (gc, lb, di):
        cmd.add_argument("--owner", required=True)
        cmd.add_argument("--owner-kind", choices=("users", "orgs"), default="users")
        cmd.add_argument("--package", required=True)
    gc.add_argument("--keep", type=int, default=20)
    gc.add_argument("--max-delete", type=int, default=50)
    gc.add_argument("--dry-run", action="store_true")
    di.add_argument("--build", required=True, help="the commit /health reported")
    di.add_argument("--this-build", default=os.environ.get("GITHUB_SHA", ""),
                    help="this run's commit (its sha- tag was just moved)")
    args = parser.parse_args(argv)
    if args.command == "deploy-gate" and not (
            args.repo and REPOSITORY.match(args.repo) and re.fullmatch(r"[0-9a-f]{40}",
                                                                       args.sha or "")
            and NAME.match(args.branch)):
        print("error: pass --repo <owner>/<name>, --sha <40 hex> and --branch", file=sys.stderr)
        return 2

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

        if args.command == "deploy-gate":
            findings = check_deploy_gate(gh, args.repo, args.sha, branch=args.branch)
            lines = ["### Deploy gate", "", "| Check | Result | Detail |", "|---|---|---|"]
            for f in findings:
                print(f"[{'PASS' if f.ok else 'FAIL'}] {f.name}: {f.detail}", flush=True)
                lines.append(f"| {f.name} | {'ok' if f.ok else '**FAIL**'} | {f.detail} |")
            _summary(lines)
            failed = sum(1 for f in findings if not f.ok)
            print(f"deploy gate: {failed} failed")
            return 1 if failed else 0

        if not (NAME.match(args.owner) and NAME.match(args.package)):
            print("error: malformed --owner or --package", file=sys.stderr)
            return 2
        if args.command == "live-build":
            return print_live_build(gh, args.owner_kind, args.owner, args.package,
                                    require=args.require_live)
        if args.command == "deployed-image":
            try:
                versions = ghcr_versions(gh, args.owner_kind, args.owner, args.package)
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                versions = []
            if not COMMIT.match(args.build):
                print(f"unknown build {args.build[:60]!r}: no image to name", file=sys.stderr)
                print("image_ref=\nimage_source=unknown")
                return 0
            digest, source = deployed_image(versions, args.build, args.this_build)
            ref = f"ghcr.io/{args.owner.lower()}/{args.package}@{digest}" if digest else ""
            print(f"image_ref={ref}\nimage_source={source}")
            return 0
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
