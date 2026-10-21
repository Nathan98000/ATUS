"""scripts/ops/api_pause.py: deploy.yml's pause-api / resume-api operations
(review round 2, api-paused-owner-switch), against a local fake site."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from .conftest import OPS_DIR, json_reply, load_script

api_pause = load_script(OPS_DIR / "api_pause.py", "atus_ops_api_pause")

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def health(api_paused) -> tuple:
    return json_reply(200, {"status": "ok", "edge_version": "0.1.0", "api_paused": api_paused})


@pytest.mark.parametrize("until, reported", [
    ("2026-12-01", "2026-12-01T00:00:00.000Z"),
    ("2026-10-21T13:30Z", "2026-10-21T13:30:00.000Z"),
    ("2026-10-21T13:30:05Z", "2026-10-21T13:30:05.000Z"),
    ("unknown", None),
])
def test_pause_writes_both_secrets_and_the_state_the_edge_reports(until, reported):
    secrets, expected = api_pause.plan("pause-api", until, "render_quota", now=NOW)
    assert secrets == {"API_PAUSED_UNTIL": until, "API_PAUSED_REASON": "render_quota"}
    assert expected == {"until": reported, "reason": "render_quota"}


def test_resume_deletes_both_secrets_whatever_else_was_passed():
    secrets, expected = api_pause.plan("resume-api", "2026-12-01", "bogus", now=NOW)
    assert secrets == {"API_PAUSED_UNTIL": None, "API_PAUSED_REASON": None}
    assert expected is None


@pytest.mark.parametrize("operation, until, reason, message", [
    ("deploy", "2026-12-01", "render_quota", "--operation"),
    ("pause-api", "2026-12-01", "", "--reason"),
    ("pause-api", "2026-12-01", "quota", "--reason"),
    ("pause-api", "", "render_quota", "--until"),
    ("pause-api", "next month", "render_quota", "--until"),
    ("pause-api", "2026-12-01T00:00:00", "render_quota", "--until"),  # no Z: not UTC
    ("pause-api", "2026-02-30", "render_quota", "does not exist"),
    ("pause-api", "2026-10-21", "render_quota", "already past"),
    ("pause-api", "2026-10-21T11:59Z", "render_quota", "already past"),
])
def test_refuses_inputs_the_worker_would_not_read_as_intended(operation, until, reason, message):
    with pytest.raises(api_pause.OpsError, match=message):
        api_pause.plan(operation, until, reason, now=NOW)


def test_secrets_command_writes_the_wrangler_bulk_file(tmp_path, capsys):
    out = tmp_path / "pause.json"
    assert api_pause.main(["secrets", "--operation", "pause-api", "--until", "2099-01-01",
                           "--reason", "maintenance", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"API_PAUSED_UNTIL": "2099-01-01",
                                           "API_PAUSED_REASON": "maintenance"}
    assert api_pause.main(["secrets", "--operation", "resume-api", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"API_PAUSED_UNTIL": None, "API_PAUSED_REASON": None}
    assert api_pause.main(["secrets", "--operation", "pause-api", "--until", "2000-01-01",
                           "--reason", "maintenance", "--out", str(out)]) == 1
    assert "already past" in capsys.readouterr().err


def test_verify_waits_for_the_edge_to_report_the_new_state(fake_server):
    replies = iter([health(None), health({"until": None, "reason": "render_suspended"})])
    fake_server.routes[("GET", "/api/_edge/health")] = lambda _request: next(replies)
    assert api_pause.main(["verify", "--base-url", fake_server.base, "--operation",
                           "pause-api", "--until", "unknown",
                           "--reason", "render_suspended"]) == 0
    assert [call["path"] for call in fake_server.calls] == ["/api/_edge/health"] * 2


def test_verify_fails_within_its_budget_when_the_edge_never_shows_it(fake_server, capsys):
    fake_server.routes[("GET", "/api/_edge/health")] = health(None)
    assert api_pause.main(["verify", "--base-url", fake_server.base, "--operation",
                           "pause-api", "--until", "2099-01-01",
                           "--reason", "render_quota"]) == 1
    assert len(fake_server.calls) <= api_pause.VERIFY_REQUESTS
    assert "not ready" in capsys.readouterr().err
    # Only the Worker's own health route: nothing that reaches Render or Neon.
    assert {call["path"] for call in fake_server.calls} == {"/api/_edge/health"}


def test_verify_confirms_a_resume(fake_server):
    fake_server.routes[("GET", "/api/_edge/health")] = health(None)
    assert api_pause.main(["verify", "--base-url", fake_server.base,
                           "--operation", "resume-api"]) == 0
