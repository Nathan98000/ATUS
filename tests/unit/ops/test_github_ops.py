"""scripts/ops/github_ops.py against a fake GitHub REST API (schedules, the deploy
gate, GHCR retention)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .conftest import OPS_DIR, json_reply, load_script

gh_ops = load_script(OPS_DIR / "github_ops.py", "atus_ops_github")

REPO = "Nathan98000/ATUS"
TOKEN = "ghs_faketoken0123456789abcdef"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def github(server, *, state=None, last_runs=None, pushed=NOW - timedelta(days=3),
           committed=None):
    """``pushed``: the repository's pushed_at (any branch, Dependabot's
    included); ``committed``: the newest commit on the default branch
    (default: the same time)."""
    state = state or {}
    last_runs = last_runs or {}
    workflows = []
    for index, path in enumerate(gh_ops.SCHEDULED_WORKFLOWS, 1):
        workflows.append({"id": index, "name": path.rsplit("/", 1)[-1], "path": path,
                          "state": state.get(path, "active"),
                          "created_at": iso(NOW - timedelta(days=90))})
        runs = [] if last_runs.get(path) is None and path in last_runs else [
            {"created_at": iso(last_runs.get(path, NOW - timedelta(hours=1)))}]
        server.routes[("GET", f"/repos/{REPO}/actions/workflows/{index}/runs"
                              "?event=schedule&per_page=1")] = json_reply(
            200, {"workflow_runs": runs}, api=False)
    workflows.append({"id": 99, "name": "CI", "path": ".github/workflows/ci.yml",
                      "state": "active"})
    server.routes[("GET", f"/repos/{REPO}/actions/workflows?per_page=100&page=1")] = \
        json_reply(200, {"total_count": len(workflows), "workflows": workflows}, api=False)
    server.routes[("GET", f"/repos/{REPO}")] = json_reply(
        200, {"pushed_at": iso(pushed), "default_branch": "main"}, api=False)
    commit = {"sha": "a" * 40, "commit": {
        "author": {"date": iso((committed or pushed) - timedelta(hours=2))},
        "committer": {"date": iso(committed or pushed)}}}
    server.routes[("GET", f"/repos/{REPO}/commits?sha=main&per_page=1")] = json_reply(
        200, [commit], api=False)


def run_schedules(server, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return gh_ops.main(["--api-url", server.base, "schedules", "--repo", REPO,
                        "--now", iso(NOW)])


def test_all_schedules_active_and_recent_pass(fake_server, monkeypatch, capsys):
    github(fake_server)
    assert run_schedules(fake_server, monkeypatch) == 0
    out = capsys.readouterr()
    assert "schedules: 0 failed" in out.out
    assert TOKEN not in out.out + out.err
    assert all(c["headers"]["authorization"] == f"Bearer {TOKEN}" for c in fake_server.calls)


def test_a_workflow_disabled_for_inactivity_fails_loudly(fake_server, monkeypatch, capsys):
    github(fake_server, state={".github/workflows/edge-check.yml": "disabled_inactivity"})
    assert run_schedules(fake_server, monkeypatch) == 1
    assert "disabled_inactivity" in capsys.readouterr().out


def test_dropped_scheduled_runs_fail(fake_server, monkeypatch, capsys):
    github(fake_server, last_runs={".github/workflows/deep-check.yml": NOW - timedelta(days=2)})
    assert run_schedules(fake_server, monkeypatch) == 1
    assert "runs are being dropped" in capsys.readouterr().out


def test_a_missing_scheduled_workflow_fails(fake_server, monkeypatch, capsys):
    github(fake_server)
    route = f"/repos/{REPO}/actions/workflows?per_page=100&page=1"
    fake_server.routes[("GET", route)] = json_reply(200, {"workflows": []}, api=False)
    assert run_schedules(fake_server, monkeypatch) == 1
    assert "not found" in capsys.readouterr().out


def test_repository_inactivity_alerts_before_the_60_day_cutoff(fake_server, monkeypatch, capsys):
    github(fake_server, pushed=NOW - timedelta(days=30))
    assert run_schedules(fake_server, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "60 days" in out and "about 30 days left" in out


def test_branch_pushes_by_dependabot_do_not_hide_an_idle_default_branch(
        fake_server, monkeypatch, capsys):
    """pushed_at counts pushes to any branch: Dependabot's weekly update
    branches keep it days old. Whether GitHub counts those as activity is not
    documented, so the check measures the default branch's newest commit."""
    github(fake_server, pushed=NOW - timedelta(days=2), committed=NOW - timedelta(days=30))
    assert run_schedules(fake_server, monkeypatch) == 1
    out = capsys.readouterr().out
    assert "last commit on main 30.0 days ago" in out and "about 30 days left" in out


def test_the_weekly_cadence_leaves_several_runs_before_the_cutoff():
    """The check runs weekly (off the hour): after the alert threshold, at
    least four more scheduled runs come before the 60-day cutoff, so one
    dropped run never costs the only warning."""
    workflow = (OPS_DIR.parents[1] / ".github" / "workflows" / "schedules-check.yml").read_text()
    assert '- cron: "41 9 * * 1"' in workflow
    assert gh_ops.INACTIVITY_ALERT + 4 * timedelta(days=7) < gh_ops.INACTIVITY_CUTOFF


def test_schedules_needs_a_token(fake_server):
    assert gh_ops.main(["--api-url", fake_server.base, "schedules", "--repo", REPO]) == 2
    assert fake_server.calls == []


# --------------------------------------------------------------------------- #
# deploy gate                                                                  #
# --------------------------------------------------------------------------- #

SHA = "0123456789abcdef0123456789abcdef01234567"
CI_RUNS = (f"/repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={SHA}&event=push"
           "&branch=main&status=success&per_page=1")


def deploy_gate(server, *, ci_passed=True, compare=("identical", 0)):
    runs = [{"id": 77, "head_sha": SHA, "conclusion": "success", "event": "push",
             "head_branch": "main"}] if ci_passed else []
    server.routes[("GET", CI_RUNS)] = json_reply(
        200, {"total_count": len(runs), "workflow_runs": runs}, api=False)
    status, behind = compare
    server.routes[("GET", f"/repos/{REPO}/compare/{SHA}...main")] = json_reply(
        200, {"status": status, "behind_by": behind, "ahead_by": 3}, api=False)


def run_gate(server, monkeypatch, sha=SHA):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return gh_ops.main(["--api-url", server.base, "deploy-gate", "--repo", REPO, "--sha", sha])


def test_a_commit_ci_passed_on_main_may_deploy(fake_server, monkeypatch, capsys):
    deploy_gate(fake_server, compare=("ahead", 0))     # main moved on since: still on main
    assert run_gate(fake_server, monkeypatch) == 0
    out = capsys.readouterr()
    assert "deploy gate: 0 failed" in out.out and TOKEN not in out.out + out.err


def test_a_commit_without_a_green_ci_run_is_refused(fake_server, monkeypatch, capsys):
    """deploy.yml runs only the Worker's config tests and the SPA build: the
    Worker tests, typecheck, lint and Playwright are ci.yml's."""
    deploy_gate(fake_server, ci_passed=False)
    assert run_gate(fake_server, monkeypatch) == 1
    assert "no successful ci.yml run" in capsys.readouterr().out


@pytest.mark.parametrize("status, behind", [("diverged", 2), ("behind", 1)])
def test_a_tag_on_a_commit_off_main_is_refused(fake_server, monkeypatch, capsys, status,
                                               behind):
    deploy_gate(fake_server, compare=(status, behind))
    assert run_gate(fake_server, monkeypatch) == 1
    assert "is not on main" in capsys.readouterr().out


def test_the_gate_refuses_a_malformed_sha(fake_server, monkeypatch):
    assert run_gate(fake_server, monkeypatch, sha="main") == 2
    assert fake_server.calls == []


# --------------------------------------------------------------------------- #
# GHCR retention                                                               #
# --------------------------------------------------------------------------- #

def versions(n: int) -> list[dict]:
    return [{"id": 1000 + i, "name": f"sha256:{i:064x}",
             "created_at": iso(NOW - timedelta(days=n - i)),
             "metadata": {"container": {"tags": [f"sha-{i:040x}"]}}} for i in range(n)]


def test_plan_keeps_the_newest_versions():
    items = [gh_ops.Version(v["id"], v["name"], gh_ops._time(v["created_at"]), [])
             for v in versions(25)]
    doomed = gh_ops.plan_cleanup(items, keep=20)
    assert sorted(v.id for v in doomed) == [1000, 1001, 1002, 1003, 1004]
    assert gh_ops.plan_cleanup(items[:20], keep=20) == []


def ghcr(server, n: int) -> str:
    base = "/users/nathan98000/packages/container/atus-api/versions"
    page1 = versions(n)[:100]
    server.routes[("GET", f"{base}?per_page=100&page=1")] = json_reply(200, page1, api=False)
    server.routes[("GET", f"{base}?per_page=100&page=2")] = json_reply(200, [], api=False)
    for v in versions(n):
        server.routes[("DELETE", f"{base}/{v['id']}")] = (204, {}, b"")
    return base


def run_cleanup(server, monkeypatch, *extra):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return gh_ops.main(["--api-url", server.base, "ghcr-cleanup", "--owner", "nathan98000",
                        "--package", "atus-api", "--keep", "20", *extra])


def test_cleanup_deletes_all_but_the_newest_20(fake_server, monkeypatch):
    base = ghcr(fake_server, 23)
    assert run_cleanup(fake_server, monkeypatch) == 0
    deleted = sorted(c["path"] for c in fake_server.calls if c["method"] == "DELETE")
    assert deleted == [f"{base}/{1000 + i}" for i in range(3)]


def test_cleanup_dry_run_deletes_nothing(fake_server, monkeypatch, capsys):
    ghcr(fake_server, 23)
    assert run_cleanup(fake_server, monkeypatch, "--dry-run") == 0
    assert fake_server.count("DELETE") == 0
    assert capsys.readouterr().out.count("would delete") == 3


def test_cleanup_refuses_a_mass_deletion(fake_server, monkeypatch):
    ghcr(fake_server, 90)
    assert run_cleanup(fake_server, monkeypatch, "--max-delete", "50") == 1
    assert fake_server.count("DELETE") == 0


def test_cleanup_without_a_package_is_a_no_op(fake_server, monkeypatch, capsys):
    fake_server.spa_fallback = False  # an API, not the site: unknown paths are 404
    assert run_cleanup(fake_server, monkeypatch) == 0  # the listing is a 404
    assert "nothing to do" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the `live` and `deployed-<commit>` tags (deploy.yml job promote)              #
# --------------------------------------------------------------------------- #

C_OLD, C_LIVE = "1" * 40, "2" * 40
D_OLD, D_LIVE, D_RERUN = (f"sha256:{c * 64}" for c in "abc")


def tagged(server, *, extra=20) -> str:
    """GHCR as deploy.yml leaves it: D_LIVE deployed from C_LIVE (tags live,
    deployed-C_LIVE) long ago, then a re-run of C_LIVE pushed D_RERUN (moving
    sha-C_LIVE) that was never deployed, then ``extra`` newer pushes."""
    items = [
        {"id": 1, "name": D_OLD, "created_at": iso(NOW - timedelta(days=60)),
         "metadata": {"container": {"tags": [f"sha-{C_OLD}", f"deployed-{C_OLD}"]}}},
        {"id": 2, "name": D_LIVE, "created_at": iso(NOW - timedelta(days=40)),
         "metadata": {"container": {"tags": ["live", f"deployed-{C_LIVE}"]}}},
        {"id": 3, "name": D_RERUN, "created_at": iso(NOW - timedelta(days=30)),
         "metadata": {"container": {"tags": [f"sha-{C_LIVE}"]}}},
    ] + [{"id": 100 + i, "name": f"sha256:{i:064x}",
          "created_at": iso(NOW - timedelta(days=20 - i * 0.5)),
          "metadata": {"container": {"tags": [f"sha-{i:040x}"]}}} for i in range(extra)]
    base = "/users/nathan98000/packages/container/atus-api/versions"
    server.routes[("GET", f"{base}?per_page=100&page=1")] = json_reply(200, items, api=False)
    for item in items:
        server.routes[("DELETE", f"{base}/{item['id']}")] = (204, {}, b"")
    return base


def test_cleanup_never_deletes_the_image_render_is_configured_with(fake_server, monkeypatch):
    """Render's service runs `:live` on any deploy not made through the hook
    (Manual Deploy): that digest must survive however many pushes follow."""
    base = tagged(fake_server)
    assert run_cleanup(fake_server, monkeypatch) == 0
    deleted = sorted(c["path"] for c in fake_server.calls if c["method"] == "DELETE")
    assert f"{base}/2" not in deleted
    assert deleted == [f"{base}/1", f"{base}/3"]


def run_tags(server, monkeypatch, *argv):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return gh_ops.main(["--api-url", server.base, argv[0], "--owner", "nathan98000",
                        "--package", "atus-api", *argv[1:]])


def test_live_build_names_the_commit_of_the_last_successful_deploy(fake_server, monkeypatch,
                                                                   capsys):
    tagged(fake_server)
    assert run_tags(fake_server, monkeypatch, "live-build") == 0
    assert capsys.readouterr().out.splitlines() == [f"build={C_LIVE}"]


def test_live_build_is_empty_before_the_first_deploy(fake_server, monkeypatch, capsys):
    ghcr(fake_server, 3)
    assert run_tags(fake_server, monkeypatch, "live-build") == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["build="] and "no version is tagged live" in out.err


def test_a_same_commit_rerun_finds_the_digest_that_was_deployed(fake_server, monkeypatch,
                                                                 capsys):
    """The re-run's push moved sha-C_LIVE to D_RERUN before the deploy job
    looks: the rollback target must still be D_LIVE."""
    tagged(fake_server)
    assert run_tags(fake_server, monkeypatch, "deployed-image", "--build", C_LIVE,
                    "--this-build", C_LIVE) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"image_ref=ghcr.io/nathan98000/atus-api@{D_LIVE}", "image_source=deployed-tag"]


def test_an_undeployed_build_falls_back_to_its_sha_tag_only_for_another_commit(
        fake_server, monkeypatch, capsys):
    tagged(fake_server)
    other = f"{0:040x}"
    assert run_tags(fake_server, monkeypatch, "deployed-image", "--build", other,
                    "--this-build", C_LIVE) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"image_ref=ghcr.io/nathan98000/atus-api@sha256:{0:064x}", "image_source=sha-tag"]
    assert run_tags(fake_server, monkeypatch, "deployed-image", "--build", other,
                    "--this-build", other) == 0
    assert capsys.readouterr().out.splitlines() == ["image_ref=", "image_source=unknown"]
