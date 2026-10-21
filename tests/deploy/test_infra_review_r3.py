"""Workflows and runbooks: the confirmed infra findings of the Phase 5 review, round 3.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script), the test reads it too, so a change
there fails here until the docs follow. Read-only; needs neither Docker nor
the network.
"""

from __future__ import annotations

import re
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
