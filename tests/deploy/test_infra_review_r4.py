"""Edge config, workflows and runbooks: the confirmed infra findings of the Phase 5
review, round 4.

Each test pins one finding (named in its docstring). Where a statement has a
source of truth (a workflow, a script, a test), the test reads it too, so a
change there fails here until the docs follow. Read-only; needs neither Docker
nor the network (the git tests build throwaway repositories under tmp_path).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from .conftest import REPO_ROOT
from .test_docs_accuracy import (
    DEPLOYMENT,
    FRONTEND,
    OPERATIONS,
    WORKFLOWS,
    _flat,
    _read,
    _section,
    e2e_suite_objects,
    stack_record_problems,
)
from .test_infra_review_r2 import _step

EDGE = REPO_ROOT / "edge"
SMOKE = REPO_ROOT / "scripts" / "ops" / "deploy_smoke.py"


def _deploy_steps() -> list[dict]:
    return yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))["jobs"]["deploy"]["steps"]


# --- epoch-prewake-levers-blocked-by-config-test ------------------------------------------


def test_the_manual_edge_levers_change_wrangler_jsonc_and_no_test():
    """epoch-prewake-levers-blocked-by-config-test: the docs have the owner bump EPOCH (a
    full edge-cache flush; a step of the leaked-token runbook) or set PREWAKE to off in
    edge/wrangler.jsonc and deploy, but config.test.ts pinned EPOCH '1' and PREWAKE 'on'
    and 8 (EPOCH) or 11 (PREWAKE) worker tests inherited the file's values, so CI and
    deploy.yml's edge-config job refused the commit. The config test now accepts the
    levers, the worker tests pin them, and the docs say no test changes."""
    config_test = _read(EDGE / "test" / "node" / "config.test.ts")
    levers = re.search(r"const OWNER_LEVERS: Record<string, RegExp> = \{\n(.*?)\n\}",
                       config_test, re.DOTALL)
    assert levers
    names = re.findall(r"^  ([A-Z_]+): /", levers.group(1), re.MULTILINE)
    assert names == ["EPOCH", "PREWAKE", "NEON_PROJECT_ID"]
    release = _read(EDGE / "test" / "release.ts")
    for name in [*names, "RESPONSE_FORMAT"]:
        assert re.search(rf"^  {name}: '[^']*',$", release, re.MULTILINE), name
    vitest = _read(EDGE / "vitest.config.ts")
    assert "import { TEST_RELEASE } from './test/release'" in vitest
    assert "bindings: { ...TEST_SECRETS, ...TEST_RELEASE }" in vitest
    # The vars the docs call manual are exactly the levers.
    table = _section(_read(DEPLOYMENT), "| Var | Set by |", "These are all the vars")
    manual = re.findall(r"^\| `([A-Z_]+)` \| `edge/wrangler\.jsonc`[^|]*\|[^|]*\|[^|]*\| manually",
                        table, re.MULTILINE)
    assert manual == ["EPOCH", "PREWAKE"] and set(manual) < set(names)
    asserts = _flat(_section(_read(DEPLOYMENT), "- **What CI asserts on `edge/wrangler.jsonc`",
                             "- **Names**"))
    for name in names:
        assert name in asserts, name
    assert "changes edge/wrangler.jsonc alone" in asserts
    assert "keep their safe defaults, except NEON_PROJECT_ID" not in asserts


# --- smoke-recompute-answered-from-kv, smoke-fails-on-governor-refusal ----------------------


def _smoke_step() -> dict:
    (step,) = [s for s in _deploy_steps() if "scripts/ops/deploy_smoke.py" in s.get("run", "")]
    return step


@pytest.mark.parametrize("smoke_status", [0, 1, 3])
def test_the_deploy_job_reports_an_owed_recompute_without_calling_for_a_rollback(
        tmp_path, smoke_status):
    """smoke-fails-on-governor-refusal: deploy.yml's smoke step, run with a stand-in
    `python` that exits like deploy_smoke.py. Exit 3 (OWED: the quota governor refused the
    live recompute) still fails the job, so promote never tags a digest no live analysis
    ran on, but says it is not a reason to roll back."""
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text(f"#!/bin/sh\necho \"smoke args: $*\"\nexit {smoke_status}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_STEP_SUMMARY": str(summary), "PUBLIC_BASE_URL": "https://site.invalid",
           "GITHUB_SHA": "0" * 40, "DATA_VERSION": "dv", "SNAPSHOT_ID": "snap",
           "EDGE_OPS_TOKEN": "", "RENDER_ORIGIN": ""}
    result = subprocess.run(["bash", "-e", "-c", _smoke_step()["run"]], env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == smoke_status
    assert "--recompute 2" in result.stdout and "--require-snapshot" in result.stdout
    owed = "::error title=Live recompute owed (no rollback)::"
    if smoke_status == 3:
        assert owed in result.stdout
        assert "not a reason to roll back" in summary.read_text()
    else:
        assert owed not in result.stdout and not summary.exists()
    script = _read(SMOKE)
    assert "EXIT_RECOMPUTE_OWED = 3" in script


def test_an_owed_recompute_skips_promote_and_the_runbook_keeps_the_release():
    """smoke-fails-on-governor-refusal: Deploy step 6 sent every failed smoke to Rollback,
    a data release's included."""
    jobs = yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))["jobs"]
    assert "deploy" in jobs["promote"]["needs"] and "if" not in jobs["promote"]
    (notes,) = [s["run"] for s in _deploy_steps() if s.get("name") == "Rollback notes"]
    assert "**Smoke failed only with OWED rows** (exit 3" in notes
    assert "Deploy step 6" in notes
    step = _flat(_step(_section(_read(OPERATIONS), "### Deploy", "### Rollback"), 6))
    for text in ("unless the only rows that did not pass are OWED",
                 "OWED is not a reason to roll back", "exit status 3",
                 "daily_capacity_reached, capacity_paused or rate_limited",
                 "capacity_unavailable (the governor failed) and every other refusal stay FAIL",
                 "promote does not tag a digest no live analysis has run on yet",
                 'finish the run as under "To keep the new version"'):
        assert text in step, text
    script = _read(SMOKE)
    assert 'QUOTA_REFUSALS = frozenset({"rate_limited", "daily_capacity_reached", ' \
           '"capacity_paused"})' in script


def test_the_docs_describe_a_recompute_the_edge_cache_cannot_answer():
    """smoke-recompute-answered-from-kv: the docs said the smoke's different bytes kept
    the edge cache from answering; they differed from the snapshot's only, and every
    deploy of a release sent the same ones."""
    deployment = _flat(_read(DEPLOYMENT))
    assert "recomputed live with different request bytes (so the edge cache cannot " \
           "answer)" not in deployment
    assert "recomputed live with request bytes unique to the run" in deployment
    assert "an X-Cache: edge answer now fails the check" in deployment
    row = next(line for line in _read(OPERATIONS).splitlines()
               if line.startswith("| `scripts/ops/deploy_smoke.py --recompute 2`"))
    assert "with different request bytes |" not in row
    assert "unique to the run" in _flat(row) and "X-Cache: edge answer fails" in _flat(row)
    script = _read(SMOKE)
    assert "json.dumps(spec, indent=1).encode()" not in script
    assert "recompute_body(spec, self.nonce)" in script
    assert "secrets.randbits(NONCE_BITS)" in script


# --- ci-git-history-guard-breaks-on-squash ------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


def _write(root: Path, name: str, text: str = "x") -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text)


def test_the_stack_mode_guard_survives_a_squashed_landing(tmp_path):
    """ci-git-history-guard-breaks-on-squash: with ATUS_REQUIRE_GIT_HISTORY=1, the guard
    failed whenever the recorded commit was not in the checkout, so a squashed, rebased
    or curated landing of Phase 5 (the way earlier phases landed) failed every CI push run
    on main, and the deploy gate refused every deploy. It now compares the suite's git
    object ids, which such a landing keeps."""
    if subprocess.run(["git", "init", "-q", "-b", "phase", str(tmp_path)],
                      capture_output=True, check=False).returncode != 0:
        pytest.skip("git is not available")
    for name in ("frontend/e2e/a.spec.ts", "frontend/e2e/helpers.ts",
                 "frontend/playwright.config.ts", "frontend/src/app.ts"):
        _write(tmp_path, name)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "recorded")
    recorded = _git(tmp_path, "rev-parse", "HEAD")
    objects = tuple(i[:12] for i in e2e_suite_objects("HEAD", tmp_path))
    _write(tmp_path, "frontend/src/app.ts", "changed")       # app code: not the suite
    _git(tmp_path, "commit", "-q", "-am", "later")
    assert stack_record_problems(recorded[:7], objects, tmp_path) == []

    # Land it squashed: one new root commit with the same tree, the branch history gone.
    _git(tmp_path, "checkout", "-q", "--orphan", "main")
    _git(tmp_path, "commit", "-q", "-m", "Phase 5 (squashed)")
    _git(tmp_path, "branch", "-q", "-D", "phase")
    _git(tmp_path, "reflog", "expire", "--expire=now", "--all")
    _git(tmp_path, "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "cat-file", "-e", f"{recorded}^{{commit}}"], cwd=tmp_path,
                          capture_output=True, check=False).returncode != 0
    assert stack_record_problems(recorded[:7], objects, tmp_path) == []

    # A later change to the suite is still caught, committed or untracked.
    _write(tmp_path, "frontend/e2e/b.spec.ts")
    assert stack_record_problems(recorded[:7], objects, tmp_path) == [
        "untracked: frontend/e2e/b.spec.ts"]
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "a new spec")
    (problem,) = stack_record_problems(recorded[:7], objects, tmp_path)
    assert problem.startswith("HEAD's suite is ") and "not the recorded" in problem


def test_the_stack_mode_guard_checks_the_record_against_its_commit_when_present(tmp_path):
    if subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                      check=False).returncode != 0:
        pytest.skip("git is not available")
    _write(tmp_path, "frontend/e2e/a.spec.ts")
    _write(tmp_path, "frontend/playwright.config.ts")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "recorded")
    recorded = _git(tmp_path, "rev-parse", "HEAD")
    tree, blob = e2e_suite_objects("HEAD", tmp_path)
    _write(tmp_path, "frontend/e2e/a.spec.ts", "changed")
    _git(tmp_path, "commit", "-q", "-am", "changed")
    head_tree, _ = e2e_suite_objects("HEAD", tmp_path)
    # The record names the commit but the ids of a later suite: refused, with the files.
    problems = stack_record_problems(recorded, (head_tree[:12], blob[:12]), tmp_path)
    assert problems == [f"the recorded object ids are not those of {recorded}"]
    problems = stack_record_problems(recorded, (tree[:12], blob[:12]), tmp_path)
    assert problems[1:] == [f"changed since {recorded}: frontend/e2e/a.spec.ts"]


def test_the_stack_mode_record_names_the_suites_object_ids_in_both_docs():
    # Any record's commit and ids: each new stack-mode run replaces them
    # (test_docs_accuracy checks that they are HEAD's suite).
    for path in (FRONTEND, DEPLOYMENT):
        assert re.search(r"at [0-9a-f]{7,40} \(E2E suite: frontend/e2e tree [0-9a-f]{12,40}, "
                         r"frontend/playwright\.config\.ts blob [0-9a-f]{12,40}\)",
                         _flat(_read(path))), path
    # The old failure message advised fetch-depth: 0, which CI already used.
    assert "check out the full history (fetch-depth: 0)" not in _read(
        REPO_ROOT / "tests" / "deploy" / "test_docs_accuracy.py")
    assert "suite's git object ids instead" in _read(WORKFLOWS / "ci.yml")


# --- pgbouncer-rehearsal-green ------------------------------------------------------------


def test_the_pooler_rehearsal_record_is_the_current_trees_and_its_exit_status():
    """pgbouncer-rehearsal-green: review round 3 marked the tests that need a direct
    connection but never re-ran the brief §10 rehearsal on the merged tree. Round 4 ran
    it at 3dffe1a: exit 0. The record names the commit, the exit status and both counts,
    and the script's exit status is pytest's (pipefail through the redaction)."""
    pooler = _flat(_section(_read(DEPLOYMENT), "- **Through a transaction pooler**",
                            "- **The image gate"))
    record = re.search(r"Measured on (2026-\d\d-\d\d) \(review round 4, at ([0-9a-f]{7}),.*?\): "
                       r"with max_prepared_statements=1000 the command \*\*exited 0, (\d+) "
                       r"passed, (\d+) deselected\*\*", pooler)
    assert record, pooler
    assert "21 failed, 13 passed, 1 skipped and 85 errors" in pooler
    assert "With 0 the command exits 1 by design: 14 failed, 98 passed, 120 deselected and " \
           "32 errors" in pooler
    smoke = _read(REPO_ROOT / "scripts" / "smoke_stack.sh")
    assert "set -euo pipefail" in smoke
    assert re.search(r'-m pytest tests/integration -m "not direct_connection" -q '
                     r'-p no:cacheprovider \\\n\s+2>&1 \| redact\nfi', smoke)
