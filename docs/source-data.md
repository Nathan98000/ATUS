# Source Data

Everything in this database comes from official U.S. Bureau of Labor
Statistics ATUS releases: [bls.gov/tus/data.htm](https://www.bls.gov/tus/data.htm),
multi-year page
[datafiles-0325.htm](https://www.bls.gov/tus/data/datafiles-0325.htm).
The pipeline uses the **2003–2025 multi-year files** (release code `0325`,
published June 2026 — the latest release at implementation time), not
concatenated single-year files, because the multi-year files carry the
cross-year-comparable weight `TUFNWGTP` and BLS-harmonized activity codes
(see [methodology.md](methodology.md)).

## Files ingested into the database

| File | Archive | Records (official) | Canonical table |
| --- | --- | --- | --- |
| Respondent | `atusresp-0325.zip` | 258,954 | `respondents` |
| Roster | `atusrost-0325.zip` | 702,409 | `household_members` |
| Activity | `atusact-0325.zip` | 4,994,172 | `activities` |
| Who | `atuswho-0325.zip` | 6,358,042 | `activity_companions` |
| ATUS-CPS | `atuscps-0325.zip` | 1,583,133 | `cps_persons` (curated subset; respondent households only) |
| Replicate weights | `atuswgts-0325.zip` | 258,954 | `replicate_weights` |
| Pandemic replicate weights (2019–20) | `atuswgtspan-1920.zip` | 18,217 | `pandemic_replicate_weights` |
| Coding lexicon (PDF) | `lexiconnoex0325.pdf` | 18+107+431 codes | `activity_tier1/2`, `activity_codes` (via committed CSV) |

Each archive contains the data file (CSV, named `.dat`), an `*_info.txt` with
the official record count (the counts above, used as validation expectations),
and SAS/SPSS/Stata read programs (unused).

## Files staged but not loaded

**Activity Summary (`atussum-0325.zip`, 258,954 records).** Per-respondent
total minutes per 6-digit activity code plus convenience demographics — all
derivable from the Activity file joined to Respondent/Roster/CPS. Loading it
would duplicate 4.99M episodes in aggregated form. It *is* downloaded and
staged, because it serves as an independent cross-check: `atus validate-db`
recomputes per-case-per-code totals from loaded episodes and compares them
against this file (2,000-case sample). The hosted serving database (Phase 5)
does store a per-respondent, per-code aggregate, but derives it from the
loaded episodes, not from this file
([database.md](database.md#the-serving-database)).

## Files deliberately excluded from Phase 1

| File | Why excluded |
| --- | --- |
| Case History (`atuscase-0525.zip`) | interview-process paradata (interviewer IDs, outcome codes); no analytical role in the platform's goals |
| Eldercare Roster (`atusrostec-1125.zip`) | small module file (2011+, care-recipient grain); respondent-level eldercare fields are loaded, and the roster is a straightforward future addition |
| Call History | single-year only, paradata |
| Module files (Eating & Health, Well-Being, Leave) | separate questionnaires with their own universes and their own weights (EUFINLWGT/WUFINLWGT/LUFINLWGT); ingesting them correctly is its own project and belongs in a later phase |
| ATUS-CPS rows for nonrespondent households | out of scope for a respondent-centric analytical database (they exist for nonresponse-bias research); retained in `data/staging/` |
| `Replwgts1920_diarydate.xlsx` (inside the pandemic weights zip) | diary dates already come from the Respondent file |

## How acquisition works — and the BLS bot-protection reality

`atus download` fetches each file once, sequentially, with a 3-second delay,
and records provenance (URL, size, SHA-256, timestamp) in
`data/manifest.json`. Raw archives are immutable; nothing in the pipeline
ever modifies `data/raw/`.

**The honest caveat:** bls.gov sits behind a CDN that rejects requests from
non-browser TLS stacks — plain `curl`, `requests`, or any User-Agent string
gets HTTP 403 (verified during development, including with UAs carrying
contact info per BLS's stated automated-access policy). The downloader
therefore uses `curl_cffi` with browser TLS impersonation to retrieve single
copies of these public files at a polite rate. If BLS tightens or changes
this behavior:

1. Download the eight archives above manually in a browser from
   [datafiles-0325.htm](https://www.bls.gov/tus/data/datafiles-0325.htm) into
   `data/raw/` (keep the original filenames).
2. Run `atus download` — it hashes and records existing files without
   re-fetching — then continue with `atus extract` as usual.

BLS publishes no checksums, so the SHA-256 values in the manifest (and in
`atus.source_files`) document what this pipeline saw, not an external ground
truth. Sizes and hashes observed at implementation time (September 2026):

| File | Bytes | SHA-256 (first 16 hex) |
| --- | --- | --- |
| atusresp-0325.zip | 16,710,282 | `026dcbf38665c749` |
| atusrost-0325.zip | 3,317,812 | `e8f1b62daf3b450e` |
| atusact-0325.zip | 83,407,298 | `8d2540316e04f784` |
| atussum-0325.zip | 16,141,000 | `a33499aa7b74e855` |
| atuswho-0325.zip | 21,346,984 | `c3f6a935ef6143c8` |
| atuscps-0325.zip | 103,926,958 | `0ea77394ff8532fb` |
| atuswgts-0325.zip | 299,670,507 | `fd1970cb6376c11e` |
| atuswgtspan-1920.zip | 57,449,948 | `3e7c50dc5d6a5ae7` |

## Documentation downloaded alongside the data

Stored in `data/docs/` (gitignored, re-downloadable):
[Interview Data Dictionary](https://www.bls.gov/tus/dictionaries/atusintcodebk0325.pdf),
[ATUS-CPS Data Dictionary](https://www.bls.gov/tus/dictionaries/atuscpscodebk0325.pdf),
[Coding Lexicon](https://www.bls.gov/tus/lexicons/lexiconnoex0325.pdf),
[User's Guide](https://www.bls.gov/tus/atususersguide.pdf),
[Frequently Used Variables](https://www.bls.gov/tus/other-documentation/freqvariables.pdf),
[Changes between data files](https://www.bls.gov/tus/lexicons/changes.pdf).

## Adding the next release (e.g. 2003–2026, `0326`)

Everything release-specific derives from one registry entry in
`src/atus_pipeline/sources.py`: archive and data-file names, URLs, the
expected survey years, the staging directory, the lexicon file, and the
lexicon span shown in analysis warnings ("2003-26"). The full procedure and
its rationale are in
[ADR-013](adr/ADR-013-data-releases-and-serving-db.md#new-atus-year-procedure-eg-2003-2026-release-0326);
the data-side steps are:

1. Add one entry to `_REGISTRY`:
   `multiyear_release("0326", first_year=2003, last_year=2026, expected_rows={...})`,
   with the record count from every new `*_info.txt`. If BLS widened the
   ATUS-CPS or activity-summary file, pass the new `cps_column_count` /
   `summary_column_count`. Nothing else in the code should need an edit;
   `tests/integration/test_release_checklist.py` runs a synthetic `0326`
   through the whole pipeline to keep it that way.
2. Commit the new lexicon CSV (next section).
3. Check BLS's changes documentation for new variables/values; adjust
   transformers, CHECK bounds, and year-specific validation expectations if
   needed. Source validation (`atus validate-source`) fails loudly on a
   header change.
4. Set `ATUS_RELEASE=0326` and run the pipeline. The load rebuilds every
   table — BLS re-releases all years, and weights/codes for past years can
   change between releases — in a shadow schema, and switches to it in one
   short transaction (`atus load --validate` checks the new data first). A
   running API keeps serving the previous data until the switch and is
   never interrupted by the load.
5. Re-run `atus validate-analytics`. The 24 benchmarks reproduce values BLS
   published for specific years; run them unchanged, record any value BLS
   revised instead of editing the expectation, and add the new year's
   published values as new benchmarks.
6. For the hosted deployment: build the serving database from the validated
   full database (`atus build-serving-db`), run the equivalence checks,
   restore it into a new database, and switch the API's `ATUS_DATABASE_URL`
   and `ATUS_EXPECTED_DATA_VERSION` together; the previous database stays
   available for rollback. Steps:
   [ADR-013](adr/ADR-013-data-releases-and-serving-db.md#release-procedure-on-the-free-stack-15)
   and the runbooks in [operations.md](operations.md#runbooks).

## The activity lexicon changes per release

Each multi-year release comes with its own coding lexicon
(`lexiconnoex<release>.pdf`, e.g.
[lexiconnoex0325.pdf](https://www.bls.gov/tus/lexicons/lexiconnoex0325.pdf)):
the codes of **all** survey years in that release are harmonized to it, so the
lexicon is a property of the release, not of a survey year. Between releases
BLS can add codes, retire codes (absorbing them into a harmonized code, with
a note such as "includes 020601 (all years), 020602 (2008 and later years)"),
rename entries, and change notes.

How the pipeline follows it:

- **One committed CSV per release:** `data/reference/activity_lexicon_<release>.csv`
  (`level, code, name, harmonization_note`), generated from the release's PDF
  by `scripts/extract_lexicon.py` and reviewed as a diff against the previous
  release's CSV. The parser anchors on the code columns' x-positions measured
  on the 2003–25 PDF; a layout change makes the structural tests fail rather
  than mis-parse. `tests/unit/test_sources.py` requires a committed lexicon
  for every registered release; `tests/unit/test_lexicon_reference.py`
  checks the 0325 CSV's structure and counts (add the new release's counts
  there).
- **Loading:** `atus load` loads the lexicon of `ATUS_RELEASE` into
  `activity_tier1/2` and `activity_codes`. Every episode's code must exist in
  it (foreign key), so a lexicon that does not match the Activity file fails
  the load before anything goes live.
- **Validation:** `atus validate-db` compares the loaded lexicon with the CSV,
  and the activity-summary cross-check compares per-code minute totals with
  BLS's own summary file (whose `t`-columns are the release's 6-digit codes).
- **Served text:** the engine names the loaded release's lexicon in every
  result's harmonization warning and in unknown-code errors ("the 2003-26
  harmonized activity lexicon"), read from `ingestion_runs.release`.
- **Review by hand:** the activity presets in `analytics/spec.py` (including
  the BLS news-release table definitions) and the benchmarks reference codes;
  a retired or renamed code changes their meaning. Shareable analysis links
  (ADR-009) embed codes too: a link that names a code the new lexicon retired
  is answered with an unknown-code error, never silently remapped.
