"""scripts/ops/github_ops.py against a fake GitHub REST API (schedules, GHCR retention)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .conftest import OPS_DIR, json_reply, load_script

gh_ops = load_script(OPS_DIR / "github_ops.py", "atus_ops_github")

REPO = "Nathan98000/ATUS"
TOKEN = "ghs_faketoken0123456789abcdef"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def github(server, *, state=None, last_runs=None, pushed=NOW - timedelta(days=3)):
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
    server.routes[("GET", f"/repos/{REPO}")] = json_reply(200, {"pushed_at": iso(pushed)},
                                                          api=False)


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


def test_the_monthly_cadence_always_lands_before_the_cutoff():
    """A check runs at most 31 days after the previous one."""
    assert gh_ops.INACTIVITY_ALERT + timedelta(days=31) < gh_ops.INACTIVITY_CUTOFF


def test_schedules_needs_a_token(fake_server):
    assert gh_ops.main(["--api-url", fake_server.base, "schedules", "--repo", REPO]) == 2
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
