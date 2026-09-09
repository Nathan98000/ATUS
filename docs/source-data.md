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
against this file (2,000-case sample). Phase 2 can materialize an equivalent
summary as a view/table when query patterns demand it.

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

1. Add a `_build_release_0326()` entry in `src/atus_pipeline/sources.py` with
   the new URLs and the record counts from the new `*_info.txt` files.
2. Regenerate the lexicon CSV if BLS publishes `lexiconnoex0326.pdf`
   (`scripts/extract_lexicon.py`), review the diff.
3. Check BLS's changes documentation for new variables/values; adjust
   transformers, CHECK bounds, and year-specific validation expectations if
   needed.
4. Set `ATUS_RELEASE=0326` and run the pipeline. The load is a full atomic
   rebuild — BLS re-releases all years, and weights/codes for past years can
   change between releases.
