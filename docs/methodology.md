# Methodology Notes

The ATUS facts that determine whether analysis on this database is *correct*,
with the design responses Phase 1 makes to each. Sources: the
[ATUS User's Guide](https://www.bls.gov/tus/atususersguide.pdf), the
[2003–25 Interview Data Dictionary](https://www.bls.gov/tus/dictionaries/atusintcodebk0325.pdf),
BLS's ["Changes between 2003–2022 Data Files"](https://www.bls.gov/tus/lexicons/changes.pdf),
and the [COVID-19 technical notice](https://www.bls.gov/tus/notices/2021/covid19tech.htm).

## 1. Survey weights are not optional

ATUS oversamples weekends (about half of all diaries are Saturday/Sunday) and
weights adjust for sampling design, nonresponse, and day-of-week: **an
unweighted average over respondents estimates nothing about the population.**
The weight is person-*day*-scaled: weighted totals estimate person-days, and

```
population mean minutes/day = Σ(weight × minutes) / Σ(weight)
```

(User's Guide ch. 7). Every future estimate must use `respondents.final_weight`
(or `pandemic_weight` where applicable).

### Which weight, when

| Analysis | Weight |
| --- | --- |
| Any year or pooled years, 2003–2019 and 2021–2025 | `final_weight` (TUFNWGTP) |
| Anything involving 2020 | `pandemic_weight` (TU20FWGT), 2019+2020 only, partial-year windows only |
| Variance / standard errors | the 160 replicate columns matching the point-estimate weight |

**Why TUFNWGTP:** BLS changed the weighting method each year from 2003 to
2006. Single-year files therefore carry weights that are *not* comparable
across early years. The multi-year files solve this by shipping `TUFNWGTP`,
computed with the 2006 method for **all** years — this is the reason Phase 1
ingests the multi-year files rather than concatenating single-year files. (The
single-year variables TUFINLWGT / TU04FWGT / TU06FWGT do not exist on the
multi-year files at all.)

### Replicate weights and variance

BLS publishes 160 replicate weights per respondent (successive difference
replication). Standard errors follow the formula in User's Guide ch. 7 —
recompute the estimate under each replicate weight and combine the squared
deviations from the full-sample estimate. Phase 1 stores replicates untouched
and 1:1 with respondents (`replicate_weights`, `pandemic_replicate_weights`);
implementing the variance formula is Phase 2's first job. Replicate weights
are **never** to be treated as 160 additional observations.

## 2. 2020 is a partial year — treat it as its own regime

Data collection stopped March 18 – May 9, 2020 (Census call centers closed).
Facts, all schema-enforced or validated here:

- 2020 has **no** `TUFNWGTP` — `final_weight` is NULL for every 2020
  respondent, and its 160 replicates are NULL too (both validated; the former
  is a CHECK constraint).
- `TU20FWGT` (`pandemic_weight`) exists for 2020 **and** 2019, so 2019 can be
  reweighted for a like-for-like pre/post comparison. Its replicates live in
  `pandemic_replicate_weights` (18,217 rows = exactly the 2019+2020
  respondents).
- 2020 estimates represent only Jan 1 – Mar 17 and May 10 – Dec 31 (313
  days). **Annual 2020 estimates are impossible**; partial-period estimates
  must account for the number of days in the window. There are no diary dates
  inside the gap (validated).
- **How the Phase 2 engine operationalizes this** (details:
  [ADR-004](adr/ADR-004-2020-handling.md)): the default `multiyear` weight
  scheme *refuses* any 2020-inclusive estimate with an error explaining what
  to do; trends return 2020 as an explicit unavailable point; the `pandemic`
  scheme must be requested explicitly, accepts only 2019/2020, uses person-day
  denominators of 312/313 (the collection-comparable windows — 2019 diaries
  inside the excluded window carry zero TU20FWGT, verified), and stamps every
  result with a window warning. Hand-written SQL should never rely on NULL
  weights silently dropping 2020 rows — ask for the weight scheme you mean.

## 3. Cross-year comparability

The multi-year release harmonizes what BLS can harmonize and documents what it
cannot. What this database relies on:

**Activity codes are harmonized by BLS.** `TRCODEP`/`TRTIER1P`/`TRTIER2P` use
the 2003–2025 multi-year lexicon: travel is tier 18 for all years (single-year
2003–04 files used 17), and retired codes are absorbed into harmonized codes
(e.g. `020681` absorbs `020601`/`020602`) — the absorption notes are stored in
`activity_codes.harmonization_note`. Lexicon changes were minor after 2005 and
codes have been identical since 2013.

**Not comparable across years without care** (stored as-is, year on every row):

- *Detailed industry/occupation*: Census classification systems changed —
  industry 2002→2007 (2010)→2012 (2014)→2017 (2020)→2022 (2025); occupation
  2002→2010 (2011)→2018 (2020). Phase 1 loads only the stable *major* recodes
  (`major_industry`, `major_occupation`); BLS itself notes even occupation
  recodes are "not strictly comparable across all the years".
- *Who codes*: 55 (co-workers/colleagues/clients) was replaced by 59–62 in
  2010, and who information exists for working episodes only from 2010 — any
  "time with coworkers" trend must start at 2010 or bridge the coding change
  explicitly. BLS's own `time_alone_minutes`/`time_with_family_minutes` also
  exclude episodes without who codes.
- *Eldercare*: questions began January 2011; all eldercare fields are NULL
  before then (validated).
- *Age topcodes*: roster age capped at 80 (2003–04) vs 85 (2005+).
- *Earnings topcodes*: weekly earnings capped at $2,884.61 until May 2024;
  from June 2024 BLS applies monthly top-3% topcodes, so later values
  legitimately exceed the old cap.
- *CPS era-split variables*: metro status (`gemetsta`→`gtmetsta`) and family
  income bands (`hufaminc`→`hefaminc`) changed definitions mid-survey; both
  variants are stored under their BLS names.
- *Sample size*: the monthly sample was cut 35% starting December 2003 —
  2003 has 20,720 interviews vs ~13–14k/year afterwards (both counts are
  regression-checked against BLS documentation).

## 4. Diary structure

The diary day runs 04:00–04:00. Episodes cross midnight; the final episode's
recorded stop time may pass 04:00 while `duration_minutes` is capped at the
boundary so each diary sums to exactly 1440 minutes (validated for all
258,954 diaries). Use `duration_minutes` for time-use accounting and
`duration_uncapped_minutes` for questions about the activity itself (e.g.
total sleep duration of the final sleep episode).

Known source anomaly: 3 of 4,994,172 episodes carry clock times inconsistent
with their durations (cases 20161008161668, 20231009230928, 20240806241220).
The durations are authoritative — they reconcile to 1440/day, to running
totals, and to the independent BLS Activity Summary file — so the pipeline
loads these rows as-is and tracks the count as a validation warning.

## 5. Missing data is mostly structure, not error

Most NULLs in this database mean "not in this question's universe": earnings
exist only for employed wage/salary workers, spouse variables only when a
spouse/partner is present, school enrollment only for ages 15–49, eldercare
only from 2011, location/who only where collected. `docs/data-lineage.md`
records the universe for the affected columns; treating these NULLs as data
errors — or imputing over them — would be wrong. The one deliberate
information reduction: −1/−2/−3 all become NULL in analytical columns, with
the raw distinction recoverable from staged files. `-4` ("hours vary") is the
exception that is preserved (`usual_hours_vary`), because it is an answer,
not missingness.

## 6. One respondent ≠ one household, one diary ≠ typical behavior

ATUS interviews one person per household about one specific day. Estimates
are about *person-days of the civilian noninstitutional population aged
15+* — "average minutes per day spent X" — never "how a typical person spends
a typical day", and household-level statements need the roster/CPS context,
not respondent counts.
