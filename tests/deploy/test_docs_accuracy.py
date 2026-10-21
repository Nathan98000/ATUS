"""Documentation accuracy: the confirmed docs findings of the Phase 5 review, round 1.

Each test pins one finding (named in its docstring) so the docs cannot drift
back. Wherever the statement has a source of truth (code, a workflow, a test,
a script), the test reads that source too, so a change there fails here until
the docs follow.

Read-only. Needs neither Docker nor the network; the git checks skip in a
shallow clone.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
ADR = DOCS_DIR / "adr"
DOCS = sorted([REPO_ROOT / "README.md", *DOCS_DIR.glob("*.md"), *ADR.glob("*.md")])
DEPLOYMENT = DOCS_DIR / "deployment.md"
OPERATIONS = DOCS_DIR / "operations.md"
SECURITY = DOCS_DIR / "security.md"
FRONTEND = DOCS_DIR / "frontend.md"
ADR_011 = ADR / "ADR-011-hosting.md"
ADR_013 = ADR / "ADR-013-data-releases-and-serving-db.md"
ADR_014 = ADR / "ADR-014-observability.md"
ADR_015 = ADR / "ADR-015-edge-cache-snapshot-and-governor.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ENV_PRODUCTION = REPO_ROOT / ".env.production.example"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, single spaces, no backticks: wording checks ignore wrapping and code spans."""
    return re.sub(r"\s+", " ", text.replace("`", ""))


def _section(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))] if end else text[begin:]


def _row(text: str, first_cell: str) -> str:
    rows = [line for line in text.splitlines() if line.startswith(f"| {first_cell} |")]
    assert len(rows) == 1, (first_cell, rows)
    return rows[0]


def _name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _sentences(path: Path):
    """(paragraph start line, sentence) pairs, prose only: fenced code is skipped."""
    fenced = False
    paragraph: list[str] = []
    start = 0
    lines = _read(path).splitlines()
    for number, line in enumerate([*lines, ""], 1):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.strip() == "" or line.lstrip().startswith(("- ", "* ", "| ")) or \
                re.match(r"^\s*\d+\. ", line):
            for sentence in re.split(r"(?<=[.;:])\s+", _flat(" ".join(paragraph))):
                if sentence:
                    yield start, sentence
            paragraph, start = [], number
        paragraph.append(line)


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                                text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


# --- spike-day-refused-by-governor ---------------------------------------------------

SPIKE_TEST = REPO_ROOT / "edge" / "test" / "worker" / "spike-day.test.ts"


def _spike_results() -> dict[str, tuple[int, int, int]]:
    """mode -> (admitted, first denial after s, refused), as the edge test asserts them."""
    found = re.findall(
        r"toEqual\(\{\s*mode: '(\w+)',\s*admitted: (\d+),\s*firstDenialAfterS: ([\d_]+),"
        r"\s*budgets: \{ cu_day: (\d+) \}", _read(SPIKE_TEST))
    results = {mode: (int(a), int(s.replace("_", "")), int(r)) for mode, a, s, r in found}
    assert set(results) == {"conservative", "ground_truth"}
    for admitted, _, refused in results.values():
        assert admitted + refused == 300
    return results


def test_the_spike_day_states_what_the_governor_refuses():
    """spike-day-refused-by-governor: the budget assumed the spike day fully served."""
    results = _spike_results()
    text = _read(DEPLOYMENT)
    spike = _flat(_section(text, "### The spike day under the governor", "## Scraping-burst"))
    assert "edge/test/worker/spike-day.test.ts" in spike
    for admitted, seconds, refused in results.values():
        assert f"{admitted} of 300" in spike
        assert f"{seconds:,} s" in spike
        assert f"{refused} ({round(100 * refused / 300)}%)" in spike
    assert "daily_capacity_reached" in spike and "cu_day" in spike
    failure = _row(text, "Governor daily cap reached")
    assert "spike day" in failure and "The spike day under the governor" in failure
    for name in ("KV writes", "Durable Object requests"):
        assert "The spike day under the governor" in _row(text, name), name
    consequences = _flat(_section(_read(ADR_015), "## Consequences", "## Failure modes"))
    for admitted, _, _ in results.values():
        assert f"{admitted} of 300" in consequences
    # ADR-015's KV table: writes follow admissions, so the spike day writes only the admitted
    kv_writes = _row(_read(ADR_015), "writes")
    assert "300 spike" not in kv_writes
    assert all(f"{admitted}" in kv_writes for admitted, _, _ in results.values())


# --- bootstrap-kv-id-after-dispatch -------------------------------------------------


def test_the_bootstrap_commits_the_kv_namespace_id_before_the_run_is_dispatched():
    """bootstrap-kv-id-after-dispatch: the deploy job checks out the dispatched commit
    and refuses the placeholder id, so the id must be on main before the run starts."""
    deploy = _read(WORKFLOWS / "deploy.yml")
    assert re.search(r'grep -Eq \'"id": \*"0\{32\}"\' wrangler\.jsonc', deploy)
    text = _read(DEPLOYMENT)
    bootstrap = _section(text, "**Bootstrap: the very first deploy.**", "### 8. Prove")
    dispatch = bootstrap.index("**Actions → Deploy → Run workflow**")
    before = _flat(bootstrap[:dispatch])
    assert "kv namespace create" in before
    assert re.search(r"commit and push [^.]*\bto main\b", before)
    step_132 = next(line for line in text.splitlines()
                    if "npx wrangler kv namespace create" in line
                    and line.lstrip().startswith("2."))
    assert "Bootstrap" in step_132 and "commit" in step_132


# --- waking-notice-3s-analysis-claim -------------------------------------------------


def test_only_pending_metadata_starts_the_3_s_waking_notice():
    """waking-notice-3s-analysis-claim: no doc says an analysis shows the notice after 3 s."""
    status = _read(REPO_ROOT / "frontend" / "src" / "api" / "serviceStatus.ts")
    assert "export const SLOW_METADATA_MS = 3_000" in status
    assert re.search(r"kind === 'metadata'\s*\?\s*schedule\(SLOW_METADATA_MS", status)
    wrong = []
    for path in DOCS:
        for number, sentence in _sentences(path):
            if re.search(r"\bwak", sentence, re.I) and "after 3 s" in sentence \
                    and not re.search(r"metadata|/meta\b", sentence):
                wrong.append(f"{_name(path)}:{number}: {sentence}")
    assert wrong == []


# --- nonjson-error-mapping-overclaim ------------------------------------------------


def test_only_a_non_json_429_is_named_the_daily_request_allowance():
    """nonjson-error-mapping-overclaim: client.ts maps a non-JSON 429 to edge_daily_limit,
    a non-JSON 5xx to upstream_unavailable (a cold start) and any other 4xx to
    malformed_response; nothing recognises a 1027 page by its body."""
    client = _read(REPO_ROOT / "frontend" / "src" / "api" / "client.ts")
    body = _section(client, "function nonJsonError(", "\n}\n")
    assert re.search(r"if \(status === 429\) \{\s*return new ApiError\(\s*429,\s*"
                     r"'edge_daily_limit'", body)
    assert re.search(r"if \(status >= 500\) \{\s*return new ApiError\(\s*status,\s*"
                     r"'upstream_unavailable'", body)
    assert "'malformed_response'" in body and "1027" not in client
    wrong = []
    for path in DOCS:
        for number, sentence in _sentences(path):
            if re.search(r"429/4xx/5xx|429 or (?:error |a )?1027|both handled in code|"
                         r"Both are handled in code", sentence):
                wrong.append(f"{_name(path)}:{number}: {sentence}")
    assert wrong == []
    text = _read(DEPLOYMENT)
    assert "only a non-JSON 429" in _row(text, "Worker requests (account-wide)")
    exhausted = _row(text, "Worker 100k/day exhausted")
    assert "non-JSON 429" in exhausted and "upstream_unavailable" in exhausted


# --- owner-first-wrangler-deploy-missing-prereqs ----------------------------------------


def test_the_first_worker_deploy_builds_the_spa_and_installs_the_locked_wrangler_first():
    """owner-first-wrangler-deploy-missing-prereqs: wrangler.jsonc publishes
    ../frontend/dist, which a fresh clone lacks."""
    assert '"directory": "../frontend/dist"' in _read(REPO_ROOT / "edge" / "wrangler.jsonc")
    text = _read(DEPLOYMENT)
    for start, end in (("### 1. Cloudflare (Workers Free)", "4. **Worker → Settings → Builds:**"),
                       ("### 6. First Worker deploy (owner)", "- **What CI asserts")):
        part = _flat(_section(text, start, end))
        deploy = part.index("wrangler deploy")
        assert 0 <= part.find("npm run build") < deploy, start
        assert 0 <= part.find("cd edge && npm ci") < deploy, start


# --- invocation-logs-vs-privacy-claims ----------------------------------------------


def test_workers_invocation_logs_are_off_as_the_privacy_statements_require():
    """invocation-logs-vs-privacy-claims: C17 and the About page say full IPs are never
    logged; Cloudflare's invocation records may hold request headers."""
    wrangler = _read(REPO_ROOT / "edge" / "wrangler.jsonc")
    assert re.search(r'"invocation_logs":\s*false', wrangler)
    assert "enabled in `edge/wrangler.jsonc`" not in _read(SECURITY)
    section = _flat(_section(_read(ADR_014), "### Workers invocation logs"))
    assert "invocation_logs: false" in section and "turns them off" in section
    # the check that would allow them back has to be possible while they are off
    assert "nothing is recorded while they are off" in section


# --- c2-edge-auth-scope-inaccurate ---------------------------------------------------


def test_c2_names_the_ops_bearer_exception_to_edge_authentication():
    """c2-edge-auth-scope-inaccurate: /ready and /metrics accept the ops bearer alone."""
    middleware = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "middleware.py")
    assert 'OPS_PATHS = frozenset({"/ready", "/metrics"})' in middleware
    assert "and not (path in OPS_PATHS and ops_authorized(headers, settings))" in middleware
    text = _read(SECURITY)
    c2 = _flat(_section(text, "**C2. Edge authentication at the API.**", "Code:"))
    first = c2.split(" - ")[1]
    assert "/ready" in first and "/metrics" in first and "ATUS_OPS_TOKEN" in first
    boundary = _row(text, "Worker → API")
    assert "/ready" in boundary and "ATUS_OPS_TOKEN" in boundary
    lock = _flat(_section(text, "- **The origin lock.**", "- **`/health`.**"))
    assert "/ready" in lock and "/metrics" in lock


# --- ops-f69-cache-docstring-stale ---------------------------------------------------


def test_the_audit_resolution_matches_the_cache_docstring_and_names_its_recheck():
    """ops-f69-cache-docstring-stale: f8c57e7 fixed cache.py's docstring; rows F52/F53
    describe commits after 928f50e."""
    cache = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "cache.py")
    assert "run<id>" not in cache and "<release>:serving-" in cache
    text = _read(OPERATIONS)
    resolution = _section(text, "## Audit resolution", "## Equivalence criterion")
    assert "Still stale" not in resolution
    remains = _section(resolution, "### What remains open")
    assert "cache.py" not in remains
    assert "f8c57e7" in _row(resolution, "F69")
    header = _flat(resolution.split("Status values:")[0])
    for commit in ("6222a19", "2f46bd2", "f8c57e7", "b2e44a5", "f208719"):
        assert commit in header, commit


# --- adr015-test-counts-stale -------------------------------------------------------


def test_adr_015_dates_its_test_counts():
    """adr015-test-counts-stale: undated counts went stale (412; 49 + 2)."""
    verification = _section(_read(ADR_015), "## Verification", "## Cited terms")
    counted = [line for line in verification.splitlines()
               if re.search(r"\b\d+ (?:unit |integration )?tests\b", line)]
    assert counted
    for line in counted:
        assert re.search(r"\b20\d\d-\d\d-\d\d\b", line), line
        assert re.search(r"\b[0-9a-f]{7}\b", line), line


# --- ops-newyear-frontend-gap-stale --------------------------------------------------


def test_the_new_year_runbook_names_the_frontend_tests_that_feed_a_new_year():
    """ops-newyear-frontend-gap-stale: b2e44a5 added the tests the runbook said were missing."""
    tests = ("frontend/src/pages/newSurveyYear.test.tsx", "frontend/e2e/new-survey-year.spec.ts")
    for path in tests:
        assert (REPO_ROOT / path).is_file(), path
    text = _read(OPERATIONS)
    runbook = _section(text, "### New ATUS year", "### Restore drill")
    assert "no test feeds" not in runbook and "**Gap:**" not in runbook
    for path in tests:
        assert path in runbook, path
    resolution = _section(text, "## Audit resolution", "## Equivalence criterion")
    f55 = _row(resolution, "F55")
    assert "Frontend gap" not in f55 and all(path in f55 for path in tests)


# --- budget-table-count-inaccuracies -------------------------------------------------


def _crons() -> dict[str, str]:
    crons = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        match = re.search(r'^\s*- cron: "([^"]+)"', _read(path), re.MULTILINE)
        if match:
            crons[path.name] = match.group(1)
    return crons


def test_the_budget_counts_the_monitors_and_the_workers_cpu_work_as_configured():
    """budget-table-count-inaccuracies: the deep check makes up to --max-requests Worker
    requests; five workflows are scheduled; the Worker parses JSON."""
    crons = _crons()
    assert crons["edge-check.yml"] == "17,47 * * * *" and crons["deep-check.yml"] == "17 */6 * * *"
    assert crons["quota-report.yml"].endswith("* * *")       # daily
    # The deep check builds its arguments in a bash array (the optional
    # --expect-build of the `live` tag), so the budget is not on the
    # `monitors.py deep` line itself.
    deep_check = _read(WORKFLOWS / "deep-check.yml")
    assert "monitors.py deep" in deep_check
    budgets = re.findall(r"--max-requests (\d+)", deep_check)
    assert len(budgets) == 1, budgets
    deep = int(budgets[0])
    monitoring = 48 * 1 + 4 * deep + 1
    text = _read(DEPLOYMENT)
    assert f"1–{deep} Worker requests" in _row(text, "Monitoring")
    worker = _row(text, "Worker requests (account-wide)")
    assert f"up to {monitoring} from monitoring" in worker
    assert f"{60 + monitoring} on a typical day" in worker
    assert "no JSON parsing" not in text
    assert "JSON parse" in _row(text, "Worker CPU per request")
    scheduled = _row(text, "Scheduled workflows")
    assert f"{len(crons)} schedules" in scheduled
    for name in crons:
        assert name in scheduled, name


# --- data-rollback-doc-conflict -------------------------------------------------------


def test_the_data_rollback_dispatches_from_main_after_restoring_current_json():
    """data-rollback-doc-conflict: preflight runs only main or a vX.Y.Z tag, so deploy.yml
    cannot run 'at the commit' of the previous release without rolling the code back."""
    deploy = _read(WORKFLOWS / "deploy.yml")
    assert 'elif [ "$REF_NAME" != main ]; then' in deploy
    stale = "at the commit whose releases/current.json names the previous release"
    for path in [*DOCS, WORKFLOWS / "deploy.yml"]:
        assert stale not in _flat(_read(path)), _name(path)
    for path in (DEPLOYMENT, WORKFLOWS / "deploy.yml"):
        flat = _flat(_read(path))
        assert "-- releases/current.json" in flat, _name(path)
        assert "gh workflow run deploy.yml --ref main" in flat, _name(path)


# --- single-neon-env-vs-per-project ----------------------------------------------------


def test_deployment_keeps_one_neon_secrets_file_per_project():
    """single-neon-env-vs-per-project: a rollback needs the previous project's DSN."""
    per_project = "~/.config/atus/neon-<release>-<build8>.env"
    text = _read(DEPLOYMENT)
    # neon.env is only ever the restore's default, never the file a procedure fills in
    singular = [f"{_name(path)}: {line}" for path in [*DOCS, ENV_PRODUCTION]
                for line in _read(path).splitlines()
                if "neon.env" in line and "default" not in line]
    assert singular == []
    step_5 = _flat(_section(text, "### 5. Load the serving DB into Neon (owner)", "### 6."))
    assert f"--secrets-file {per_project}" in step_5
    owner_files = _row(text, "`neon-<release>-<build8>.env`")
    assert "previous project" in owner_files and "deletion" in owner_files


# --- runbook-cp-template-not-runnable ---------------------------------------------------


def test_every_copy_of_the_stack_template_says_to_fill_in_its_secrets():
    """runbook-cp-template-not-runnable: the template's secrets are empty and compose
    interpolates them all, even for `up db`."""
    template = _read(REPO_ROOT / "deploy" / "stack" / "stack.env.example")
    assert re.search(r"^ATUS_STACK_OPS_TOKEN=$", template, re.MULTILINE)
    copies = [(path, line) for path in DOCS for line in _read(path).splitlines()
              if "cp deploy/stack/stack.env.example" in line]
    assert len(copies) >= 2
    for path, line in copies:
        assert "fill in" in line, f"{_name(path)}: {line}"


# --- incident-not-configured-incomplete -------------------------------------------------


def test_the_not_configured_incident_row_names_both_worker_secrets():
    """incident-not-configured-incomplete: index.ts answers not_configured for either."""
    index = _read(REPO_ROOT / "edge" / "src" / "index.ts")
    assert "if (target === null || !env.EDGE_AUTH_SECRET) {" in index
    row = next(line for line in _read(OPERATIONS).splitlines()
               if line.startswith("|") and "not_configured" in line)
    for needle in ("RENDER_ORIGIN", "EDGE_AUTH_SECRET", "origin_not_configured"):
        assert needle in row, needle


# --- edge-rollback-restores-old-secrets ---------------------------------------------------


def test_the_edge_rollback_handles_wranglers_changed_secrets_prompt():
    """edge-rollback-restores-old-secrets: confirming the prompt restores old secrets."""
    for path, start, end in ((OPERATIONS, "- **Edge** (SPA, snapshot", "- **Keep the API and edge"),
                             (DEPLOYMENT, "- **Edge (SPA, snapshot", "- **Data:**")):
        edge = _flat(_section(_read(path), start, end))
        assert "secrets have changed" in edge, _name(path)
        assert "EDGE_AUTH_SECRET" in edge and "wrangler secret put" in edge, _name(path)
    summary = _flat(_section(_read(WORKFLOWS / "deploy.yml"), "- **Edge:**", "- **Data:**"))
    assert "secrets have changed" in summary and "decline" in summary


# --- verify-serving-db-env-var-wrong ---------------------------------------------------


def test_verify_serving_db_is_not_documented_as_reading_the_serving_url():
    """verify-serving-db-env-var-wrong: it defaults to ATUS_DATABASE_URL."""
    cli = _read(REPO_ROOT / "src" / "atus_pipeline" / "serving_cli.py")
    verify = _section(cli, "def verify_serving_db(", "\n@click.command")
    assert "SERVING_URL_ENV" not in verify and "settings.database_url" in verify
    row = _row(_read(DEPLOYMENT), "`ATUS_SERVING_DATABASE_URL`")
    assert "verify-serving-db" not in row
    template = [line for line in _read(ENV_PRODUCTION).splitlines()
                if line.startswith("# ATUS_SERVING_DATABASE_URL=")]
    assert len(template) == 1 and "verify-serving-db" not in template[0]


# --- l2-cache-key-resolved-computation ---------------------------------------------------


def test_the_l2_cache_key_is_described_as_implemented():
    """l2-cache-key-resolved-computation: resolved-computation keying is deferred (F15)."""
    cache = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "cache.py")
    assert ("(operation, canonical\nanalysis specification, analytics version, data version)"
            in cache)
    for path in (ADR_011, ADR_015):
        row = _row(_read(path), "**L2 API in-process LRU**")
        assert "canonical spec" in row and "deferred" in row, _name(path)


# --- deploy-guide-status-stale-ci-overclaim ------------------------------------------------


def test_the_deploy_guide_claims_no_ci_run_and_no_todo_markers():
    """deploy-guide-status-stale-ci-overclaim: no TODO(owner) marker exists, and the Phase
    5 workflows have no recorded run (roadmap.md item 1)."""
    text = _read(DEPLOYMENT)
    assert "TODO(owner)" not in text
    assert "CI runs it on every pull request" not in _flat(text)
    status = _flat(_section(text, "**Status.** ADR-011 fixes the order", "| Step | Who acts"))
    assert "no run is recorded" in status
    firefox = next(line for line in text.splitlines() if "Linux CI" in line and "e2e-stack" in line)
    assert "no run is recorded" in firefox


# --- security-roadmap-item-ref ---------------------------------------------------------------


def test_security_cites_the_roadmap_item_about_ci():
    """security-roadmap-item-ref: the CI item is item 1, not item 4."""
    cited = re.search(r"\(\[roadmap\]\(roadmap\.md#remaining-for-the-definition-of-done\), "
                      r"item (\d+)\)", _read(SECURITY))
    assert cited
    items = re.findall(r"^(\d+)\. \*\*([^*]+)\*\*", _read(DOCS_DIR / "roadmap.md"), re.MULTILINE)
    titles = dict(items)
    assert "CI" in titles[cited.group(1)], titles


# --- rate-requests-every-other-route ---------------------------------------------------------


def test_the_request_bucket_is_described_as_every_request_except_health():
    """rate-requests-every-other-route: analyses spend both buckets."""
    middleware = _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "middleware.py")
    assert 'RATE_LIMIT_EXEMPT_PATHS = frozenset({"/health"})' in middleware
    text = _read(DEPLOYMENT)
    for row in (_row(text, "`ATUS_API_RATE_REQUESTS`"), _row(text, "API per-IP token buckets")):
        assert "every other" not in row and "except `/health`" in row, row
    template = _flat(_read(ENV_PRODUCTION))
    assert "every other route" not in template and "except /health" in template


# --- neon-max-connections-104-vs-839 -----------------------------------------------------------


def test_the_template_cites_neons_connection_limit_for_the_configured_compute():
    """neon-max-connections-104-vs-839: ADR-011 configures 0.25-2 CU, where Neon allows 839."""
    row = _row(_read(DEPLOYMENT), "`ATUS_API_POOL_MAX_SIZE`")
    allowed = re.search(r"Neon allows (\d+) at 0\.25–2 CU", row).group(1)
    line = next(line for line in _read(ENV_PRODUCTION).splitlines() if "Neon allows" in line)
    assert re.search(rf"Neon allows {allowed} at the 0\.25-2 CU compute", line), line


# --- image-description-stale-after-5844ab7 -----------------------------------------------------


def test_the_image_description_names_every_check_image_check():
    """image-description-stale-after-5844ab7: the stdlib precompile and the bytecode and
    startup_imports checks."""
    gate = _read(REPO_ROOT / "deploy" / "ci" / "check_image.py")
    checks = re.findall(r"^  (\w+)\s{2,}", _section(gate, "Checks:\n", "\n\nThe database"),
                        re.MULTILINE)
    assert {"bytecode", "startup_imports", "health_no_db", "detector"} <= set(checks)
    assert "compileall" in _read(REPO_ROOT / "Dockerfile")
    image = _section(_read(DEPLOYMENT), "### 3. The image", "### 4. Accounts")
    listed = _section(image, "- **`check_image.py`**", "- **Strict.**")
    for check in checks:
        assert f"`{check}`" in listed, check
    contents = _flat(_section(image, "- **What is in it.**", "- **What is not in it:**"))
    assert "compileall" in contents and "standard library" in contents


# --- api-md-omits-tls-refusal ---------------------------------------------------------------------


def test_the_api_configuration_reference_states_the_database_tls_rule():
    """api-md-omits-tls-refusal: production refuses a URL that does not verify the server."""
    config = _read(REPO_ROOT / "src" / "atus_pipeline" / "config.py")
    assert 'VERIFYING_SSLMODES' in config and '"ATUS_DATABASE_TLS"' in config
    api = _read(DOCS_DIR / "api.md")
    url = _row(api, "`ATUS_DATABASE_URL`")
    assert "verify-full" in url and "verify-ca" in url
    tls = _row(api, "`ATUS_DATABASE_TLS`")
    assert "local-stack-only" in tls and "deployment.md#tls-to-the-database" in tls
    assert "sslmode" in _row(api, "`ATUS_ENV`")


# --- adr013-vs-ops-f23-openapi-text -----------------------------------------------------


def _descriptions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _descriptions(value)


def test_adr_013_agrees_that_the_openapi_descriptions_are_release_neutral():
    """adr013-vs-ops-f23-openapi-text: only an example response's warning names 2003-25."""
    schema = json.loads(_read(REPO_ROOT / "frontend" / "openapi.json"))
    assert not [d for d in _descriptions(schema) if "2003-25" in d]
    assert "2003-25" in _read(REPO_ROOT / "frontend" / "openapi.json")   # the example's warning
    text = _flat(_read(ADR_013))
    assert "OpenAPI descriptions still name" not in text
    assert not re.search(r'lexicon"? in a description', text)
    assert "example response" in _flat(_section(_read(ADR_013), "### Still open"))


# --- stack-mode-e2e-evidence-stale ----------------------------------------------------------------


_STACK_RECORD = re.compile(
    r"On (20\d\d-\d\d-\d\d), at ([0-9a-f]{7,40}), all (\d+) runs passed in stack mode "
    r"\(Chromium desktop (\d+), mobile (\d+), WebKit (\d+)\)")


def test_the_stack_mode_e2e_record_names_a_commit_that_has_every_e2e_spec():
    """stack-mode-e2e-evidence-stale: the record predated two specs that need the edge."""
    records = {}
    for path in (FRONTEND, DEPLOYMENT):
        match = _STACK_RECORD.search(_flat(_read(path)))
        assert match, _name(path)
        records[_name(path)] = match.groups()
    assert len(set(records.values())) == 1, records
    _, commit, total, desktop, mobile, webkit = next(iter(records.values()))
    assert int(total) == int(desktop) + int(mobile) + int(webkit)
    if _git("rev-parse", "--is-shallow-repository") != "false" or \
            _git("cat-file", "-e", f"{commit}^{{commit}}") is None:
        pytest.skip("needs the full git history")
    for spec in sorted((REPO_ROOT / "frontend" / "e2e").glob("*.spec.ts")):
        added = _git("log", "-1", "--format=%H", "--", str(spec.relative_to(REPO_ROOT)))
        assert added, spec.name
        assert _git("merge-base", "--is-ancestor", added, commit) is not None, \
            f"{spec.name} changed after the stack-mode record ({commit})"


# --- monitors-fail-during-bootstrap ---------------------------------------------------------------


def test_the_monitors_repository_variable_is_set_only_after_the_site_is_verified():
    """monitors-fail-during-bootstrap: the monitors run as soon as the repository variable
    exists, so the deploy job gets its own copy as a production environment variable."""
    for name in ("edge-check.yml", "deep-check.yml", "quota-report.yml"):
        assert "if: ${{ vars.PUBLIC_BASE_URL != '' }}" in _read(WORKFLOWS / name), name
    text = _read(DEPLOYMENT)
    github = _flat(_section(text, "### 4. GitHub (repository", "### 5. Verification"))
    assert "now, as an environment variable of production" in github
    assert "only after step 8 has verified the site, as a repository variable" in github
    prove = _flat(_section(text, "### 8. Prove the deployment", "### 9."))
    assert "repository variable PUBLIC_BASE_URL" in prove
    for path in (DEPLOYMENT, WORKFLOWS / "edge-check.yml", WORKFLOWS / "deep-check.yml"):
        assert "(before the first deploy)" not in _flat(_read(path)), _name(path)
    reference = _section(text, "### GitHub (repository variable and environments)",
                         "### Owner files")
    assert "environment `production`" in _row(reference, "`PUBLIC_BASE_URL`")


# --- label-spoofing-undocumented -------------------------------------------------------


def test_link_supplied_labels_are_an_accepted_risk():
    """label-spoofing-undocumented: a share link sets the heading text on the site's origin."""
    text = _read(SECURITY)
    labels = _flat(_section(text, "### 4. Malformed share links", "### 5."))
    assert "content spoofing" in labels.lower() and "R14" in labels
    risk = _flat(_section(text, "**R14.", "## Verification status"))
    assert "How was this calculated?" in risk and "label" in risk
