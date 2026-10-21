#!/usr/bin/env python3
"""ci.yml's dependency audits, with a reviewed, expiring allowlist (review round 3).

pip-audit and ``npm audit`` fail on every known advisory, whether or not a
fixed version exists, and deploy.yml's preflight needs a green ci.yml push run
for the commit it deploys. Without an exception path, one disclosed advisory
in a runtime dependency with no fix yet would block every deploy, hotfixes
included, until someone edited ci.yml by hand. This gate keeps the audits
strict and adds the same policy as the image scan's ``.trivyignore``: an
advisory is accepted only by an entry with a reason and an expiry at most 31
days out, and only until that date (docs/operations.md, "A dependency audit
blocks a deploy").

    audit_gate.py pip --ignore-file .pip-audit-ignore -- <pip-audit arguments>
        runs ``pip-audit <arguments>`` with ``--ignore-vuln ID`` for each entry
        that has not expired (pip-audit matches an advisory's ID or any alias)

    audit_gate.py npm --ignore-file .npm-audit-ignore [--audit-level high] -- <npm audit arguments>
        in the npm project: runs ``npm audit --json <arguments>`` and fails on
        every advisory at or above the level (default: any) whose GHSA ID has
        no unexpired entry, as ``npm audit --audit-level`` would without one

Allowlist (one per audited tree; the format of ``.trivyignore``)::

    # 2026-11-02 owner: libfoo in the API's lock; the API never calls it; no fix yet
    GHSA-xxxx-xxxx-xxxx exp:2026-12-02

Every entry is one advisory ID (``CVE-``, ``GHSA-`` or ``PYSEC-`` for pip;
``GHSA-`` for npm, whose report names advisories by their GitHub URL) with
``exp:YYYY-MM-DD``, preceded by a comment of at least 10 characters. An entry
stops counting on its expiry date. Standard library only.

Exit status: 0 pass, 1 an advisory not accepted (or the audit itself failed),
2 a malformed allowlist or a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

HORIZON_DAYS = 31
ENTRY = re.compile(r"^(?P<id>\S+) exp:(?P<exp>\S+)$")
GHSA = r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}"
ID_PATTERNS = {
    "pip": re.compile(rf"^(?:CVE-\d{{4}}-\d{{4,}}|{GHSA}|PYSEC-\d{{4}}-\d+)$"),
    "npm": re.compile(rf"^{GHSA}$"),
}
#: npm's severities, lowest first (``npm audit --audit-level``).
SEVERITIES = ("info", "low", "moderate", "high", "critical")


class GateError(Exception):
    """The audit could not run or answered something unreadable (exit 1)."""


class AllowlistError(Exception):
    """The allowlist is missing or breaks the policy (exit 2)."""


@dataclass(frozen=True)
class Entry:
    id: str
    expires: date
    line: int

    def active(self, today: date) -> bool:
        return today < self.expires


@dataclass(frozen=True, order=True)
class Advisory:
    id: str
    package: str
    severity: str
    title: str
    url: str


def parse_allowlist(text: str, today: date, kind: str) -> tuple[list[Entry], list[str]]:
    """The entries of an allowlist and every problem with it (pure).

    A problem is a line that is not ``<id> exp:YYYY-MM-DD`` (with an ID of the
    kind's form), an entry with no reason comment above it, or an expiry more
    than ``HORIZON_DAYS`` days after ``today``."""
    pattern = ID_PATTERNS[kind]
    entries: list[Entry] = []
    problems: list[str] = []
    reason = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            reason = False
            continue
        if stripped.startswith("#"):
            reason = len(stripped.lstrip("# ")) >= 10
            continue
        match = ENTRY.match(stripped)
        try:
            expires = date.fromisoformat(match["exp"]) if match else None
        except ValueError:
            expires = None
        if not match or expires is None or not pattern.match(match["id"]):
            problems.append(f"line {number}: not '<advisory id> exp:YYYY-MM-DD' "
                            f"(a {kind} advisory ID)")
            continue
        if not reason:
            problems.append(f"line {number}: {match['id']} has no reason comment above it")
        if expires > today + timedelta(days=HORIZON_DAYS):
            problems.append(f"line {number}: {match['id']} expires more than "
                            f"{HORIZON_DAYS} days out")
        entries.append(Entry(match["id"], expires, number))
    return entries, problems


def npm_advisories(report: object) -> list[Advisory]:
    """Every advisory in an ``npm audit --json`` report (auditReportVersion 2)."""
    if not isinstance(report, dict):
        raise GateError("npm audit printed no JSON object")
    if "error" in report:
        error = report["error"] if isinstance(report["error"], dict) else {}
        raise GateError(f"npm audit failed: {error.get('code', '?')} "
                        f"{str(error.get('summary', ''))[:300]}")
    if report.get("auditReportVersion") != 2:
        raise GateError("unknown npm audit report format "
                        f"(auditReportVersion {report.get('auditReportVersion')!r})")
    found: set[Advisory] = set()
    for package in (report.get("vulnerabilities") or {}).values():
        for via in package.get("via", []) if isinstance(package, dict) else []:
            if not isinstance(via, dict):  # a dependency name: its own entry has the advisory
                continue
            url = str(via.get("url", ""))
            ghsa = re.search(GHSA, url)
            found.add(Advisory(
                id=ghsa.group(0) if ghsa else f"npm-advisory-{via.get('source', '?')}",
                package=str(via.get("name", "?")),
                severity=str(via.get("severity", "critical")),
                title=str(via.get("title", ""))[:200],
                url=url,
            ))
    return sorted(found)


def severity_rank(severity: str) -> int:
    """Unknown severities count as critical: the gate fails closed."""
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def judge_npm(advisories: list[Advisory], entries: list[Entry], level: str,
              today: date) -> tuple[list[str], bool]:
    """(report lines, passed): every advisory at or above ``level`` needs an active entry."""
    active = {entry.id: entry.expires for entry in entries if entry.active(today)}
    lines, passed, used = [], True, set()
    below = 0
    for advisory in advisories:
        if severity_rank(advisory.severity) < severity_rank(level):
            below += 1
            continue
        detail = f"{advisory.id} {advisory.severity} {advisory.package}: {advisory.title}"
        if advisory.id in active:
            used.add(advisory.id)
            lines.append(f"[ACCEPTED until {active[advisory.id]}] {detail}")
        else:
            passed = False
            lines.append(f"[FAIL] {detail} {advisory.url}".rstrip())
    if below:
        lines.append(f"{below} advisory(ies) below --audit-level {level}: not gated")
    for entry in entries:
        if entry.active(today) and entry.id not in used:
            lines.append(f"note: {entry.id} (line {entry.line}) matched nothing at or above "
                         f"{level}: remove it")
    return lines, passed


def load_allowlist(path: Path, kind: str, today: date) -> list[Entry]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AllowlistError(f"cannot read the allowlist {path}: {exc.strerror}") from None
    entries, problems = parse_allowlist(text, today, kind)
    if problems:
        raise AllowlistError("\n".join(f"{path}: {problem}" for problem in problems))
    return entries


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        usage="%(prog)s {npm,pip} --ignore-file FILE [--audit-level LEVEL] -- AUDIT_ARGS...")
    parser.add_argument("kind", choices=sorted(ID_PATTERNS))
    parser.add_argument("--ignore-file", required=True, type=Path,
                        help="the reviewed allowlist of this audit")
    parser.add_argument("--audit-level", choices=SEVERITIES, default="info",
                        help="npm: the lowest severity that fails (default: any)")
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    audit_args = argv[split + 1:]  # the audit tool's own arguments
    today = today or date.today()
    try:
        entries = load_allowlist(args.ignore_file, args.kind, today)
    except AllowlistError as exc:
        print(f"audit_gate: {exc}", file=sys.stderr)
        return 2
    for entry in entries:
        state = (f"accepted until {entry.expires}" if entry.active(today)
                 else f"EXPIRED on {entry.expires}, no longer accepted")
        print(f"allowlist {args.ignore_file}: {entry.id} {state}")

    if args.kind == "pip":
        ignores = [arg for entry in entries if entry.active(today)
                   for arg in ("--ignore-vuln", entry.id)]
        command = ["pip-audit", *audit_args, *ignores]
        print("$ " + " ".join(command), flush=True)
        try:
            return 0 if subprocess.run(command, check=False).returncode == 0 else 1
        except OSError as exc:
            print(f"audit_gate: cannot run pip-audit: {exc.strerror}", file=sys.stderr)
            return 1

    command = ["npm", "audit", "--json", *audit_args]
    print("$ " + " ".join(command), flush=True)
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        print(f"audit_gate: cannot run npm: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        advisories = npm_advisories(json.loads(result.stdout or "null"))
    except (ValueError, GateError) as exc:
        print(f"audit_gate: {exc}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[-1000:], file=sys.stderr)
        return 1
    lines, passed = judge_npm(advisories, entries, args.audit_level, today)
    for line in lines:
        print(line)
    print(f"npm audit gate: {'passed' if passed else 'FAILED'} "
          f"({len(advisories)} advisory(ies) in the report)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
