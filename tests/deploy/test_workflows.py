"""Invariants of the GitHub workflows (brief §16, ADR-011 "Deploy" and "Keep-alive policy").

actionlint checks syntax, expressions and the shell in every ``run:`` step
(CI job supply-chain). These tests hold the project's own rules:

* every action is pinned to a full commit SHA with its version in a comment;
* every workflow and job declares its permissions, every job a timeout;
* no secret is interpolated into a script, echoed, or traced with ``set -x``;
* deploy.yml is the only deploy path: tag or manual trigger only, the
  ``production`` environment on the job that holds deploy secrets, and
  ``packages: write`` only on the push job;
* the monitors never call a path that touches the database;
* the CI jobs the brief asks for exist and run what they promise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from .conftest import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOWS_DIR.glob("*.yml"))
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"#\s*v\d+(\.\d+){0,2}\s*$")
#: Secrets that can deploy or reach production (everything but GITHUB_TOKEN).
DEPLOY_SECRETS = {"RENDER_DEPLOY_HOOK_URL", "RENDER_ORIGIN", "CLOUDFLARE_API_TOKEN",
                  "CLOUDFLARE_ACCOUNT_ID"}
#: Paths that reach the database (the API's analysis and metadata routes, /ready).
DB_PATHS = re.compile(r"/api/v1/|/ready\b|/metrics\b")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(workflow: dict) -> dict:
    # YAML 1.1 reads a bare `on` key as boolean True.
    on = workflow.get("on", workflow.get(True))
    return on if isinstance(on, dict) else {name: None for name in on}


def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def by_name(name: str) -> dict:
    return load(WORKFLOWS_DIR / name)


def flat(node) -> str:
    """Every key and value of a parsed YAML node, one per line, unwrapped."""
    if isinstance(node, dict):
        return "\n".join(f"{key}: {flat(value)}" for key, value in node.items())
    if isinstance(node, list):
        return "\n".join(flat(item) for item in node)
    return str(node)


def run_scripts(path: Path):
    for job_id, job in jobs(load(path)).items():
        for step in steps(job):
            if "run" in step:
                yield job_id, step


def test_the_expected_workflows_exist():
    assert {p.name for p in WORKFLOWS} == {
        "ci.yml", "image.yml", "deploy.yml", "edge-check.yml", "deep-check.yml",
        "quota-report.yml", "schedules-check.yml", "ghcr-cleanup.yml",
    }


def pin_problems(text: str) -> list[str]:
    """``uses:`` lines not pinned to a full commit SHA with a version comment."""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$", line)
        if not match:
            continue
        ref, rest = match.groups()
        if ref.startswith("./"):
            if not (REPO_ROOT / ref[2:]).exists():
                problems.append(f"{number}: {ref} does not exist")
        elif not SHA_PIN.match(ref):
            problems.append(f"{number}: {ref} is not pinned by commit SHA")
        elif not VERSION_COMMENT.search(rest):
            problems.append(f"{number}: {ref} has no '# vX.Y.Z' comment")
    return problems


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_by_full_sha_with_a_version_comment(path):
    assert pin_problems(path.read_text()) == []


def test_the_pin_check_catches_tags_and_missing_comments():
    sha = "a" * 40
    assert pin_problems("      - uses: actions/checkout@v4")
    assert pin_problems(f"      - uses: actions/checkout@{sha}")
    assert pin_problems("    uses: ./.github/workflows/missing.yml")
    assert not pin_problems(f"      - uses: actions/checkout@{sha} # v7.0.1")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_permissions_and_timeouts_are_explicit(path):
    workflow = load(path)
    assert "permissions" in workflow, f"{path.name}: no top-level permissions"
    assert "id-token" not in path.read_text(), f"{path.name}: requests an OIDC token"
    for job_id, job in jobs(workflow).items():
        assert "permissions" in job, f"{path.name}/{job_id}: no job permissions"
        if "uses" not in job:  # a reusable-workflow call takes the callee's timeouts
            assert "timeout-minutes" in job, f"{path.name}/{job_id}: no timeout-minutes"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_risky_triggers(path):
    on = triggers(load(path))
    assert "pull_request_target" not in on and "workflow_run" not in on


def secret_problems(workflow: dict) -> list[str]:
    """Scripts that interpolate a secret, trace with set -x, or print a secret's variable."""
    problems = []
    for job_id, job in jobs(workflow).items():
        for step in steps(job):
            script = step.get("run")
            if script is None:
                continue
            where = f"{job_id}/{step.get('name')}"
            if "secrets." in script:
                problems.append(f"{where}: interpolates a secret into the script")
            if re.search(r"\bset\s+-[a-z]*x", script):
                problems.append(f"{where}: set -x traces secrets")
            secret_vars = [name for name, value in (step.get("env") or {}).items()
                           if "secrets." in str(value) and "!= ''" not in str(value)]
            for name in secret_vars:
                for line in script.splitlines():
                    printed = re.search(rf"\b(echo|printf|cat)\b.*\${{?{name}\b", line)
                    if printed and "add-mask" not in line:
                        problems.append(f"{where}: prints ${name}")
    return problems


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_never_reach_a_script_directly_or_get_echoed(path):
    assert secret_problems(load(path)) == []


def test_the_secret_check_catches_leaks():
    def workflow(run: str, env: dict | None = None) -> dict:
        return {"jobs": {"j": {"steps": [{"name": "s", "run": run, "env": env or {}}]}}}

    hook = {"HOOK": "${{ secrets.RENDER_DEPLOY_HOOK_URL }}"}
    assert secret_problems(workflow("curl ${{ secrets.RENDER_DEPLOY_HOOK_URL }}"))
    assert secret_problems(workflow('echo "$HOOK"', hook))
    assert secret_problems(workflow('set -ex\ncurl "$HOOK"', hook))
    assert not secret_problems(workflow("python scripts/ops/render_deploy.py trigger", hook))
    assert not secret_problems(workflow('echo "::add-mask::$HOOK"', hook))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_do_not_persist_credentials(path):
    for job_id, job in jobs(load(path)).items():
        for step in steps(job):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, \
                    f"{path.name}/{job_id}"


# --------------------------------------------------------------------------- #
# deploy.yml: the only deploy path                                             #
# --------------------------------------------------------------------------- #

def test_deploy_runs_only_on_tags_or_by_hand():
    on = triggers(by_name("deploy.yml"))
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"] == {"tags": ["v*"]}, "a push to a branch must deploy nothing"


def test_deploy_permissions_are_minimal():
    workflow = by_name("deploy.yml")
    assert workflow["permissions"] == {}
    writers = {job_id for job_id, job in jobs(workflow).items()
               if any(v == "write" for v in (job.get("permissions") or {}).values())}
    assert writers == {"push"}
    assert jobs(workflow)["push"]["permissions"] == {"contents": "read", "packages": "write"}


def test_only_the_production_environment_job_holds_deploy_secrets():
    workflow = by_name("deploy.yml")
    text_by_job = {job_id: flat(job) for job_id, job in jobs(workflow).items()}
    holders = {job_id for job_id, text in text_by_job.items()
               if any(f"secrets.{name}" in text for name in DEPLOY_SECRETS)}
    assert holders == {"deploy"}
    deploy = jobs(workflow)["deploy"]
    assert deploy["environment"]["name"] == "production"
    assert set(deploy["needs"]) >= {"push", "snapshot"}, "nothing deploys before the snapshot"


def test_the_deploy_job_follows_adr_011():
    deploy = flat(jobs(by_name("deploy.yml"))["deploy"])
    order = ["render_deploy.py trigger", "render_deploy.py wait --expect-build",
             "npx --no-install wrangler deploy \\", "deploy_smoke.py",
             "playwright.deployed.config.ts"]
    positions = [deploy.index(marker) for marker in order]
    assert positions == sorted(positions)
    for var in ("DATA_VERSION", "ANALYTICS_VERSION", "RESPONSE_FORMAT", "SNAPSHOT_ID"):
        assert f'--var "{var}:${var}"' in deploy
    assert "--no-install wrangler" in deploy  # the lockfile's wrangler, never a download
    # A same-commit redeploy cannot be observed through /health: the wait warns.
    assert '--previous-build "$PREVIOUS_BUILD"' in deploy
    assert "steps.before.outputs.build" in deploy
    assert "--max-requests 30" in deploy and "--origin-lock" in deploy
    assert "Rollback" in deploy


def test_the_deploy_header_states_the_smoke_budgets_the_steps_enforce():
    """The header comment's request budgets are the ones the steps apply: the smoke
    script's --max-requests and the browser journey's /api cap."""
    text = (WORKFLOWS_DIR / "deploy.yml").read_text()
    header = re.sub(r"\n#\s+", " ", text[:text.index("\nname:")])
    smoke = re.search(r"post-deploy smoke \(<= (\d+) requests, the step's --max-requests\)",
                      header)
    browser = re.search(r"browser smoke \(<= (\d+) /api requests\)", header)
    assert smoke and browser, header
    deploy = flat(jobs(by_name("deploy.yml"))["deploy"])
    assert re.findall(r"--max-requests (\d+)", deploy) == [smoke.group(1)]
    assert f"Post-deploy smoke (at most {smoke.group(1)} requests" in deploy
    starter = (REPO_ROOT / "frontend" / "e2e" / "deployed" / "starter.deployed.ts").read_text()
    assert re.search(rf"^const MAX_API_REQUESTS = {browser.group(1)}$", starter, re.MULTILINE)


def test_the_snapshot_is_built_from_the_pushed_digest_and_the_committed_release():
    snapshot = flat(jobs(by_name("deploy.yml"))["snapshot"])
    assert "release_manifest.py fetch" in snapshot
    assert "atus restore-serving-db" in snapshot
    assert "--read-only" in snapshot and "$IMAGE_REF" in snapshot
    assert "scripts/build_snapshot.py" in snapshot and "snapshot-requests.mjs" in snapshot
    assert "postgres:16@sha256:" in snapshot


def test_the_snapshot_api_starts_in_production_mode_against_the_runner_database():
    """The snapshot job runs the image in production mode against the runner's
    postgres:16 on localhost, which has no TLS: the URL the restore writes for
    that owner is refused without the local-stack escape hatch and accepted
    with it, so the job must set it."""
    from atus_pipeline.config import ConfigError, check_api_settings, load_settings
    from atus_pipeline.serving.secrets_file import api_database_url

    snapshot = flat(jobs(by_name("deploy.yml"))["snapshot"])
    assert 'echo "ATUS_ENV=production"' in snapshot
    assert 'echo "ATUS_DATABASE_TLS=local-stack-only"' in snapshot
    owner = re.search(r"ATUS_OWNER_DATABASE_URL=(postgresql://neondb_owner:%s@\S+?)\\n",
                      snapshot)
    url = api_database_url(owner.group(1).replace("%s", "pw"), "atus_api", "api-pw")
    env = {"ATUS_ENV": "production", "ATUS_EDGE_SECRETS": "e" * 64, "ATUS_DATABASE_URL": url}
    with pytest.raises(ConfigError, match="sslmode"):
        load_settings(env)
    check_api_settings(load_settings({**env, "ATUS_DATABASE_TLS": "local-stack-only"}))


def test_the_pushed_image_is_the_tested_one():
    push = flat(jobs(by_name("deploy.yml"))["push"])
    assert "needs.build.outputs.image-id" in push
    assert jobs(by_name("deploy.yml"))["build"]["with"]["use-cache"] is False


#: GitHub cancels a run whose job has waited 30 days for an environment's approval.
APPROVAL_WAIT_DAYS = 30


def uses_action(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def run_uploads(workflow: dict) -> dict[str, object]:
    """Artifact name -> ``retention-days`` for every upload in one run of ``workflow``:
    its own jobs and the reusable workflows they call (same run, same artifacts)."""
    uploads: dict[str, object] = {}
    for job in jobs(workflow).values():
        called = job.get("uses")
        if isinstance(called, str) and called.startswith("./"):
            uploads.update(run_uploads(load(REPO_ROOT / called[2:])))
        for step in steps(job):
            if uses_action(step, "actions/upload-artifact"):
                spec = step.get("with") or {}
                uploads[spec.get("name")] = spec.get("retention-days")
    return uploads


def approval_artifact_problems(workflow: dict) -> list[str]:
    """Artifacts a job behind an environment downloads that can expire before approval.

    Such a job starts only once the owner approves, up to 30 days after the
    upload; an artifact kept for less fails the download (retention-days unset
    means the repository default, 90 days).
    """
    uploads = run_uploads(workflow)
    problems = []
    for job_id, job in jobs(workflow).items():
        if "environment" not in job:
            continue
        for step in steps(job):
            if not uses_action(step, "actions/download-artifact"):
                continue
            name = (step.get("with") or {}).get("name")
            if name not in uploads:
                problems.append(f"{job_id}: downloads {name!r}, which this run never uploads")
                continue
            days = uploads[name]
            if days is not None and int(days) < APPROVAL_WAIT_DAYS:
                problems.append(f"{job_id}: {name!r} is kept {days} days, but approval can "
                                f"take {APPROVAL_WAIT_DAYS}")
    return problems


def test_artifacts_outlive_the_production_approval():
    assert approval_artifact_problems(by_name("deploy.yml")) == []


def test_the_artifact_check_catches_an_artifact_that_expires_before_approval():
    def workflow(days, *, environment: bool = True) -> dict:
        upload = {"uses": f"actions/upload-artifact@{'a' * 40}",
                  "with": {"name": "site", "path": "dist/"}}
        if days is not None:
            upload["with"]["retention-days"] = days
        deploy = {"steps": [{"uses": f"actions/download-artifact@{'b' * 40}",
                             "with": {"name": "site"}}]}
        if environment:
            deploy["environment"] = {"name": "production"}
        return {"jobs": {"build": {"steps": [upload]}, "deploy": deploy}}

    assert approval_artifact_problems(workflow(3))
    assert not approval_artifact_problems(workflow(30))
    assert not approval_artifact_problems(workflow(None))
    assert not approval_artifact_problems(workflow(3, environment=False))
    missing = workflow(30)
    missing["jobs"]["deploy"]["steps"][0]["with"]["name"] = "other"
    assert approval_artifact_problems(missing)


def test_no_other_workflow_can_deploy_or_push():
    for path in WORKFLOWS:
        if path.name == "deploy.yml":
            continue
        text = path.read_text()
        for marker in ("wrangler deploy", "render_deploy.py trigger", "docker push",
                       "CLOUDFLARE_API_TOKEN", "RENDER_DEPLOY_HOOK_URL", "environment: production",
                       "push: true"):
            assert marker not in text, f"{path.name} contains {marker!r}"
        if path.name != "ghcr-cleanup.yml":
            for job_id, job in jobs(load(path)).items():
                assert (job.get("permissions") or {}).get("packages") != "write", \
                    f"{path.name}/{job_id}"


# --------------------------------------------------------------------------- #
# monitors                                                                     #
# --------------------------------------------------------------------------- #

SCHEDULES = {
    "edge-check.yml": "17,47 * * * *",
    "deep-check.yml": "17 */6 * * *",
    "quota-report.yml": "5 22 * * *",
    "schedules-check.yml": "41 9 * * 1",
    "ghcr-cleanup.yml": "23 4 * * 1",
}


@pytest.mark.parametrize("name, cron", SCHEDULES.items())
def test_scheduled_workflows_have_their_cadence_and_a_manual_trigger(name, cron):
    workflow = by_name(name)
    on = triggers(workflow)
    assert on["schedule"] == [{"cron": cron}]
    assert "workflow_dispatch" in on
    assert "concurrency" in workflow


def test_the_schedules_check_knows_every_scheduled_workflow():
    scheduled = {f".github/workflows/{p.name}" for p in WORKFLOWS
                 if "schedule" in triggers(load(p))}
    source = (REPO_ROOT / "scripts" / "ops" / "github_ops.py").read_text()
    listed = set(re.findall(r'"(\.github/workflows/[a-z-]+\.yml)"', source))
    assert listed == scheduled


@pytest.mark.parametrize("name", ["edge-check.yml", "deep-check.yml", "quota-report.yml"])
def test_monitors_never_touch_the_database(name):
    text = (WORKFLOWS_DIR / name).read_text()
    assert not DB_PATHS.search(text), name
    for _job, step in run_scripts(WORKFLOWS_DIR / name):
        assert step["run"].startswith("python3 scripts/ops/monitors.py "), step["run"]
    monitors = (REPO_ROOT / "scripts" / "ops" / "monitors.py").read_text()
    paths = re.findall(r'"(/[a-z_/.<>-]*)"', monitors.split("EDGE_PATHS", 1)[1]
                       .split("# Provider quotas", 1)[0])
    assert paths and not [p for p in paths if DB_PATHS.search(p)]


def test_monitors_skip_until_the_site_exists():
    for name in ("edge-check.yml", "deep-check.yml", "quota-report.yml"):
        (job,) = jobs(by_name(name)).values()
        assert job["if"] == "${{ vars.PUBLIC_BASE_URL != '' }}"


def test_quota_report_reads_its_token_from_the_ops_environment():
    (job,) = jobs(by_name("quota-report.yml")).values()
    assert job["environment"] == "ops"
    assert "secrets.EDGE_OPS_TOKEN" in flat(job)


def test_ghcr_cleanup_keeps_20():
    (job,) = jobs(by_name("ghcr-cleanup.yml")).values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert "--keep 20" in flat(job)


# --------------------------------------------------------------------------- #
# ci.yml and image.yml                                                          #
# --------------------------------------------------------------------------- #

def test_ci_runs_on_pull_requests_and_main_with_read_only_defaults():
    workflow = by_name("ci.yml")
    on = triggers(workflow)
    assert set(on) == {"pull_request", "push"}
    assert on["push"] == {"branches": ["main"]}
    assert workflow["permissions"] == "read-all"
    assert "concurrency" in workflow
    for job_id, job in jobs(workflow).items():
        assert job["permissions"] == {"contents": "read"}, job_id


def test_ci_backend_covers_both_pythons_and_the_openapi_drift():
    backend = jobs(by_name("ci.yml"))["backend"]
    assert backend["strategy"]["matrix"]["python"] == ["3.11", "3.12"]
    text = flat(backend)
    assert "--require-hashes" in text and "requirements/dev.lock.txt" in text
    for command in ("ruff check .", "pytest tests/unit", "pytest tests/deploy",
                    "pytest tests/integration", "scripts/export_openapi.py",
                    "git diff --exit-code -- frontend/openapi.json"):
        assert command in text, command
    assert backend["services"]["postgres"]["image"].startswith("postgres:16@sha256:")


def test_ci_audits_and_lints():
    text = flat(jobs(by_name("ci.yml"))["supply-chain"])
    assert "pip-audit --strict --require-hashes" in text and "api.lock.txt" in text
    assert "actionlint" in text and "sha256sum --check" in text


@pytest.mark.parametrize("job, commands", [
    ("frontend", ["npm ci", "npm run typecheck", "npm run lint", "npm run format:check",
                  "npm test", "npm run build", "npm run check:bundle",
                  "npm run check:api-types", "npm audit --omit=dev --audit-level=high"]),
    ("edge", ["npm ci", "npm run typecheck", "npm run lint", "npm run format:check",
              "npm test", "npm audit --omit=dev"]),
    ("e2e", ["npx playwright install --with-deps chromium firefox webkit",
             "npm run test:e2e"]),
    ("e2e-stack", ["npm ci", "npx playwright install --with-deps chromium firefox webkit",
                   "scripts/smoke_stack.sh --no-build", "--edge --e2e"]),
])
def test_ci_jobs_run_what_they_promise(job, commands):
    text = flat(jobs(by_name("ci.yml"))[job])
    for command in commands:
        assert command in text, f"{job}: {command}"


def test_ci_runs_the_public_origin_with_the_local_stack_commands():
    """e2e-stack: the tested image behind the edge Worker, exactly as
    ``scripts/smoke_stack.sh --edge --e2e`` runs it locally (docs/deployment.md
    step 0): the API smoke, the SPA and static snapshot, the edge probe, and the
    whole Playwright suite in stack mode, every project; no hand-built stack."""
    job = jobs(by_name("ci.yml"))["e2e-stack"]
    assert job["needs"] == "image"
    assert job["env"]["ATUS_STACK_IMAGE"] == "atus-api:ci"
    text = flat(job)
    assert 'test "$loaded" = "$(cat "$RUNNER_TEMP/image/atus-api.id")"' in text
    (smoke,) = [s for s in steps(job) if "scripts/smoke_stack.sh" in s.get("run", "")]
    assert "--no-build" in smoke["run"] and "--edge --e2e" in smoke["run"]
    assert set(smoke["env"]["E2E_PROJECTS"].split()) == {"desktop", "mobile", "firefox", "webkit"}
    assert smoke["env"]["ATUS_BUILD_SHA"] == "${{ github.sha }}"
    assert "docker compose" not in text  # the script starts, probes and removes the stack
    assert "TODO" not in (WORKFLOWS_DIR / "ci.yml").read_text()


def test_the_image_job_follows_the_documented_recipe():
    image = jobs(by_name("image.yml"))["image"]
    text = flat(image)
    order = ["deploy/ci/lint.sh", "docker/build-push-action@", "deploy/ci/check_image.py",
             "pytest tests/deploy", "smoke_stack.sh --no-build --project",
             "smoke_stack.sh --no-build --emulation", "trivy image"]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "--allow-startup-connection" not in text  # strict gate
    assert "ATUS_DOCKER_TESTS: 1" in text
    # run as steps of their own here, and in ci.yml's e2e-stack job
    assert "--deselect tests/deploy/test_stack_smoke.py::test_stack_smoke" in text
    assert "--deselect tests/deploy/test_edge_stack.py::test_edge_cycle" in text
    build = next(s for s in steps(image) if "build-push-action" in str(s.get("uses")))
    assert build["with"]["platforms"] == "linux/amd64"
    assert build["with"]["push"] is False and build["with"]["load"] is True


def test_trivy_is_a_pinned_binary_never_the_hijacked_actions():
    text = (WORKFLOWS_DIR / "image.yml").read_text()
    assert "aquasecurity/trivy-action" not in text.replace("trivy-action/setup-trivy", "")
    for workflow in WORKFLOWS:
        for line in workflow.read_text().splitlines():
            if "uses:" in line:
                assert "aquasecurity/" not in line, workflow.name
    env = jobs(by_name("image.yml"))["image"]["env"]
    assert re.fullmatch(r"0\.\d+\.\d+", env["TRIVY_VERSION"])
    # GHSA-69fq-xp46-6x23: v0.69.4 (binary) and v0.69.5/6 (images) were malicious.
    assert env["TRIVY_VERSION"] not in ("0.69.4", "0.69.5", "0.69.6")
    assert re.fullmatch(r"[0-9a-f]{64}", env["TRIVY_SHA256"])
    assert "sha256sum --check --strict" in text
    assert "--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1" in text
