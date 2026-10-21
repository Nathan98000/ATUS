"""Workflows and runbooks: the confirmed infra findings of the Phase 5 review, round 3.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script), the test reads it too, so a change
there fails here until the docs follow. Read-only; needs neither Docker nor
the network (the audit gate runs against stand-in pip-audit and npm executables).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from .conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
DEPLOYMENT = DOCS_DIR / "deployment.md"
OPERATIONS = DOCS_DIR / "operations.md"
SECURITY = DOCS_DIR / "security.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, single spaces, no backticks: wording checks ignore wrapping and code spans."""
    return re.sub(r"\s+", " ", text.replace("`", ""))


def _section(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))] if end else text[begin:]


def _workflow(name: str) -> dict:
    return yaml.safe_load(_read(WORKFLOWS / name))


# --- pause-switch-queued-behind-pending-deploy ----------------------------------------


def github_expression(expression: str, context: dict) -> object:
    """Evaluate the small subset of GitHub's expression syntax a concurrency group uses
    (``&&``, ``||``, ``==``, ``!=``, parentheses, string literals, dotted context names).
    GitHub's ``&&``/``||`` return an operand, as Python's ``and``/``or`` do."""
    inner = re.fullmatch(r"\$\{\{(.*)\}\}", expression.strip(), re.DOTALL)
    if not inner:
        return expression
    names = {}

    def name(match: re.Match) -> str:
        key = f"_v{len(names)}"
        names[key] = context.get(match.group(0))
        return key

    python = re.sub(r"'[^']*'|[A-Za-z_][A-Za-z0-9_.-]*",
                    lambda m: m.group(0) if m.group(0).startswith("'") else name(m),
                    inner.group(1))
    python = python.replace("&&", " and ").replace("||", " or ")
    return eval(python, {"__builtins__": {}}, names)  # noqa: S307 (test-only, literal input)


@pytest.mark.parametrize(("event", "operation", "group"), [
    ("push", None, "deploy-production"),                     # a v* tag
    ("workflow_dispatch", "deploy", "deploy-production"),
    ("workflow_dispatch", "pause-api", "deploy-production-switch"),
    ("workflow_dispatch", "resume-api", "deploy-production-switch"),
])
def test_the_pause_switch_has_its_own_concurrency_group(event, operation, group):
    """pause-switch-queued-behind-pending-deploy: pause-api and resume-api (job api-switch)
    shared deploy.yml's one workflow-level group, so the emergency switch waited behind any
    Deploy run in progress or awaiting approval (up to 30 days), and a later dispatch
    cancelled it while it waited. Deploys (and re-runs of their promote job) keep their
    group; the switch has its own, still never cancelling a running switch."""
    deploy = _workflow("deploy.yml")
    concurrency = deploy["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    context = {"github.event_name": event, "inputs.operation": operation}
    assert github_expression(concurrency["group"], context) == group
    # The jobs a pause operation runs are exactly the ones its group covers.
    switch = deploy["jobs"]["api-switch"]
    assert switch["if"] == \
        "${{ github.event_name == 'workflow_dispatch' && inputs.operation != 'deploy' }}"
    assert all("concurrency" not in job for job in deploy["jobs"].values())


def test_the_pause_runbooks_say_the_switch_does_not_wait_for_deploys():
    """pause-switch-queued-behind-pending-deploy: 'Pausing the API' (the runbook every
    containment step points to) says the switch never waits behind a deploy and what
    happens to a deploy running at the same time; the rollback's promote caveat names the
    deploys' group, not 'one' group for the whole workflow."""
    operations = _read(OPERATIONS)
    runbook = _flat(_section(operations, "### Pausing the API", "\n### "))
    for needle in ("own concurrency group (deploy-production-switch;",
                   "never waits behind a Deploy run",
                   "only the latest runs"):
        assert needle in runbook, needle
    rollback = _flat(_section(operations, "### Rollback", "- **Edge**"))
    assert "deploy.yml's deploys share one concurrency group (deploy-production)" in rollback
    assert "has one workflow-level concurrency group" not in rollback
    step = _flat(_section(_read(DEPLOYMENT), "### Rollback", "- **Data:**"))
    assert "the deploys' concurrency group (deploy-production)" in step
    assert "one concurrency group" not in step
    workflow = _read(WORKFLOWS / "deploy.yml")
    assert "(one concurrency group)" not in workflow
    header = _flat(workflow[:workflow.index("\nname:")])
    assert "its own concurrency group" in header
    r13 = _flat(_section(_read(SECURITY), "**R13.", "**R14."))
    assert "its own concurrency group" in r13


# --- cf-token-first-proven-after-render-switch ----------------------------------------


def _deploy_steps() -> list[dict]:
    return _workflow("deploy.yml")["jobs"]["deploy"]["steps"]


def _index(steps: list[dict], marker: str) -> int:
    """The first step whose script contains ``marker`` (the rollback notes, last, quote
    several commands)."""
    return next(i for i, step in enumerate(steps) if marker in step.get("run", ""))


def test_the_cloudflare_token_is_proven_before_the_render_hook():
    """cf-token-first-proven-after-render-switch: the only use of CLOUDFLARE_API_TOKEN
    before the Render deploy hook was `wrangler deployments status` in a
    continue-on-error step that discarded stderr and ended in `|| true`, so an expired,
    revoked or wrong-account token first failed the job at `wrangler deploy`, after Render
    had switched to the new image: a half deploy. The check is now fatal and comes first."""
    steps = _deploy_steps()
    check = _index(steps, "wrangler deployments status")
    hook = _index(steps, "render_deploy.py trigger")
    assert check < hook < _index(steps, "npx --no-install wrangler deploy \\")
    step = steps[check]
    assert "continue-on-error" not in step and "if" not in step
    assert "|| true" not in step["run"] and "/dev/null" not in step["run"]
    assert step["env"]["CLOUDFLARE_API_TOKEN"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    assert step["env"]["CLOUDFLARE_ACCOUNT_ID"] == "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}"
    # Every step between it and the hook may fail only without consequence.
    for between in steps[check + 1:hook]:
        assert "CLOUDFLARE_API_TOKEN" not in str(between.get("env", {})), between["name"]
    # The rollback notes still read what it recorded.
    assert 'worker-before.json"' in step["run"]
    notes = steps[_index(steps, "### Rollback (docs/deployment.md")]
    assert "$RUNNER_TEMP/worker-before.json" in notes["run"]


def test_every_documented_token_check_authenticates():
    """cf-token-first-proven-after-render-switch: owner step 1.5 tested the token with
    `wrangler deploy --dry-run`, which never authenticates (wrangler 4.148 skips
    requireAuth for a dry run and exits before any API call), so it passed with any
    token; the 90-day rotation had no check at all. Both now run an authenticated,
    read-only `wrangler deployments status` with the new token, read without echo."""
    deployment = _read(DEPLOYMENT)
    owner = _flat(_section(deployment, "5. **Account API Tokens", "### 2. Render"))
    assert "only if a wrangler deploy --dry-run" not in owner
    assert "a dry run never authenticates" in owner
    for needle in ("read -rs CLOUDFLARE_API_TOKEN", "npx --no-install wrangler deployments "
                   "status", "unset CLOUDFLARE_API_TOKEN"):
        assert needle in owner, needle
    rotation = _flat(_section(deployment, "5. **Cloudflare API token**", "6. **Render deploy"))
    assert "wrangler deployments status" in rotation and "owner step 1.5" in rotation
    assert rotation.index("wrangler deployments status") < rotation.index("Delete the old token")
    row = next(line for line in _read(OPERATIONS).splitlines()
               if line.startswith("| Cloudflare API token (90-day TTL)"))
    assert "wrangler deployments status" in row
    job = _flat(_section(deployment, "**The deploy job.**", "**Rehearsed locally"))
    assert "before the Render hook" in job and "nothing has changed" in job
    for text in (deployment, _read(OPERATIONS)):  # the old advice, anywhere
        assert "--dry-run with the token" not in _flat(text)


# --- audit-gates-have-no-exception-path ------------------------------------------------

#: ci.yml job -> (gate kind, its allowlist, the audit's own arguments after `--`).
AUDIT_GATES = {
    "supply-chain": ("pip", ".pip-audit-ignore",
                     "--strict --require-hashes --disable-pip -r requirements/api.lock.txt"),
    "frontend": ("npm", "frontend/.npm-audit-ignore", "--omit=dev"),
    "edge": ("npm", "edge/.npm-audit-ignore", "--omit=dev"),
}


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location(
        "audit_gate", REPO_ROOT / "deploy" / "ci" / "audit_gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("job_id", sorted(AUDIT_GATES))
def test_every_dependency_audit_has_a_reviewed_expiring_allowlist(job_id):
    """audit-gates-have-no-exception-path: pip-audit and npm audit failed on any advisory,
    fixed or not, with no reviewed exception path like .trivyignore, so one disclosed but
    unfixed advisory in a runtime dependency failed every push run and deploy.yml's
    preflight refused every deploy, hotfixes included. Each audit now runs through
    deploy/ci/audit_gate.py with its own allowlist, under the .trivyignore policy."""
    kind, allowlist, audit_args = AUDIT_GATES[job_id]
    job = _workflow("ci.yml")["jobs"][job_id]
    runs = [step["run"].strip() for step in job["steps"] if "run" in step]
    assert not [run for run in runs if re.match(r"(pip-audit|npm audit)\b", run)], runs
    (command,) = [run for run in runs if "audit_gate.py" in run]
    workdir = job.get("defaults", {}).get("run", {}).get("working-directory")
    prefix = "../" if workdir else ""
    ignore = allowlist[len(workdir) + 1:] if workdir else allowlist
    level = " --audit-level high" if job_id == "frontend" else ""
    assert command == (f"python3 {prefix}deploy/ci/audit_gate.py {kind} --ignore-file "
                       f"{ignore}{level} -- {audit_args}")
    assert (REPO_ROOT / allowlist).is_file()


@pytest.mark.parametrize("allowlist", sorted({gate[1] for gate in AUDIT_GATES.values()}))
def test_the_committed_audit_allowlists_follow_the_policy(gate, allowlist):
    kind = next(k for k, path, _ in AUDIT_GATES.values() if path == allowlist)
    entries, problems = gate.parse_allowlist(_read(REPO_ROOT / allowlist), date.today(), kind)
    assert problems == []
    assert all(entry.active(date.today()) for entry in entries), "remove expired entries"


def test_the_audit_allowlist_policy(gate):
    today = date(2026, 11, 2)
    good = ("# 2026-11-02 owner: libfoo, not reachable from the API\n"
            "GHSA-68fv-2mgg-jv7q exp:2026-12-02\n")
    assert gate.parse_allowlist(good, today, "npm") == (
        [gate.Entry("GHSA-68fv-2mgg-jv7q", date(2026, 12, 2), 2)], [])
    pip = ("# 2026-11-02 owner: reviewed, no fix\nCVE-2026-12345 exp:2026-11-30\n"
           "PYSEC-2026-7 exp:2026-11-30\n")
    assert gate.parse_allowlist(pip, today, "pip")[1] == []
    for bad in ("GHSA-68fv-2mgg-jv7q exp:2026-12-02\n",                           # no reason
                "# a reason, reviewed\nGHSA-68fv-2mgg-jv7q\n",                     # no expiry
                "# a reason, reviewed\nGHSA-68fv-2mgg-jv7q exp:2027-06-01\n",      # too far
                "# a reason, reviewed\nGHSA-68fv-2mgg-jv7q exp:2026-13-01\n",      # no date
                "# a reason, reviewed\nCVE-2026-12345 exp:2026-12-01\n",           # not npm's
                "# reason\nGHSA-68fv-2mgg-jv7q exp:2026-12-01\n"):                 # too short
        assert gate.parse_allowlist(bad, today, "npm")[1], bad
    expired = gate.Entry("GHSA-68fv-2mgg-jv7q", date(2026, 11, 2), 2)
    assert not expired.active(today)  # stops counting on its expiry date, as Trivy's


def _fake_tool(tmp_path: Path, monkeypatch, name: str, stdout: str, status: int) -> Path:
    """An executable ``name`` first on PATH that records its arguments, prints ``stdout``
    and exits with ``status``; returns where it records the arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / f"{name}.out").write_text(stdout)
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{tmp_path}/{name}.argv'\n"
                      f"cat '{tmp_path}/{name}.out'\nexit {status}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / f"{name}.argv"


def _allowlist(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "audit-ignore"
    path.write_text(text)
    return path


def test_pip_audit_gets_the_unexpired_entries_as_ignore_vuln(gate, tmp_path, monkeypatch,
                                                             capsys):
    argv = _fake_tool(tmp_path, monkeypatch, "pip-audit", "", 0)
    allowlist = _allowlist(tmp_path, "# 2026-11-02 owner: no fix, not reachable\n"
                                     "PYSEC-2026-7 exp:2026-11-30\n"
                                     "GHSA-68fv-2mgg-jv7q exp:2026-11-02\n")
    args = ["pip", "--ignore-file", str(allowlist), "--", "--strict", "-r", "api.lock.txt"]
    assert gate.main(args, today=date(2026, 11, 2)) == 0
    assert argv.read_text().split() == ["--strict", "-r", "api.lock.txt",
                                        "--ignore-vuln", "PYSEC-2026-7"]
    out = capsys.readouterr().out
    assert "PYSEC-2026-7 accepted until 2026-11-30" in out
    assert "GHSA-68fv-2mgg-jv7q EXPIRED on 2026-11-02" in out
    _fake_tool(tmp_path, monkeypatch, "pip-audit", "", 1)  # a finding not accepted
    assert gate.main(args, today=date(2026, 11, 2)) == 1


def _npm_report(*advisories: tuple[str, str, str]) -> str:
    """An ``npm audit --json`` report (auditReportVersion 2) with one package per
    (package, GHSA id, severity), plus a dependent that names it only by package."""
    vulnerabilities: dict = {}
    for package, ghsa, severity in advisories:
        entry = vulnerabilities.setdefault(package, {"name": package, "severity": severity,
                                                     "via": [], "effects": ["parent"]})
        entry["via"].append({"source": 1241209, "name": package, "dependency": package,
                             "title": f"{package} advisory", "severity": severity,
                             "url": f"https://github.com/advisories/{ghsa}"})
    if advisories:
        vulnerabilities["parent"] = {"name": "parent", "severity": "high",
                                     "via": [advisories[0][0]], "effects": []}
    return json.dumps({"auditReportVersion": 2, "vulnerabilities": vulnerabilities,
                       "metadata": {"vulnerabilities": {"total": len(advisories)}}})


@pytest.mark.parametrize(("advisories", "allowlist", "level", "status", "expected"), [
    # Not accepted: fails, as npm audit --audit-level would.
    ([("source-map-js", "GHSA-68fv-2mgg-jv7q", "high")], "", "high", 1,
     "[FAIL] GHSA-68fv-2mgg-jv7q high source-map-js"),
    # Accepted until its expiry.
    ([("source-map-js", "GHSA-68fv-2mgg-jv7q", "high")],
     "# 2026-11-02 owner: dev path only\nGHSA-68fv-2mgg-jv7q exp:2026-11-20\n", "high", 0,
     "[ACCEPTED until 2026-11-20] GHSA-68fv-2mgg-jv7q"),
    # Expired: fails again.
    ([("source-map-js", "GHSA-68fv-2mgg-jv7q", "high")],
     "# 2026-10-02 owner: dev path only\nGHSA-68fv-2mgg-jv7q exp:2026-11-02\n", "high", 1,
     "[FAIL] GHSA-68fv-2mgg-jv7q"),
    # Below the frontend's level: not gated; the edge gates every severity.
    ([("brace-expansion", "GHSA-q2hr-2g5m-vwhr", "moderate")], "", "high", 0,
     "1 advisory(ies) below --audit-level high"),
    ([("brace-expansion", "GHSA-q2hr-2g5m-vwhr", "moderate")], "", "info", 1,
     "[FAIL] GHSA-q2hr-2g5m-vwhr moderate"),
    # One package, two advisories: accepting one does not accept the other.
    ([("brace-expansion", "GHSA-qhr7-859c-m2p7", "high"),
      ("brace-expansion", "GHSA-6j4f-fj2g-mc7p", "high")],
     "# 2026-11-02 owner: reviewed\nGHSA-qhr7-859c-m2p7 exp:2026-11-20\n", "high", 1,
     "[FAIL] GHSA-6j4f-fj2g-mc7p"),
    ([], "", "info", 0, "npm audit gate: passed (0 advisory(ies) in the report)"),
])
def test_the_npm_gate(gate, tmp_path, monkeypatch, capsys, advisories, allowlist, level,
                      status, expected):
    argv = _fake_tool(tmp_path, monkeypatch, "npm", _npm_report(*advisories),
                      1 if advisories else 0)
    path = _allowlist(tmp_path, allowlist)
    args = ["npm", "--ignore-file", str(path), "--audit-level", level, "--", "--omit=dev"]
    assert gate.main(args, today=date(2026, 11, 2)) == status
    assert expected in capsys.readouterr().out
    assert argv.read_text().split() == ["audit", "--json", "--omit=dev"]


@pytest.mark.parametrize("stdout", [
    json.dumps({"error": {"code": "ENOTFOUND", "summary": "request to registry failed"}}),
    "npm ERR! something else",
    json.dumps({"auditReportVersion": 1, "advisories": {}}),
])
def test_the_npm_gate_fails_when_the_audit_does(gate, tmp_path, monkeypatch, stdout):
    _fake_tool(tmp_path, monkeypatch, "npm", stdout, 1)
    path = _allowlist(tmp_path, "")
    assert gate.main(["npm", "--ignore-file", str(path), "--", "--omit=dev"]) == 1


def test_a_broken_or_missing_allowlist_stops_the_gate(gate, tmp_path, monkeypatch, capsys):
    argv = _fake_tool(tmp_path, monkeypatch, "pip-audit", "", 0)
    bad = _allowlist(tmp_path, "GHSA-68fv-2mgg-jv7q exp:2027-06-01\n")
    assert gate.main(["pip", "--ignore-file", str(bad), "--", "-r", "x"]) == 2
    assert "no reason comment" in capsys.readouterr().err
    assert gate.main(["pip", "--ignore-file", str(tmp_path / "missing"), "--", "-r", "x"]) == 2
    assert not argv.exists()  # the audit never ran


def test_a_blocking_dependency_audit_has_a_runbook():
    """audit-gates-have-no-exception-path: operations.md's 'scan blocks a deploy' runbook
    covered Trivy only."""
    operations = _read(OPERATIONS)
    runbook = _flat(_section(operations, "### A dependency audit blocks a deploy", "\n### "))
    for needle in (".pip-audit-ignore", "frontend/.npm-audit-ignore", "edge/.npm-audit-ignore",
                   "deploy/ci/audit_gate.py", "exp:", "at most 31 days out",
                   "requirements/compile.sh --upgrade-package", "deploy-gate",
                   "#the-image-scan-blocks-a-deploy"):
        assert needle in runbook, needle
    assert "[A dependency audit blocks a deploy](#a-dependency-audit-blocks-a-deploy)" in \
        _section(operations, "### Deploy", "### Rollback")
    r10 = _flat(_section(_read(SECURITY), "**R10.", "**R11."))
    assert ".pip-audit-ignore" in r10 and "audit_gate.py" in r10
    assert "a deploy blocked by the image scan or a dependency audit" in _read(DEPLOYMENT)
