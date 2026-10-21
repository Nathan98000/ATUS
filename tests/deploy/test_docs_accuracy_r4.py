"""Documentation accuracy: the confirmed docs findings of the Phase 5 review, round 4.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, in the manner of ``test_docs_accuracy_r3.py``. Wherever the statement has
a source of truth (the Worker's cache, a script, a workflow), the test reads
that source too, so a change there fails here until the docs follow.

Read-only. Needs neither Docker nor the network.
"""

from __future__ import annotations

import re

import yaml

from .conftest import REPO_ROOT
from .test_docs_accuracy import (
    ADR,
    DEPLOYMENT,
    DOCS_DIR,
    OPERATIONS,
    SECURITY,
    WORKFLOWS,
    _flat,
    _read,
    _row,
    _section,
)
from .test_infra_review_r2 import _step

SCRIPTS = REPO_ROOT / "scripts"
EDGE_SRC = REPO_ROOT / "edge" / "src"
FLUSH = "bump EPOCH in edge/wrangler.jsonc and deploy"

# --- leak-recovery-keeps-tampered-kv-results --------------------------------------------


def test_every_leak_that_can_alter_served_results_flushes_the_edge_cache():
    """leak-recovery-keeps-tampered-kv-results: a leaked neondb_owner DSN or NEON_API_KEY
    (restore of the same release into a new project) or atus_api password (roles.sql
    again) can alter what the API computes without changing a row, yet none of those
    runbooks changed an input of the Worker's CACHE_SCOPE: every analysis KV stored
    meanwhile stayed servable (X-Cache: edge) for up to 30 days. Only the Cloudflare-token
    runbook bumped EPOCH. Every such runbook now bumps it too, and the docs no longer claim
    that no cached result can outlive a database or that a leaked API string cannot change
    what is served."""
    cache = _read(EDGE_SRC / "cache.ts")
    scope = re.search(r"export async function cacheScope\(.*?\n\}", cache, re.S).group(0)
    # a restore of the same release changes none of these but EPOCH
    assert re.findall(r"env\.(\w+)", scope) == [
        "EPOCH", "API_VERSION", "ANALYTICS_VERSION", "DATA_VERSION", "RESPONSE_FORMAT"]
    assert "export const KV_TTL_SECONDS = 30 * 24 * 60 * 60" in _read(EDGE_SRC / "constants.ts")
    # the API's own cache lives in the process, so the switch's restart empties it
    assert "the cache is lost on every restart" in _flat(
        _read(REPO_ROOT / "src" / "atus_pipeline" / "api" / "cache.py"))

    deployment = _read(DEPLOYMENT)
    rotation = _section(deployment, "### Rotation", "### TLS to the database")
    for number in (2, 5, 7):
        assert FLUSH in _flat(_step(rotation, number)), number
    for number in (2, 7):
        step = _flat(_step(rotation, number))
        assert "X-Cache: edge" in step and "up to 30 days" in step, number
        assert "empties the API's in-process cache" in step, number

    operations = _read(OPERATIONS)
    rows = _section(operations, "### Rotating tokens and secrets", "### A quota is exhausted")
    for secret in ("atus_api password", "Cloudflare API token (90-day TTL)",
                   "Neon owner password, NEON_API_KEY"):
        (row,) = [line for line in rows.splitlines()
                  if _flat(line).startswith(f"| {secret} |")]
        assert FLUSH in _flat(row), secret
    lost = _flat(_section(operations, "**Restore after a lost project.**",
                          "**Re-apply roles**"))
    assert FLUSH in lost and "X-Cache: edge" in lost
    assert "the Worker vars and the snapshot do not change" not in lost

    security = _read(SECURITY)
    for secret in ("`atus_api` password (in `ATUS_DATABASE_URL`)", "`neondb_owner` DSN",
                   "`NEON_API_KEY` (optional)", "`CLOUDFLARE_API_TOKEN`"):
        assert "bump EPOCH" in _flat(_row(security, secret)), secret
    poisoning = _flat(_section(security, "### 7. Cache poisoning", "### 8. Header injection"))
    assert "passes the store gate like any other" in poisoning and "bump EPOCH" in poisoning

    architecture = _flat(_read(DOCS_DIR / "architecture.md"))
    assert "a cached result can never be served for a different database" not in architecture
    assert "A restore of the same release artifact keeps its data_version" in architecture
    adr = _flat(_read(ADR / "ADR-013-data-releases-and-serving-db.md"))
    assert "a leaked API connection string cannot change data." not in adr
    assert "No cache crosses versions" not in adr
    assert "No cache crosses releases." in adr and "bump EPOCH" in adr


# --- deploy-step6-edge-monitor-cannot-confirm-release -----------------------------------

EXPECT = "--expect-snapshot-id"


def test_the_edge_monitor_confirms_the_release_wherever_it_decides_one():
    """deploy-step6-edge-monitor-cannot-confirm-release: Deploy step 6 concluded from a
    passing `monitors.py edge` that a late release had arrived, and the edge rollbacks used
    it as their only identity check, but it took no expected release and passed on the
    previous one, exactly what the failed smoke had reported. It now takes the release's
    snapshot id and data version (tests/unit/ops/test_monitors.py), and every runbook that
    decides from it which release is served passes them."""
    monitors = _read(SCRIPTS / "ops" / "monitors.py")
    assert '"--expect-snapshot-id"' in monitors and '"--expect-data-version"' in monitors
    assert "the static pointer names another release" in monitors

    operations = _read(OPERATIONS)
    deploy = _section(operations, "### Deploy", "### Rollback")
    step = _flat(_step(deploy, 6))
    assert 'monitors.py edge --base-url "$SITE" --expect-snapshot-id <snapshot> ' \
        "--expect-data-version <data>" in step
    assert 'This run" column of the summary\'s Rollback table' in step
    assert "Without them it passes on the previous release too" in step
    edge = _section(operations, "- **Edge** (SPA, snapshot", "- **Keep the API and edge")
    assert "wrangler versions view <version id> --json" in edge
    assert re.search(r"monitors\.py edge --base-url \"\$SITE\" \\\n\s+--expect-snapshot-id "
                     r"<its SNAPSHOT_ID> --expect-data-version <its DATA_VERSION>", edge)

    rollback = _flat(_section(_read(DEPLOYMENT), "- **Edge (SPA, snapshot, Worker and its vars):**",
                              "- **Data:**"))
    assert "monitors.py edge --base-url https://atus.<sub>.workers.dev --expect-snapshot-id " \
        "<its SNAPSHOT_ID> --expect-data-version <its DATA_VERSION>" in rollback
    assert "wrangler versions view <version id> --json" in rollback

    jobs = yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))["jobs"]
    (notes,) = [s["run"] for s in jobs["deploy"]["steps"] if s.get("name") == "Rollback notes"]
    assert "monitors.py edge --base-url $PUBLIC_BASE_URL" in notes
    for line in notes.splitlines():
        if "monitors.py edge" in line:
            assert EXPECT in line and "--expect-data-version" in line, line
    assert "wrangler versions view <version id> --json" in notes

    # every edge check of the Deploy and Rollback runbooks names the release it expects
    commands = re.findall(r"monitors\.py edge (?:\\\n|[^`\n])*",
                          _section(operations, "### Deploy", "### The image scan blocks"))
    assert len(commands) == 3, commands             # step 5, step 6, Rollback's Edge
    for command in commands:
        assert EXPECT in command and "--expect-data-version" in command, command


# --- deep-check-runbook-read-role -------------------------------------------------------


def test_the_deep_check_runbook_keeps_the_repository_admin_on_the_package():
    """deep-check-runbook-read-role: on a 401/403 the runbook said to add the repository
    to the package's Manage Actions access with the Read role, and that ghcr-cleanup fails
    the same way. Read can only download: deploy.yml's push and promote upload the image
    and its tags, and the cleanup deletes versions (Admin, owner step 4.5), so following
    it on the existing entry broke the next deploy and fixed nothing for the cleanup."""
    jobs = yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))["jobs"]
    for name in ("push", "promote"):
        assert jobs[name]["permissions"]["packages"] == "write", name
    cleanup = _read(WORKFLOWS / "ghcr-cleanup.yml")
    (job,) = yaml.safe_load(cleanup)["jobs"].values()
    assert job["permissions"]["packages"] == "write"
    header = _flat("".join(line.lstrip("#") for line in cleanup.splitlines(keepends=True)
                           if line.startswith("#")))
    assert "the Admin role" in header and "owner step 4.5" in header

    owner = _flat(_step(_section(_read(DEPLOYMENT), "### 4. GitHub", "### 5. Verification"), 5))
    assert "**Manage Actions access:** Nathan98000/ATUS with role **Admin**" in owner

    runbook = _flat(_section(_read(OPERATIONS), "### The deep check cannot read the live tag",
                             "### Scheduled workflows were disabled"))
    assert "with the Read role" not in runbook
    assert "listed with the Admin role" in runbook and "never lower it to Read" in runbook
    assert "owner step 4.5" in runbook
    # the deep check's own error says the same (tests/unit/ops/test_github_ops.py)
