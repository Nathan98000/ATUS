"""scripts/equivalence: the 3-way HTTP equivalence harness, without servers
or databases (the end-to-end run is tests/integration/test_equivalence_harness.py).

Covers the comparison rules (the §9 criterion through HTTP bodies, including
the scale floor that makes zero-variance specs comparable, the field
whitelist, the matched headers, the intended Phase 5 differences and that
nothing else is explained away), the spec format, the report, the server
environments and process handling, and the replay's inputs and retries.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from atus_pipeline.api.errors import validation_details
from atus_pipeline.config import ConfigError, check_api_settings, load_settings
from atus_pipeline.serving.equivalence import REL_TOL

HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "equivalence"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))  # replay.py imports its siblings by name

import httpdiff  # noqa: E402
import replay  # noqa: E402
import servers  # noqa: E402
from httpdiff import RequestInfo, Response  # noqa: E402

ESTIMATE = httpdiff.ENDPOINTS["estimate"]
FULL_VERSION = "0325:run1:1788919495952073"
SERVING_VERSION = "0325:serving-20261020T224252Z-a7420a5c:de834e672474"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def estimate_body(value=480.0, se=1.25, lo=477.55, hi=482.45, **extra) -> dict:
    body = {
        "measure": "average_minutes_per_day",
        "estimate": {"value": value, "unit": "minutes", "standard_error": se,
                     "confidence_level": 0.95, "ci_lower": lo, "ci_upper": hi},
        "years": [2024],
        "activity": {"label": "Sleeping", "include": ["0101"], "exclude": [],
                     "leaf_code_count": 3},
        "population": "All respondents",
        "n_respondents": 7669, "n_participants": 7669,
        "weighted_population_per_day": 2.7e8, "days_in_period": 366,
        "weight": {"scheme": "multiyear", "bls_variable": "TUFNWGTP", "column": "final_weight"},
        "variance_method": "replicate", "analytics_version": "0.2",
        "spec": {"measure": "average_minutes_per_day", "years": [2024]},
        "warnings": ["Activity codes use the BLS-harmonized 2003-25 multi-year lexicon."],
    }
    body.update(extra)
    return body


def response(status: int, payload, headers: dict | None = None) -> Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return Response(status, dict(headers or {}), body)


def envelope(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def ok(payload, version=None, **headers) -> Response:
    base = {"content-type": "application/json", "x-analytics-version": "0.2",
            "x-response-format": "1", "cache-control": "no-store"}
    if version:
        base["x-data-version"] = version
    base.update(headers)
    return response(200, payload, base)


POST = RequestInfo("POST", ESTIMATE, b'{"activity":{"preset":"sleep"},"years":[2024]}',
                   "application/json")


def b_c(left: Response, right: Response, **kwargs) -> httpdiff.PairResult:
    return httpdiff.evaluate_pair("b_c", left, right, POST, **kwargs)


def a_b(left: Response, right: Response, request: RequestInfo = POST) -> httpdiff.PairResult:
    return httpdiff.evaluate_pair("a_b", left, right, request)


# --------------------------------------------------------------------------- #
# the §9 criterion through HTTP bodies                                         #
# --------------------------------------------------------------------------- #


def test_identical_answers_are_equivalent():
    result = b_c(ok(estimate_body()), ok(estimate_body()))
    assert result.outcome == "equivalent" and not result.mismatches
    assert result.compared > 20 and result.max_relative == 0 == result.max_scaled


def test_standard_errors_may_differ_by_plan_order_noise():
    noisy = estimate_body(se=1.25 * (1 + 4e-13), hi=482.45 * (1 + 2e-15))
    result = b_c(ok(estimate_body()), ok(noisy))
    assert result.outcome == "equivalent"
    assert 0 < result.max_relative < REL_TOL and 0 < result.max_scaled < REL_TOL


def test_a_standard_error_beyond_the_tolerance_is_a_mismatch_with_its_path():
    # bound: 1e-12 x max(|SE|, |estimate|) = 4.8e-10 minutes
    result = b_c(ok(estimate_body()), ok(estimate_body(se=1.25 + 6e-10)))
    assert result.outcome == "mismatch"
    assert [m["path"] for m in result.mismatches] == ["$.estimate.standard_error"]


def test_zero_variance_estimates_are_compared_on_the_estimates_scale():
    # F40/I12: every respondent's day sums to 1,440 minutes; the true SE is 0
    # and the engine reports floating-point noise (measured on the full and
    # serving databases): 1.3e-2 apart relative to each other.
    full = estimate_body(value=1440.0, se=7.83e-12, lo=1440.0 - 1.5e-11, hi=1440.0 + 1.5e-11)
    serving = estimate_body(value=1440.0, se=7.93e-12, lo=1440.0 - 1.6e-11,
                            hi=1440.0 + 1.6e-11)
    result = b_c(ok(full), ok(serving))
    assert result.outcome == "equivalent"
    assert result.max_relative == pytest.approx(0.1e-12 / 7.93e-12)  # 1.26e-2
    assert result.max_scaled < 1e-15


def test_the_floor_is_the_estimate_not_a_free_pass_for_small_values():
    # The same SE noise on an estimate of a millionth of a minute is a mismatch:
    # the floor scales with the point value it belongs to.
    full = estimate_body(value=1e-6, se=7.83e-12, lo=1e-6, hi=1e-6)
    serving = estimate_body(value=1e-6, se=7.93e-12, lo=1e-6, hi=1e-6)
    assert b_c(ok(full), ok(serving)).outcome == "mismatch"


def test_zero_estimate_with_zero_variance_is_exact():
    zero = estimate_body(value=0.0, se=0.0, lo=0.0, hi=0.0, n_participants=0)
    assert b_c(ok(zero), ok(zero)).outcome == "equivalent"
    tiny = estimate_body(value=0.0, se=1e-300, lo=0.0, hi=0.0, n_participants=0)
    assert b_c(ok(zero), ok(tiny)).outcome == "mismatch"


def test_a_self_comparisons_difference_uses_both_groups_as_its_floor():
    group = estimate_body(value=500.0)

    def compare(diff_se):
        return {"measure": "average_minutes_per_day", "label_a": "a", "label_b": "b",
                "group_a": group, "group_b": group,
                "difference": {"value": 0.0, "unit": "minutes", "standard_error": diff_se,
                               "confidence_level": 0.95, "ci_lower": -diff_se,
                               "ci_upper": diff_se},
                "warnings": []}

    result = b_c(ok(compare(1.1e-13)), ok(compare(2.0e-13)))
    assert result.outcome == "equivalent"
    assert result.max_relative > 0.4 and result.max_scaled < 1e-15
    assert b_c(ok(compare(1.1e-13)), ok(compare(1e-9))).outcome == "mismatch"


def test_each_trend_point_has_its_own_floor():
    def trend(se_2019):
        point = {"year": 2019, "estimate": {"value": 1440.0, "unit": "minutes",
                                            "standard_error": se_2019, "confidence_level": 0.95,
                                            "ci_lower": 1440.0, "ci_upper": 1440.0},
                 "n_respondents": 10, "n_participants": 10,
                 "weighted_population_per_day": 1.0, "unavailable_reason": None}
        unavailable = {"year": 2020, "estimate": None, "n_respondents": None,
                       "n_participants": None, "weighted_population_per_day": None,
                       "unavailable_reason": "multiyear weights exclude 2020"}
        return {"measure": "average_minutes_per_day", "points": [point, unavailable],
                "warnings": []}

    assert b_c(ok(trend(7.8e-12)), ok(trend(7.9e-12))).outcome == "equivalent"
    result = b_c(ok(trend(7.8e-12)), ok(trend(1e-6)))
    assert [m["path"] for m in result.mismatches] == ["$.points[0].estimate.standard_error"]


@pytest.mark.parametrize("change, path", [
    ({"estimate": {**estimate_body()["estimate"], "value": 480.0 + 1e-13}}, "$.estimate.value"),
    ({"n_respondents": 7670}, "$.n_respondents"),
    ({"n_participants": 7668}, "$.n_participants"),
    ({"weighted_population_per_day": 2.7e8 * (1 + 1e-15)}, "$.weighted_population_per_day"),
    ({"days_in_period": 365}, "$.days_in_period"),
    ({"warnings": ["another"]}, "$.warnings[0]"),
    ({"spec": {"measure": "average_minutes_per_day", "years": [2023]}}, "$.spec.years[0]"),
    ({"population": "Women"}, "$.population"),
    ({"analytics_version": "0.3"}, "$.analytics_version"),
])
def test_everything_but_se_and_ci_is_exactly_equal(change, path):
    result = b_c(ok(estimate_body()), ok(estimate_body(**change)))
    assert result.outcome == "mismatch"
    assert path in [m["path"] for m in result.mismatches]


def test_integer_and_float_are_different_values():
    result = b_c(ok(estimate_body(n_respondents=7669)), ok(estimate_body(n_respondents=7669.0)))
    assert result.outcome == "mismatch"


def test_extra_or_missing_fields_are_mismatches():
    body = estimate_body()
    del body["warnings"]
    result = b_c(ok(estimate_body()), ok(body))
    assert result.outcome == "mismatch" and "warnings" in result.mismatches[0]["detail"]


def test_different_status_codes_are_a_mismatch_at_status():
    result = b_c(ok(estimate_body()), response(422, envelope("insufficient_data", "x")))
    assert result.outcome == "mismatch"
    assert result.mismatches[0] == {"path": "status", "detail": "200 vs 422 insufficient_data"}


def test_error_bodies_must_be_identical():
    left = response(422, envelope("unknown_activity", "Activity code '010198' is not ..."))
    assert b_c(left, response(422, envelope("unknown_activity",
                                            "Activity code '010198' is not ..."))
               ).outcome == "equivalent"
    other = b_c(left, response(422, envelope("unknown_activity", "Activity code '010199'")))
    assert [m["path"] for m in other.mismatches] == ["$.error.message"]
    details = b_c(left, response(422, envelope("unknown_activity",
                                               "Activity code '010198' is not ...", {"x": 1})))
    assert details.outcome == "mismatch"


def test_error_bodies_get_no_tolerance_even_on_numbers():
    left = response(422, envelope("validation_error", "m", {"standard_error": 1.0}))
    right = response(422, envelope("validation_error", "m", {"standard_error": 1.0 + 1e-15}))
    assert b_c(left, right).outcome == "mismatch"


def test_non_json_bodies_are_compared_byte_for_byte():
    assert b_c(response(502, b"<html>bad"), response(502, b"<html>bad")).outcome == "equivalent"
    assert b_c(response(502, b"<html>bad"), response(502, b"<html>Bad")).outcome == "mismatch"


# --------------------------------------------------------------------------- #
# whitelist, headers, data versions                                            #
# --------------------------------------------------------------------------- #


def test_whitelisted_keys_may_differ_and_are_reported():
    meta = {"api_version": "v1", "data": {"release": "0325", "data_version": FULL_VERSION,
                                          "years": [2024]}}
    serving = json.loads(json.dumps(meta))
    serving["data"]["data_version"] = SERVING_VERSION
    result = b_c(ok(meta), ok(serving))
    assert result.outcome == "equivalent"
    assert result.whitelisted == [{"rule": "data_version", "path": "$.data.data_version",
                                   "left": FULL_VERSION, "right": SERVING_VERSION}]


def test_ingestion_metadata_is_not_whitelisted():
    meta = {"data": {"release": "0325", "last_ingested_at": "2026-09-09T02:07:25+00:00",
                     "ingestion_run_id": 1}}
    changed = {"data": {"release": "0325", "last_ingested_at": "2026-10-20T00:00:00+00:00",
                        "ingestion_run_id": 1}}
    result = b_c(ok(meta), ok(changed))
    assert [m["path"] for m in result.mismatches] == ["$.data.last_ingested_at"]


def test_every_whitelist_rule_is_justified():
    for rule in httpdiff.WHITELIST:
        assert rule.justification and set(rule.pairs) <= set(httpdiff.PAIRS)
    assert all(httpdiff.WHITELISTED_HEADERS.values())
    assert not set(httpdiff.MATCHED_HEADERS) & set(httpdiff.WHITELISTED_HEADERS)


def test_strip_whitelisted_walks_lists_and_keeps_order():
    a = {"items": [{"data_version": "x", "v": 1}, {"v": 2}], "k": 3}
    b = {"items": [{"data_version": "y", "v": 1}, {"v": 2}, {"v": 9}], "k": 3}
    left, right, notes = httpdiff.strip_whitelisted(a, b, "b_c")
    assert left == {"items": [{"v": 1}, {"v": 2}], "k": 3}
    assert right == {"items": [{"v": 1}, {"v": 2}, {"v": 9}], "k": 3}
    assert notes[0]["path"] == "$.items[0].data_version"


def test_matched_headers_must_be_equal_between_b_and_c():
    left = ok(estimate_body(), FULL_VERSION, **{"x-request-id": "1", "x-cache": "miss",
                                                "x-analysis-key": "k1"})
    right = ok(estimate_body(), SERVING_VERSION, **{"x-request-id": "2", "x-cache": "hit",
                                                    "x-analysis-key": "k2"})
    assert b_c(left, right).outcome == "equivalent"
    changed = ok(estimate_body(), SERVING_VERSION, **{"x-analytics-version": "0.3"})
    assert [m["path"] for m in b_c(left, changed).mismatches] == [
        "headers.x-analytics-version"]


def test_an_answer_from_another_data_version_is_a_mismatch_never_explained():
    expected = {"b": FULL_VERSION, "c": SERVING_VERSION}
    fine = b_c(ok(estimate_body(), FULL_VERSION), ok(estimate_body(), SERVING_VERSION),
               expected_versions=expected)
    assert fine.outcome == "equivalent"
    reloaded = b_c(ok(estimate_body(), FULL_VERSION), ok(estimate_body(), "0325:other"),
                   expected_versions=expected)
    assert reloaded.outcome == "mismatch"
    assert reloaded.mismatches[0]["path"] == "headers.x-data-version"
    # In a_b too, even when an intended difference would otherwise apply.
    request = RequestInfo("POST", ESTIMATE, b"{}", "text/plain")
    result = httpdiff.evaluate_pair(
        "a_b", response(422, envelope("validation_error", "m")),
        response(415, envelope("unsupported_media_type", "m"), {"x-data-version": "other"}),
        request, expected_versions={"b": FULL_VERSION})
    assert result.outcome == "mismatch"


# --------------------------------------------------------------------------- #
# intended Phase 5 differences (a vs b only)                                   #
# --------------------------------------------------------------------------- #

def _baseline_validation(n: int, message_length: int = 20) -> dict:
    errors = [{"location": ["body", "population", f"extra_{i:02d}"],
               "message": "E" * message_length, "type": "extra_forbidden"} for i in range(n)]
    return envelope("validation_error", "The request does not match the API schema.",
                    {"errors": errors})


def _phase5_validation(baseline: dict) -> dict:
    raw = [{"loc": e["location"], "msg": e["message"], "type": e["type"]}
           for e in baseline["error"]["details"]["errors"]]
    return envelope("validation_error", baseline["error"]["message"], validation_details(raw))


def test_capped_validation_details_are_an_intended_difference():
    baseline = _baseline_validation(30, message_length=250)
    result = a_b(response(422, baseline), response(422, _phase5_validation(baseline)))
    assert result.outcome == "intended_difference"
    assert result.classification["id"] == "validation_details_capped"
    assert result.mismatches  # the difference itself stays in the report


def test_capping_must_explain_the_whole_difference():
    baseline = _baseline_validation(30)
    phase5 = _phase5_validation(baseline)
    phase5["error"]["details"]["errors"][3]["type"] = "missing"
    assert a_b(response(422, baseline), response(422, phase5)).outcome == "mismatch"
    other_message = _phase5_validation(baseline)
    other_message["error"]["message"] = "Something else."
    assert a_b(response(422, baseline), response(422, other_message)).outcome == "mismatch"


def test_identical_small_validation_errors_are_simply_equivalent():
    baseline = _baseline_validation(2)
    assert a_b(response(422, baseline), response(422, _phase5_validation(baseline))
               ).outcome == "equivalent"


def test_a_long_preset_is_an_intended_schema_refusal():
    request = RequestInfo("POST", ESTIMATE, json.dumps(
        {"activity": {"preset": "p" * 70}, "years": [2024]}).encode(), "application/json")
    baseline = response(422, envelope("invalid_spec", "Unknown activity preset 'ppp...'."))
    phase5 = response(422, envelope(
        "validation_error", "The request does not match the API schema.",
        {"errors": [{"location": ["body", "activity", "preset"],
                     "message": "String should have at most 64 characters",
                     "type": "string_too_long"}]}))
    assert a_b(baseline, phase5, request).classification["id"] == "preset_max_length"
    short = RequestInfo("POST", ESTIMATE, json.dumps(
        {"activity": {"preset": "p" * 10}, "years": [2024]}).encode(), "application/json")
    assert a_b(baseline, phase5, short).outcome == "mismatch"


def test_body_size_and_media_type_refusals_are_intended():
    big = RequestInfo("POST", ESTIMATE, b"x" * 70_000, "application/json")
    small = RequestInfo("POST", ESTIMATE, b"{}", "application/json")
    too_large = response(413, envelope("payload_too_large", "The request body is too large."))
    baseline = response(422, envelope("validation_error", "m", {"errors": []}))
    assert a_b(baseline, too_large, big).classification["id"] == "payload_too_large"
    assert a_b(baseline, too_large, small).outcome == "mismatch"
    unsupported = response(415, envelope("unsupported_media_type", "Request bodies must be JSON"))
    text = RequestInfo("POST", ESTIMATE, b"{}", "text/plain; charset=utf-8")
    assert a_b(baseline, unsupported, text).classification["id"] == "unsupported_media_type"
    assert a_b(baseline, unsupported, small).outcome == "mismatch"


def test_the_parser_limit_and_control_characters_are_intended():
    deep = RequestInfo("POST", ESTIMATE, b"[" * 30000 + b"]" * 30000, "application/json")
    baseline = response(400, envelope("http_400", "There was an error parsing the body"))
    phase5 = response(400, envelope("malformed_json",
                                    "The request body could not be parsed as JSON."))
    assert a_b(baseline, phase5, deep).classification["id"] == "malformed_json_parser_limit"
    nul = RequestInfo("GET", "/api/v1/activities?search=ab%00")
    crashed = response(500, envelope("internal_error", "An unexpected internal error occurred."))
    refused = response(422, envelope("validation_error", "The request does not match the API "
                                     "schema.", {"errors": [{
                                         "location": ["query", "search"],
                                         "message": "Control characters are not allowed.",
                                         "type": "value_error"}]}))
    assert a_b(crashed, refused, nul).classification["id"] == "control_characters_rejected"
    clean = RequestInfo("GET", "/api/v1/activities?search=abc")
    assert a_b(crashed, refused, clean).outcome == "mismatch"


def test_the_lexicon_wording_and_health_build_are_intended():
    request = RequestInfo("GET", "/api/v1/activities/999999")
    baseline = response(404, envelope(
        "activity_not_found", "Activity code '999999' does not exist in the 2003-25 lexicon.",
        {"code": "999999"}))
    phase5 = response(404, envelope(
        "activity_not_found",
        "Activity code '999999' does not exist in the 2003-25 activity lexicon.",
        {"code": "999999"}))
    assert a_b(baseline, phase5, request).classification["id"] == "lexicon_wording"
    other_details = response(404, envelope(
        "activity_not_found",
        "Activity code '999999' does not exist in the 2003-25 activity lexicon.",
        {"code": "99"}))
    assert a_b(baseline, other_details, request).outcome == "mismatch"
    health = RequestInfo("GET", "/health")
    assert a_b(response(200, {"status": "ok"}), response(200, {"status": "ok", "build": "abc"}),
               health).classification["id"] == "health_reports_build"
    assert a_b(response(200, {"status": "ok"}), response(200, {"status": "ok", "x": 1}),
               health).outcome == "mismatch"


@pytest.mark.parametrize("code", sorted(httpdiff.DOMAIN_ERROR_CODES))
def test_domain_errors_have_no_intended_difference(code):
    baseline = response(422, envelope(code, "Old text."))
    assert a_b(baseline, response(422, envelope(code, "New text."))).outcome == "mismatch"
    assert a_b(baseline, response(422, envelope("invalid_spec" if code != "invalid_spec"
                                                else "unknown_activity", "Old text."))
               ).outcome == "mismatch"


def test_a_statistic_change_between_code_bases_is_never_intended():
    result = a_b(ok(estimate_body()), ok(estimate_body(n_respondents=7670)))
    assert result.outcome == "mismatch" and result.classification is None


def test_intended_differences_never_apply_between_b_and_c():
    text = RequestInfo("POST", ESTIMATE, b"{}", "text/plain")
    result = httpdiff.evaluate_pair(
        "b_c", response(422, envelope("validation_error", "m")),
        response(415, envelope("unsupported_media_type", "m")), text)
    assert result.outcome == "mismatch"


# --------------------------------------------------------------------------- #
# spec status                                                                  #
# --------------------------------------------------------------------------- #


def _spec(expect="ok", **kwargs) -> httpdiff.Spec:
    return httpdiff.Spec(id=kwargs.get("id", "s1"), endpoint="estimate",
                         body={"activity": {"preset": "sleep"}, "years": [2024]},
                         category="cat", expect=expect, source="f.jsonl:1")


def test_a_spec_with_three_equal_answers_passes():
    answers = {k: ok(estimate_body()) for k in "abc"}
    entry = httpdiff.evaluate_spec(_spec(), answers)
    assert entry["status"] == "equivalent" and entry["passed"] and entry["expect_met"]
    assert set(entry["pairs"]) == {"a_b", "b_c"}


def test_only_run_pairs_are_compared():
    entry = httpdiff.evaluate_spec(_spec(), {k: ok(estimate_body()) for k in "bc"})
    assert set(entry["pairs"]) == {"b_c"}


def test_a_phase5_5xx_fails_even_when_identical():
    crash = response(500, envelope("internal_error", "An unexpected internal error occurred."))
    entry = httpdiff.evaluate_spec(_spec(), {"a": crash, "b": crash, "c": crash})
    assert entry["status"] == "server_error" and not entry["passed"]


def test_a_baseline_5xx_is_judged_by_the_pair():
    crash = response(500, envelope("internal_error", "x"))
    entry = httpdiff.evaluate_spec(_spec(), {"a": crash, "b": ok(estimate_body()),
                                             "c": ok(estimate_body())})
    assert entry["status"] == "mismatch"


def test_missing_or_capacity_answers_make_a_spec_incomplete():
    lost = Response(None, transport_error="ConnectionRefusedError")
    busy = response(503, envelope("overloaded", "busy"), {"retry-after": "10"})
    for bad in (lost, busy):
        entry = httpdiff.evaluate_spec(_spec(), {"a": ok(estimate_body()),
                                                 "b": ok(estimate_body()), "c": bad})
        assert entry["status"] == "incomplete" and not entry["passed"]
    assert httpdiff.is_retryable(busy) and httpdiff.is_retryable(lost)
    timeout = response(503, envelope("analysis_timeout", "slow"))
    assert not httpdiff.is_retryable(timeout)


def test_expectation_misses_are_reported_not_failed():
    answers = {k: response(422, envelope("insufficient_data", "none")) for k in "abc"}
    entry = httpdiff.evaluate_spec(_spec(expect="ok"), answers)
    assert entry["passed"] and entry["expect_met"] is False
    report = {"summary": httpdiff.summarize([entry], [])}
    assert report["summary"]["expectation_misses"] == ["s1"]
    assert httpdiff.report_passed(report)
    assert not httpdiff.report_passed(report, fail_on_unexpected=True)


def test_harness_errors_fail_the_entry():
    entry = httpdiff.harness_error_entry({"id": "s1", "category": "c", "endpoint": "estimate",
                                          "expect": "ok", "expect_met": None},
                                         {"b": ok({})}, RuntimeError("boom"))
    assert not entry["passed"] and entry["harness_error"] == "RuntimeError: boom"
    assert not httpdiff.summarize([entry], [])["passed"]


# --------------------------------------------------------------------------- #
# specs                                                                        #
# --------------------------------------------------------------------------- #


def _write(directory: Path, name: str, lines: list) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("\n".join(
        line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")


def test_specs_load_in_file_and_line_order(tmp_path):
    _write(tmp_path, "b.jsonl", [{"id": 2, "endpoint": "trend", "expect": "ok", "body": {}}])
    _write(tmp_path, "a.jsonl", [
        {"id": "x", "endpoint": "estimate", "expect": "error", "category": "c",
         "body": {"years": [2024], "activity": {"preset": "sleep"}}},
        "",
        {"id": "raw", "endpoint": "compare", "expect": "error", "body": "{not json",
         "content_type": "text/plain"},
    ])
    (tmp_path / "notes.txt").write_text("ignored")
    specs, files = httpdiff.load_specs(tmp_path)
    assert [s.id for s in specs] == ["x", "raw", "2"]
    assert [f["file"] for f in files] == ["a.jsonl", "b.jsonl"]
    assert files[0]["specs"] == 2 and len(files[0]["sha256"]) == 64
    assert specs[0].body_bytes() == b'{"years":[2024],"activity":{"preset":"sleep"}}'
    assert specs[1].body_bytes() == b"{not json" and specs[1].content_type == "text/plain"
    assert specs[2].category == "uncategorized" and specs[2].path == "/api/v1/analysis/trend"


def test_non_ascii_bodies_are_sent_as_utf8():
    spec = httpdiff.Spec("u", "estimate", {"activity": {"label": "Café"}}, "c", "ok", "f:1")
    assert spec.body_bytes() == '{"activity":{"label":"Café"}}'.encode()


def test_every_spec_problem_is_reported_at_once(tmp_path):
    _write(tmp_path, "a.jsonl", [
        "{broken",
        [1, 2],
        {"id": "a", "endpoint": "nope", "expect": "maybe", "body": {}},
        {"endpoint": "estimate", "expect": "ok"},
        {"id": "dup", "endpoint": "estimate", "expect": "ok", "body": {}},
        {"id": True, "endpoint": "estimate", "expect": "ok", "body": {}},
    ])
    _write(tmp_path, "b.jsonl", [{"id": "dup", "endpoint": "trend", "expect": "ok", "body": {}},
                                 '{"id": "nan", "endpoint": "trend", "expect": "ok", '
                                 '"body": {"x": NaN}}'])
    with pytest.raises(httpdiff.SpecError) as raised:
        httpdiff.load_specs(tmp_path)
    text = str(raised.value)
    for fragment in ("a.jsonl:1: not JSON", "a.jsonl:2: a spec line must be a JSON object",
                     "'endpoint' must be one of", "'expect' must be one of",
                     "a.jsonl:4: missing 'id'", "missing 'body'",
                     "b.jsonl:1: duplicate id 'dup' (first at a.jsonl:5)",
                     "a.jsonl:6: 'id' must be", "non-finite"):
        assert fragment in text, fragment


def test_an_empty_or_missing_spec_directory_is_refused(tmp_path):
    with pytest.raises(httpdiff.SpecError, match="no .jsonl"):
        httpdiff.load_specs(tmp_path)
    with pytest.raises(httpdiff.SpecError, match="not a directory"):
        httpdiff.load_specs(tmp_path / "missing")
    _write(tmp_path, "a.jsonl", [""])
    with pytest.raises(httpdiff.SpecError, match="hold no specs"):
        httpdiff.load_specs(tmp_path)


def test_the_committed_smoke_specs_are_valid():
    specs, _ = httpdiff.load_specs(HARNESS / "specs")
    assert len(specs) >= 20
    assert {s.endpoint for s in specs} == set(httpdiff.ENDPOINTS)
    assert {s.expect for s in specs} == {"ok", "error"}


# --------------------------------------------------------------------------- #
# GET list and report                                                          #
# --------------------------------------------------------------------------- #


def _listing() -> dict:
    entries = [{"code": f"{i:02d}", "level": 1} for i in range(1, 19)]
    entries += [{"code": f"{i:02d}{j:02d}", "level": 2} for i in range(1, 19) for j in (1, 2)]
    entries += [{"code": f"{i:02d}{j:02d}01", "level": 3} for i in range(1, 19) for j in (1, 2)]
    return {"activities": entries, "total": len(entries)}


def test_activity_details_cover_every_tier1_code_and_a_seeded_sample():
    paths = httpdiff.activity_detail_paths(_listing(), sample=10, seed=1)
    codes = [p.rsplit("/", 1)[1] for p in paths]
    assert codes[:18] == [f"{i:02d}" for i in range(1, 19)]
    assert sum(len(c) == 4 for c in codes) == 5 and sum(len(c) == 6 for c in codes) == 5
    assert paths == httpdiff.activity_detail_paths(_listing(), sample=10, seed=1)
    assert paths != httpdiff.activity_detail_paths(_listing(), sample=10, seed=2)
    assert len(httpdiff.activity_detail_paths(_listing(), sample=1000, seed=1)) == 18 + 72
    assert httpdiff.activity_detail_paths(None, sample=10, seed=1) == []


def test_fixed_gets_are_valid_request_targets():
    for path in httpdiff.FIXED_GETS:
        assert path.startswith("/") and path.isascii() and " " not in path


def _report(specs: list[dict], gets: list[dict]) -> dict:
    return {
        "harness_version": httpdiff.HARNESS_VERSION, "generated_at": "2026-10-20T00:00:00Z",
        "inputs": {"spec_dir": "/specs", "spec_files": [{"file": "a.jsonl"}],
                   "fail_on_unexpected": False},
        "servers": {k: {"label": httpdiff.TARGET_LABELS[k], "commit": "abc",
                        "database": "u@h:1/d", "data_version": "v"} for k in "abc"},
        "criterion": httpdiff.criterion_description(),
        "whitelist": {"body": [r.as_dict() for r in httpdiff.WHITELIST],
                      "headers": httpdiff.WHITELISTED_HEADERS},
        "intended_differences": [d.as_dict() for d in httpdiff.INTENDED_DIFFERENCES],
        "summary": httpdiff.summarize(specs, gets), "specs": specs, "gets": gets,
    }


def test_the_report_summarizes_and_renders():
    good = httpdiff.evaluate_spec(_spec(), {k: ok(estimate_body()) for k in "abc"})
    noisy = httpdiff.evaluate_spec(
        _spec(id="zero"), {"a": ok(estimate_body(value=1440.0, se=7.8e-12)),
                           "b": ok(estimate_body(value=1440.0, se=7.8e-12)),
                           "c": ok(estimate_body(value=1440.0, se=7.9e-12))})
    noisy["id"] = "zero"
    bad = httpdiff.evaluate_spec(_spec(), {"a": ok(estimate_body()), "b": ok(estimate_body()),
                                           "c": ok(estimate_body(n_respondents=1))})
    bad["id"] = "bad|id"
    get = httpdiff.evaluate_get("/health", {"a": response(200, {"status": "ok"}),
                                            "b": response(200, {"status": "ok", "build": "x"}),
                                            "c": response(200, {"status": "ok", "build": "x"})})
    report = _report([good, noisy, bad], [get])
    summary = report["summary"]
    assert not summary["passed"] and summary["specs_failed"] == 1
    assert summary["by_status"]["mismatch"] == 1 and summary["by_status"]["equivalent"] == 2
    assert summary["pairs"]["b_c"]["max_relative_at"] == "zero"
    assert 0.01 < summary["pairs"]["b_c"]["max_relative"] < 0.02
    assert summary["pairs"]["b_c"]["max_scaled"] < 1e-15
    assert summary["intended_differences"] == [
        {"pair": "a_b", "id": "health_reports_build", "count": 1}]
    json.dumps(report, allow_nan=False)  # machine-readable: strict JSON
    text = httpdiff.render_markdown(report)
    assert text.startswith("# HTTP equivalence report\n\n**FAIL**: 2 of 3 specs passed")
    assert "`bad\\|id`" in text and "$.n_respondents" in text
    assert "`health_reports_build`" in text


def test_infinite_ratios_stay_strict_json():
    result = httpdiff.PairResult("b_c", max_relative=math.inf, max_scaled=math.inf)
    assert result.as_dict()["max_relative"] == "inf"
    entry = {"id": "s", "category": "c", "endpoint": "estimate", "status": "mismatch",
             "passed": False, "expect_met": True, "targets": {},
             "pairs": {"b_c": result.as_dict()}}
    summary = httpdiff.summarize([entry], [])
    assert summary["pairs"]["b_c"]["max_relative"] == "inf"
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("text, targets", [("abc", ["a", "b", "c"]), ("b,c", ["b", "c"]),
                                            ("ba", ["a", "b"])])
def test_targets(text, targets):
    assert list(httpdiff.iter_targets(text)) == targets


@pytest.mark.parametrize("text", ["ac", "a", "abd", ""])
def test_targets_must_form_a_pair(text):
    with pytest.raises(ValueError):
        httpdiff.iter_targets(text)


# --------------------------------------------------------------------------- #
# servers                                                                      #
# --------------------------------------------------------------------------- #


def test_read_only_dsn_adds_the_option_once_and_keeps_others():
    from psycopg.conninfo import conninfo_to_dict

    url = "postgresql://u:p%40ss@localhost:5434/atus?options=-c%20jit%3Doff&sslmode=disable"
    params = conninfo_to_dict(servers.read_only_dsn(url))
    assert params["options"] == "-c jit=off -c default_transaction_read_only=on"
    assert params["password"] == "p@ss" and params["sslmode"] == "disable"
    again = conninfo_to_dict(servers.read_only_dsn(servers.read_only_dsn(url)))
    assert again["options"] == params["options"]


def test_log_secrets_are_the_real_passwords_only():
    assert servers.dsn_secrets("postgresql://atus_api:s3cr3t-value@h:5466/atus") == (
        "s3cr3t-value",)
    assert servers.dsn_secrets("postgresql://atus:atus@localhost:5434/atus") == ()
    assert servers.dsn_secrets("postgresql://u@h/db") == ()


def test_children_get_a_clean_environment(monkeypatch):
    monkeypatch.setenv("ATUS_EDGE_SECRETS", "inherited-secret")
    monkeypatch.setenv("PGPASSWORD", "inherited")
    env = servers.clean_env({"ATUS_DATABASE_URL": "x"})
    assert "ATUS_EDGE_SECRETS" not in env and "PGPASSWORD" not in env
    assert env["ATUS_DATABASE_URL"] == "x" and env["PATH"] == os.environ["PATH"]


def test_phase5_servers_run_without_edge_auth_and_with_generous_limits():
    env = servers.phase5_env("postgresql://u:p@h:1/db", 8642, build_sha="abc123")
    settings = load_settings(env)          # the real API's validation
    check_api_settings(settings)
    assert not settings.is_production and not settings.edge_auth_enabled
    assert settings.ops_token is None and settings.expected_data_version is None
    assert settings.api_daily_analysis_cap >= 1_000_000
    assert settings.api_max_inflight >= 2 and settings.api_max_inflight_heavy >= 1
    assert settings.api_rate_analysis.limit >= 1_000_000
    assert settings.api_statement_timeout_ms >= 600_000
    assert settings.api_max_body_bytes == servers.DEFAULT_MAX_BODY_BYTES
    assert settings.port == 8642 and settings.build_sha == "abc123"


def test_phase5_env_never_enables_production():
    env = servers.phase5_env("postgresql://u:p@h:1/db", 1)
    env["ATUS_ENV"] = "production"
    with pytest.raises(ConfigError):
        check_api_settings(load_settings(env))   # no edge secret: refused, as designed


def test_server_commands_use_each_code_bases_entry_point():
    assert servers.phase5_command("py", 8642) == [
        "py", "-m", "atus_pipeline", "api", "--host", "127.0.0.1", "--port", "8642"]
    assert servers.baseline_command("py", 8641)[:3] == ["py", "-m", "atus_pipeline.cli"]
    assert servers.baseline_env("dsn") == {"ATUS_DATABASE_URL": "dsn",
                                           "ATUS_LOG_LEVEL": "WARNING"}


def test_stray_dotenv_files_are_found_in_parents(tmp_path):
    child = tmp_path / "x" / "y"
    child.mkdir(parents=True)
    assert tmp_path / ".env" not in servers.stray_dotenv([child])
    (tmp_path / ".env").write_text("ATUS_EDGE_SECRETS=x\n")
    assert tmp_path / ".env" in servers.stray_dotenv([child])


def test_port_is_free():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert not servers.port_is_free(port)
    assert servers.port_is_free(port)


FAKE_SERVER = """
import http.server, sys
print("connecting with postgresql://atus_api:s3cr3t-value@h:5466/atus", flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status": "ok"}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(tmp_path, command, port) -> servers.ServerConfig:
    return servers.ServerConfig(
        key="b", label="fake", command=command, cwd=tmp_path / "run", env=servers.clean_env({}),
        port=port, log_path=tmp_path / "logs" / "b.log", secrets=("s3cr3t-value",))


def test_a_server_starts_answers_and_stops_with_a_redacted_log(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(FAKE_SERVER)
    port = _free_port()
    group = servers.ServerGroup([_config(tmp_path, [sys.executable, str(script), str(port)],
                                         port)])
    with group:
        server = group.servers["b"]
        assert server.alive()
        assert servers.http_get(port, "/health")[0] == 200
    assert not server.alive()
    log = (tmp_path / "logs" / "b.log").read_text()
    assert "s3cr3t-value" not in log and "atus_api:***@h:5466" in log


def test_a_server_that_exits_is_reported_with_its_log(tmp_path):
    port = _free_port()
    command = [sys.executable, "-c",
               "print('boom postgresql://u:s3cr3t-value@h/db'); raise SystemExit(3)"]
    with pytest.raises(servers.ServerError) as raised, servers.ServerGroup(
            [_config(tmp_path, command, port)]):
        pass
    assert "exited during start-up" in str(raised.value) and "boom" in str(raised.value)
    assert "s3cr3t-value" not in str(raised.value)


def test_a_busy_port_is_refused(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        with pytest.raises(servers.ServerError, match="already in use"):
            servers.ManagedServer(_config(tmp_path, [sys.executable, "-c", "pass"], port)
                                  ).start()


# --------------------------------------------------------------------------- #
# replay inputs and HTTP                                                       #
# --------------------------------------------------------------------------- #


def test_target_a_needs_a_baseline_checkout(tmp_path, capsys):
    with pytest.raises(SystemExit):
        replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path), "--targets", "ab"])
    assert "--baseline-dir" in capsys.readouterr().err
    args = replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path),
                              "--baseline-dir", str(tmp_path), "--ports", "9001,9002,9003"])
    assert args.targets == ["a", "b", "c"]
    assert args.port_of == {"a": 9001, "b": 9002, "c": 9003}
    assert args.baseline_python == tmp_path / ".venv" / "bin" / "python"


@pytest.mark.parametrize("ports", ["1,2", "1,1,2", "a,b,c", "0,1,2"])
def test_ports_are_validated(tmp_path, ports):
    with pytest.raises(SystemExit):
        replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path), "--targets", "bc",
                           "--ports", ports])


def test_the_serving_url_prefers_the_api_role(tmp_path, monkeypatch):
    monkeypatch.delenv(replay.SERVING_URL_ENV, raising=False)
    monkeypatch.setenv(replay.FULL_URL_ENV, "postgresql://atus:atus@localhost:5434/atus")
    secrets_file = tmp_path / "neon.env"
    secrets_file.write_text("ATUS_OWNER_DATABASE_URL=postgresql://owner:o-secret@h/atus\n"
                            "ATUS_API_DATABASE_URL='postgresql://atus_api:a-secret@h/atus'\n")
    args = replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path), "--targets",
                              "bc", "--serving-secrets-file", str(secrets_file)])
    urls = replay.database_urls(args)
    assert urls["c"] == ("postgresql://atus_api:a-secret@h/atus",
                         "neon.env:ATUS_API_DATABASE_URL")
    assert urls["b"][1] == f"${replay.FULL_URL_ENV}"
    secrets_file.write_text("ATUS_OWNER_DATABASE_URL=postgresql://owner:o-secret@h/atus\n")
    assert replay.database_urls(args)["c"][1] == "neon.env:ATUS_OWNER_DATABASE_URL"
    secrets_file.write_text("OTHER=1\n")
    with pytest.raises(replay.SetupError) as raised:
        replay.database_urls(args)
    assert "secret" not in str(raised.value)


def test_the_serving_url_can_come_from_the_environment(tmp_path, monkeypatch):
    args = replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path),
                              "--targets", "bc"])
    monkeypatch.delenv(replay.SERVING_URL_ENV, raising=False)
    with pytest.raises(replay.SetupError, match="needs --serving-secrets-file"):
        replay.database_urls(args)
    monkeypatch.setenv(replay.SERVING_URL_ENV, "postgresql://u:p@h/db")
    assert replay.database_urls(args)["c"][1] == f"${replay.SERVING_URL_ENV}"


class _Flaky(BaseHTTPRequestHandler):
    answers: list[tuple[int, dict, bytes]] = []
    seen: list[tuple[str, str, bytes, str | None]] = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).seen.append((self.command, self.path, body,
                                self.headers.get("Content-Type")))
        status, headers, payload = type(self).answers.pop(0)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def flaky_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Flaky.answers, _Flaky.seen = [], []
    yield server.server_address[1]
    server.shutdown()


class _Alive:
    def __init__(self, port):
        self.port = port

    def alive(self):
        return True


def test_capacity_answers_are_retried_then_kept(flaky_api, monkeypatch):
    sleeps = []
    monkeypatch.setattr(replay.time, "sleep", sleeps.append)
    busy = (503, {"Retry-After": "7", "Content-Type": "application/json"},
            json.dumps(envelope("overloaded", "busy")).encode())
    _Flaky.answers = [busy, (200, {"X-Data-Version": "v"}, b'{"ok": true}')]
    request = httpdiff.Spec("s", "estimate", {"years": [2024]}, "c", "ok", "f:1").request(65536)
    answer = replay.send_with_retries(_Alive(flaky_api), request, retries=3, timeout=5)
    assert answer.status == 200 and answer.attempts == 2 and answer.payload == {"ok": True}
    assert answer.headers["x-data-version"] == "v"
    assert sleeps == [7.0]
    method, path, body, content_type = _Flaky.seen[-1]
    assert (method, path, body, content_type) == (
        "POST", ESTIMATE, b'{"years":[2024]}', "application/json")
    _Flaky.answers = [busy] * 3
    answer = replay.send_with_retries(_Alive(flaky_api), request, retries=2, timeout=5)
    assert answer.status == 503 and answer.attempts == 3


def test_a_lost_server_is_an_answer_not_an_exception(monkeypatch):
    class Dead:
        port = _free_port()

        def alive(self):
            return False

    answer = replay.send_with_retries(Dead(), RequestInfo("GET", "/health"), retries=3,
                                      timeout=1)
    assert answer.status is None and answer.transport_error == "server process exited"
    refused = replay.send(_free_port(), RequestInfo("GET", "/health"), timeout=1)
    assert refused.status is None and "Connection" in refused.transport_error


def test_saved_responses_have_safe_unique_names(tmp_path):
    answers = {"b": ok({"a": 1}), "c": response(502, b"<html>")}
    replay.save_responses(tmp_path, "../weird id/1", answers)
    replay.save_responses(tmp_path, "../weird id?1", answers)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 4 and all("/" not in f and not f.startswith(".") for f in files)
    saved = json.loads((tmp_path / files[0]).read_text())
    assert saved["status"] == 200 and saved["body"] == {"a": 1}


# --------------------------------------------------------------------------- #
# server configuration and start-up checks                                     #
# --------------------------------------------------------------------------- #

SERVING_URL = "postgresql://atus_api:s3cr3t-value@localhost:5466/atus"


def _urls(targets: str) -> dict:
    full = ("postgresql://atus:atus@localhost:5434/atus", "development default")
    return {k: (SERVING_URL, "$X") if k == "c" else full for k in targets}


def test_server_configs_are_read_only_loopback_and_secret_free_in_argv(tmp_path):
    from psycopg.conninfo import conninfo_to_dict

    args = replay.parse_args(["--specs", str(tmp_path), "--out", str(tmp_path), "--targets",
                              "bc", "--ports", "9001,9002,9003"])
    configs = replay.server_configs(args, _urls("bc"), tmp_path / "out", "abc123")
    assert [c.key for c in configs] == ["b", "c"]
    serving = configs[1]
    assert serving.port == 9003 and serving.cwd == tmp_path / "out" / "run" / "c"
    assert serving.command == servers.phase5_command(str(args.phase5_python), 9003)
    assert "s3cr3t-value" not in " ".join(serving.command)
    params = conninfo_to_dict(serving.env["ATUS_DATABASE_URL"])
    assert params["user"] == "atus_api" and "default_transaction_read_only=on" in params["options"]
    assert serving.secrets == ("s3cr3t-value",) and serving.package_dir is None
    assert serving.env["ATUS_BUILD_SHA"] == "abc123" and "ATUS_EDGE_SECRETS" not in serving.env
    replay.check_setup(args, configs)
    (tmp_path / "out" / ".env").write_text("ATUS_EDGE_SECRETS=x\n")
    with pytest.raises(replay.SetupError, match=r"\.env file would configure"):
        replay.check_setup(args, configs)


def test_setup_refuses_a_missing_interpreter_or_a_wrong_baseline(tmp_path):
    baseline = tmp_path / "baseline"
    (baseline / "src" / "atus_pipeline").mkdir(parents=True)
    (baseline / "src" / "atus_pipeline" / "cli.py").write_text("")
    common = ["--specs", str(tmp_path), "--out", str(tmp_path / "out"),
              "--baseline-dir", str(baseline)]
    args = replay.parse_args(common)
    configs = replay.server_configs(args, _urls("abc"), tmp_path / "out", "x")
    assert configs[0].command[:3] == [str(baseline / ".venv" / "bin" / "python"), "-m",
                                      "atus_pipeline.cli"]
    assert configs[0].env == servers.clean_env(servers.baseline_env(
        configs[0].env["ATUS_DATABASE_URL"]))
    with pytest.raises(replay.SetupError, match="interpreter .* does not exist"):
        replay.check_setup(args, configs)
    args = replay.parse_args(common + ["--baseline-python", sys.executable])
    configs = replay.server_configs(args, _urls("abc"), tmp_path / "out", "x")
    with pytest.raises(replay.SetupError, match="not 387fc2d"):
        replay.check_setup(args, configs)      # not a git checkout of the baseline
    args = replay.parse_args(common + ["--baseline-python", sys.executable,
                                       "--baseline-commit", ""])
    replay.check_setup(args, configs)
    (baseline / ".env").write_text("ATUS_DATABASE_URL=elsewhere\n")
    with pytest.raises(replay.SetupError, match=r"baseline/\.env"):
        replay.check_setup(args, configs)


# --------------------------------------------------------------------------- #
# generated specs                                                              #
# --------------------------------------------------------------------------- #


def test_generated_specs_are_valid_api_requests(tmp_path):
    import make_specs

    from atus_pipeline.api.schemas.analysis import CompareRequest, EstimateRequest, TrendRequest

    years = [2018, 2019, 2020, 2021, 2023]
    tier1 = ["01", "02", "12", "18"]
    leaves = ["010101", "010102", "020101", "120303", "120304", "180101"]
    lines = make_specs.build(years, tier1, leaves, n=60, seed=7)
    assert lines == make_specs.build(years, tier1, leaves, n=60, seed=7)
    seeds = [line for line in lines if line["category"] == "seed-extreme"]
    assert {line["note"] for line in seeds if line["expect"] == "error"} == {
        "empty population", "unknown code"}
    random_lines = [line for line in lines if line["category"].startswith("random-")]
    assert len(random_lines) == 60 and {line["expect"] for line in random_lines} == {"any"}
    models = {"estimate": EstimateRequest, "trend": TrendRequest, "compare": CompareRequest}
    for line in lines:
        models[line["endpoint"]].model_validate(line["body"])   # the API accepts it
    (tmp_path / "gen.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    specs, _ = httpdiff.load_specs(tmp_path)
    assert len(specs) == len(lines)
    compare = next(s for s in specs if s.endpoint == "compare")
    assert {"group_a", "group_b", "measure", "years"} <= set(compare.body)


def test_any_expectation_is_never_a_miss():
    spec = httpdiff.Spec("g", "estimate", {}, "random-estimate", "any", "f:1")
    for answer in (ok(estimate_body()), response(422, envelope("insufficient_data", "x"))):
        entry = httpdiff.evaluate_spec(spec, {"b": answer, "c": answer})
        assert entry["expect_met"] is None and entry["passed"]
