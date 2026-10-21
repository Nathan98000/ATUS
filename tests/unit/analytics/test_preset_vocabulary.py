"""Activity presets are pinned to the versions the edge cache is scoped by.

Edge KV keys are the exact request bytes, and the SPA, the starters and the
prewarm list name presets (``{"activity": {"preset": "watching_tv"}, ...}``).
``CACHE_SCOPE`` covers ``EPOCH``, ``API_VERSION``, ``ANALYTICS_VERSION``,
``DATA_VERSION`` and ``RESPONSE_FORMAT``, not the preset vocabulary or the
build (ADR-015, "Staleness across releases"). A code-only deploy that
redefined a preset would therefore keep serving the old definition's numbers
from KV for up to 30 days, next to the API's new ones for any other spelling
of the same request. Nothing tied ``ACTIVITY_PRESETS`` to a version (review
round 2, preset-definitions-outside-kv-scope); ``test_response_format.py``
pins bytes from a stub engine and cannot see it.

``PINNED`` holds, per ``(ANALYTICS_VERSION, RESPONSE_FORMAT)``, every preset's
include codes, exclude codes and label. If a test fails:

1. a preset's codes changed, or a preset was removed: the statistics a
   request naming it gets change. Bump ``ANALYTICS_VERSION``
   (``src/atus_pipeline/analytics/__init__.py`` and ``engine.py``) and record
   it in docs/analytics.md's version history;
2. only a label changed: the response bytes change. Bump ``RESPONSE_FORMAT``
   (``src/atus_pipeline/api/__init__.py`` and the Worker's var, as
   ``test_response_format.py`` says), or ``ANALYTICS_VERSION``;
3. add the whole table under the new key. Never edit an existing entry's
   presets: that is the silent change this test exists to catch.

A new preset name needs no bump (no cached request can name it yet): add it
to the current entry.
"""

from __future__ import annotations

from atus_pipeline.analytics import ANALYTICS_VERSION
from atus_pipeline.analytics.spec import ACTIVITY_PRESETS
from atus_pipeline.api import RESPONSE_FORMAT

Preset = tuple[tuple[str, ...], tuple[str, ...], str | None]

#: (ANALYTICS_VERSION, RESPONSE_FORMAT) -> {name: (include, exclude, label)}.
PINNED: dict[tuple[str, str], dict[str, Preset]] = {
    ("0.2", "1"): {
        "watching_tv": (("120303", "120304"), (), "Watching TV"),
        "sleep": (("0101",), (), "Sleeping"),
        "work_main_and_other_jobs": (
            ("050101", "050102", "050189"), (), "Working (main and other jobs)"),
        "housework": (("0201",), (), "Housework"),
        "eating_and_drinking": (("11",), (), "Eating and drinking"),
        "household_activities_bls_table": (
            ("02", "1802"), ("020903", "020904"),
            "Household activities (BLS table A-1 definition)"),
        "eating_and_drinking_bls_table": (
            ("11", "1811"), (), "Eating and drinking (BLS table A-1 definition)"),
        "leisure_and_sports_bls_table": (
            ("12", "13", "1812", "1813"), (),
            "Leisure and sports (BLS table A-1 definition)"),
        "work_related_bls_table": (
            ("05", "1805"), (),
            "Working and work-related activities (BLS table A-1 definition)"),
    },
}


def _current() -> dict[str, Preset]:
    return {name: (tuple(selector.include), tuple(selector.exclude), selector.label)
            for name, selector in ACTIVITY_PRESETS.items()}


def _pinned_for_these_versions() -> dict[str, Preset]:
    key = (ANALYTICS_VERSION, RESPONSE_FORMAT)
    assert key in PINNED, (
        f"no preset table for ANALYTICS_VERSION={ANALYTICS_VERSION!r}, RESPONSE_FORMAT="
        f"{RESPONSE_FORMAT!r}: add {key!r} with every preset (see the module docstring)")
    return PINNED[key]


def test_no_preset_is_redefined_or_removed_under_the_same_versions():
    pinned, current = _pinned_for_these_versions(), _current()
    removed = sorted(set(pinned) - set(current))
    assert not removed, (
        f"presets removed under the same versions: {removed}. Edge-cached results that "
        "name them would still be served (ADR-015): bump ANALYTICS_VERSION and pin the new "
        "table under the new key")
    for name, (include, exclude, label) in pinned.items():
        now = current[name]
        assert (now[0], now[1]) == (include, exclude), (
            f"preset {name!r} now selects include={now[0]}, exclude={now[1]}, pinned "
            f"include={include}, exclude={exclude}. That changes the statistics of every "
            "request naming it, and edge-cached results keyed by the name would keep the old "
            "numbers for up to 30 days: bump ANALYTICS_VERSION (docs/analytics.md, version "
            "history) and pin the new table under the new key; never edit this entry")
        assert now[2] == label, (
            f"preset {name!r}'s label is now {now[2]!r}, pinned {label!r}: the response "
            "bytes change, so bump RESPONSE_FORMAT (or ANALYTICS_VERSION) and pin the new "
            "table under the new key")


def test_every_preset_is_pinned():
    unpinned = sorted(set(_current()) - set(_pinned_for_these_versions()))
    assert not unpinned, (
        f"presets not pinned: {unpinned}. A new name needs no version bump (no cached "
        "request can name it yet): add it to the current entry of PINNED")


def test_an_analytics_version_never_carries_two_definitions_of_a_preset():
    """A label may change with RESPONSE_FORMAT alone; codes need ANALYTICS_VERSION."""
    by_version: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {}
    for (analytics_version, _), presets in PINNED.items():
        seen = by_version.setdefault(analytics_version, {})
        for name, (include, exclude, _label) in presets.items():
            assert seen.setdefault(name, (include, exclude)) == (include, exclude), (
                f"ANALYTICS_VERSION {analytics_version!r} pins two definitions of {name!r}")
