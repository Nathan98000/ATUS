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


def test_conservative_mode_has_no_month_lines_but_keeps_the_rolling_total():
    report = governor(mode="conservative", month={"elapsed_share": None, "cu_hours": None})
    lines = by_name(monitors.evaluate_budget(report))
    assert "Neon CU-h, month to date" not in lines
    assert lines["Neon CU-h, last 31 days"].used == 20.0
    report["compute_rolling_31d_cu_hours"] = 71.0
    assert by_name(monitors.evaluate_budget(report))["Neon CU-h, last 31 days"].alert


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
