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


# --- Review round 1, edge findings: what the docs say the governor bounds -----------------

DEPLOYMENT = REPO_ROOT / "docs" / "deployment.md"
SECURITY = REPO_ROOT / "docs" / "security.md"
API_DOC = REPO_ROOT / "docs" / "api.md"
ADR_011 = ADR / "ADR-011-hosting.md"
ADR_015 = ADR / "ADR-015-edge-cache-snapshot-and-governor.md"
CONSTANTS = REPO_ROOT / "edge" / "src" / "constants.ts"


def _row(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


def test_docs_say_one_network_cannot_spend_the_daily_compute():
    """single-network-cu-ceiling-exhaustion: the per-network count did not bound
    compute; the docs said exhausting the ceiling took "many networks"."""
    assert re.search(r"^export const PREFIX_DAILY_CU_SHARE = 0\.2$", CONSTANTS.read_text(),
                     re.MULTILINE)
    r1 = _row(SECURITY.read_text(), "**R1.")
    assert "An attacker with many networks" not in r1
    assert "five spacing analyses" in r1 and "ten at their 60" in r1
    adr = ADR_015.read_text()
    assert "Many networks can use up" not in adr
    budgets = _section(adr, "| # | Budget |", "Every denial")
    assert "| 4 | per client network's compute | a fifth (`PREFIX_DAILY_CU_SHARE`)" in budgets
    deployment = DEPLOYMENT.read_text()
    assert "≤ ~1 CU-h even under scenario C" not in deployment
    assert "**S1b. One client spaces cheap novel specs" in deployment


def test_docs_bound_render_outbound_by_every_path_through_the_worker():
    """ungoverned-metadata-gets-render-bandwidth and kv-writes-not-bounded-by-global-cap:
    the 600-a-day cap was said to bound Render outbound (0.74 GB) and "No month-long
    pause" held only for analyses."""
    deployment = DEPLOYMENT.read_text()
    outbound = _row(deployment, "| Outbound bandwidth (includes API→DB traffic")
    assert "Hard bound from governor caps: 600/day × 40 KB × 31 ≈ 0.74 GB (85% free)" \
        not in outbound
    for needle in ("metadata lookups", "`/api/health`", "Refunded admissions"):
        assert needle in outbound, needle
    assert "also bounds KV writes and Render outbound)" not in deployment
    assert "**S5. Replay of metadata GETs**" in deployment
    assert "This also bounds KV writes and Render outbound traffic." not in ADR_011.read_text()
    adr = ADR_015.read_text()
    assert "it is not implemented." not in _section(adr, "## What this does not protect")
    assert "**Render outbound through the Worker.**" in adr
    assert "**Metadata at the edge.**" in adr
    assert "the API (query string forwarded) |" not in API_DOC.read_text()
    assert "metadata GETs reach the origin ungoverned" not in ADR_011.read_text()
    assert "**Metadata cannot spend Render's monthly outbound.**" in SECURITY.read_text()


def test_docs_store_only_computed_answers_in_kv():
    """kv-writes-not-bounded-by-global-cap: the gate said `miss` or `hit`."""
    for path in (ADR_011, ADR_015, SECURITY, API_DOC):
        text = path.read_text()
        assert not re.search(r"`miss` or `hit`|`miss\|hit`", text), _name(path)
    assert "const STORABLE_ORIGIN_CACHE = new Set(['miss'])" in \
        (REPO_ROOT / "edge" / "src" / "cache.ts").read_text()


def test_docs_say_the_egress_stop_needs_ground_truth_and_use_the_estimates_in_code():
    """conservative-mode-neon-egress-unbounded: without ground truth nothing bounded
    egress, and the docs quoted 32/256 KiB estimates as the code's."""
    constants = CONSTANTS.read_text()
    assert "EGRESS_ESTIMATE_BYTES = { light: 64 * 1024, heavy: 512 * 1024 }" in constants
    adr = ADR_015.read_text()
    model = next(line for line in adr.splitlines() if line.startswith("- **Model error.**"))
    assert "64 KiB light and 512 KiB heavy" in model and "32 KiB" not in model
    egress = _row(DEPLOYMENT.read_text(), "| Public network transfer (egress")
    assert "Estimated, **unmeasured**" not in egress
    assert "**without it**" in egress and "4.5 GB / 31" in egress
    assert "| 6 | Neon egress today, without ground truth only |" in adr


def test_adr_015_charges_lost_settles_as_a_union():
    """abandoned-lease-full-charge: expired leases were charged 780 CU-s each, and the
    ADR said a lost settle showed visitors "nothing"."""
    adr = ADR_015.read_text()
    assert "| Settle lost | the lease expires after 10 minutes and is charged in full | nothing |" \
        not in adr
    never = next(line for line in adr.splitlines() if line.startswith("- **Never settled**"))
    assert "as part of the union" in never and "share one window" in never
    upstream = (REPO_ROOT / "edge" / "src" / "upstream.ts").read_text()
    assert "The client's\n *   disconnect does not abort the origin call either." not in upstream


def test_adr_015_does_not_promise_the_provisional_charge_is_never_overshot():
    """settled-charge-exceeds-provisional."""
    adr = ADR_015.read_text()
    assert "never overshot" not in adr
    assert "That is an estimate, not a bound" in adr


def test_docs_describe_the_prewake_retry():
    """wake-stale-waking: "waking" was remembered for 10 minutes, and the comment said the
    probe always outlives the reply."""
    assert "at most once a minute while it was waking" in ADR_011.read_text()
    security = SECURITY.read_text()
    assert "per 10 minutes once the origin answered warm, and at most one a minute" in security
    assert "At most one origin `/health` call per isolate per 10 minutes;" not in security
    wake = (REPO_ROOT / "edge" / "src" / "wake.ts").read_text()
    assert "so a slow\n *   spin-up still completes" not in wake
    assert "30 s" in wake


def test_adr_015_settles_data_availability_refusals_as_shed():
    """non-computing-refusals-settled-as-computed."""
    shed = _row(ADR_015.read_text(), "| `shed` |")
    for code in ("database_paused", "data_version_mismatch", "data_not_loaded",
                 "schema_not_initialized"):
        assert code in shed, code


def test_docs_scope_the_security_headers_to_worker_responses():
    """transport-layer-errors-skip-security-headers: runtime answers carry none."""
    index = (REPO_ROOT / "edge" / "src" / "index.ts").read_text()
    assert "security headers on everything" not in index
    assert "on every `/api` response it produces" in API_DOC.read_text()
    r8 = _section(SECURITY.read_text(), "**R8.", "**R9.")
    assert "unknown HTTP method" in r8 and "Content-Length" in r8
    c4 = _row(SECURITY.read_text(), "- **Edge.** An analysis POST")
    assert "refused before the body is read" not in c4 and "(R8)" in c4


def test_docs_state_what_the_compute_share_leaves_a_real_visitor():
    """single-network-cu-ceiling-exhaustion, verification: the per-network compute share
    also stops a real visitor's continuous run (about 11 minutes a day without the Neon
    key, `governor.test.ts`); the ADR said such a run "costs little more than one"."""
    adr = ADR_015.read_text()
    share = _section(adr, "**Each network's share of the compute (row 4).**",
                     "**Metadata lookups.**")
    assert "costs little more than one" not in share
    assert "about 11 minutes a day in conservative mode and about 54 with ground truth" in share
    assert "about 11 minutes of continuous novel analyses" in _row(adr, "- **Shared networks.**")
    prefix = _row(DEPLOYMENT.read_text(), "| Per IP prefix (/24 or /48")
    assert "about 11 minutes a day without the Neon key, about 54 with it" in prefix
    assert "well under the network's fifth" not in DEPLOYMENT.read_text()


def test_adr_015_says_estimates_can_be_passed_and_settles_with_timing_can_overshoot():
    """conservative-mode-neon-egress-unbounded and settled-charge-exceeds-provisional,
    verification: the light estimate was described as an upper bound ("at most 61,892 B"),
    and the overshoot text implied a settle with Server-Timing could not overshoot."""
    adr = ADR_015.read_text()
    model = next(line for line in adr.splitlines() if line.startswith("- **Model error.**"))
    assert "at most 61,892 B" not in model and "may pass 64 KiB" in model
    assert "such a settle can overshoot too" in adr
    assert "light at most 61,892 B" not in CONSTANTS.read_text()


def _spa_source_text() -> str:
    """The SPA's shipped source (tests excluded): the only copy visitors can see."""
    files = sorted((REPO_ROOT / "frontend" / "src").rglob("*.ts*"))
    return "\n".join(path.read_text(encoding="utf-8") for path in files
                     if ".test." not in path.name)


@pytest.mark.parametrize("phrase", ["paused until the 1st"])
def test_the_docs_promise_no_page_the_spa_does_not_show(phrase):
    """Review round 1 (render-suspension-shown-as-waking): deployment.md and
    operations.md said the SPA shows "Analysis service paused until the 1st"
    when Render suspends the API. No such copy exists: a suspension looks like
    a cold start (security.md R13)."""
    if phrase in _spa_source_text().lower():
        return
    quoted = [f"{_name(path)}:{number}" for path in DOCS
              for number, line in _lines_outside_fences(path) if phrase in line.lower()]
    assert quoted == []


def test_the_render_suspension_gap_is_a_recorded_accepted_risk():
    """Review round 2 (api-paused-owner-switch) narrowed R13: the owner's pause
    switch gives visitors an honest paused page; what remains is the time before
    the owner sets it."""
    security = (REPO_ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    risks = _section(security, "## Accepted risks", "\n## ")
    r13 = _section(risks, "**R13.", "**R14.")
    assert r13.startswith("**R13. A Render suspension looks like a cold start until the owner "
                          "sets the pause switch.**")
    for needle in ("`pause-api`", "`api_paused`", "edge/test/worker/pause.test.ts",
                   "frontend/e2e/service-codes.spec.ts", "**What remains.**"):
        assert needle in r13, needle
    assert "There is no dedicated page" not in r13 and "Neither exists today" not in r13
    adr_011 = (ADR / "ADR-011-hosting.md").read_text(encoding="utf-8")
    accepted = _section(adr_011, "### Accepted risks", "\n### ")
    assert "**A Render suspension is presented as a cold start until the owner sets the " \
        "pause switch**" in accepted
    assert "it has no dedicated page" not in accepted


def test_the_pause_switch_is_documented_where_operators_look():
    """api-paused-owner-switch: the runbook, the config reference, the error table and
    the levers paragraph name the switch; none says there is no runtime switch."""
    operations = (REPO_ROOT / "docs" / "operations.md").read_text()
    assert "There is no runtime switch that stops new analyses at the edge." not in operations
    runbook = _section(operations, "### Pausing the API", "\n### ")
    for needle in ("operation=pause-api", "operation=resume-api", "wrangler secret bulk",
                   "`unknown`", "**Deploys keep it.**", "**Edge rollbacks**"):
        assert needle in runbook, needle
    assert "| 503 | `api_paused` |" in API_DOC.read_text()
    secrets = _row(DEPLOYMENT.read_text(), "| `API_PAUSED_UNTIL`, `API_PAUSED_REASON` |")
    assert "`pause-api`" in secrets
    assert "`api_paused`" in _row((REPO_ROOT / "docs" / "frontend.md").read_text(),
                                  "| `database_paused`, `capacity_paused`, `api_paused`")


# --- Review round 2 -----------------------------------------------------------------------

ADR_012 = ADR / "ADR-012-load-and-quota-protection.md"
GOVERNOR = REPO_ROOT / "edge" / "src" / "governor.ts"


def test_no_doc_says_a_metadata_lookup_cannot_wake_neon():
    """lookup-wakes-neon-uncharged: the first lookup on a freshly started API process
    runs its snapshot read on Neon; the governor now charges it, and the docs and the
    governor's comment said lookups never touch Neon, or that the uncharged wakes showed
    in the daily quota report (in conservative mode no figure it reads did)."""
    stale = {
        GOVERNOR: ["Only counted: it never touches Neon"],
        ADR_015: ["A lookup is only counted (no lease, no settle)",
                  "costs Render outbound, not Neon compute",
                  "which the governor does not count but Neon's ground truth shows"],
        ADR_012: ["The governor does not count these; Neon's ground truth (ADR-015) and the "
                  "daily quota report would show them."],
        SECURITY: ["**Metadata cannot keep Neon awake.**"],
    }
    for path, phrases in stale.items():
        text = path.read_text()
        for phrase in phrases:
            assert phrase not in text, f"{_name(path)}: {phrase}"
    lookups = _section(ADR_015.read_text(), "**Metadata lookups.**", "\n\n")
    for needle in ("`SNAPSHOT_READ_EGRESS_BYTES`", "never refunded", "about 3 such wakes"):
        assert needle in lookups, needle
    assert "**Metadata cannot keep Neon awake for free.**" in SECURITY.read_text()
    s5 = _row(DEPLOYMENT.read_text(), "| **S5. Replay of metadata GETs**")
    assert "charges that wake session" in s5 and "Worker requests only, as in S3" not in s5
    assert "export const SNAPSHOT_READ_EGRESS_BYTES = 128 * 1024" in CONSTANTS.read_text()


def test_adr_015_says_a_preset_redefinition_needs_a_version_bump():
    """preset-definitions-outside-kv-scope."""
    staleness = _section(ADR_015.read_text(), "### Staleness across releases", "\n### ")
    assert "**Preset definitions are not in the scope.**" in staleness
    assert "tests/unit/analytics/test_preset_vocabulary.py" in staleness
    assert (REPO_ROOT / "tests" / "unit" / "analytics" / "test_preset_vocabulary.py").is_file()
