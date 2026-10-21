"""``RESPONSE_FORMAT`` changes whenever analysis response bytes change.

Edge KV entries are keyed on the data version, ``ANALYTICS_VERSION`` and
``RESPONSE_FORMAT``, never on the code's commit, so they survive code-only
deploys for up to 30 days (ADR-015, "Staleness across releases"). A code
change that alters the bytes of an analysis response under the same pair of
versions would serve old-format bytes from KV next to new ones from the API.
Nothing tied the manual constant to the bytes (review r1,
response-format-bump-unenforced).

This test pins, for each ``(RESPONSE_FORMAT, ANALYTICS_VERSION)``, a
fingerprint of the OpenAPI schemas of the three analysis responses (with
every schema they reference) and of the exact bytes the API serves for one
known result per operation (field names and order, number formatting, null
handling, warnings and labels as serialized). If it fails:

1. the analysis response bytes changed. Bump ``RESPONSE_FORMAT`` in
   ``src/atus_pipeline/api/__init__.py`` (and the Worker's ``RESPONSE_FORMAT``
   var with it: ``edge/wrangler.jsonc``, ``deploy/stack/smoke_probe.py``), or
   bump ``ANALYTICS_VERSION`` if the statistics changed;
2. add the printed fingerprint under the new key. Never edit an existing
   entry: that is exactly the silent change this test exists to catch.

Engine-produced text (warnings, labels) changes the bytes too; ADR-015 asks
for a bump on any change to analysis response bytes.
"""

from __future__ import annotations

import hashlib
import json

from atus_pipeline.analytics import ANALYTICS_VERSION
from atus_pipeline.analytics.results import (
    ComparisonResult,
    EstimateValue,
    TrendPoint,
    TrendResult,
)
from atus_pipeline.api import RESPONSE_FORMAT
from tests.unit.api.test_contract import known_estimate_result

PINNED: dict[tuple[str, str], str] = {
    ("1", "0.2"): "efdb9f5a27f976b98fc63a2ba408263db82b62a75c029ce574e31fb3854e9b23",
}

ANALYSIS_RESPONSES = ("EstimateResponse", "TrendResponse", "CompareResponse")
BODY = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}


def _known_trend() -> TrendResult:
    return TrendResult(
        measure="average_minutes_per_day",
        points=(
            TrendPoint(
                year=2019,
                estimate=EstimateValue(value=120.0, unit="minutes_per_day",
                                       standard_error=1.5, confidence_level=0.95,
                                       ci_lower=117.06, ci_upper=122.94),
                n_respondents=1, n_participants=1, weighted_population_per_day=3 / 312,
            ),
            TrendPoint(
                year=2020, estimate=None, n_respondents=None, n_participants=None,
                weighted_population_per_day=None,
                unavailable_reason="TUFNWGTP is undefined for 2020",
            ),
        ),
        activity=known_estimate_result().activity,
        population="civilian noninstitutional population age 15+",
        weight=known_estimate_result().weight,
        variance_method="replicate",
        analytics_version="0.2",
        spec={"measure": "average_minutes_per_day"},
        warnings=("a methodological note",),
    )


def _known_comparison() -> ComparisonResult:
    group = known_estimate_result()
    return ComparisonResult(
        measure="average_minutes_per_day",
        label_a="men", label_b="women",
        group_a=group, group_b=group,
        difference=EstimateValue(
            value=33.33, unit="minutes_per_day", standard_error=11.79,
            confidence_level=0.95, ci_lower=10.2, ci_upper=56.4,
        ),
        analytics_version="0.2",
        warnings=("difference SE uses per-replicate differences",),
    )


def _referenced(schemas: dict, name: str, seen: dict) -> None:
    if name in seen:
        return
    seen[name] = schemas[name]
    text = json.dumps(schemas[name])
    for ref in sorted(set(part.split('"')[0] for part in
                          text.split('"$ref": "#/components/schemas/')[1:])):
        _referenced(schemas, ref, seen)


def response_fingerprint(client, stub_engine) -> tuple[str, dict]:
    stub_engine.estimate_behavior = lambda spec: known_estimate_result()
    stub_engine.trend_behavior = lambda spec: _known_trend()
    stub_engine.compare_behavior = lambda comparison: _known_comparison()
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    pinned_schemas: dict = {}
    for name in ANALYSIS_RESPONSES:
        _referenced(schemas, name, pinned_schemas)
    bodies = {}
    for operation, body in (
        ("estimate", BODY),
        ("trend", {**BODY, "years": [2019, 2020]}),
        ("compare", {**BODY, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
    ):
        response = client.post(f"/api/v1/analysis/{operation}", json=body)
        assert response.status_code == 200, response.text
        assert response.headers["X-Response-Format"] == RESPONSE_FORMAT
        bodies[operation] = response.content.decode()
    material = {"schemas": pinned_schemas, "bodies": bodies}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, material


def test_analysis_response_bytes_are_pinned_to_the_response_format(client, stub_engine):
    digest, _ = response_fingerprint(client, stub_engine)
    key = (RESPONSE_FORMAT, ANALYTICS_VERSION)
    assert key in PINNED, (
        f"no fingerprint for RESPONSE_FORMAT={RESPONSE_FORMAT!r}, ANALYTICS_VERSION="
        f"{ANALYTICS_VERSION!r}: add {key!r}: {digest!r} to PINNED (see the module docstring)"
    )
    assert PINNED[key] == digest, (
        "The bytes of an analysis response changed under the same RESPONSE_FORMAT and "
        "ANALYTICS_VERSION. Edge KV entries survive code-only deploys (ADR-015): bump "
        "RESPONSE_FORMAT (src/atus_pipeline/api/__init__.py and the Worker's var) and add "
        f"the new fingerprint {digest!r} under the new key; never edit an existing entry."
    )


def test_every_pin_names_a_format_and_an_analytics_version():
    for (response_format, analytics_version), digest in PINNED.items():
        assert response_format.isdigit() and analytics_version
        assert len(digest) == 64 and int(digest, 16) >= 0
