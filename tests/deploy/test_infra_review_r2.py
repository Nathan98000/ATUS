"""Runbooks and workflows: the confirmed infra findings of the Phase 5 review, round 2.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script), the test reads it too, so a change
there fails here until the docs follow. Read-only; needs neither Docker nor
the network.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from .conftest import REPO_ROOT
from .test_dependabot import repository_files

DOCS_DIR = REPO_ROOT / "docs"
DEPLOYMENT = DOCS_DIR / "deployment.md"
OPERATIONS = DOCS_DIR / "operations.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, single spaces, no backticks: wording checks ignore wrapping and code spans."""
    return re.sub(r"\s+", " ", text.replace("`", ""))


def _section(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))] if end else text[begin:]


def _step(section: str, number: int) -> str:
    """Numbered step ``number`` of a runbook section (up to the next top-level step)."""
    begin = section.index(f"\n{number}. ")
    following = re.search(rf"\n{number + 1}\. ", section[begin:])
    return section[begin:begin + following.start()] if following else section[begin:]


# --- replay-local-smoke-proves-wrong-db ---------------------------------------------------


def test_the_data_release_replays_prove_which_database_answered():
    """replay-local-smoke-proves-wrong-db: step 1 left the full-database API on :8000, so
    step 5's API on the new project could not bind and the smoke replayed the full
    database against itself. Step 1 stops its API; steps 5 and 7 name the release's
    data version, which replay_deployed.py checks."""
    release = _section(_read(OPERATIONS), "### Data release", "### New ATUS year")
    record = _step(release, 1)
    assert record.index("--record") < record.index("kill %1")
    for number in (5, 7):
        replay = _step(release, number)
        assert "replay_deployed.py" in replay and "--expect-data-version" in replay, number
    assert "kill %1" in _step(release, 5)
    site = _flat(_section(_read(DEPLOYMENT), "### 9. Quota-budgeted post-deploy replay",
                          "- **What it sends.**"))
    assert "--expected expected-<release>.json --expect-data-version" in site
    script = _read(REPO_ROOT / "scripts" / "replay_deployed.py")
    assert '"--expect-data-version"' in script and "def check_data_version" in script


# --- trivy-gate-blocks-data-rollback ---------------------------------------------------

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TRIVY_ENTRY = re.compile(r"^(?P<id>(?:CVE-\d{4}-\d{4,}|GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}))"
                         r" exp:(?P<exp>\d{4}-\d{2}-\d{2})$")
TRIVY_HORIZON_DAYS = 31


def trivyignore_problems(text: str, today: date) -> list[str]:
    """Every entry of .trivyignore: one ID with an expiry at most 31 days out, after a
    comment that gives the reason (pure; the policy of operations.md's runbook)."""
    problems, reason = [], False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            reason = False
        elif stripped.startswith("#"):
            reason = len(stripped.lstrip("# ")) >= 10
        else:
            match = TRIVY_ENTRY.match(stripped)
            if not match:
                problems.append(f"line {number}: not '<CVE or GHSA id> exp:YYYY-MM-DD'")
                continue
            if not reason:
                problems.append(f"line {number}: {match['id']} has no reason comment above it")
            if date.fromisoformat(match["exp"]) > today + timedelta(days=TRIVY_HORIZON_DAYS):
                problems.append(f"line {number}: {match['id']} expires more than "
                                f"{TRIVY_HORIZON_DAYS} days out")
    return problems


def test_the_image_scan_has_a_reviewed_expiring_allowlist():
    """trivy-gate-blocks-data-rollback: a newly fixable HIGH/CRITICAL CVE (or a failed
    Trivy DB download) blocked every deploy, the data rollback's deploy included, with
    no override and no runbook."""
    image = _read(WORKFLOWS / "image.yml")
    (gate,) = [line for line in image.splitlines() if "--exit-code 1" in line]
    assert "trivy image" in gate and "--ignorefile .trivyignore" in gate
    assert "--ignorefile /dev/null" in image      # the SARIF report lists everything
    assert trivyignore_problems(_read(REPO_ROOT / ".trivyignore"), date.today()) == []
    # The policy itself.
    today = date(2026, 11, 2)
    good = ("# 2026-11-02 owner: libfoo, not reachable from the API\n"
            "CVE-2026-12345 exp:2026-12-02\n")
    assert trivyignore_problems(good, today) == []
    assert trivyignore_problems("CVE-2026-12345 exp:2026-12-02\n", today)        # no reason
    assert trivyignore_problems("# a reason, reviewed\nCVE-2026-12345\n", today)  # no expiry
    assert trivyignore_problems("# a reason, reviewed\nCVE-2026-12345 exp:2027-06-01\n",
                                today)                                           # too far


def test_a_scan_blocked_deploy_has_a_runbook_and_rollbacks_skip_the_pipeline():
    """trivy-gate-blocks-data-rollback: the runbook, and the data rollback rolls the edge
    back with wrangler rollback (no build, no scan) before the manifest and deploy steps."""
    operations = _read(OPERATIONS)
    runbook = _flat(_section(operations, "### The image scan blocks a deploy", "\n### "))
    for needed in (".trivyignore", "exp:", "PYTHON_DIGEST", "compile.sh",
                   "Render deploy hook", "wrangler rollback"):
        assert needed in runbook, needed
    data = _section(operations, "- **Data: the release switch in reverse**", "- **API host:**")
    assert data.index("wrangler rollback") < data.index("releases/current.json") \
        < data.index("gh workflow run deploy.yml")
    assert "#the-image-scan-blocks-a-deploy" in data
    deployment = _section(_read(DEPLOYMENT), "- **Data:** the release switch in reverse",
                          "- **API host:**")
    assert deployment.index("wrangler rollback") < deployment.index("gh workflow run deploy.yml")


# --- no-prepublish-data-version-check ----------------------------------------------------


def _workflow(name: str) -> dict:
    return yaml.safe_load(_read(WORKFLOWS / name))


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def test_the_edge_release_is_published_only_over_an_origin_bound_to_it():
    """no-prepublish-data-version-check: the deploy checked only the origin's build before
    wrangler deploy, so a run approved before the Render switch published the new
    release's snapshot and DATA_VERSION over an API still serving the old one."""
    deploy = _workflow("deploy.yml")["jobs"]["deploy"]
    runs = [step.get("run", "") for step in _steps(deploy)]
    (wait,) = [i for i, run in enumerate(runs)
               if run.startswith("python scripts/ops/render_deploy.py wait")]
    (publish,) = [i for i, run in enumerate(runs) if re.search(r"wrangler deploy(?!ments)", run)]
    assert wait < publish
    assert '--expect-data-version "$DATA_VERSION"' in runs[wait]
    assert deploy["env"]["DATA_VERSION"] == "${{ needs.snapshot.outputs.data_version }}"
    source = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "routes" / "health.py")
    assert "expected_data_version=settings.expected_data_version" in source
    step = _flat(_section(_read(DEPLOYMENT), "**The deploy job.**", "A push to `main`"))
    assert "expected_data_version" in step and "nothing is published at the edge" in step


# --- deploy-job-runs-dev-toolchain-before-secrets ----------------------------------------

DEPLOY_SECRETS = ("RENDER_DEPLOY_HOOK_URL", "RENDER_ORIGIN", "CLOUDFLARE_API_TOKEN",
                  "CLOUDFLARE_ACCOUNT_ID", "EDGE_OPS_TOKEN")


def _pip_installs(path: Path) -> list[str]:
    """Every `pip install ... -r <lock>` command of a workflow, continuations joined."""
    text = _read(path).replace("\\\n", " ")
    return [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines()
            if "pip install" in line and " -r " in line]


def test_the_job_holding_deploy_secrets_runs_no_test_or_browser_tooling():
    """deploy-job-runs-dev-toolchain-before-secrets: the deploy job installed the whole dev
    lock and ran vitest (and later the frontend tree and Playwright) in the job that holds
    CLOUDFLARE_API_TOKEN and the Render deploy hook."""
    workflow = _workflow("deploy.yml")
    holders = {job_id for job_id, job in workflow["jobs"].items()
               if any(f"secrets.{name}" in yaml.safe_dump(job) for name in DEPLOY_SECRETS)}
    # api-switch (the owner's pause switch, review round 2 edge) holds the
    # Cloudflare token for `wrangler secret bulk`, behind the same approval.
    assert holders == {"deploy", "api-switch"}
    for holder in sorted(holders):
        runs = "\n".join(step.get("run", "") for step in _steps(workflow["jobs"][holder])
                         if step.get("name") != "Rollback notes")
        for tool in ("vitest", "playwright", "requirements/dev.lock.txt", "frontend"):
            assert tool not in runs, (holder, tool)
    runs = "\n".join(step.get("run", "") for step in _steps(workflow["jobs"]["deploy"])
                     if step.get("name") != "Rollback notes")
    assert "-r requirements/api.lock.txt" in runs
    for job_id, tool in (("edge-config", "vitest run --project node"),
                         ("browser-smoke", "playwright.deployed.config.ts")):
        job = workflow["jobs"][job_id]
        assert "environment" not in job and "secrets." not in yaml.safe_dump(job), job_id
        assert tool in yaml.safe_dump(job, width=1000), job_id
    assert "edge-config" in workflow["jobs"]["deploy"]["needs"]
    assert "browser-smoke" in workflow["jobs"]["promote"]["needs"]


def test_every_hash_locked_install_takes_binary_wheels_only():
    """security.md C19 says Python installs are binary wheels only; only the Dockerfile
    passed --only-binary, so a future sdist-only pin would have been built (its code run)
    in CI and in the deploy job."""
    installs = [cmd for path in sorted(WORKFLOWS.glob("*.yml")) for cmd in _pip_installs(path)]
    assert len(installs) >= 6
    for command in installs:
        assert "--require-hashes" in command and "--only-binary=:all:" in command, command
    assert "--only-binary=:all:" in _read(REPO_ROOT / "Dockerfile")
    c19 = _flat(_section(_read(DOCS_DIR / "security.md"), "**C19. Supply chain.**", "**C20."))
    assert "--only-binary=:all:" in c19


# --- stack-e2e-evidence-guard-skips-in-ci ------------------------------------------------


def test_ci_runs_the_stack_mode_record_guard_with_the_full_history():
    """stack-e2e-evidence-guard-skips-in-ci: the guard skips in a shallow clone, and every
    CI checkout was shallow (fetch-depth 1), so CI never enforced it."""
    backend = _workflow("ci.yml")["jobs"]["backend"]
    (checkout,) = [s for s in _steps(backend) if "actions/checkout@" in str(s.get("uses"))]
    assert checkout["with"]["fetch-depth"] == 0
    (deploy_tests,) = [s for s in _steps(backend) if "pytest tests/deploy" in s.get("run", "")]
    assert deploy_tests["env"]["ATUS_REQUIRE_GIT_HISTORY"] == "1"


def test_the_stack_mode_guard_covers_the_whole_e2e_suite(tmp_path):
    """stack-e2e-evidence-guard-skips-in-ci: the guard compared only frontend/e2e/*.spec.ts,
    so a change to helpers.ts or fixtures.ts left the dated record looking current."""
    from .test_docs_accuracy import e2e_suite_changes_since

    def git(*args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               *args], cwd=tmp_path, capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(name: str, text: str = "x") -> None:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)

    if subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                      check=False).returncode != 0:
        pytest.skip("git is not available")
    for name in ("frontend/e2e/a.spec.ts", "frontend/e2e/helpers.ts",
                 "frontend/playwright.config.ts", "frontend/src/app.ts"):
        write(name)
    git("add", "-A")
    git("commit", "-q", "-m", "recorded")
    record = git("rev-parse", "HEAD")
    assert e2e_suite_changes_since(record, tmp_path) == []
    write("frontend/e2e/helpers.ts", "changed")
    write("frontend/e2e/deployed/starter.deployed.ts")
    write("frontend/src/app.ts", "changed")               # app code: CI's e2e-stack covers it
    git("add", "-A")
    git("commit", "-q", "-m", "later")
    write("frontend/e2e/b.spec.ts")                       # untracked
    assert e2e_suite_changes_since(record, tmp_path) == [
        "frontend/e2e/b.spec.ts", "frontend/e2e/deployed/starter.deployed.ts",
        "frontend/e2e/helpers.ts"]


# --- owner-credential-tools-unpinned -----------------------------------------------------

#: A wrangler command that lets npx download wrangler instead of refusing to.
UNLOCKED_WRANGLER = re.compile(rb"npx\s+(?:-y\s+)?wrangler\S*")
#: Regression notes in the tests quote the old commands on purpose.
WRANGLER_SCAN_SKIP = ("tests/", "edge/test/")


def unlocked_wrangler_commands(root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Every unlocked wrangler command in the repository's files (as git lists them:
    the docs, the config templates and comments, the scripts, the workflows)."""
    found: dict[str, list[str]] = {}
    for path in repository_files(root):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(WRANGLER_SCAN_SKIP) or not path.is_file():
            continue
        hits = UNLOCKED_WRANGLER.findall(path.read_bytes())
        if hits:
            found[relative] = [hit.decode(errors="replace") for hit in hits]
    return found


def test_the_owner_runs_only_the_wrangler_the_lockfile_installed():
    """owner-credential-tools-unpinned: owner step 1.3 (and its copies in the deploy
    guide and Bootstrap) ran `npx wrangler@4 login` from the repository root before
    `npm ci` in edge/, so npx downloaded whatever wrangler 4.x was newest and that
    performed the OAuth login. Every wrangler command now runs with `--no-install`
    (npx refuses to download), and each login follows `npm ci` in edge/.

    Review round 2 verification: the check read six files, and the config template
    (.env.production.example) and edge/wrangler.jsonc's comments still told the owner
    `npx wrangler secret put` and `npx wrangler kv namespace create`. It now reads every
    file of the repository."""
    assert unlocked_wrangler_commands() == {}
    logins = [line for line in _read(DEPLOYMENT).splitlines() if "wrangler login" in line]
    assert len(logins) == 3                    # step 1.3.1, the guide's 6, Bootstrap 2
    for line in logins:
        assert "npx --no-install wrangler login" in line
        assert 0 <= line.index("cd edge && npm ci") < line.index("wrangler login"), line
    security = _flat(_read(DOCS_DIR / "security.md"))
    assert "wrangler login" in security and "serving/pgtools.py" in security


def test_the_unlocked_wrangler_scan_reads_every_file_but_the_tests(tmp_path):
    if subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                      check=False).returncode != 0:
        pytest.skip("git is not available")
    for name, text in ((".env.production.example", "# (`npx wrangler secret put NAME`)\n"),
                       ("edge/wrangler.jsonc", "// `npx -y wrangler@4 login`\n"),
                       ("docs/ok.md", "`npx --no-install wrangler login`\n"),
                       ("tests/deploy/test_x.py", "# it ran `npx wrangler@4 login`\n"),
                       ("edge/test/node/x.test.ts", "// was `npx wrangler deploy`\n")):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert unlocked_wrangler_commands(tmp_path) == {
        ".env.production.example": ["npx wrangler"],
        "edge/wrangler.jsonc": ["npx -y wrangler@4"],
    }


# --- ci-concurrency-strands-main-commits -------------------------------------------------


def test_the_deploy_runbook_says_not_every_main_commit_gets_ci():
    """ci-concurrency-strands-main-commits: ci.yml's push concurrency cancels a pending
    main run when a newer push queues, and a multi-commit push runs CI on its head only,
    while Deploy step 1 said only to wait for CI. The runbook now says how to recover
    (github_ops.py's refusal is tested in tests/unit/ops/test_github_ops.py)."""
    ci = yaml.safe_load(_read(WORKFLOWS / "ci.yml"))
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    step = _flat(_step(_section(_read(OPERATIONS), "### Deploy", "### Rollback"), 1))
    for text in ("runs CI on its head only", "cancelled when a newer push", "gh run rerun",
                 "tag a commit whose CI passed"):
        assert text in step, text


# --- promote-rerun-queues-behind-pending-deploy ------------------------------------------


def test_the_rollback_says_a_promote_rerun_waits_behind_a_pending_deploy():
    """promote-rerun-queues-behind-pending-deploy: deploy.yml has one workflow-level
    concurrency group, so re-running `promote` of an earlier run (the documented way to
    move `live` back after an API rollback) waits behind any Deploy run in progress or
    awaiting approval, and a later dispatch cancels it. Every place that gives that step
    says so and offers the way out (reject the pending run, or move the tag by hand)."""
    deploy = yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))
    # Deploys, whose promote job the re-run belongs to, share deploy-production; only
    # the pause switch has a group of its own (round 3, test_infra_review_r3.py).
    assert deploy["concurrency"]["cancel-in-progress"] is False
    assert deploy["concurrency"]["group"].endswith("|| 'deploy-production' }}")
    assert all("concurrency" not in job for job in deploy["jobs"].values())
    api = _flat(_section(_read(OPERATIONS), "### Rollback", "- **Edge**"))
    assert "no other Deploy run is in progress or waiting for approval" in api
    for text in ("Reject", "gh run cancel", "while another Deploy run is pending, by hand"):
        assert text in api, text
    rollback = _section(_read(DEPLOYMENT), "### Rollback", "- **Data:**")
    step = _flat(rollback[rollback.index("3. move the GHCR tag `live` back"):])
    assert "while another Deploy run is in progress or waiting for approval" in step
    assert "imagetools create" in step and "rejecting that pending run" in step
    summary = _flat(_read(WORKFLOWS / "deploy.yml"))
    assert "While another Deploy run is in progress or waits for approval, that re-run waits" \
        in summary
