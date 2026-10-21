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
every schema they reference; their documentation keywords, which never
reach the bytes, are left out: ``DOC_KEYWORDS``) and of the exact bytes the
API serves for one known result per operation (field names and order, number
formatting, null handling, warnings and labels as serialized). If it fails:

1. the analysis response bytes changed. Bump ``RESPONSE_FORMAT`` in every
   file of ``BUMP_FILES`` (the constant in ``src/atus_pipeline/api/__init__.py``,
   the Worker's default var in ``edge/wrangler.jsonc`` and its config test;
   the snapshot build, the stack smoke and the deploy follow the constant), or
   bump ``ANALYTICS_VERSION`` if the statistics changed;
2. add the printed fingerprint under the new key. Never edit an existing
   entry: that is exactly the silent change this test exists to catch.

Engine-produced text (warnings, labels) changes the bytes too; ADR-015 asks
for a bump on any change to analysis response bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

from atus_pipeline.analytics import ANALYTICS_VERSION
from atus_pipeline.analytics.results import (
    ComparisonResult,
    EstimateValue,
    TrendPoint,
    TrendResult,
)
from atus_pipeline.api import RESPONSE_FORMAT
from tests.unit.api.test_contract import known_estimate_result

ROOT = Path(__file__).resolve().parents[3]
# Every file a RESPONSE_FORMAT bump edits; everything else follows the constant.
BUMP_FILES = (
    "src/atus_pipeline/api/__init__.py",
    "edge/wrangler.jsonc",
    "edge/test/node/config.test.ts",
)
BUMP_INSTRUCTIONS = (
    f"bump RESPONSE_FORMAT in {', '.join(BUMP_FILES)} and add the new fingerprint under "
    "the new key; never edit an existing entry"
)

PINNED: dict[tuple[str, str], str] = {
    ("1", "0.2"): "017328a7f8aeecbc48b759b2af6c601b4d8e6c359c0990c44ba7588569fa1645",
}

ANALYSIS_RESPONSES = ("EstimateResponse", "TrendResponse", "CompareResponse")
DOC_KEYWORDS = frozenset({"description", "title", "examples", "example"})
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


def _structure(node, names: bool = False):
    """A schema without its documentation keywords: editing a field's
    description changes no response byte and needs no bump. ``names`` marks
    a ``properties`` mapping, whose keys are field names, never keywords."""
    if isinstance(node, dict):
        return {key: _structure(value, names=key == "properties" and not names)
                for key, value in node.items() if names or key not in DOC_KEYWORDS}
    if isinstance(node, list):
        return [_structure(value) for value in node]
    return node


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
    material = {"schemas": _structure(pinned_schemas), "bodies": bodies}
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
        "ANALYTICS_VERSION. Edge KV entries survive code-only deploys (ADR-015): "
        f"{BUMP_INSTRUCTIONS}. New fingerprint: {digest!r}."
    )


def _script(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_every_copy_of_the_response_format_follows_the_constant():
    """response-format-bump-breaks-snapshot (review round 2): build_snapshot.py's
    ``--response-format`` default and smoke_probe.py's constant hard-coded "1", and the
    bump instructions named neither, so a bump done as documented failed the snapshot
    build (deploy.yml, smoke_stack.sh --edge) and the stack smokes. The scripts now
    follow the constant; the copies a bump must edit are the ``BUMP_FILES``."""
    build_snapshot = _script("scripts/build_snapshot.py", "build_snapshot_rf")
    args = build_snapshot.parse_args(["--api-base", "http://127.0.0.1:1", "--out", "x"])
    assert args.response_format is None          # the format the API reports
    probe = _script("deploy/stack/smoke_probe.py", "smoke_probe_rf")
    assert not hasattr(probe, "RESPONSE_FORMAT")  # smoke_stack.sh passes the constant
    wrangler = (ROOT / "edge" / "wrangler.jsonc").read_text()
    (default,) = re.findall(r'"RESPONSE_FORMAT":\s*"([^"]*)"', wrangler)
    assert default == RESPONSE_FORMAT, f"edge/wrangler.jsonc: {BUMP_INSTRUCTIONS}"
    config_test = (ROOT / "edge" / "test" / "node" / "config.test.ts").read_text()
    (expected,) = re.findall(r"RESPONSE_FORMAT: '([^']*)'", config_test)
    assert expected == RESPONSE_FORMAT, f"edge/test/node/config.test.ts: {BUMP_INSTRUCTIONS}"


def test_the_bump_instructions_name_every_file_a_bump_edits():
    constant = (ROOT / "src" / "atus_pipeline" / "api" / "__init__.py").read_text()
    docstring = " ".join(constant[constant.index("RESPONSE_FORMAT = "):].split())
    module = " ".join((__doc__ or "").split())
    for path in BUMP_FILES:
        assert path in BUMP_INSTRUCTIONS
        assert path in docstring or path.endswith("api/__init__.py"), path
    assert "BUMP_FILES" in module
    api_doc = " ".join((ROOT / "docs" / "api.md").read_text().split())
    assert "edge/wrangler.jsonc" in api_doc


_FORMAT_KEY = r"""["'](?:[xX]-[rR]esponse-[fF]ormat|response_format)["']"""
#: A test comparing a reported format (a header or a snapshot field) with a literal.
REPORTED_FORMAT_LITERAL = re.compile(
    rf"""(?:\[\s*{_FORMAT_KEY}\s*\]|\.get\(\s*{_FORMAT_KEY}\s*\))\s*==\s*["'](?P<value>[^"']*)["']""")


def _fakes_the_format(text: str, value: str) -> bool:
    """Whether ``text`` sets up a fake that reports ``value`` itself (a header or a
    field ``<key>: "<value>"``): its comparisons then check the fake, not the API."""
    return re.search(rf"""{_FORMAT_KEY}\s*:\s*["']{re.escape(value)}["']""", text) is not None


def test_a_bump_edits_no_test_outside_the_bump_files():
    """response-format-bump-breaks-snapshot, verified in review round 2 by doing a bump as
    instructed (the BUMP_FILES and a new fingerprint): CI still failed outside them.
    tests/unit/api/test_guard.py and tests/integration/test_build_snapshot_api.py compared
    the real API's format with a literal "1", and 14 edge worker tests inherited
    wrangler.jsonc's default although their fixtures (origin headers, KV metadata, cache
    scopes computed independently) are written for "1". Python tests compare the API's
    format with the constant; the worker tests pin the release var they are written for
    in edge/vitest.config.ts, as the deploy's --var does."""
    copies = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in REPORTED_FORMAT_LITERAL.finditer(text):
            if match["value"] == RESPONSE_FORMAT and not _fakes_the_format(text, match["value"]):
                copies.append(f"{path.relative_to(ROOT)}: {match[0]}")
    assert copies == [], f"compare with atus_pipeline.api.RESPONSE_FORMAT, not a copy: {copies}"
    vitest = (ROOT / "edge" / "vitest.config.ts").read_text(encoding="utf-8")
    (pinned,) = re.findall(r"const TEST_RELEASE = \{ RESPONSE_FORMAT: '([^']*)' \}", vitest)
    assert pinned.isdigit(), pinned
    assert "bindings: { ...TEST_SECRETS, ...TEST_RELEASE }" in vitest
    # The scan itself: a real-API comparison is caught, a fake's own value is not.
    sample = f'assert response.headers["X-Response-Format"] == "{RESPONSE_FORMAT}"'
    assert [m["value"] for m in REPORTED_FORMAT_LITERAL.finditer(sample)] == [RESPONSE_FORMAT]
    assert not _fakes_the_format(sample, RESPONSE_FORMAT)
    assert _fakes_the_format(f'analysis(**{{"X-Response-Format": "{RESPONSE_FORMAT}"}})',
                             RESPONSE_FORMAT)


def test_every_pin_names_a_format_and_an_analytics_version():
    for (response_format, analytics_version), digest in PINNED.items():
        assert response_format.isdigit() and analytics_version
        assert len(digest) == 64 and int(digest, 16) >= 0
