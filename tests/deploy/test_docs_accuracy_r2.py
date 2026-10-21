"""Documentation accuracy: the confirmed docs findings of the Phase 5 review, round 2.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, in the manner of ``test_docs_accuracy.py`` (round 1). Wherever the
statement has a source of truth (code, a workflow, a test, a script, a
recorded measurement), the test reads that source too, so a change there fails
here until the docs follow.

Read-only. Needs neither Docker nor the network. The git checks skip in a
shallow clone, and the Playwright count skips without ``frontend/node_modules``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from .conftest import REPO_ROOT, worker_env_names, wrangler_vars
from .test_docs_accuracy import (
    ADR,
    ADR_011,
    ADR_014,
    ADR_015,
    DEPLOYMENT,
    DOCS,
    DOCS_DIR,
    ENV_PRODUCTION,
    FRONTEND,
    OPERATIONS,
    SECURITY,
    WORKFLOWS,
    _flat,
    _git,
    _name,
    _read,
    _row,
    _section,
)

ADR_012 = ADR / "ADR-012-load-and-quota-protection.md"
API_MD = DOCS_DIR / "api.md"
EDGE = REPO_ROOT / "edge"
WRANGLER = EDGE / "wrangler.jsonc"
FRONTEND_DIR = REPO_ROOT / "frontend"
EVIDENCE = DOCS_DIR / "evidence" / "phase5"


def _between(text: str, start: str, *ends: str) -> str:
    """From `start` to the first of `ends` that follows it (or to the end)."""
    begin = text.index(start)
    stops = [i for i in (text.find(end, begin + len(start)) for end in ends) if i >= 0]
    return text[begin:min(stops)] if stops else text[begin:]


# --- cf-token-leak-keeps-worker-secrets ------------------------------------------------


def _worker_secrets() -> set[str]:
    """Every `env.NAME` the Worker reads that is neither a var nor a binding."""
    wrangler = _read(WRANGLER)
    bindings = set(re.findall(r'"binding":\s*"([A-Z][A-Z0-9_]*)"', wrangler))
    bindings |= set(re.findall(r'\{\s*"name":\s*"([A-Z][A-Z0-9_]*)",\s*"class_name"', wrangler))
    secrets = worker_env_names() - wrangler_vars() - bindings
    assert {"EDGE_AUTH_SECRET", "OPS_TOKEN", "IP_HMAC_KEY", "RENDER_ORIGIN"} <= secrets, secrets
    return secrets


def test_a_cloudflare_token_leak_rotates_every_worker_secret():
    """cf-token-leak-keeps-worker-secrets: Worker code deployed with the token can read
    every Worker secret from `env` and send it anywhere, then be rolled back."""
    secrets = _worker_secrets()
    security = _read(SECURITY)
    row = _row(security, "`CLOUDFLARE_API_TOKEN`")
    for name in secrets:
        assert f"`{name}`" in row, name
    intro = _flat(_section(security, "### 10. Leaked tokens", "| Secret |"))
    assert "every secret" in intro
    rotation = _flat(_section(_read(DEPLOYMENT), "### Rotation", "### TLS to the database"))
    item = _between(rotation, "5. **Cloudflare API token**", "6. **Render deploy hook")
    for name in secrets:
        assert name in item, name
    assert "EPOCH" in item and "wrangler deployments list" in item
    assert "every secret it exposes" in _between(rotation, "**If a secret leaks:**", "###")
    cloudflare = next(line for line in _read(OPERATIONS).splitlines()
                      if line.startswith("| Cloudflare API token"))
    for name in secrets - {"RENDER_ORIGIN"}:
        assert name in cloudflare, name
    l1 = next(line for line in _read(ADR_015).splitlines() if line.startswith("- **L1:**"))
    assert "every Worker secret" in l1
    location = _row(_read(DEPLOYMENT), "**Worker secret**")
    assert "readable by any code deployed to the Worker" in location


def test_a_neon_key_or_owner_leak_checks_the_served_statistics():
    """cf-token-leak-keeps-worker-secrets: editor rights on the project (NEON_API_KEY) or
    the owner DSN can alter the statistics; the runbooks must check them or restore."""
    cli = _read(REPO_ROOT / "src" / "atus_pipeline" / "serving_cli.py")
    assert '@click.option("--content-hash"' in cli
    rotation = _flat(_section(_read(DEPLOYMENT), "### Rotation", "### TLS to the database"))
    item = _between(rotation, "7. **Neon owner password and project key.**", "8. **",
                    "**If a secret leaks:**")
    assert "verify-serving-db --manifest" in item and "--content-hash" in item, item
    assert "atus_api" in item and "Restore after a lost project" in item
    security = _read(SECURITY)
    for first in ("`NEON_API_KEY` (optional)", "`neondb_owner` DSN"):
        assert "verify-serving-db" in _row(security, first), first
    neon = next(line for line in _read(OPERATIONS).splitlines()
                if line.startswith("| Neon owner password"))
    assert "verify-serving-db" in neon and "--content-hash" in neon


# --- weekly-reminder-never-delivered ---------------------------------------------------


def _owner_checklist() -> tuple[str, list[str]]:
    """(heading, items) of the schedules-check run summary's owner checklist."""
    workflow = _read(WORKFLOWS / "schedules-check.yml")
    block = _section(workflow, "cat >> \"$GITHUB_STEP_SUMMARY\" <<'EOF'", "\n          EOF")
    heading = re.search(r"^\s*### (.+)$", block, re.MULTILINE).group(1)
    items: list[str] = []
    for line in block.splitlines()[2:]:
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:])
        elif re.match(r"^(Weekly|Monthly)\b.*:$", stripped):
            continue
        elif items and stripped:
            items[-1] += " " + stripped
    return heading, [_flat(item) for item in items]


def test_the_owner_checklist_is_documented_as_something_the_owner_opens():
    """weekly-reminder-never-delivered: the checklist is only the summary of a run that
    normally passes, and owner step 4.4 emails failed runs only."""
    assert "Only notify for failed workflows" in _read(DEPLOYMENT)
    heading, items = _owner_checklist()
    assert len(items) >= 5
    assert 'docs/deployment.md, "Monitoring and alerting"' in heading
    text = _read(DEPLOYMENT)
    monitoring = _section(text, "### Monitoring and alerting ($0)")
    checklist = _flat(_section(monitoring, "#### Owner checklist"))
    for item in items:
        assert item in checklist, item
    assert "no email" in checklist and "Actions → Schedules check" in checklist
    # the workflow's own backstop for a disabled schedule check
    assert "a monthly look at the Actions tab" in _read(WORKFLOWS / "schedules-check.yml")
    assert "Actions tab" in checklist
    wrong = []
    for path in [*DOCS, WORKFLOWS / "quota-report.yml", WORKFLOWS / "schedules-check.yml"]:
        flat = _flat(_read(path))
        for phrase in re.findall(r"summary reminds[^.]*|reminds (?:you|the owner)[^.]*|"
                                 r"the weekly reminder[^.]*", flat):
            wrong.append(f"{_name(path)}: {phrase}")
    assert wrong == []
    # every place that sends the owner to look points at the checklist
    assert "#owner-checklist" in _section(text, "- **Not monitored: Render outbound bandwidth**",
                                          "- **Email.**")
    outbound = _row(_read(OPERATIONS), "Render outbound (5 GB/month)")
    assert "deployment.md#owner-checklist" in outbound
    risks = _read(SECURITY)
    for start, end in (("**R2.", "**R3."), ("**R3.", "**R4.")):
        assert "deployment.md#owner-checklist" in _section(risks, start, end), start


# --- governor-hmac-retention-overstated -------------------------------------------------


def test_purged_prefix_rows_are_described_as_recoverable_for_30_days():
    """governor-hmac-retention-overstated: the day key is derived from the static
    IP_HMAC_KEY, and the purge is a DELETE in a Durable Object whose storage has 30 days
    of point-in-time recovery."""
    admission = _read(EDGE / "src" / "admission.ts")
    assert "hmac(new TextEncoder().encode(secret), `atus-ip-prefix|${utcDay(now)}`)" in admission
    governor = _read(EDGE / "src" / "governor.ts")
    assert "this.sql.exec('DELETE FROM prefixes WHERE day < ?', today)" in governor
    security = _read(SECURITY)
    row = _flat(_row(security, "`IP_HMAC_KEY`"))
    assert "already deleted" not in row
    assert "point-in-time recovery" in row and "30 days" in row
    c17 = _flat(_section(security, "**C17. Visitor privacy.**", "Code:"))
    assert "point-in-time recovery" in c17 and "30 days" in c17
    assert "developers.cloudflare.com/durable-objects/api/sqlite-storage-api" in c17
    adr = _read(ADR_015)
    assert "the key rotates every UTC day" not in _flat(adr)
    governor_text = _flat(_section(adr, "### Quota governor", "**Cost class.**"))
    assert "point-in-time recovery" in governor_text and "30 days" in governor_text


# --- ready-database-ok-within-ttl -------------------------------------------------------


def test_ready_is_described_as_answering_from_a_snapshot_within_its_ttl():
    """ready-database-ok-within-ttl: strict mode skips pinning and stale-if-error but still
    answers from a TTL-fresh snapshot, with a fixed `"database": "ok"`."""
    snapshot = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "snapshot.py")
    usable = _section(snapshot, "def _usable(", "def _stale_fallback(")
    assert usable.index("if now < self._fresh_until:") < \
        usable.index("if strict or not self._trusted:")
    health = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "routes" / "health.py")
    assert "strict=True" in health and '"database": "ok"' in health
    for path in DOCS:
        assert "reports the database as it is now" not in _flat(_read(path)), _name(path)
    security = _read(SECURITY)
    for first in ("`ATUS_OPS_TOKEN`", "`/ready`"):
        row = _flat(_row(security, first))
        assert "every call" not in row and "Each /ready call queries" not in row, row
        assert "ATUS_API_SNAPSHOT_TTL_S" in row, row
    adr = _flat(_section(_read(ADR_012), "**Stale if error.**", "- **What it makes free.**"))
    assert "ATUS_API_SNAPSHOT_TTL_S" in adr and "snapshot_age_s" in adr
    api = _flat(_read(API_MD))
    ready = api[api.index("/ready never uses a pinned or stale snapshot"):][:400]
    assert "ATUS_API_SNAPSHOT_TTL_S" in ready and "snapshot_age_s" in ready
    runbook = next(line for line in _read(OPERATIONS).splitlines() if '"$ORIGIN/ready"' in line)
    assert "ATUS_API_SNAPSHOT_TTL_S" in runbook


# --- static-paths-invoke-worker --------------------------------------------------------


def test_only_navigations_and_existing_files_skip_the_worker():
    """static-paths-invoke-worker: with a Worker script and SPA not_found_handling, a
    non-navigation request that matches no file invokes the Worker and is metered."""
    wrangler = _read(WRANGLER)
    assert '"main": "src/index.ts"' in wrangler
    assert '"not_found_handling": "single-page-application"' in wrangler
    assert "(free, never invokes the Worker)" not in wrangler
    adr = _read(ADR_011)
    flow = _flat(_section(adr, "2. **Static paths.**", "3. **`/api` and `/api/*`.**"))
    assert "never invoke the Worker and never count toward" not in flow
    assert "Sec-Fetch-Mode: navigate" in flow and "non-navigation" in flow
    consequences = _flat(_section(adr, "- **Quota failures.**", "- **What novel analyses pay.**"))
    assert "non-navigation" in consequences
    unknowns = _section(adr, "## Unknowns to measure after deploy", "## Amendments")
    assert "Sec-Fetch-Mode" in unknowns
    text = _read(DEPLOYMENT)
    diagram = _section(text, "```mermaid", "```\n\n**Trust boundaries:**")
    static = next(line for line in diagram.splitlines() if "Free, not metered" in line)
    assert "navigation" in static, static
    assert "non-navigation" in _row(text, "Worker requests (account-wide)")
    snapshot = _section(_read(ADR_015), "### Static snapshot (L0)", "```text")
    assert "non-navigation" in snapshot


# --- frontend-md-e2e-counts-stale ------------------------------------------------------


def test_frontend_md_states_the_configured_playwright_runs():
    """frontend-md-e2e-counts-stale: the comment said 65 runs (37 desktop, 2 mobile)."""
    block = _section(_read(FRONTEND), "npm run test:e2e", "npx playwright install")
    match = re.search(r"# (\d+) Playwright runs: (\d+) tests in Chromium desktop, (\d+) mobile,"
                      r"\s*#\s*and the (\d+) @core journeys again in Firefox and in WebKit", block)
    assert match, block
    total, desktop, mobile, core = map(int, match.groups())
    assert total == desktop + mobile + 2 * core
    playwright = FRONTEND_DIR / "node_modules" / ".bin" / "playwright"
    if not playwright.exists():
        pytest.skip("needs frontend/node_modules (npm ci in frontend/)")
    env = {key: value for key, value in os.environ.items()
           if key not in ("E2E_BASE_URL", "E2E_BROWSER_SERVER")}
    result = subprocess.run([str(playwright), "test", "--list"], cwd=FRONTEND_DIR, env=env,
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    listed = Counter(re.findall(r"^\s*\[(\w+)\] ›", result.stdout, re.MULTILINE))
    assert listed == {"desktop": desktop, "mobile": mobile, "firefox": core, "webkit": core}


# --- health-outbound-bound-understated --------------------------------------------------


def test_the_health_outbound_bound_uses_the_measured_answer_size():
    """health-outbound-bound-understated: the bound assumed ~0.5 KB per /api/health
    answer; the production-mode API alone sends more."""
    evidence = _read(EVIDENCE / "review-r2-bytes" / "health_bytes.txt")
    # Byte counts only: the record must not carry the API's Server-Timing durations.
    assert re.search(r"^\d+ server-timing: ", evidence, re.MULTILINE)
    assert not re.search(r"dur=\d", evidence), "unmasked Server-Timing duration"
    totals = set(re.findall(r"^TOTAL (\d+) B", evidence, re.MULTILINE))
    assert len(totals) == 1, totals
    size = int(totals.pop())
    month = size * 100_000 * 31 / 1e9
    worst = 0.74 + 0.6 + month
    margin = (5e9 - (0.74 + 0.6 + month) * 1e9) / (100_000 * 31)
    adr = _flat(_section(_read(ADR_015), "- **Render outbound through the Worker.**",
                         "## Verification"))
    assert "~0.5 KB" not in adr
    assert f"{size} B" in adr and f"≈ {month:.1f} GB a month" in adr, adr
    assert f"about {worst:.1f} GB of the 5 GB" in adr
    assert f"less than about {round(margin, -2):.0f} B" in adr and "unmeasured" in adr
    assert "review-r2-bytes" in adr
    row = _row(_read(DEPLOYMENT), "Outbound bandwidth (includes API→DB traffic the service "
                                  "initiates; inbound not billed)")
    assert "1.5 GB" not in row and "2.8 GB" not in row
    assert f"≤ ~{month:.1f} GB" in row and f"≈ {worst:.1f} GB in all" in row
    assert f"less than about {round(margin, -2):.0f} B" in row
    unknowns = _section(_read(ADR_011), "## Unknowns to measure after deploy", "## Amendments")
    assert "/api/health" in _row(unknowns, "Render outbound and Neon `data_transfer_bytes` per "
                                           "uncached analysis (checks the ~40 KB and ~80 KB "
                                           "estimates)")


# --- schedules-check-cadence-docs-stale --------------------------------------------------


def test_the_schedule_check_is_documented_as_a_weekly_workflow_that_only_alerts():
    """schedules-check-cadence-docs-stale: since review round 1 it runs weekly with a
    read-only token and re-enables nothing."""
    workflow = _read(WORKFLOWS / "schedules-check.yml")
    cron = re.search(r'- cron: "([^"]+)"', workflow).group(1)
    assert cron.split()[2:] == ["*", "*", "1"], cron
    assert "actions: read" in workflow and "actions: write" not in workflow
    text = _read(DEPLOYMENT)
    scheduled = _row(text, "Scheduled workflows")
    assert "`schedules-check.yml` (weekly)" in scheduled
    assert "(monthly)" not in scheduled and "re-enables" not in scheduled
    assert "operations.md#scheduled-workflows-were-disabled" in scheduled
    minutes = _row(text, "Actions minutes (standard runners)")
    assert "4–5 schedule checks" in minutes and "1 schedule check" not in minutes
    disabled = _row(text, "GitHub schedules auto-disabled")
    assert "operations.md#scheduled-workflows-were-disabled" in disabled
    for path in DOCS:
        assert "monthly runbook" not in _flat(_read(path)), _name(path)
    check = _row(_read(ADR_014), "Schedule check")
    assert "weekly" in check and "re-enables auto-disabled" not in check
    adr = _read(ADR_011)
    grafted = next(line for line in adr.splitlines()
                   if line.startswith("| Daily quota-report workflow"))
    assert "weekly" in grafted
    risks = _flat(_section(adr, "### Other risks and their mitigations", "## Consequences"))
    assert "schedules-check.yml" in risks and "review round 2" in risks


# --- drill-step5-port-conflict ----------------------------------------------------------


def _compose_project(line: str) -> str:
    match = re.search(r"\s-p (\S+)", line)
    return match.group(1) if match else "atus-stack"


def test_the_dump_smoke_never_shares_the_drills_port_and_is_torn_down():
    """drill-step5-port-conflict: the stack's db publishes the drill db's port, its roles
    service resets atus_api's password, and `down -v` needs the profile."""
    compose = _read(REPO_ROOT / "docker-compose.prod.yml")
    assert "name: atus-stack" in compose
    assert '"127.0.0.1:${ATUS_STACK_DB_PORT-5470}:5432"' in compose
    assert "ATUS_API_ROLE_PASSWORD: ${ATUS_STACK_API_DB_PASSWORD" in compose
    drill = _section(_read(OPERATIONS), "### Restore drill", "### Incident first steps")
    teardown = drill.index("-p atus-drill --env-file .env.stack -f docker-compose.prod.yml down -v")
    dump_up = [match.start() for match in re.finditer(r"--profile dump up", drill)]
    assert dump_up and min(dump_up) > teardown
    # The password reset happens only where the stack shares the drill's database,
    # that is in the drill's own project; a project of its own has a db of its own.
    (reset,) = [sentence for sentence in re.split(r"(?<=\.) ", _flat(drill))
                if "ATUS_STACK_API_DB_PASSWORD" in sentence]
    assert "-p atus-drill" in reset and "drill.env" in reset, reset
    step_2 = _section(_read(DEPLOYMENT), "### 2. Restore drill", "### 3. The image")
    for section in (drill, step_2):
        lines = [line for line in section.splitlines() if "docker compose" in line]
        ups = [line for line in lines if "--profile dump" in line and " up " in line]
        downs = [line for line in lines if "down -v" in line]
        assert ups
        for up in ups:
            project = _compose_project(up)
            assert any(_compose_project(down) == project and "--profile dump" in down
                       and "--profile smoke" in down for down in downs), up


# --- image-gate-tag-mismatch -------------------------------------------------------------


def test_the_image_gate_checks_the_image_the_guide_builds():
    """image-gate-tag-mismatch: the build tagged `atus-api` (:latest), the gate checked
    `atus-api:local`."""
    image = _section(_read(DEPLOYMENT), "### 3. The image", "### 4. Accounts")
    built = re.search(r"docker buildx build [^`]*?-t (\S+) \.`", image).group(1)
    gated = re.search(r"python deploy/ci/check_image\.py (\S+)", image).group(1)
    assert built == gated and ":" in built, (built, gated)


# --- data-rollback-neon-key-unavailable ---------------------------------------------------


def test_the_data_rollback_creates_a_key_for_the_previous_project():
    """data-rollback-neon-key-unavailable: NEON_API_KEY is not kept, so nothing holds the
    previous project's key to put back."""
    assert "not kept: re-create it instead" in _row(_read(DEPLOYMENT), "`NEON_API_KEY`")
    procedures = (
        _flat(_section(_read(OPERATIONS), "- **Data: the release switch in reverse**",
                       "- **API host:**")),
        _flat(_section(_read(DEPLOYMENT), "- **Data:** the release switch in reverse",
                       "- **API host:**")),
    )
    for procedure in procedures:
        assert "NEON_API_KEY back" not in procedure
        assert "new project-scoped key for the previous project" in procedure, procedure


# --- analytics-token-missing-from-config-reference -----------------------------------------


def _workflow_secrets() -> set[str]:
    names: set[str] = set()
    for path in WORKFLOWS.glob("*.yml"):
        names |= set(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", _read(path)))
    names.discard("GITHUB_TOKEN")
    assert {"CLOUDFLARE_API_TOKEN", "EDGE_OPS_TOKEN"} <= names
    return names


def test_the_configuration_reference_names_every_secret_the_workflows_read():
    """analytics-token-missing-from-config-reference: it claims to list exactly the names
    .github/workflows/ reads."""
    secrets = _workflow_secrets()
    reference = _section(_read(DEPLOYMENT), "### GitHub (repository variable and environments)",
                         "### Owner files")
    rows = set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", reference, re.MULTILINE))
    assert secrets <= rows, secrets - rows
    usage = _section(reference, "**Which workflow uses which name.**")
    template = _section(_read(ENV_PRODUCTION), "# 4. GitHub")
    for name in sorted(secrets):
        assert name in usage, name
        assert name in template, name
    leaked = _section(_read(SECURITY), "### 10. Leaked tokens", "## Production exposure")
    for name in sorted(secrets):
        if re.search(r"TOKEN|KEY|HOOK|SECRET|PASSWORD", name):
            assert f"`{name}`" in leaked, name


# --- runbooks-dispatch-before-ci-gate ------------------------------------------------------

_CI_WAIT = re.compile(r"until CI|once CI|CI (?:on|for) (?:that|this) (?:push|commit)")


def test_every_push_then_dispatch_waits_for_ci_first():
    """runbooks-dispatch-before-ci-gate: preflight's deploy-gate refuses a commit until a
    ci.yml push run on main has passed for it."""
    deploy = _read(WORKFLOWS / "deploy.yml")
    assert "github_ops.py deploy-gate" in deploy
    gate = _read(REPO_ROOT / "scripts" / "ops" / "github_ops.py")
    # The refusal names what happened to the commit's CI run (review round 2,
    # ci-concurrency-strands-main-commits); a pending run is waited for.
    assert "run for {sha[:12]} on a push to {branch}" in gate
    assert "wait until it passes, then re-run" in gate
    procedures = (
        (DEPLOYMENT, "**Bootstrap: the very first deploy.**", "### 8. Prove",
         "**Actions → Deploy → Run workflow**"),
        (OPERATIONS, "6. **Switch.**", "7. **Verify", "gh workflow run deploy.yml"),
        (OPERATIONS, "- **Data: the release switch in reverse**", "- **API host:**",
         "gh workflow run deploy.yml"),
        (DEPLOYMENT, "- **Data:** the release switch in reverse", "- **API host:**",
         "gh workflow run deploy.yml"),
        (WORKFLOWS / "deploy.yml", "- **Data:**", "EOF", "gh workflow run deploy.yml"),
    )
    for path, start, end, dispatch in procedures:
        procedure = _flat(_section(_read(path), start, end))
        before = procedure[:procedure.index(dispatch)]
        push = before.lower().find("push")   # the procedure's push step comes first
        assert push >= 0, (_name(path), start)
        assert _CI_WAIT.search(before[push:]), (_name(path), start, before[push:])


# --- quota-runbook-omits-neon-project-id ----------------------------------------------------


def test_the_ground_truth_lever_names_both_neon_settings():
    """quota-runbook-omits-neon-project-id: the governor polls Neon only with both the key
    and a committed NEON_PROJECT_ID."""
    governor = _read(EDGE / "src" / "governor.ts")
    configured = _section(governor, "private neonConfigured(): boolean {", "\n  }\n")
    assert "this.env.NEON_API_KEY" in configured and "this.env.NEON_PROJECT_ID" in configured
    assert re.search(r'"NEON_PROJECT_ID":\s*""', _read(WRANGLER))
    row = _row(_read(OPERATIONS), "Governor daily caps")
    assert "NEON_PROJECT_ID" in row and "edge/wrangler.jsonc" in row
    assert "ground_truth" in row and "/api/_ops/budget" in row


# --- pool-ceiling-cites-server-not-role-limit ------------------------------------------------


def test_the_pool_size_references_name_the_api_roles_connection_limit():
    """pool-ceiling-cites-server-not-role-limit: atus_api's CONNECTION LIMIT binds first."""
    limit = int(re.search(r"CONNECTION LIMIT (\d+)",
                          _read(REPO_ROOT / "scripts" / "ops" / "roles.sql")).group(1))
    row = _flat(_row(_read(DEPLOYMENT), "`ATUS_API_POOL_MAX_SIZE`"))
    assert f"CONNECTION LIMIT {limit}" in row and f"2 × ATUS_API_POOL_MAX_SIZE ≤ {limit}" in row
    lines = _read(ENV_PRODUCTION).splitlines()
    end = lines.index("ATUS_API_POOL_MAX_SIZE=4")
    start = end
    while lines[start - 1].startswith("#"):
        start -= 1
    comment = _flat(" ".join(lines[start:end]).replace("#", " "))
    assert f"CONNECTION LIMIT {limit}" in comment and f"2 x pool <= {limit}" in comment, comment


# --- workers-logs-projection-predates-invocation-logs-off -------------------------------------


def test_the_workers_logs_projection_is_one_event_per_worker_request():
    """workers-logs-projection-predates-invocation-logs-off: ~420/day assumed invocation
    logs; the Worker writes one line per /api request and nothing else."""
    calls = [f"{_name(path)}: {line.strip()}" for path in sorted((EDGE / "src").rglob("*.ts"))
             for line in _read(path).splitlines() if re.search(r"\bconsole\.\w+\(", line)]
    assert calls == ["edge/src/index.ts: console.log(JSON.stringify(log))"], calls
    assert re.search(r'"invocation_logs":\s*false', _read(WRANGLER))
    text = _read(DEPLOYMENT)
    worker = _row(text, "Worker requests (account-wide)")
    typical = re.search(r"up to ([\d,]+) on a typical day", worker).group(1)
    spike = re.search(r"up to ([\d,]+) on the spike day", worker).group(1)
    logs = _row(text, "Workers Logs")
    assert "~420" not in logs and "2,800" not in logs
    assert f"up to {typical} events/day typical" in logs and f"{spike} on the spike day" in logs
    assert "invocation logs are off" in logs


# --- adr011-edge-loc-stale ----------------------------------------------------------------


def _git_bytes(*args: str) -> bytes | None:
    try:
        result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                                check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def test_adr_011_dates_the_workers_size():
    """adr011-edge-loc-stale: the undated 13 files and 2,816 lines went stale."""
    match = re.search(r"At ([0-9a-f]{7,40}), `edge/src` (?:was|is) (\d+) TypeScript files and "
                      r"([\d,]+) lines", _read(ADR_011))
    assert match
    commit, files, lines = match.group(1), int(match.group(2)), int(match.group(3).replace(",", ""))
    if _git("rev-parse", "--is-shallow-repository") != "false" or \
            _git("cat-file", "-e", f"{commit}^{{commit}}") is None:
        pytest.skip("needs the full git history")
    listed = _git("ls-tree", "-r", "--name-only", commit, "--", "edge/src").splitlines()
    assert len(listed) == files and all(path.endswith(".ts") for path in listed)
    assert sum(_git_bytes("show", f"{commit}:{path}").count(b"\n") for path in listed) == lines


# --- security-status-overclaims-tests -------------------------------------------------------

_RAW_HTML_OR_STORAGE = re.compile(
    r"document\.cookie|cookieStore|localStorage|sessionStorage|indexedDB|"
    r"dangerouslySetInnerHTML|innerHTML|outerHTML\s*=|insertAdjacentHTML|document\.write")


def _spa_sources() -> list[Path]:
    """The SPA's shipped sources: frontend/src without tests and test setup, and index.html."""
    src = FRONTEND_DIR / "src"
    files = [path for path in sorted(src.rglob("*.ts*"))
             if not re.search(r"\.test\.tsx?$", path.name)
             and "test" not in path.relative_to(src).parts[:-1]]
    return [*files, FRONTEND_DIR / "index.html"]


def test_the_spa_sets_no_cookies_uses_no_web_storage_and_writes_no_raw_html():
    """security-status-overclaims-tests: C17 (no cookies, no Web Storage) and abuse case 4
    (no raw HTML from link labels) were stated by inspection only."""
    sources = _spa_sources()
    assert len(sources) > 30
    hits = [f"{_name(path)}:{number}: {line.strip()}" for path in sources
            for number, line in enumerate(_read(path).splitlines(), 1)
            if _RAW_HTML_OR_STORAGE.search(line)]
    assert hits == []


def test_security_names_a_test_for_each_spa_claim():
    """security-status-overclaims-tests: the status line says every control is covered by
    the tests named with it."""
    text = _read(SECURITY)
    assert "by inspection" not in text.lower()
    name = "test_the_spa_sets_no_cookies_uses_no_web_storage_and_writes_no_raw_html"
    c17 = _flat(_section(text, "**C17. Visitor privacy.**", "**C18."))
    labels = _flat(_section(text, "- **Labels.**", "- **No stored content.**"))
    for part in (c17, labels):
        assert f"tests/deploy/test_docs_accuracy_r2.py::{name}" in part
    assert "non-test" in labels


# --- r1-bytes-evidence-misindexed -----------------------------------------------------------


def test_the_byte_count_index_files_each_measurement_where_it_is():
    """r1-bytes-evidence-misindexed: the narrow-population rows (242,394 B, cited by
    ADR-015 and EGRESS_ESTIMATE_BYTES) are in rejected_422_egress.txt."""
    folder = EVIDENCE / "review-r1-bytes"
    rows = dict(re.findall(r"^\| \[`([^`]+)`\]\([^)]+\) \| (.+) \|$",
                           _read(folder / "README.md"), re.MULTILINE))
    assert len(rows) == 3, rows
    for file, description in rows.items():
        content = _read(folder / file)
        assert ("narrow" in description) == ("age80-85" in content), file
        assert ("status=422" in content) == ("422" in description), file
    (cited,) = [file for file in rows if "242394" in _read(folder / file)]
    assert "242,394" in rows[cited]


# --- env-example-public-base-url-stale --------------------------------------------------------


def test_the_production_template_sets_public_base_url_as_the_guide_does():
    """env-example-public-base-url-stale: production's environment variable first, the
    repository variable only after step 8 (the monitors start with it)."""
    section = _section(_read(ENV_PRODUCTION), "# 4. GitHub")
    production = _section(section, "# production environment", "# ops environment")
    assert re.search(r"variable:\s*\n#\s+PUBLIC_BASE_URL", production), production
    flat = _flat(section.replace("#", " "))
    assert "repository variable, only after deployment.md step 8 has verified the site" in flat
    assert "monitors start" in flat
