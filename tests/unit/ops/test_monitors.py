"""scripts/ops/monitors.py (and scripts/ops/httpcheck.py) against a local fake site."""

from __future__ import annotations

import json

import pytest

from .conftest import (
    OPS_DIR,
    envelope,
    html_reply,
    json_reply,
    load_script,
)

monitors = load_script(OPS_DIR / "monitors.py", "atus_ops_monitors")
httpcheck = load_script(OPS_DIR / "httpcheck.py", "httpcheck")

SNAP = "abcdef012345-0123456789abcdef"
TOKEN = "ops-token-0123456789abcdef0123456789abcdef"


def healthy_site(server, *, snapshot_id: str = SNAP) -> None:
    server.routes.update({
        ("GET", "/"): html_reply(),
        ("GET", "/api/_edge/health"): json_reply(200, {
            "status": "ok", "edge_version": "0.1.0", "data_version": "dv",
            "snapshot_id": snapshot_id}),
        ("GET", "/snap/current.json"): json_reply(200, {"snapshot_id": SNAP}, api=False),
        ("GET", f"/snap/{SNAP}/manifest.json"): json_reply(200, {"snapshot_id": SNAP},
                                                          api=False),
    })


# --------------------------------------------------------------------------- #
# edge                                                                         #
# --------------------------------------------------------------------------- #

def test_edge_check_passes_with_four_requests_and_one_worker_call(fake_server, capsys):
    healthy_site(fake_server)
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 0
    assert len(fake_server.calls) == len(monitors.EDGE_PATHS) == 4
    worker_calls = [c for c in fake_server.calls if c["path"].startswith("/api")]
    assert [c["path"] for c in worker_calls] == ["/api/_edge/health"]
    assert "edge: 0 failed" in capsys.readouterr().out


def test_edge_check_reads_the_base_url_from_the_environment(fake_server, monkeypatch):
    healthy_site(fake_server)
    monkeypatch.setenv("PUBLIC_BASE_URL", fake_server.base)
    assert monitors.main(["edge"]) == 0


def test_edge_check_fails_without_the_site_security_headers(fake_server, capsys):
    healthy_site(fake_server)
    status, headers, body = html_reply()
    del headers["Strict-Transport-Security"]
    fake_server.routes[("GET", "/")] = (status, headers, body)
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 1
    assert "strict-transport-security missing" in capsys.readouterr().out


def test_edge_check_fails_when_the_snapshot_pointer_is_the_spa_fallback(fake_server, capsys):
    healthy_site(fake_server)
    del fake_server.routes[("GET", "/snap/current.json")]  # the SPA fallback answers (HTML)
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 1
    assert "SPA fallback" in capsys.readouterr().out


def test_edge_check_fails_when_worker_and_static_snapshot_disagree(fake_server, capsys):
    healthy_site(fake_server, snapshot_id="other-0000000000000000")
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 1
    assert "half-finished deploy" in capsys.readouterr().out


OLD_SNAP = "0123456789ab-fedcba9876543210"


def previous_release(server) -> None:
    """A self-consistent edge still on the previous release: the Worker's vars and the
    static pointer agree with each other, not with the release just deployed."""
    server.routes.update({
        ("GET", "/"): html_reply(),
        ("GET", "/api/_edge/health"): json_reply(200, {
            "status": "ok", "edge_version": "0.1.0", "data_version": "dv-old",
            "snapshot_id": OLD_SNAP}),
        ("GET", "/snap/current.json"): json_reply(200, {
            "snapshot_id": OLD_SNAP, "data_version": "dv-old"}, api=False),
        ("GET", f"/snap/{OLD_SNAP}/manifest.json"): json_reply(
            200, {"snapshot_id": OLD_SNAP}, api=False),
    })


def test_edge_check_with_a_release_expectation_fails_on_the_previous_release(fake_server,
                                                                              capsys):
    """deploy-step6-edge-monitor-cannot-confirm-release (review round 4): Deploy step 6
    and the edge rollbacks used `monitors.py edge` to confirm which release the edge
    serves, but it only proved the edge self-consistent, which the previous release is
    too. With the release's snapshot id and data version it fails there, within the same
    four requests."""
    previous_release(fake_server)
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 0
    capsys.readouterr()
    fake_server.calls.clear()
    assert monitors.main(["edge", "--base-url", fake_server.base,
                          "--expect-snapshot-id", SNAP, "--expect-data-version", "dv"]) == 1
    assert len(fake_server.calls) <= len(monitors.EDGE_PATHS)
    out = capsys.readouterr().out
    assert f"[FAIL] edge_health: the Worker serves another release: snapshot {OLD_SNAP}, " \
        f"expected {SNAP}; data dv-old, expected dv" in out
    assert f"[FAIL] snapshot: the static pointer names another release: snapshot " \
        f"{OLD_SNAP}, expected {SNAP}" in out
    assert "edge: 2 failed" in out
    # each expectation on its own is enough
    for flag, value in (("--expect-snapshot-id", SNAP), ("--expect-data-version", "dv")):
        assert monitors.main(["edge", "--base-url", fake_server.base, flag, value]) == 1


def test_edge_check_with_a_release_expectation_passes_on_that_release(fake_server, capsys):
    healthy_site(fake_server)
    fake_server.routes[("GET", "/snap/current.json")] = json_reply(
        200, {"snapshot_id": SNAP, "data_version": "dv"}, api=False)
    assert monitors.main(["edge", "--base-url", fake_server.base,
                          "--expect-snapshot-id", SNAP, "--expect-data-version", "dv"]) == 0
    assert len(fake_server.calls) == len(monitors.EDGE_PATHS)
    assert "edge: 0 failed" in capsys.readouterr().out


def test_edge_check_fails_when_the_worker_is_down(fake_server, capsys):
    healthy_site(fake_server)
    fake_server.routes[("GET", "/api/_edge/health")] = (429, {"Content-Type": "text/html"},
                                                         b"error 1027")
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 1
    assert "edge_health" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# deep                                                                         #
# --------------------------------------------------------------------------- #

def test_deep_check_waits_through_a_cold_start(fake_server, _no_real_sleep):
    replies = iter([envelope(503, "service_waking", {"Retry-After": "5"}),
                    envelope(503, "service_waking", {"Retry-After": "30"}),
                    json_reply(200, {"status": "ok", "build": "abc123"})])
    fake_server.routes[("GET", "/api/health")] = lambda _req: next(replies)
    assert monitors.main(["deep", "--base-url", fake_server.base]) == 0
    assert fake_server.count("GET", "/api/health") == 3
    # Retry-After is honoured, with a 15 s floor so a cold start costs few requests.
    assert _no_real_sleep == [15.0, 30.0]
    assert [c["path"] for c in fake_server.calls] == ["/api/health"] * 3


def test_deep_check_gives_up_within_its_request_budget(fake_server, capsys):
    fake_server.routes[("GET", "/api/health")] = envelope(503, "service_waking",
                                                          {"Retry-After": "5"})
    assert monitors.main(["deep", "--base-url", fake_server.base, "--max-requests", "3"]) == 1
    assert fake_server.count() == 3
    assert "not ready after 3 attempt(s)" in capsys.readouterr().out


def paused_envelope() -> tuple:
    return json_reply(503, {"error": {"code": "api_paused", "message": "Paused.", "details": {
        "until": "2026-11-01T00:00:00.000Z", "reason": "render_quota",
        "retry_after_seconds": 864000}}}, {"Retry-After": "864000"})


def test_deep_check_stops_at_the_owners_pause_switch_and_warns(fake_server, capsys):
    """Review round 2 (api-paused-owner-switch): while the owner's switch is on,
    /api/health answers 503 api_paused at the edge. One request is enough, and a
    deliberate pause is reported (WARN), not retried until the job fails."""
    fake_server.routes[("GET", "/api/health")] = paused_envelope()
    assert monitors.main(["deep", "--base-url", fake_server.base]) == 0
    assert fake_server.count() == 1
    out = capsys.readouterr().out
    assert "[WARN] api_paused" in out and "2026-11-01T00:00:00.000Z" in out
    assert "render_quota" in out and "Render not checked" in out


def test_edge_check_reports_the_pause_switch(fake_server, capsys):
    healthy_site(fake_server)
    fake_server.routes[("GET", "/api/_edge/health")] = json_reply(200, {
        "status": "ok", "edge_version": "0.1.0", "data_version": "dv", "snapshot_id": SNAP,
        "api_paused": {"until": None, "reason": "render_suspended"}})
    assert monitors.main(["edge", "--base-url", fake_server.base]) == 0
    out = capsys.readouterr().out
    assert "[WARN] api_paused" in out and "until unknown" in out and "render_suspended" in out


def test_deep_check_refuses_a_large_budget(fake_server):
    assert monitors.main(["deep", "--base-url", fake_server.base, "--max-requests", "50"]) == 2
    assert fake_server.calls == []


def test_monitors_never_request_a_database_path():
    """ADR-011: monitors never touch the DB (no /api/v1/*, /ready, /metrics)."""
    paths = monitors.EDGE_PATHS + monitors.DEEP_PATHS + monitors.QUOTA_PATHS
    for path in paths:
        assert not path.startswith("/api/v1"), path
        assert path not in ("/ready", "/metrics", "/api/ready", "/api/metrics")
    source = (OPS_DIR / "monitors.py").read_text()
    assert '"/api/v1' not in source and "'/api/v1" not in source


# --------------------------------------------------------------------------- #
# quota                                                                        #
# --------------------------------------------------------------------------- #

def governor(**overrides):
    report = {
        "generated_at": "2026-10-20T12:00:00.000Z",  # half the UTC day elapsed
        "day": "2026-10-20",
        "mode": "ground_truth",
        "analyses": {"admitted": 100, "limit": 600, "heavy": 10, "heavy_limit": 300},
        "prefixes": {"active": 3, "max_admitted": 20, "limit": 60},
        "compute_today": {"settled_cu_hours": 0.5, "provisional_cu_hours": 0.1,
                          "ceiling_cu_hours": 10.0},
        "compute_rolling_31d_cu_hours": 20.0,
        "month": {"elapsed_share": 0.5, "cu_hours": 20.0, "bridge_cu_hours": 1.0,
                  "egress_bytes": 1e9, "bridge_egress_bytes": 1e6},
    }
    report.update(overrides)
    return report


def by_name(lines):
    return {line.name: line for line in lines}


def test_budgets_are_pro_rated_by_the_elapsed_day_and_period():
    lines = by_name(monitors.evaluate_budget(governor()))
    assert lines["global analyses/day"].budget == pytest.approx(300.0)  # 600 x 0.5
    assert lines["global analyses/day"].ratio == pytest.approx(100 / 300)
    assert lines["Neon CU-h today"].used == pytest.approx(0.6)
    assert lines["Neon CU-h today"].budget == pytest.approx(5.0)
    assert lines["Neon CU-h, month to date"].budget == pytest.approx(50.0)  # 100 x 0.5
    assert lines["Neon CU-h, month to date"].used == pytest.approx(21.0)
    assert lines["Neon egress, month to date (bytes)"].budget == pytest.approx(2.5e9)
    assert lines["Neon CU-h, last 31 days"].budget == 100.0
    assert not any(line.alert for line in lines.values())


def test_a_budget_above_70_percent_of_its_pro_rated_value_alerts():
    report = governor(analyses={"admitted": 211, "limit": 600, "heavy": 0,
                                "heavy_limit": 300})  # 211 / 300 = 70.3%
    lines = by_name(monitors.evaluate_budget(report))
    assert lines["global analyses/day"].alert
    report["month"]["cu_hours"] = 36.0  # (36 + 1) / 50 = 74%
    assert by_name(monitors.evaluate_budget(report))["Neon CU-h, month to date"].alert


def test_the_per_network_cap_is_informational_only():
    report = governor(prefixes={"max_admitted": 60, "limit": 60})
    line = by_name(monitors.evaluate_budget(report))["busiest client network today"]
    assert line.ratio == 1.0 and not line.alert


def test_early_in_the_day_the_pro_rating_has_a_floor():
    report = governor(generated_at="2026-10-20T00:01:00Z",
                      analyses={"admitted": 5, "limit": 600, "heavy": 0, "heavy_limit": 300})
    line = by_name(monitors.evaluate_budget(report))["global analyses/day"]
    assert line.budget == pytest.approx(600 / 24)  # one hour's worth, not one minute's
    assert not line.alert


def test_conservative_mode_has_no_month_lines_and_the_rolling_total_is_informational():
    """quota-report-alerts-on-normal-traffic: the rolling total is the governor's own
    charges (2 CU for every second, the whole idle tail), which its daily ceiling holds
    down; compared with Neon's 100 CU-h it crossed 70% on typical traffic."""
    report = governor(mode="conservative", month={"elapsed_share": None, "cu_hours": None})
    lines = by_name(monitors.evaluate_budget(report))
    assert "Neon CU-h, month to date" not in lines
    assert lines["Neon CU-h, last 31 days"].used == 20.0
    report["compute_rolling_31d_cu_hours"] = 86.0
    line = by_name(monitors.evaluate_budget(report))["Neon CU-h, last 31 days"]
    assert line.ratio == pytest.approx(0.86) and not line.alert


# deployment.md's typical traffic (16 sessions a day, ~1.9 analyses 60 s apart, 1 s of DB
# time each) played through the Worker and the governor for a month (review round 2,
# quota-report-alerts-on-normal-traffic): the 22:05 UTC budget reports, abridged.
def typical_day(mode: str, day: int) -> dict:
    if mode == "conservative":   # no Neon key; day 31 (the rolling total at its steady state)
        assert day == 31
        return {
            "generated_at": "2036-10-31T22:05:00.000Z", "day": "2036-10-31",
            "mode": "conservative", "neon_configured": False,
            "analyses": {"admitted": 27, "limit": 600, "heavy": 0, "heavy_limit": 300},
            "prefixes": {"active": 14, "max_admitted": 2, "limit": 60},
            "compute_today": {"settled_cu_hours": 2.7667, "provisional_cu_hours": 0,
                              "ceiling_cu_hours": 2.9032},
            "compute_rolling_31d_cu_hours": 85.7667,
            "month": {"polled_at": None, "last_poll_error": None, "elapsed_share": None,
                      "cu_hours": None, "bridge_cu_hours": None, "allowance_cu_hours": None,
                      "egress_bytes": None, "bridge_egress_bytes": None},
        }
    month = {  # ground truth: Neon's own figure lags the governor's bridge by about 2 days
        1: (0.0297, 0.0, 3.1667, 12.2261, 0, 2031616, 3.1667),
        3: (0.0942, 1.1, 6.3333, 17.0649, 6_000_000, 4063232, 9.5),
        31: (0.9974, 16.5, 6.3333, 84.8068, 90_000_000, 4063232, 98.1667),
    }[day]
    share, cu, bridge, allowance, egress, bridge_egress, rolling = month
    return {
        "generated_at": f"2036-10-{day:02d}T22:05:00.000Z", "day": f"2036-10-{day:02d}",
        "mode": "ground_truth", "neon_configured": True,
        "analyses": {"admitted": 31, "limit": 600, "heavy": 0, "heavy_limit": 300},
        "prefixes": {"active": 16, "max_admitted": 2, "limit": 60},
        "compute_today": {"settled_cu_hours": 3.1667, "provisional_cu_hours": 0,
                          "ceiling_cu_hours": 10},
        "compute_rolling_31d_cu_hours": rolling,
        "month": {"polled_at": f"2036-10-{day:02d}T08:00:00.000Z", "last_poll_error": None,
                  "elapsed_share": share, "cu_hours": cu, "bridge_cu_hours": bridge,
                  "allowance_cu_hours": allowance, "egress_bytes": egress,
                  "bridge_egress_bytes": bridge_egress},
    }


@pytest.mark.parametrize("mode, day", [("conservative", 31), ("ground_truth", 1),
                                       ("ground_truth", 3), ("ground_truth", 31)])
def test_a_typical_day_never_alerts(mode, day, fake_server, monkeypatch, capsys):
    """quota-report-alerts-on-normal-traffic: the report failed on 31 of 31 typical days
    in conservative mode (the governor's charges today against its own ceiling) and on 12
    of 31 with ground truth (days 1-3 against the linear share, days 23-31 on the rolling
    charges), so the only alert channel for quota exhaustion was red in normal operation."""
    report = typical_day(mode, day)
    alerts = [line.name for line in monitors.evaluate_budget(report) if line.alert]
    assert alerts == []
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": report})
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 0
    assert "[FAIL]" not in capsys.readouterr().out


def test_the_governors_own_charges_never_alert():
    """Today's charges against the daily ceiling: reaching it refuses analyses
    (daily_capacity_reached), which is the governor working, like the per-network cap."""
    report = typical_day("conservative", 31)
    report["compute_today"]["settled_cu_hours"] = 2.9032         # the ceiling, reached
    lines = by_name(monitors.evaluate_budget(report))
    today = lines["Neon CU-h today"]
    assert today.ratio > 1 and not today.alert
    assert "daily_capacity_reached" in today.note


def test_neon_month_to_date_is_judged_against_the_governors_pacing_allowance():
    """Neon's own figure (plus the bridge) is the usage that can exhaust the quota, also
    through what the governor does not count; the governor pauses analyses above its
    pacing allowance, so the report warns at 70% of that allowance."""
    report = typical_day("ground_truth", 31)
    line = by_name(monitors.evaluate_budget(report))["Neon CU-h, month to date"]
    assert line.used == pytest.approx(16.5 + 6.3333)
    assert line.budget == pytest.approx(84.8068)
    assert not line.alert
    report["month"]["cu_hours"] = 55.0        # e.g. uncounted wakes: (55 + 6.3) / 84.8 = 72%
    assert by_name(monitors.evaluate_budget(report))["Neon CU-h, month to date"].alert
    # The rolling charges stay informational even far above Neon's figure.
    assert not by_name(monitors.evaluate_budget(report))["Neon CU-h, last 31 days"].alert


def test_a_neon_key_whose_poll_keeps_failing_fails_the_report(fake_server, monkeypatch,
                                                             capsys):
    """With the key configured, conservative mode with a poll error means the ground truth
    broke: the one Neon signal the report has is gone, so it fails (without the key,
    conservative mode is the chosen setup and only warns)."""
    report = typical_day("conservative", 31)
    report.update(neon_configured=True)
    report["month"]["last_poll_error"] = "http_401"
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": report})
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] neon_ground_truth" in out and "http_401" in out
    report.update(neon_configured=False)
    report["month"]["last_poll_error"] = None
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": report})
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 0
    assert "[WARN] neon_ground_truth" in capsys.readouterr().out


def test_quota_report_sends_the_bearer_and_never_prints_it(fake_server, monkeypatch, capsys,
                                                           tmp_path):
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": governor()})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 0
    (call,) = fake_server.calls
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert TOKEN not in summary.read_text()
    assert "| Neon CU-h, month to date |" in summary.read_text()


def test_quota_report_fails_on_an_alert(fake_server, monkeypatch):
    report = governor()
    report["month"]["egress_bytes"] = 2.4e9  # 2.4 GB of a pro-rated 2.5 GB
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": report})
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 1


def test_quota_report_fails_when_the_token_is_rejected(fake_server, monkeypatch, capsys):
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)  # the route is absent: 404 envelope
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 1
    assert "rejected EDGE_OPS_TOKEN" in capsys.readouterr().out


def test_quota_report_needs_the_token(fake_server):
    assert monitors.main(["quota", "--base-url", fake_server.base]) == 2
    assert fake_server.calls == []


# The Worker's account-wide 100,000 requests/day: Cloudflare's GraphQL
# Analytics API, read with a read-only Account Analytics token (optional).
CF_TOKEN = "cf-analytics-token-0123456789abcdef"
CF_ACCOUNT = "0123456789abcdef0123456789abcdef"


def cloudflare(server, *requests_per_row, errors=None):
    rows = [{"sum": {"requests": n}} for n in requests_per_row]
    server.routes[("POST", "/client/v4/graphql")] = json_reply(200, {
        "data": None if errors else {"viewer": {"accounts": [
            {"workersInvocationsAdaptive": rows}]}},
        "errors": errors}, api=False)
    server.routes[("GET", "/api/_ops/budget")] = json_reply(
        200, {"status": "ok", "governor": governor()})   # noon: half the day elapsed


def run_quota(server, monkeypatch, *, analytics=True):
    monkeypatch.setenv("EDGE_OPS_TOKEN", TOKEN)
    if analytics:
        monkeypatch.setenv("CLOUDFLARE_ANALYTICS_TOKEN", CF_TOKEN)
        monkeypatch.setenv("CLOUDFLARE_ANALYTICS_ACCOUNT", CF_ACCOUNT)
    return monitors.main(["quota", "--base-url", server.base,
                          "--cloudflare-api-url", server.base])


def test_worker_requests_are_judged_against_the_pro_rated_daily_allowance(
        fake_server, monkeypatch, capsys):
    cloudflare(fake_server, 15_000, 5_000)       # 20,000 of a pro-rated 50,000
    assert run_quota(fake_server, monkeypatch) == 0
    out = capsys.readouterr()
    assert "[PASS] Worker requests today (account)" in out.out
    assert CF_TOKEN not in out.out + out.err
    (query,) = [c for c in fake_server.calls if c["path"] == "/client/v4/graphql"]
    assert query["headers"]["authorization"] == f"Bearer {CF_TOKEN}"
    sent = json.loads(query["body"])
    assert "workersInvocationsAdaptive" in sent["query"] and "requests" in sent["query"]
    assert sent["variables"] == {"accountTag": CF_ACCOUNT,
                                 "datetimeStart": "2026-10-20T00:00:00Z",
                                 "datetimeEnd": "2026-10-20T12:00:00Z"}


def test_worker_requests_above_70_percent_alert_before_the_allowance_runs_out(
        fake_server, monkeypatch, capsys):
    cloudflare(fake_server, 36_000)              # 72% of the pro-rated 50,000
    assert run_quota(fake_server, monkeypatch) == 1
    assert "[FAIL] Worker requests today (account)" in capsys.readouterr().out


def test_a_cloudflare_error_fails_the_report(fake_server, monkeypatch, capsys):
    cloudflare(fake_server, errors=[{"message": "not authorized for that account"}])
    assert run_quota(fake_server, monkeypatch) == 1
    assert "not authorized" in capsys.readouterr().out


def test_without_an_analytics_token_worker_requests_are_reported_as_unmonitored(
        fake_server, monkeypatch, capsys):
    cloudflare(fake_server, 99_999)
    assert run_quota(fake_server, monkeypatch, analytics=False) == 0
    out = capsys.readouterr().out
    assert "worker_requests" in out and "not monitored" in out
    assert all(c["path"] != "/client/v4/graphql" for c in fake_server.calls)


# --------------------------------------------------------------------------- #
# httpcheck                                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://example.com",                 # plain HTTP off loopback
    "https://user:pw@example.com",        # credentials
    "https://example.com/?key=secret",    # query string
    "ftp://example.com",
    "not a url",
])
def test_base_urls_are_restricted(url):
    with pytest.raises(httpcheck.OpsError) as info:
        httpcheck.validate_base_url(url)
    assert "secret" not in str(info.value) and "pw" not in str(info.value)


def test_loopback_http_and_https_are_accepted():
    assert httpcheck.validate_base_url("http://127.0.0.1:8541/") == "http://127.0.0.1:8541"
    assert httpcheck.validate_base_url("https://atus.x.workers.dev") == \
        "https://atus.x.workers.dev"


def test_the_budget_is_enforced_before_sending(fake_server):
    fake_server.routes[("GET", "/x")] = json_reply(200, {})
    client = httpcheck.Client(fake_server.base, label="site", max_requests=2, log=lambda _m: None)
    client.get("/x")
    client.get("/x")
    with pytest.raises(httpcheck.BudgetExceeded):
        client.get("/x")
    assert fake_server.count() == 2


def test_redirects_are_returned_not_followed(fake_server):
    fake_server.routes[("GET", "/r")] = (302, {"Location": "https://elsewhere.invalid/"}, b"")
    client = httpcheck.Client(fake_server.base, label="site", max_requests=1, log=lambda _m: None)
    assert client.get("/r").status == 302


def test_gzip_bodies_are_decoded(fake_server):
    fake_server.gzip_json = True
    fake_server.routes[("GET", "/j")] = json_reply(200, {"ok": True})
    client = httpcheck.Client(fake_server.base, label="site", max_requests=1, log=lambda _m: None)
    assert client.get("/j").json() == {"ok": True}


def test_log_lines_name_the_label_and_path_never_the_base_url(fake_server):
    fake_server.routes[("GET", "/health")] = json_reply(200, {"status": "ok"})
    lines: list[str] = []
    client = httpcheck.Client(fake_server.base, label="origin", max_requests=1, log=lines.append)
    client.get("/health")
    assert lines and lines[0].startswith("origin GET /health -> HTTP 200")
    assert "127.0.0.1" not in lines[0]


@pytest.mark.parametrize("value, expected", [("5", 5.0), (None, None), ("", None),
                                             ("soon", None)])
def test_retry_after_parsing(value, expected):
    assert httpcheck.parse_retry_after(value) == expected


def test_retry_after_http_date():
    assert httpcheck.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT",
                                       now=1792567660.0) == pytest.approx(20.0)
