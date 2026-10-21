"""Documentation accuracy: the confirmed docs findings of the Phase 5 review, round 3.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, in the manner of ``test_docs_accuracy.py`` and ``test_docs_accuracy_r2.py``.
Wherever the statement has a source of truth (a workflow, a script, a test, the
locked wrangler), the test reads that source too, so a change there fails here
until the docs follow.

Read-only. Needs neither Docker nor the network; the wrangler source check skips
without ``edge/node_modules``.
"""

from __future__ import annotations

import ast
import re

import yaml

from .conftest import REPO_ROOT
from .test_docs_accuracy import DEPLOYMENT, OPERATIONS, WORKFLOWS, _flat, _read, _row, _section
from .test_infra_review_r2 import _step

SCRIPTS = REPO_ROOT / "scripts"

# --- failed-smoke-leaves-live-tag-behind -----------------------------------------------


def test_a_failed_smoke_says_how_to_move_the_live_tags():
    """failed-smoke-leaves-live-tag-behind: the Render hook deploys the new digest before
    the smoke, and `promote` (which moves `live` and `deployed-<commit>`) needs the
    smoke's job and has no `if:`, so a failed smoke leaves Render on a digest `live` does
    not name. Deploy step 6 said "nothing needs rolling back" after a late-propagation
    pass and stopped: deep-check then failed every 6 h, and a later "Save and deploy"
    could bring back the previous image. The runbooks now finish the run or move both
    tags, and the deep check's build mismatch has an incident entry."""
    jobs = yaml.safe_load(_read(WORKFLOWS / "deploy.yml"))["jobs"]
    promote = jobs["promote"]
    assert {"deploy", "browser-smoke"} <= set(promote["needs"])
    assert jobs["browser-smoke"]["needs"] == "deploy"
    assert "if" not in promote, "promote now runs after a failed smoke: revise Deploy step 6"
    runs = [step.get("run", "") for step in jobs["deploy"]["steps"]]
    (hook,) = [i for i, run in enumerate(runs)
               if run.startswith("python scripts/ops/render_deploy.py trigger")]
    (smoke,) = [i for i, run in enumerate(runs) if "scripts/ops/deploy_smoke.py" in run]
    assert hook < smoke                       # Render runs the new digest before the smoke

    operations = _read(OPERATIONS)
    step = _flat(_step(_section(operations, "### Deploy", "### Rollback"), 6))
    for text in ("the Render hook and the Worker deploy both come before the smoke",
                 "A failed smoke skips browser-smoke and promote",
                 "Re-run failed jobs", "gh run rerun <run id> --failed",
                 "both live and deployed-<commit> by hand",
                 "roll back the API as well as the edge",
                 "If you roll back only the edge and keep the new API"):
        assert text in step, text
    # the deep check's own failure line (monitors.py through httpcheck.Report)
    monitors = _read(SCRIPTS / "ops" / "monitors.py")
    assert 'report.fail("api_health", f"build {build}, expected {expect_build}")' in monitors
    assert 'print(f"[{status}] {name}" + (f": {detail}" if detail else "")' in \
        _read(SCRIPTS / "ops" / "httpcheck.py")
    assert "[FAIL] api_health: build <this commit>, expected <previous commit>" in step

    # by hand, a digest that never reached promote needs its deployed- tag too: the deep
    # check reads the commit it expects from it (github_ops.live_build)
    github_ops = _read(SCRIPTS / "ops" / "github_ops.py")
    assert 'DEPLOYED_PREFIX = "deployed-"' in github_ops and "def live_build" in github_ops
    rollback = _flat(_section(operations, "### Rollback", "### The image scan blocks a deploy"))
    assert "--tag ghcr.io/nathan98000/atus-api:deployed-<its commit sha>" in rollback
    assert "After a deploy whose smoke failed, promote never ran" in rollback
    row = _flat(_row(operations, "deep check fails with `[FAIL] api_health: build <X>, "
                                 "expected <Y>`"))
    assert "a deploy whose smoke failed (promote skipped)" in row and "step 6" in row
    assert "without deployed-<commit>" in _flat(_section(
        operations, "### The deep check cannot read the live tag", "### Scheduled"))

    deployment = _flat(_read(DEPLOYMENT))
    assert "A failed smoke skips browser-smoke and promote" in deployment
    assert "After a deploy whose smoke failed, promote never ran" in deployment


# --- pgbouncer-rehearsal-now-fails ------------------------------------------------------

#: What failed or errored when `scripts/smoke_stack.sh --integration-via-pgbouncer` ran
#: all of tests/integration through PgBouncer at cd9ad7e (20 failed, 79 errors): a whole
#: module, a class, or one test (module, class, function).
FAILED_THROUGH_PGBOUNCER = [
    ("test_serving_restore.py", None, None),
    ("test_reload_switch.py", None, None),
    ("api/test_protection_db.py", "TestStaleConnections", None),
    ("test_equivalence_harness.py", None,
     "test_a_changed_serving_database_is_caught_with_json_paths"),
    *(("test_serving_contract.py", None, name) for name in (
        "test_the_role_cannot_read_outside_the_contract",
        "test_the_engine_reads_only_contract_columns",
        "test_every_endpoint_works_within_the_contract",
        "test_benchmarks_record_their_results_within_the_contract")),
    *(("test_serving_build.py", None, name) for name in (
        "test_the_content_hash_changes_with_the_data",
        "test_an_unconvertible_replicate_weight_stops_the_build",
        "test_a_failed_build_into_an_empty_database_leaves_it_empty",
        "test_an_omitted_row_that_an_estimate_would_read_stops_the_build",
        "test_the_build_requires_recorded_validation_by_default",
        "test_only_results_recorded_since_the_data_went_live_validate_it",
        "test_a_select_only_role_can_build_from_the_source",
        "test_the_commands_build_verify_and_compare")),
]


def _marked(node: ast.AST) -> bool:
    return any("pytest.mark.direct_connection" in ast.unparse(d)
               for d in getattr(node, "decorator_list", []))


def _direct_connection(module: str, cls: str | None, function: str | None) -> bool:
    tree = ast.parse(_read(REPO_ROOT / "tests" / "integration" / module))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            if "pytest.mark.direct_connection" in ast.unparse(node.value):
                return True
    if function is None and cls is None:
        return False
    (owner,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls] \
        if cls else [tree]
    if cls and _marked(owner):
        return True
    if function is None:
        return False
    (test,) = [n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == function]
    return _marked(test)


def test_the_pooler_rehearsal_leaves_out_what_no_transaction_pooler_supports():
    """pgbouncer-rehearsal-now-fails: the brief §10 rehearsal ran all of tests/integration
    through PgBouncer and exited 1 (20 failed, 79 errors), while deployment.md still cited
    "50/50". The failures were tests that create roles or template databases, set startup
    options, terminate backends or hold session-level locks. They are now marked
    ``direct_connection`` and the rehearsal deselects them; the docs record the scoped
    result."""
    pyproject = _read(REPO_ROOT / "pyproject.toml")
    assert '"direct_connection: ' in pyproject            # a registered marker
    smoke = _read(SCRIPTS / "smoke_stack.sh")
    assert re.search(r'-m pytest tests/integration -m "not direct_connection"', smoke)
    unmarked = [entry for entry in FAILED_THROUGH_PGBOUNCER if not _direct_connection(*entry)]
    assert unmarked == []

    pooler = _flat(_section(_read(DEPLOYMENT), "- **Through a transaction pooler**",
                            "- **The image gate"))
    assert "direct_connection" in pooler and "deselect" in pooler
    assert "50/50 integration tests pass" not in pooler
    assert re.search(r"Measured on 2026-10-21 .*\d+ passed, \d+ deselected", pooler)
    f18 = _flat(_row(_section(_read(OPERATIONS), "### Findings F01", "### Recorded facts"),
                     "F18"))
    assert "direct_connection" in f18


# --- equivalence-harness-readme-targets-neon --------------------------------------------


def test_the_equivalence_harness_is_documented_against_the_local_drill_only():
    """equivalence-harness-readme-targets-neon: the README (and replay.py's docstring) gave
    the Neon project's secrets file as the serving target of the 544-spec replay, a
    high-volume probe of a free provider (brief §5). Both now name the local drill, the
    harness refuses a database that is not on this machine (tests/unit/
    test_equivalence_harness.py), and the README says so."""
    readme = _read(SCRIPTS / "equivalence" / "README.md")
    replay = _read(SCRIPTS / "equivalence" / "replay.py")
    docstring = ast.get_docstring(ast.parse(replay))
    for text in (readme, docstring):
        targets = re.findall(r"--serving-secrets-file\s+(\S+)", text)
        assert targets and all("neon" not in t for t in targets), targets
        assert "~/.config/atus/drill.env" in targets
        assert "replay_deployed.py" in text and "--allow-remote-database" in text
    assert '"--allow-remote-database"' in replay and "def is_local_database" in replay
    flat = _flat(readme)
    assert "Never point it at a provider's database (Neon)" in flat
    assert "refuses a database that is not on this machine" in flat
