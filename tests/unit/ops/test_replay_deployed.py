"""scripts/replay_deployed.py against a fake API that answers the 24 BLS benchmarks.

The fake builds each benchmark's response so that the benchmark's own
extractor yields the published value, which lets every rule be exercised
without a database: the request plan and its hard budget, record and replay,
the published-value checks, the §9 comparison, cold starts, retries, pacing
and the stop on capacity refusals.
"""

from __future__ import annotations

import copy
import json

import pytest

from atus_pipeline.analytics import benchmarks as bm

from .conftest import REPO_ROOT, envelope, json_reply, load_script

replay = load_script(REPO_ROOT / "scripts" / "replay_deployed.py", "atus_replay_deployed")

YEARS = list(range(2003, 2026))
DV_FULL = "0325:load-7-20261020T120000Z-abc123"
DV_SERVING = "0325:serving-20261020T224252Z-a7420a5c:de834e672474"


def benchmark_responses() -> dict[str, dict]:
    """Request body -> a response whose extractors give the published values."""
    out: dict[str, dict] = {}
    for b in bm.build_benchmarks():
        body = replay.stable_json(b.spec.to_dict())
        payload = out.setdefault(body, {
            "measure": b.spec.measure.value if hasattr(b.spec.measure, "value") else "m",
            "estimate": {"value": 100.0, "standard_error": 1.0, "ci_lower": 98.0,
                         "ci_upper": 102.0},
            "n_respondents": 5000, "n_participants": 4000, "warnings": [],
            "spec": b.spec.to_dict(),
        })
        if b.extract is bm._value:
            payload["estimate"]["value"] = b.expected
        elif b.extract is bm._mean_hours:
            payload["estimate"]["value"] = b.expected * 60
        elif b.extract is bm._se_hours:
            payload["estimate"]["standard_error"] = b.expected * 60
        elif b.extract is bm._participation_pct:
            payload["estimate"]["value"] = b.expected / 100
        elif b.extract is bm._sample_participants:
            payload["n_participants"] = int(b.expected)
    return out


TREND = {"points": [{"year": y, "estimate": None if y == 2020 else {"value": 530.0}}
                    for y in YEARS], "warnings": []}
COMPARE = {"label_a": "Men", "label_b": "Women",
           "group_a": {"estimate": {"value": 1.58 * 60, "standard_error": 1.0}},
           "group_b": {"estimate": {"value": 2.38 * 60, "standard_error": 1.0}},
           "difference": {"value": 0.8 * 60, "standard_error": 1.4}, "warnings": []}
META = {"api_version": "v1", "analytics_version": "0.2", "data": {"years": YEARS}}
#: The F66 broad selector as the full database answers it (value and SE as computed
#: by the engine on the full local database, pooled over the 22 multi-year years).
BROAD = {"estimate": {"value": 1440.0, "standard_error": 2.5073038618011603e-12,
                      "ci_lower": 1440.0, "ci_upper": 1440.0},
         "n_respondents": 250172, "n_participants": 250172, "warnings": []}


def fake_api(server, *, data_version: str = DV_SERVING, responses=None,
             health_path: str = "/health", broad: dict | None = None) -> dict[str, dict]:
    responses = responses if responses is not None else benchmark_responses()
    headers = {"X-Data-Version": data_version, "X-Cache": "miss"}
    broad_body = replay.broad_selector_body(YEARS)

    def estimate(req):
        body = req["body"].decode()
        if body == broad_body:
            return json_reply(200, broad if broad is not None else BROAD, headers)
        if body not in responses:
            return envelope(422, "invalid_request")
        return json_reply(200, responses[body], headers)

    server.routes.update({
        ("GET", health_path): json_reply(200, {"status": "ok", "build": "abc"}),
        ("GET", "/api/v1/meta"): json_reply(200, META, {"X-Data-Version": data_version}),
        ("POST", "/api/v1/analysis/estimate"): estimate,
        ("POST", "/api/v1/analysis/trend"): json_reply(200, TREND, headers),
        ("POST", "/api/v1/analysis/compare"): json_reply(200, COMPARE, headers),
    })
    return responses


def record(server, tmp_path) -> str:
    fake_api(server, data_version=DV_FULL)
    path = tmp_path / "expected.json"
    assert replay.main(["--base-url", server.base, "--record", str(path)]) == 0
    server.calls.clear()
    return str(path)


def test_the_plan_is_28_requests_for_24_benchmarks():
    plan = replay.plan_requests(list(bm.build_benchmarks()), replays=True,
                                health_path="/api/health")
    analyses = [p for p in plan if p.method == "POST"]
    assert len(plan) == 28 and len(analyses) == 26
    assert sum(len(p.benchmarks) for p in plan) == 24
    assert plan[0].path == "/api/health" and plan[1].path == "/api/v1/meta"
    # ADR-011 graft: the F66 broad selector is in the post-deploy replay, last.
    assert plan[-1].id == "replay:f66_broad_selector_pooled" and plan[-1].replay == "broad"
    assert "broad" not in {p.replay for p in replay.plan_requests(
        list(bm.build_benchmarks()), replays=False, health_path="/api/health")}


def test_the_broad_selector_is_every_tier1_code_pooled_with_replicates():
    from atus_pipeline.api.schemas.analysis import EstimateRequest

    body = json.loads(replay.broad_selector_body(YEARS))
    assert body["activity"]["include"] == list(replay.F66_TIER1_CODES)
    assert len(replay.F66_TIER1_CODES) == 18 and "17" not in replay.F66_TIER1_CODES
    assert body["years"] == [y for y in YEARS if y != 2020]  # 22 multi-year years
    assert body["variance"] == "replicate" and body["weights"] == "multiyear"
    assert body["measure"] == "average_minutes_per_day"
    # A valid request for the API (not a 422), and the same specification back.
    spec = EstimateRequest.model_validate(body).to_spec()
    assert replay.stable_json(spec.to_dict()) == replay.broad_selector_body(YEARS)


def test_a_broad_selector_short_of_the_whole_day_fails(fake_server, capsys):
    fake_api(fake_server, broad={**BROAD, "estimate": {**BROAD["estimate"], "value": 1380.0}})
    assert replay.main(["--base-url", fake_server.base]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] replay F66 broad selector" in out and "missing from F66_TIER1_CODES" in out


def test_record_writes_every_response_and_needs_a_loopback_api(fake_server, tmp_path, capsys):
    path = record(fake_server, tmp_path)
    expected = json.loads(open(path).read())
    assert expected["format"] == 1 and expected["analytics_version"] == "0.2"
    assert expected["data_version"] == DV_FULL
    assert len(expected["responses"]) == 27  # meta + 23 benchmarks + 3 replays
    assert replay.main(["--base-url", "https://atus.example.workers.dev",
                        "--record", str(tmp_path / "x.json")]) == 2
    assert "only against a local API" in capsys.readouterr().err


def test_replay_passes_against_equivalent_responses(fake_server, tmp_path, capsys):
    expected = record(fake_server, tmp_path)
    capsys.readouterr()
    fake_api(fake_server, data_version=DV_SERVING)
    report = tmp_path / "report.json"
    assert replay.main(["--base-url", fake_server.base, "--expected", expected,
                        "--report", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.count("[PASS] BLS ") == 26  # 24 benchmarks + the two compare groups
    assert "[PASS] §9 benchmark:" in out and "[PASS] §9 meta" in out
    assert "[PASS] replay F66 broad selector" in out
    assert "[PASS] §9 replay:f66_broad_selector_pooled" in out
    assert len(fake_server.calls) == 28
    data = json.loads(report.read_text())
    assert data["requests_sent"] == 28 and data["data_version"] == DV_SERVING
    assert not [o for o in data["outcomes"] if o["status"] == "FAIL"]


def test_a_value_outside_the_published_tolerance_fails(fake_server, capsys):
    responses = benchmark_responses()
    body = replay.stable_json(next(b for b in bm.build_benchmarks()
                                   if b.name.startswith("A1-2025: sleeping, hours/day")
                                   ).spec.to_dict())
    responses[body]["estimate"]["value"] += 1.0  # one minute off 9.03 h
    fake_api(fake_server, responses=responses)
    assert replay.main(["--base-url", fake_server.base]) == 1
    assert "[FAIL] BLS A1-2025: sleeping, hours/day:" in capsys.readouterr().out


def test_a_difference_from_the_full_database_beyond_the_criterion_fails(fake_server, tmp_path,
                                                                        capsys):
    expected = record(fake_server, tmp_path)
    responses = copy.deepcopy(benchmark_responses())
    first = next(iter(responses))
    responses[first]["estimate"]["standard_error"] *= 1 + 1e-9  # 1e-9 relative > 1e-12
    fake_api(fake_server, responses=responses)
    assert replay.main(["--base-url", fake_server.base, "--expected", expected]) == 1
    assert "|diff|" in capsys.readouterr().out


def test_plan_order_noise_passes_the_criterion(fake_server, tmp_path):
    expected = record(fake_server, tmp_path)
    responses = copy.deepcopy(benchmark_responses())
    for payload in responses.values():
        payload["estimate"]["standard_error"] *= 1 + 1e-14
    fake_api(fake_server, responses=responses)
    assert replay.main(["--base-url", fake_server.base, "--expected", expected]) == 0


def test_an_engine_version_change_is_reported(fake_server, tmp_path, capsys):
    expected = record(fake_server, tmp_path)
    fake_api(fake_server)
    fake_server.routes[("GET", "/api/v1/meta")] = json_reply(
        200, {**META, "analytics_version": "0.3"}, {"X-Data-Version": DV_SERVING})
    assert replay.main(["--base-url", fake_server.base, "--expected", expected]) == 1
    assert "the §9 comparison needs the same engine" in capsys.readouterr().out


@pytest.mark.parametrize("budget", ["0", "41", "100"])
def test_budgets_above_40_are_refused(fake_server, budget, capsys):
    assert replay.main(["--base-url", fake_server.base, "--max-requests", budget]) == 2
    assert fake_server.calls == []


def test_a_plan_over_the_budget_is_refused_before_sending(fake_server, capsys):
    fake_api(fake_server)
    assert replay.main(["--base-url", fake_server.base, "--max-requests", "27"]) == 2
    assert fake_server.calls == []
    assert "needs 28 requests" in capsys.readouterr().err


def test_a_subset_fits_a_small_budget(fake_server):
    fake_api(fake_server)
    assert replay.main(["--base-url", fake_server.base, "--max-requests", "4", "--no-replays",
                        "--benchmark", "UG ch7.4: TV watching 2007, mean minutes/day",
                        "--benchmark", "UG ch7.5: TV watching 2007, replicate-weight SE (hours)"
                        ]) == 0
    assert fake_server.count("POST") == 1  # two benchmarks, one specification


def test_a_cold_start_is_waited_out(fake_server, _no_real_sleep):
    fake_api(fake_server, health_path="/api/health")
    replies = iter([envelope(503, "service_waking", {"Retry-After": "5"}),
                    json_reply(200, {"status": "ok", "build": "abc"})])
    fake_server.routes[("GET", "/api/health")] = lambda _r: next(replies)
    assert replay.main(["--base-url", fake_server.base, "--health-path", "/api/health",
                        "--no-replays", "--benchmark",
                        "A1-2025: sleeping, hours/day"]) == 0
    assert fake_server.count("GET", "/api/health") == 2
    assert 15.0 in _no_real_sleep


def test_transient_answers_are_retried_and_paced(fake_server, _no_real_sleep):
    responses = fake_api(fake_server)
    attempts = iter([envelope(503, "overloaded", {"Retry-After": "10"})])

    def estimate(req):
        reply = next(attempts, None)
        return reply or json_reply(200, responses[req["body"].decode()],
                                   {"X-Data-Version": DV_SERVING})

    fake_server.routes[("POST", "/api/v1/analysis/estimate")] = estimate
    assert replay.main(["--base-url", fake_server.base, "--pace", "10.5", "--no-replays",
                        "--benchmark", "A1-2025: sleeping, hours/day",
                        "--benchmark", "A1-2025: watching TV, hours/day"]) == 0
    assert fake_server.count("POST") == 3
    assert _no_real_sleep.count(10.5) == 2  # paced before each later analysis


def test_a_capacity_refusal_stops_the_run(fake_server, capsys):
    fake_api(fake_server)
    fake_server.routes[("POST", "/api/v1/analysis/estimate")] = envelope(
        503, "daily_capacity_reached", {"Retry-After": "40000"})
    assert replay.main(["--base-url", fake_server.base]) == 1
    assert fake_server.count("POST") == 1  # nothing more is spent once refused
    out = capsys.readouterr().out
    assert "refused for capacity" in out and "not sent:" in out


def test_retries_never_exceed_the_budget(fake_server):
    fake_api(fake_server)
    fake_server.routes[("POST", "/api/v1/analysis/estimate")] = envelope(
        503, "overloaded", {"Retry-After": "1"})
    assert replay.main(["--base-url", fake_server.base]) == 1
    assert len(fake_server.calls) == 40


def test_the_benchmark_set_is_the_engine_suite():
    """The replay reuses the definitions, never a copy of the expected values."""
    source = (REPO_ROOT / "scripts" / "replay_deployed.py").read_text()
    assert "from atus_pipeline.analytics import benchmarks as bm" in source
    assert len(bm.build_benchmarks()) == 24
    assert set(replay.EXTRACTORS) == {b.extract for b in bm.build_benchmarks()}
