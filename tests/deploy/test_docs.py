"""Static checks on the documentation (brief §18, "complete and accurate").

* every relative link and ``#anchor`` in the README, ``docs/*.md`` and the
  ADRs resolves (GitHub's heading slugs);
* no "see loadtests/results (§13)" placeholder is left: the docs quote the
  2026-10-21 emulation record instead;
* the figures ADR-011's calibration amendment quotes are the record's;
* ADR-012 states the edge governor's values as ``edge/src/constants.ts``
  defines them, and qualifies which 4xx answers are free;
* ADR-008 and ADR-009 point at their Phase 5 successors.

Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from .conftest import REPO_ROOT

DOCS = sorted(
    [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").glob("*.md"),
     *(REPO_ROOT / "docs" / "adr").glob("*.md")]
)
RECORD = REPO_ROOT / "loadtests" / "results" / "2026-10-21-emulation.md"
ADR = REPO_ROOT / "docs" / "adr"

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LINK = re.compile(r"\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def _name(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: link text kept, punctuation dropped, spaces → '-'."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").replace("*", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def _lines_outside_fences(path: Path):
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    for _, line in _lines_outside_fences(path):
        match = _HEADING.match(line)
        if match:
            base = _slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            yield base if count == 0 else f"{base}-{count}"


def _relative_links(path: Path):
    for number, line in _lines_outside_fences(path):
        for match in _LINK.finditer(re.sub(r"`[^`]*`", "", line)):
            target = match.group(1)
            if not re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                yield number, target


def test_the_slug_rule_matches_githubs_for_the_headings_the_docs_link():
    assert _slug("Calibration (§13)") == "calibration-13"
    assert _slug("Fallback: the runner-up (P3's API host, Vercel Hobby)") == \
        "fallback-the-runner-up-p3s-api-host-vercel-hobby"
    assert _slug("Decision 2: a globally unique `data_version`, written with the data "
                 "(F06, F20)") == ("decision-2-a-globally-unique-data_version-written-with-"
                                   "the-data-f06-f20")
    assert _slug("3. A. Restore drill (timed)") == "3-a-restore-drill-timed"


@pytest.mark.parametrize("path", DOCS, ids=_name)
def test_relative_links_and_anchors_resolve(path):
    anchors: dict[Path, set[str]] = {}
    broken = []
    for number, target in _relative_links(path):
        file_part, _, fragment = target.partition("#")
        destination = (path.parent / unquote(file_part)).resolve() if file_part else path
        if not destination.exists():
            broken.append(f"{_name(path)}:{number}: {target} (no such file)")
        elif fragment and destination.suffix == ".md":
            if destination not in anchors:
                anchors[destination] = set(_anchors(destination))
            if unquote(fragment) not in anchors[destination]:
                broken.append(f"{_name(path)}:{number}: {target} (no such heading)")
    assert broken == []


@pytest.mark.parametrize("path", DOCS, ids=_name)
def test_no_load_test_placeholder_is_left(path):
    """The placeholders pointed at results that did not exist yet; the docs now quote
    loadtests/results/2026-10-21-emulation.md and link it."""
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"loadtests/results(?:\]\([^)]*\))? \(§13\)", text)


def test_the_record_is_what_the_docs_cite():
    assert RECORD.is_file()
    citing = [p for p in DOCS if "loadtests/results/2026-10-21-emulation.md" in p.read_text()]
    for name in ("README.md", "docs/architecture.md", "docs/operations.md", "docs/security.md",
                 "docs/roadmap.md", "docs/frontend.md", "docs/analytics.md",
                 "docs/deployment.md", "docs/adr/ADR-011-hosting.md",
                 "docs/adr/ADR-012-load-and-quota-protection.md",
                 "docs/adr/ADR-013-data-releases-and-serving-db.md"):
        assert REPO_ROOT / name in citing, name


def _section(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)] if end else text[begin:]


def test_adr_011_amendment_quotes_the_records_figures():
    """The superseded-measurements amendment copies the record's numbers; each one
    quoted here must be in the record (and in the amendment)."""
    record = RECORD.read_text()
    amendment = _section((ADR / "ADR-011-hosting.md").read_text(),
                         "### Calibration (§13): superseded measurements")
    for figure in ("13.12", "12.88", "14.28", "76.1 MiB", "59.3 MiB", "21.35", "21.81",
                   "25.80", "33.49", "34.99", "69.21", "71.02", "70.52", "38.58", "40.71",
                   "3.03", "3.53", "0.99", "4.60", "1.14", "649,903,180", "336,549,536"):
        assert figure in record, figure
        assert figure in amendment, figure
    assert "PROJECTION" in amendment
    assert "85 s" in amendment and "90 s" in amendment


_TERM = r"\d+(?:\.\d+)?(?:–\d+(?:\.\d+)?)? s"


def test_adr_011_coldest_path_projection_adds_up():
    """Every row of the recomputed coldest-path table states its sum: the figures it
    claims are the sums of the low and high ends of its terms, rounded to a second."""
    amendment = _section((ADR / "ADR-011-hosting.md").read_text(),
                         "### Calibration (§13): superseded measurements")
    table = _section(amendment, "| Novel analysis |", "This replaces")
    rows = [line for line in table.splitlines()[2:] if line.startswith("|")]
    assert len(rows) == 4
    for row in rows:
        cell = row.split("|")[2]
        sums = re.search(rf"{_TERM}(?: \+ {_TERM})+", cell)
        assert sums, row
        low = high = 0.0
        for term in sums.group(0).split(" + "):
            a, _, b = term.removesuffix(" s").partition("–")
            low, high = low + float(a), high + float(b or a)
        claim = re.search(r"about (\d+)(?:–(\d+))? s", cell)
        assert claim, row
        if claim.group(2):
            assert (int(claim.group(1)), int(claim.group(2))) == (round(low), round(high)), row
        else:
            assert int(claim.group(1)) == round(high), row


def test_audit_and_emulation_single_year_p50s_are_not_compared_as_a_speed_up():
    """The audit's 6.95 s repeated one spec (010101, 2025); the record's 0.99 s is the
    p50 of 59 distinct specs. A line quoting both must say they are not comparable."""
    for path in DOCS:
        for number, line in _lines_outside_fences(path):
            if "6.95 s" in line and "0.99 s" in line:
                assert re.search(r"not (?:comparable|the same request|a like-for-like)",
                                 line, re.IGNORECASE), f"{_name(path)}:{number}"


def test_the_parallel_workers_decision_reads_the_same_everywhere():
    """F38: ATUS_API_MAX_PARALLEL_WORKERS stays unset (ADR-012 "Calibration (§13)")."""
    adr_012 = (ADR / "ADR-012-load-and-quota-protection.md").read_text()
    assert "| `ATUS_API_MAX_PARALLEL_WORKERS` | **unset (server default), unchanged** |" in adr_012
    adr_013 = (ADR / "ADR-013-data-releases-and-serving-db.md").read_text()
    bullet = next(line for line in adr_013.splitlines()
                  if line.startswith("- **Parallel query (F38).**"))
    assert "stays unset" in bullet and "leaves it unset" in bullet
    operations = (REPO_ROOT / "docs" / "operations.md").read_text()
    resolution = _section(operations, "## Audit resolution")
    f38 = next(line for line in resolution.splitlines() if line.startswith("| F38 |"))
    assert f38.startswith("| F38 | Accepted |") and "stays unset" in f38


def test_architecture_performance_table_attributes_db_time_to_analyses_only():
    text = (REPO_ROOT / "docs" / "architecture.md").read_text()
    performance = _section(text, "## Performance")
    assert "metadata\nand cached rows run no SQL" in performance


def _constant(name: str) -> int:
    constants = (REPO_ROOT / "edge" / "src" / "constants.ts").read_text()
    match = re.search(rf"^export const {name} = ([\d_]+)$", constants, re.MULTILINE)
    assert match, name
    return int(match.group(1).replace("_", ""))


def test_adr_012_states_the_governors_values():
    text = (ADR / "ADR-012-load-and-quota-protection.md").read_text()
    table = _section(text, "### Relation to the edge governor", "## What this does not")
    assert f"client network ({_constant('PREFIX_DAILY_ANALYSES')} per day)" in table
    assert f"globally ({_constant('GLOBAL_DAILY_ANALYSES')})" in table
    assert f"heavy ({_constant('HEAVY_DAILY_ANALYSES')})" in table
    assert f"refunded ones included ({_constant('PREFIX_DAILY_ADMISSIONS')} per day" in table
    assert f"within {_constant('GOVERNOR_TIMEOUT_MS') // 1000} s" in table
    assert f"`Retry-After: {_constant('RETRY_AFTER_GOVERNOR_S')}`" in table


def test_adr_012_says_which_4xx_answers_run_the_statistics_query():
    """A 422 found by the statistics query (insufficient_data, or a diary-date window
    outside the requested years) runs it first and reports real DB time."""
    text = (ADR / "ADR-012-load-and-quota-protection.md").read_text()
    assert "every 400/413/415/422" not in text
    assert "(hits, 4xx, sheds)" not in text
    timing = next(line for line in text.splitlines() if "db;dur=0" in line)
    assert "insufficient_data" in timing and "diary-date window" in timing
    free = next(line for line in text.splitlines() if "**What it makes free.**" in line)
    assert "insufficient_data" in free and "diary-date window" in free


def test_adr_008_and_009_point_at_their_phase_5_successors():
    adr_008 = (ADR / "ADR-008-server-state.md").read_text()
    amendment = _section(adr_008, "## Amendment (Phase 5, 2026-10-21)")
    for needle in ("0003_data_version.sql", "src/atus_pipeline/database/versions.py",
                   "X-Data-Version", "ADR-013-data-releases-and-serving-db.md#decision-2"):
        assert needle in amendment, needle
    adr_009 = (ADR / "ADR-009-shareable-urls.md").read_text()
    redirect = _section(adr_009, "a Phase 5 redirect service", "\n- ")
    assert "ADR-016-short-share-links.md" in redirect
