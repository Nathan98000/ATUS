"""scripts/ops/deploy_smoke.py against a fake deployed site (static snapshot + Worker)."""

from __future__ import annotations

import copy
import json
import re

import pytest

from .conftest import OPS_DIR, REPO_ROOT, envelope, html_reply, json_reply, load_script

smoke = load_script(OPS_DIR / "deploy_smoke.py", "atus_ops_deploy_smoke")

SNAP = "0123456789ab-0123456789abcdef"
DV = "0325:serving-20261020T224252Z-a7420a5c:de834e672474"
SHA = "0123456789abcdef0123456789abcdef01234567"

ESTIMATE = {
    "measure": "average_minutes_per_day",
    "estimate": {"value": 541.8, "unit": "minutes", "standard_error": 1.2345678901234,
                 "confidence_level": 0.95, "ci_lower": 539.38, "ci_upper": 544.22},
    "years": [2025], "n_respondents": 7000, "n_participants": 6990,
    "weighted_population_per_day": 2.7e8, "warnings": [],
    "spec": {"activity": {"preset": "sleep"}, "years": [2025]},
}
COMPARE = {
    "measure": "average_minutes_per_day", "label_a": "Men", "label_b": "Women",
    "group_a": {"estimate": {"value": 160.1, "standard_error": 3.1, "ci_lower": 154.0,
                             "ci_upper": 166.2}},
    "group_b": {"estimate": {"value": 150.2, "standard_error": 2.9, "ci_lower": 144.5,
                             "ci_upper": 155.9}},
    "difference": {"value": 9.9, "standard_error": 4.2, "ci_lower": 1.7, "ci_upper": 18.1},
    "warnings": [],
}


def starter_key(index: int, year: int = 2025) -> tuple[str, str]:
    _name, operation, spec = smoke.STARTERS[index]
    path = f"/api/v1/analysis/{operation}"
    return path, smoke.request_key(path, smoke.stable_stringify({**spec, "years": [year]}))


def deployed_site(server, *, live_estimate=None, snapshot_estimate=None) -> None:
    est_path, est_key = starter_key(0)
    cmp_path, cmp_key = starter_key(1)
    manifest = {
        "snapshot_id": SNAP, "data_version": DV,
        "get": {"/api/v1/meta": "get/meta.json"},
        "post": {est_key: {"path": est_path, "file": f"post/{est_key}.json"},
                 cmp_key: {"path": cmp_path, "file": f"post/{cmp_key}.json"}},
    }
    meta = {"api_version": "v1", "analytics_version": "0.2", "data": {"years": [2024, 2025]}}
    version = {"X-Data-Version": DV}
    server.routes.update({
        ("GET", "/"): html_reply(),
        ("GET", "/analysis/estimate"): html_reply(),
        ("GET", "/snap/current.json"): json_reply(200, {"snapshot_id": SNAP, "data_version": DV},
                                                  api=False),
        ("GET", f"/snap/{SNAP}/manifest.json"): json_reply(200, manifest, api=False),
        ("GET", f"/snap/{SNAP}/get/meta.json"): json_reply(200, meta, api=False),
        ("GET", f"/snap/{SNAP}/post/{est_key}.json"): json_reply(
            200, snapshot_estimate or ESTIMATE, api=False),
        ("GET", f"/snap/{SNAP}/post/{cmp_key}.json"): json_reply(200, COMPARE, api=False),
        ("GET", "/api/_edge/health"): json_reply(200, {
            "status": "ok", "edge_version": "0.1.0", "data_version": DV, "snapshot_id": SNAP,
            "cache_scope": "abc"}),
        ("GET", "/api/health"): json_reply(200, {"status": "ok", "build": SHA}),
        ("GET", "/api/v1/meta"): json_reply(200, meta, version),
        ("POST", est_path): json_reply(200, live_estimate or ESTIMATE,
                                       {**version, "X-Cache": "miss"}),
        ("POST", cmp_path): json_reply(200, COMPARE, {**version, "X-Cache": "miss"}),
    }
    )


def args(server, *extra):
    return ["--base-url", server.base, "--expect-build", SHA, "--expect-data-version", DV,
            "--expect-snapshot-id", SNAP, "--require-snapshot", *extra]


def test_a_healthy_deploy_passes_within_the_budget(fake_server, capsys, tmp_path):
    deployed_site(fake_server)
    report = tmp_path / "smoke.json"
    assert smoke.main(args(fake_server, "--recompute", "2", "--report", str(report))) == 0
    out = capsys.readouterr().out
    assert "smoke: 0 failed" in out
    assert len(fake_server.calls) <= 30
    results = json.loads(report.read_text())
    assert results["requests"] == len(fake_server.calls)
    names = {r["name"]: r["status"] for r in results["results"]}
    assert names["starter_1_sleep"] == names["starter_2_tv_by_sex"] == "PASS"


def test_the_live_recompute_uses_other_bytes_than_the_snapshot_key(fake_server):
    deployed_site(fake_server)
    assert smoke.main(args(fake_server)) == 0
    (post,) = [c for c in fake_server.calls if c["method"] == "POST"]
    sent = post["body"].decode()
    path, key = starter_key(0)
    assert smoke.request_key(path, sent) != key  # the edge cache cannot answer it
    assert json.loads(sent) == {**smoke.STARTERS[0][2], "years": [2025]}


def test_a_live_result_outside_the_criterion_fails(fake_server, capsys):
    live = copy.deepcopy(ESTIMATE)
    live["estimate"]["value"] = 541.9  # a point estimate must be exactly equal
    deployed_site(fake_server, live_estimate=live)
    assert smoke.main(args(fake_server)) == 1
    assert "differ under §9" in capsys.readouterr().out


def test_plan_order_noise_in_a_standard_error_passes(fake_server):
    live = copy.deepcopy(ESTIMATE)
    live["estimate"]["standard_error"] *= 1 + 1e-14  # far inside 1e-12
    deployed_site(fake_server, live_estimate=live)
    assert smoke.main(args(fake_server)) == 0


def test_a_missing_snapshot_fails_when_required(fake_server, capsys):
    deployed_site(fake_server)
    del fake_server.routes[("GET", "/snap/current.json")]  # SPA fallback answers instead
    assert smoke.main(args(fake_server)) == 1
    assert "no static snapshot is served" in capsys.readouterr().out


def test_without_a_snapshot_the_starter_is_computed_live(fake_server, capsys):
    deployed_site(fake_server)
    del fake_server.routes[("GET", "/snap/current.json")]
    assert smoke.main(["--base-url", fake_server.base, "--expect-build", SHA]) == 0
    assert "computed live" in capsys.readouterr().out


def test_a_stale_worker_or_api_is_caught(fake_server, capsys):
    deployed_site(fake_server)
    fake_server.routes[("GET", "/api/health")] = json_reply(200, {"status": "ok",
                                                                  "build": "old"})
    fake_server.routes[("GET", "/api/_edge/health")] = json_reply(200, {
        "status": "ok", "data_version": "old", "snapshot_id": SNAP})
    assert smoke.main(args(fake_server)) == 1
    out = capsys.readouterr().out
    assert "expected " + SHA in out and "DATA_VERSION old" in out


def test_a_deploy_while_the_pause_switch_is_on_fails_at_once(fake_server, capsys,
                                                             monkeypatch, _no_real_sleep):
    """Review round 2 (api-paused-owner-switch): with the owner's pause switch on,
    everything that would reach Render answers 503 api_paused at the edge. The
    smoke says so and how to clear it, instead of waiting out the cold-start
    budget on /api/health and failing the recomputes. It runs with deploy.yml's
    --origin-lock, whose one request goes to Render directly: skipped too
    (review round 2 verification)."""
    deployed_site(fake_server)
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base + "/origin-prefix")
    fake_server.routes[("GET", "/origin-prefix/api/v1/meta")] = envelope(403,
                                                                         "forbidden_origin")
    paused = {"until": "2026-11-01T00:00:00.000Z", "reason": "render_quota"}
    fake_server.routes[("GET", "/api/_edge/health")] = json_reply(200, {
        "status": "ok", "edge_version": "0.1.0", "data_version": DV, "snapshot_id": SNAP,
        "cache_scope": "abc", "api_paused": paused})
    for key in [("GET", "/api/health"), *[k for k in fake_server.routes if k[0] == "POST"]]:
        fake_server.routes[key] = envelope(503, "api_paused", {"Retry-After": "864000"})
    assert smoke.main(args(fake_server, "--recompute", "2", "--origin-lock")) == 1
    out = capsys.readouterr().out
    assert "[FAIL] api_paused" in out and "resume-api" in out and "render_quota" in out
    # Nothing that would reach Render is sent once the edge reports the switch.
    sent = {(c["method"], c["path"]) for c in fake_server.calls}
    assert ("GET", "/api/health") not in sent
    assert not any(method == "POST" for method, _path in sent)
    assert not any(path.startswith("/origin-prefix") for _method, path in sent)
    assert "[FAIL] api_health: not checked" in out
    assert "[FAIL] origin_lock: not checked" in out
    assert _no_real_sleep == []


def _old_then_new(old, new, *, old_replies: int):
    """A route answering ``old`` for the first ``old_replies`` requests: a colo
    that still serves the previous release right after `wrangler deploy`."""
    seen = []

    def route(_request):
        seen.append(1)
        return old if len(seen) <= old_replies else new

    return route


def test_a_release_still_propagating_is_waited_for_not_failed(fake_server, capsys,
                                                               _no_real_sleep):
    deployed_site(fake_server)
    stale_vars = json_reply(200, {"status": "ok", "edge_version": "0.1.0",
                                  "data_version": "0325:serving-old", "snapshot_id": "old-snap",
                                  "cache_scope": "abc"})
    stale_pointer = json_reply(200, {"snapshot_id": "old-snap",
                                     "data_version": "0325:serving-old"}, api=False)
    for path, old, replies in (("/api/_edge/health", stale_vars, 2),
                               ("/snap/current.json", stale_pointer, 1)):
        fake_server.routes[("GET", path)] = _old_then_new(
            old, fake_server.routes[("GET", path)], old_replies=replies)
    assert smoke.main(args(fake_server, "--recompute", "2")) == 0
    out = capsys.readouterr().out
    assert "smoke: 0 failed" in out and "propagation" in out
    assert len(fake_server.calls) <= 30
    assert _no_real_sleep and max(_no_real_sleep) <= 15.0


def test_a_release_that_never_arrives_still_fails_within_the_budget(fake_server, capsys):
    deployed_site(fake_server)
    fake_server.routes[("GET", "/snap/current.json")] = json_reply(
        200, {"snapshot_id": "old-snap", "data_version": DV}, api=False)
    assert smoke.main(args(fake_server, "--recompute", "2")) == 1
    out = capsys.readouterr().out
    assert "serves old-snap, expected " + SNAP in out
    assert len(fake_server.calls) <= 30


def test_the_starter_mirror_matches_the_spa(fake_server):
    """STARTERS mirrors frontend/src/domain/prewarm.ts STARTER_EXAMPLES[0] and [2]."""
    source = (REPO_ROOT / "frontend" / "src" / "domain" / "prewarm.ts").read_text()
    block = source[source.index("STARTER_EXAMPLES"):source.index("export function")]
    starters = re.split(r"\n  \{\n", block)[1:]
    sleep, tv = starters[0], starters[2]
    assert "operation: 'estimate'" in sleep and "preset: 'sleep'" in sleep
    assert "measure: 'average_minutes_per_day'" in sleep
    assert "operation: 'compare'" in tv and "preset: 'watching_tv'" in tv
    for fragment in ("group_a: { sex: 'male' }", "group_b: { sex: 'female' }",
                     "label_a: 'Men'", "label_b: 'Women'"):
        assert fragment in tv


def test_stable_stringify_matches_the_spa_serializer():
    assert smoke.stable_stringify({"years": [2025], "activity": {"preset": "sleep"},
                                   "measure": "m"}) == \
        '{"activity":{"preset":"sleep"},"measure":"m","years":[2025]}'


def test_the_origin_lock_and_ops_budget_use_secrets_without_printing_them(
        fake_server, monkeypatch, capsys):
    deployed_site(fake_server)
    token = "edge-ops-token-0123456789abcdef0123456789"
    fake_server.routes[("GET", "/api/_ops/budget")] = json_reply(200, {
        "status": "ok", "governor": {"mode": "conservative", "analyses": {"admitted": 3}}})
    # The fake origin lives under a path prefix of the same server; without the
    # edge secret it refuses, as the API does.
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base + "/origin-prefix")
    fake_server.routes[("GET", "/origin-prefix/api/v1/meta")] = envelope(403,
                                                                         "forbidden_origin")
    monkeypatch.setenv("EDGE_OPS_TOKEN", token)
    assert smoke.main(args(fake_server, "--origin-lock", "--ops-budget")) == 0
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert "/origin-prefix" not in out.out + out.err
    budget = [c for c in fake_server.calls if c["path"] == "/api/_ops/budget"]
    assert budget[0]["headers"]["authorization"] == f"Bearer {token}"
    origin = [c for c in fake_server.calls if c["path"].startswith("/origin-prefix")]
    assert len(origin) == 1 and "x-atus-edge-auth" not in origin[0]["headers"]


def test_an_open_origin_fails_the_lock(fake_server, monkeypatch, capsys):
    deployed_site(fake_server)
    monkeypatch.setenv("RENDER_ORIGIN", fake_server.base + "/origin-prefix")
    fake_server.routes[("GET", "/origin-prefix/api/v1/meta")] = json_reply(200, {})
    assert smoke.main(args(fake_server, "--origin-lock")) == 1
    assert "expected 403 forbidden_origin" in capsys.readouterr().out


def test_the_budget_has_a_hard_maximum(fake_server):
    assert smoke.main(["--base-url", fake_server.base, "--max-requests", "41"]) == 2
    assert fake_server.calls == []


def test_a_small_budget_stops_the_smoke_before_overspending(fake_server, capsys):
    deployed_site(fake_server)
    assert smoke.main(args(fake_server, "--max-requests", "5")) == 1
    assert len(fake_server.calls) == 5
    assert "budget of 5" in capsys.readouterr().out


# --- review round 4: smoke-recompute-answered-from-kv ------------------------------------


def _posts(server) -> dict[str, list[bytes]]:
    """POST bodies by path, in order."""
    bodies: dict[str, list[bytes]] = {}
    for call in server.calls:
        if call["method"] == "POST":
            bodies.setdefault(call["path"], []).append(call["body"])
    return bodies


def test_every_run_recomputes_with_bytes_no_earlier_run_sent(fake_server):
    """The edge cache keys on the exact request bytes, under a scope a code-only deploy
    keeps (ADR-015), and stores the first live answer for 30 days. json.dumps(spec,
    indent=1) gave the same bytes on every deploy of a release, so from the second on
    both starters came back X-Cache: edge and no deploy step ran the new image against
    the database. Each run's bytes now carry a new nonce."""
    deployed_site(fake_server)
    for _ in range(3):
        assert smoke.main(args(fake_server, "--recompute", "2")) == 0
    bodies = _posts(fake_server)
    assert len(bodies) == 2
    for path, sent in bodies.items():
        assert len(sent) == 3 and len(set(sent)) == 3, path
        operation = path.rsplit("/", 1)[1]
        ((_name, _operation, spec),) = [s for s in smoke.STARTERS if s[1] == operation]
        spec = {**spec, "years": [2025]}
        snapshot_key = smoke.request_key(path, smoke.stable_stringify(spec))
        for body in sent:
            assert json.loads(body) == spec                   # the same analysis
            assert smoke.request_key(path, body.decode()) != snapshot_key


def test_the_recompute_body_encodes_the_nonce_in_insignificant_whitespace():
    spec = {"activity": {"preset": "sleep"}, "years": [2025]}
    bodies = {smoke.recompute_body(spec, nonce) for nonce in (0, 1, 2, 2**63, 2**64 - 1)}
    assert len(bodies) == 5
    for body in bodies:
        assert json.loads(body) == spec
        tail = body.decode()[len(json.dumps(spec, indent=1)):]
        assert tail.strip(" \t\n") == "" and len(tail) == 1 + smoke.NONCE_BITS
    assert smoke.recompute_body(spec, 5) == smoke.recompute_body(spec, 5)


def _caching_edge(server, cache: dict) -> None:
    """A Worker in front of the fake origin that, like edge/src/cache.ts, answers a POST
    whose exact bytes it has stored before from KV, with X-Cache: edge."""
    for key in [k for k in server.routes if k[0] == "POST"]:
        status, headers, payload = server.routes[key]

        def route(request, path=key[1], status=status, headers=headers, payload=payload):
            seen = (path, request["body"])
            if seen in cache:
                return status, {**headers, "X-Cache": "edge"}, payload
            cache[seen] = 1
            return status, headers, payload

        server.routes[key] = route


def test_a_second_deploy_of_the_release_still_reaches_the_origin(fake_server, capsys):
    deployed_site(fake_server)
    cache: dict = {}
    _caching_edge(fake_server, cache)
    assert smoke.main(args(fake_server, "--recompute", "2")) == 0   # the release's first deploy
    assert smoke.main(args(fake_server, "--recompute", "2")) == 0   # a code-only redeploy
    out = capsys.readouterr().out
    assert "edge cache" not in out and "X-Cache edge" not in out
    assert out.count("snapshot vs live (X-Cache miss)") == 4
    assert len(cache) == 4


def test_an_answer_from_the_edge_cache_is_no_recompute_and_fails(fake_server, capsys):
    deployed_site(fake_server)
    for key in [k for k in fake_server.routes if k[0] == "POST"]:
        status, headers, payload = fake_server.routes[key]
        fake_server.routes[key] = (status, {**headers, "X-Cache": "edge"}, payload)
    assert smoke.main(args(fake_server, "--recompute", "2")) == 1
    out = capsys.readouterr().out
    for check in ("starter_1_sleep", "starter_2_tv_by_sex"):
        assert f"[FAIL] {check}: the live call was answered from the edge cache" in out


# --- review round 4: smoke-fails-on-governor-refusal --------------------------------------


def _refusal(status: int, code: str, scope: str, budget: str, resumes_at: str):
    return json_reply(status, {"error": {
        "code": code, "message": code,
        "details": {"scope": scope, "budget": budget, "resumes_at": resumes_at}}},
        {"Retry-After": "3600"})


GOVERNOR_REFUSALS = [
    (503, "daily_capacity_reached", "global", "global", "2026-10-22T00:00:00.000Z"),
    (503, "daily_capacity_reached", "daily", "cu_day", "2026-10-22T00:00:00.000Z"),
    (503, "capacity_paused", "monthly", "cu_month", "2026-10-25T06:00:00.000Z"),
    (429, "rate_limited", "client", "prefix", "2026-10-22T00:00:00.000Z"),
]


@pytest.mark.parametrize("refusal", GOVERNOR_REFUSALS, ids=lambda r: f"{r[1]}-{r[3]}")
def test_a_quota_refusal_of_the_recompute_is_owed_not_a_failed_deploy(fake_server, capsys,
                                                                       tmp_path, refusal):
    """The governor refuses uncached analyses late in a conservative-mode day and while
    the month's allowance is paused. The smoke runs after the Render hook and `wrangler
    deploy`; it failed the deploy, and Deploy step 6 then said to roll back a healthy
    release. It is now OWED (exit 3, which deploy.yml reports as not a reason to roll
    back), and the second starter, which the same budget would refuse, is not sent."""
    status, code, scope, budget, resumes_at = refusal
    deployed_site(fake_server)
    for key in [k for k in fake_server.routes if k[0] == "POST"]:
        fake_server.routes[key] = _refusal(status, code, scope, budget, resumes_at)
    report = tmp_path / "smoke.json"
    assert smoke.main(args(fake_server, "--recompute", "2", "--report", str(report))) == \
        smoke.EXIT_RECOMPUTE_OWED == 3
    out = capsys.readouterr().out
    assert (f"[OWED] starter_1_sleep: not recomputed: the quota governor refused the live "
            f"analysis (HTTP {status} {code}; scope {scope}, budget {budget}; resumes "
            f"{resumes_at})") in out
    assert "not a reason to roll back" in out and "Deploy step 6" in out
    assert "[OWED] starter_2_tv_by_sex: not sent: the quota governor refused" in out
    assert "smoke: 0 failed" in out and "2 live recompute(s) owed" in out
    assert sum(1 for c in fake_server.calls if c["method"] == "POST") == 1
    statuses = {r["name"]: r["status"] for r in json.loads(report.read_text())["results"]}
    assert statuses["starter_1_sleep"] == statuses["starter_2_tv_by_sex"] == "OWED"
    assert "FAIL" not in statuses.values()


@pytest.mark.parametrize("status,code", [(503, "capacity_unavailable"), (503, "overloaded"),
                                         (500, "internal_error")])
def test_other_refusals_still_fail_the_deploy(fake_server, capsys, status, code):
    """`capacity_unavailable` is the governor failing (which a broken Worker deploy can
    cause), not a budget; the others are errors of the deploy or of its capacity."""
    deployed_site(fake_server)
    for key in [k for k in fake_server.routes if k[0] == "POST"]:
        fake_server.routes[key] = envelope(status, code)
    assert smoke.main(args(fake_server, "--recompute", "2")) == 1
    out = capsys.readouterr().out
    assert f"[FAIL] starter_1_sleep: live analysis: HTTP {status} {code}" in out
    assert "OWED" not in out


def test_an_owed_recompute_with_another_failure_fails(fake_server, capsys):
    deployed_site(fake_server)
    fake_server.routes[("GET", "/api/health")] = json_reply(200, {"status": "ok",
                                                                  "build": "old"})
    for key in [k for k in fake_server.routes if k[0] == "POST"]:
        fake_server.routes[key] = _refusal(*GOVERNOR_REFUSALS[0])
    assert smoke.main(args(fake_server, "--recompute", "2")) == 1
    out = capsys.readouterr().out
    assert "[FAIL] api_health" in out and "[OWED] starter_1_sleep" in out
