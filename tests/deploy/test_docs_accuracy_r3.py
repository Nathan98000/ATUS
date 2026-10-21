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
