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

Engine-produced text (warnings, notes, labels, population descriptions,
unavailable reasons) changes the bytes too, and the known results above are
hand-built, so none of it reaches that fingerprint. ``ENGINE_PINNED`` pins
it (review r4, response-format-pin-misses-engine-output): the real
``AnalysisEngine`` answers ``ENGINE_REQUESTS`` through the API on synthetic
sufficient statistics (``SyntheticStatistics``, no database), reaching every
warning branch, every generated label and description form and both trend
unavailable reasons. Its fingerprint covers every string, key and integer
of those responses exactly, and every float to 12 significant digits (the
stub fingerprint above pins number formatting; 12 digits keep a libm's last
bit in ``NormalDist.inv_cdf`` out of it while catching any estimator change).
If it fails, the same two steps apply: bump ``RESPONSE_FORMAT`` for a text
change, ``ANALYTICS_VERSION`` for a number change, and add the new
fingerprint under the new key.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import ANALYTICS_VERSION, engine, variance
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.results import (
    ComparisonResult,
    EstimateValue,
    TrendPoint,
    TrendResult,
)
from atus_pipeline.api import RESPONSE_FORMAT, observability
from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_database
from tests.unit.api.conftest import (
    FAKE_LEXICON,
    FakeDatabase,
    FakeVersionConnection,
    make_settings,
)
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


# --------------------------------------------------------------------------- #
# the real engine's output (review r4)                                         #
# --------------------------------------------------------------------------- #

ENGINE_PINNED: dict[tuple[str, str], str] = {
    ("1", "0.2"): "7ed90b948a92a6663a939b656b0959af22b8f08f9ddccfe30cf61ea651778c1f",
}

_ALL_DIMENSIONS = {
    "age_min": 80, "sex": "female", "employment_status": "not_in_labor_force",
    "has_household_children": False, "region": 3, "state_fips": "06",
    "education_level": "high_school", "day_type": "weekend",
}
#: Requests reaching every warning and note the engine and population.py add
#: (harmonization, pandemic, pooled, CI truncation, covariance, age topcode,
#: education, day type), every form of the activity label (preset, a given
#: label, a single leaf's name, "<n> activity codes") and of the population
#: description, both trend unavailable reasons, both weight schemes, every
#: measure, and both variance methods. Year 2005 has no respondents in
#: ``SyntheticStatistics``.
ENGINE_REQUESTS: tuple[tuple[str, dict], ...] = (
    ("estimate", {"activity": {"preset": "sleep"}, "years": [2023]}),
    ("estimate", {"activity": {"preset": "sleep"}, "years": [2023],
                  "measure": "participation_rate"}),
    ("estimate", {"activity": {"include": ["0101"]}, "years": [2021, 2022, 2023],
                  "measure": "average_minutes_per_participant",
                  "population": _ALL_DIMENSIONS}),
    ("estimate", {"activity": {"include": ["12"]}, "years": [2019, 2020],
                  "weights": "pandemic", "measure": "participants_per_day",
                  "variance": "none",
                  "population": {"age_max": 64, "diary_date_min": "2019-06-01",
                                 "diary_date_max": "2020-10-31"}}),
    ("estimate", {"activity": {"include": ["01"], "exclude": ["010102"], "label": "Own"},
                  "years": [2019], "confidence_level": 0.9,
                  "population": {"age_min": 25, "has_household_children": True,
                                 "diary_date_min": "2019-03-01"}}),
    ("estimate", {"activity": {"include": ["01"], "exclude": ["010102"]}, "years": [2018],
                  "population": {"diary_date_max": "2018-06-30"}}),
    ("trend", {"activity": {"preset": "sleep"}, "years": [2004, 2005, 2019, 2020, 2021],
               "population": {"day_type": "weekday"}}),
    ("trend", {"activity": {"preset": "sleep"}, "years": [2019, 2020],
               "weights": "pandemic", "measure": "participation_rate"}),
    ("compare", {"activity": {"preset": "sleep"}, "years": [2021, 2022, 2023],
                 "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
    ("compare", {"activity": {"include": ["120303"]}, "years": [2023], "variance": "none",
                 "population": {"region": 1},
                 "group_a": {"employment_status": "employed"},
                 "group_b": {"employment_status": "unemployed"},
                 "label_a": "Employed", "label_b": "Unemployed"}),
)
_EMPTY_YEARS = frozenset({2005})


class _Rows(list):
    def fetchall(self):
        return list(self)


class _LexiconConnection:
    """Answers the three lexicon reads of ``ActivityResolver`` from
    ``FAKE_LEXICON`` (the API builds its resolver with the data snapshot)."""

    def execute(self, sql, params=None):
        level = {"activity_tier1": 1, "activity_tier2": 2, "activity_codes": 3}[
            re.search(r"atus\.(\w+)", sql)[1]
        ]
        return _Rows((e.code, e.name) if level == 3 else (e.code,)
                     for e in FAKE_LEXICON if e.level == level)


class SyntheticStatistics(FakeVersionConnection):
    """Answers the engine's sufficient-statistics query with rows computed
    from its parameters (deterministic, no database): numeric sums as
    ``Decimal`` and 160 replicate sums per array, as PostgreSQL returns them.
    Sleep codes participate near-universally, so a participation rate's CI
    crosses 1 and is truncated."""

    def execute(self, sql, params=None):
        if not params or "years" not in params:
            return super().execute(sql, params)
        observability.note_statement()
        with_replicates = "rep_sum_w" in sql
        return [self._row(year, params, with_replicates)
                for year in params["years"] if year not in _EMPTY_YEARS]

    @staticmethod
    def _row(year: int, params: dict, with_replicates: bool) -> tuple:
        others = {k: v for k, v in params.items() if k != "years"}
        seed_text = json.dumps([year, others], sort_keys=True, default=str)
        h = int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16)
        universal = all(code.startswith("0101") for code in params["activity_codes"])
        n = 120 + h % 60
        n_part = n - 1 if universal else n // 2 + h % 10
        sum_w = Decimal(1_000_000 + h % 100_000) + Decimal("0.125")
        share = Decimal("0.9995") if universal else Decimal("0.5") + Decimal(h % 100) / 1000
        minutes = Decimal(400 + h % 120) if universal else Decimal(30 + h % 90)
        sum_wp = (sum_w * share).quantize(Decimal("0.000001"))
        sum_wx = (sum_w * minutes).quantize(Decimal("0.000001"))
        row = (year, n, n_part, sum_w, sum_wx, sum_wp)
        if not with_replicates:
            return row
        return row + tuple(
            [float(total) * (1 + ((i * step + h) % mod - mod // 2) * 1e-3) for i in range(160)]
            for total, step, mod in ((sum_w, 7, 13), (sum_wx, 11, 17), (sum_wp, 5, 19))
        )


def _canonical(node):
    """Keys in order, strings, integers, booleans and nulls as they are; each
    float to 12 significant digits (module docstring)."""
    if isinstance(node, dict):
        return [[key, _canonical(value)] for key, value in node.items()]
    if isinstance(node, list):
        return [_canonical(value) for value in node]
    if isinstance(node, float):
        return f"<float {node:.12g}>"
    return node


def engine_fingerprint(tmp_path) -> tuple[str, list]:
    resolver = ActivityResolver(_LexiconConnection(), release="0325")
    database = FakeDatabase(SyntheticStatistics, resolver=resolver)
    app = create_app(make_settings(tmp_path), open_pool=False)  # the real engine
    app.dependency_overrides[get_database] = lambda: database
    material = []
    with TestClient(app, raise_server_exceptions=False) as client:
        for operation, body in ENGINE_REQUESTS:
            response = client.post(f"/api/v1/analysis/{operation}", json=body)
            assert response.status_code == 200, (operation, body, response.text)
            assert response.headers["X-Cache"] == "miss"
            assert response.headers["X-Response-Format"] == RESPONSE_FORMAT
            material.append([operation, _canonical(response.json())])
    digest = hashlib.sha256(
        json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return digest, material


def test_engine_output_is_pinned_to_the_response_and_analytics_versions(tmp_path):
    digest, _ = engine_fingerprint(tmp_path)
    key = (RESPONSE_FORMAT, ANALYTICS_VERSION)
    assert key in ENGINE_PINNED, (
        f"no engine fingerprint for {key!r}: add {key!r}: {digest!r} to ENGINE_PINNED "
        "(see the module docstring)"
    )
    assert ENGINE_PINNED[key] == digest, (
        "The real engine's analysis responses changed under the same RESPONSE_FORMAT and "
        "ANALYTICS_VERSION (warning, note, label or description text, or a number). Edge KV "
        "entries survive code-only deploys (ADR-015): for a text change, "
        f"{BUMP_INSTRUCTIONS}; for a number change, bump ANALYTICS_VERSION. New engine "
        f"fingerprint: {digest!r}."
    )


@pytest.mark.parametrize("ulps", [-1, 1])
def test_the_engine_pin_does_not_depend_on_the_platform_libm(tmp_path, monkeypatch, ulps):
    """glibc's ``NormalDist().inv_cdf(0.975)`` is 1 ulp above macOS's
    (measured in review r4), so CI bounds differ in their last bit between
    platforms. Rounding to 12 digits must absorb that for every pinned value."""
    z_value = variance.z_value
    monkeypatch.setattr(
        variance, "z_value",
        lambda level: math.nextafter(z_value(level), math.copysign(math.inf, ulps)),
    )
    digest, _ = engine_fingerprint(tmp_path)
    assert digest == ENGINE_PINNED[(RESPONSE_FORMAT, ANALYTICS_VERSION)]


def test_the_engine_requests_reach_every_warning_and_label_form(tmp_path):
    """The pin is only as wide as its requests: each engine text it is meant
    to cover must appear in the responses it fingerprints."""
    _, material = engine_fingerprint(tmp_path)
    text = json.dumps(material, ensure_ascii=False)
    for needle in (
        engine._PANDEMIC_NOTE,
        engine.harmonization_note("2003-25"),
        "Pooled estimate: represents the average day across 3 survey years",
        "(1,095 days combined)",
        "Confidence interval truncated to [0, 1]",
        "per-replicate differences, which accounts for covariance",
        "Roster age is topcoded", "Education comes from the CPS interview",
        "Day-type estimates represent the average such day",
        "TUFNWGTP is undefined for 2020", "no respondents match the population filters",
        '"2 activity codes"', '"Sleeping"', '"Own"', '"Television and movies (not religious)"',
        "civilian noninstitutional population age 15+",
        "age 80-85+ (topcoded), sex=female, employment_status=not_in_labor_force, "
        "education_level=high_school, day_type=weekend, no household children under 18, "
        "Census region 3, state FIPS 06",
        "age 25-85+ (topcoded), household children under 18 present, "
        "diary dates 2019-03-01 to (end of period)",
        "age 15-64, diary dates 2019-06-01 to 2020-10-31",
        "diary dates (start) to 2018-06-30",
        "TU20FWGT", "respondents.pandemic_weight",
    ):
        assert needle in text, needle


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
    in edge/test/release.ts (bound by edge/vitest.config.ts), as the deploy's --var does."""
    copies = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in REPORTED_FORMAT_LITERAL.finditer(text):
            if match["value"] == RESPONSE_FORMAT and not _fakes_the_format(text, match["value"]):
                copies.append(f"{path.relative_to(ROOT)}: {match[0]}")
    assert copies == [], f"compare with atus_pipeline.api.RESPONSE_FORMAT, not a copy: {copies}"
    vitest = (ROOT / "edge" / "vitest.config.ts").read_text(encoding="utf-8")
    # The pin lives in edge/test/release.ts since review round 4, with the owner's
    # manual levers (epoch-prewake-levers-blocked-by-config-test).
    release = (ROOT / "edge" / "test" / "release.ts").read_text(encoding="utf-8")
    (pinned,) = re.findall(r"^  RESPONSE_FORMAT: '([^']*)',$", release, re.MULTILINE)
    assert pinned.isdigit(), pinned
    assert "import { TEST_RELEASE } from './test/release'" in vitest
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
