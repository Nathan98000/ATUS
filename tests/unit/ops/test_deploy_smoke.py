"""scripts/ops/deploy_smoke.py against a fake deployed site (static snapshot + Worker)."""

from __future__ import annotations

import copy
import json
import re

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
