# Analytics Reference — the Statistical Contract

What every term in the Phase 2 analytical engine means, precisely, with the
BLS sources it comes from. Package: `src/atus_pipeline/analytics/`; version
stamped on every result (`analytics_version`, currently `0.1`).

## Vocabulary

| Term | Meaning here |
| --- | --- |
| **respondent / diary day** | One ATUS interview: one person reporting one 24-hour day (04:00–04:00). The unit of analysis for all estimators. Same grain as `atus.respondents`. |
| **population / universe** | With no filters: the U.S. civilian noninstitutional population age 15+ — the ATUS target population. Filters restrict it at the respondent level. "All respondents" *is* an estimate of that universe, weighted. |
| **observation (x)** | A per-respondent quantity derived from that respondent's diary: total minutes in the selected activity (0 for non-participants — a real value, not missing), or the participation indicator (x > 0). |
| **activity** | A set of 6-digit codes from the harmonized 2003–25 lexicon, selected by explicit codes or tier prefixes (a 2-/4-digit code means the tier and all descendants) with optional exclusions. Validated against `atus.activity_*` before any query. |
| **weight (w)** | Person-days the respondent represents. Summed over a period's respondents: population × days in the period (User's Guide ch. 7.1). |
| **sample size** | Unweighted respondent count (`n_respondents`); `n_participants` is the unweighted count with any time in the activity. Never confused with... |
| **weighted population** | `weighted_population_per_day` = Σw / D: persons represented on an average day of the period. |
| **D (days in period)** | Calendar days the weights represent: 365/366 per year, summed across pooled years (2003–06 → 1,461, per the User's Guide); reduced by a diary-date window; and reduced by the excluded pandemic window under the pandemic scheme (2019 → 312, 2020 → 313). |

## Estimators (User's Guide ch. 7.4, verbatim formulas)

| Measure | Formula | Unit | Denominator |
| --- | --- | --- | --- |
| `average_minutes_per_day` | Σ(w·x) / Σ(w) | minutes/day | all selected respondents (participants and non-participants) |
| `participation_rate` | Σ(w·p) / Σ(w) | proportion 0–1 | all selected respondents |
| `average_minutes_per_participant` | Σ(w·x) / Σ(w·p) | minutes/day of participants | participants only |
| `participants_per_day` | Σ(w·p) / D | persons/day | person-days → persons |

p = 1 iff the respondent spent **more than zero minutes** in the activity that
day. A recorded 0-minute episode is not participation. A participation rate is
a *daily* rate — the share doing the activity on an average day, not over any
longer period (the guide is explicit that longer-period participation cannot
be computed from time diaries).

## Weights — which, when (actual column names)

| Analysis | Point weight | Replicates | Valid years |
| --- | --- | --- | --- |
| Anything not involving 2020 | `respondents.final_weight` (BLS `TUFNWGTP`, 2006 method, cross-year comparable) | `replicate_weights.tufnwgtp001..160` | 2003–2019, 2021+ |
| Anything involving 2020 (and 2019 comparisons with it) | `respondents.pandemic_weight` (BLS `TU20FWGT`) | `pandemic_replicate_weights.tu20fwgt001..160` | 2019, 2020 only |

The engine never auto-switches schemes: `weights="multiyear"` is the default
and **refuses** 2020 with an error explaining exactly what to do;
`weights="pandemic"` must be requested explicitly and refuses any year outside
2019–2020. In a *trend*, 2020 under the multiyear scheme is returned as an
explicit unavailable point with the reason, never silently dropped. See
[ADR-004](adr/ADR-004-2020-handling.md) and the 2020 section of
[methodology.md](methodology.md).

## Variance and confidence intervals

Standard errors use the official replicate method (User's Guide ch. 7.5):

    Var(Ŷ₀) = (4/160) · Σᵢ (Ŷᵢ − Ŷ₀)²,   SE = √Var

where each Ŷᵢ re-evaluates the same estimator under replicate weight i. The
factor 4 reflects the Fay replicate factors (1.7, 1.0, 0.3) of the CPS-derived
successive-difference design. Implementation is validated against the guide's
own worked example (2007 TV watching: SE = 0.0293 hours, matched exactly).

Confidence intervals are the normal approximation, estimate ± z·SE, default
95% (z = 1.96), recorded on the result (`confidence_level`). Participation
CIs are truncated to [0, 1] with a warning when truncation occurs. A subgroup
so small that some replicate has a zero denominator raises
`InsufficientDataError` rather than returning a fabricated SE.

Group comparisons compute the difference's SE from **per-replicate
differences** (Ŷᵃᵢ − Ŷᵇᵢ), which correctly accounts for the covariance between
group estimates from the same sample; two independent SEs are never combined,
and no significance tests are reported.

## Population filters (source variables and codings)

| Filter | Source | Coding | Notes |
| --- | --- | --- | --- |
| `age_min`/`age_max` | roster `household_members.age` (lineno 1) — interview-time `TEAGE` | years, inclusive bounds | topcoded 80 (2003–04) / 85 (2005+); bounds ≥ 80 trigger a warning |
| `sex` | roster `sex` (`TESEX`) | male=1, female=2 | interview-time |
| `employment_status` | `respondents.labor_force_status` (`TELFS`) | employed {1,2}, unemployed {3,4}, not_in_labor_force {5} | ATUS-interview labor force status |
| `has_household_children` | `respondents.household_children` (`TRCHILDNUM`) | >0 / =0 | *household* children under 18, not necessarily own children |
| `region` / `state_fips` | `cps_persons.region`/`state_fips` (`GEREG`/`GESTFIPS`, lineno 1) | Census region 1–4 / 2-digit FIPS | measured at the CPS interview, 2–5 months before the diary |
| `education_level` | `cps_persons.education` (`PEEDUCA`) | <HS 31–38, HS 39, some college/assoc 40–42, bachelor+ 43–46 | CPS-time; missing excluded |
| `day_type` | `respondents.diary_day_of_week` (`TUDIARYDAY`) | weekend {1,7}=Sun/Sat, weekday {2–6} | estimates then represent the average such day |
| `diary_date_min/max` | `respondents.diary_date` (`TUDIARYDATE`) | inclusive dates | for within-year periods (how BLS published 2020: May 10–Dec 31) |

**Missing-data rule:** filtering on a dimension excludes respondents whose
value is NULL for that dimension — from numerator *and* denominator. Missing
is never treated as "no". Unfiltered dimensions include everyone. This is
tested with a fixture respondent whose CPS education is missing.

## Data pathway (Activity Summary vs episodes)

BLS describes two equivalent routes to per-respondent activity totals: the
Activity Summary file, or aggregating Activity-file episodes (User's Guide
ch. 7.4 demonstrates both and notes they "yield the same result"). This
database's canonical pathway is the episode aggregation —
`SUM(duration_minutes)` per respondent inside the query, *before* any join —
because Phase 1 deliberately keeps episodes canonical and proved the
equivalence against the real summary file (validation check: 0 of 2,000
sampled cases disagree; benchmark suite: reproduces the guide's summary-file
worked example exactly). Grain safety follows structurally: episode totals
collapse to one row per respondent before the respondent-level weight is
joined once. See [ADR-005](adr/ADR-005-summary-vs-episodes.md).

Analyses that need episode context (location, time of day, who was present)
would join `activities`/`activity_companions` directly; Phase 2 exposes
respondent-day measures only, and the who/where dimensions are future work.

## Published-table category mappings

BLS news-release tables group activities slightly differently from raw
lexicon tiers: every major category includes its related travel (tier 18xx),
and household mail/e-mail (020903, 020904) is tabulated under "Telephone
calls, mail, and e-mail" instead of Household activities. The
`*_bls_table` activity presets encode these mappings and are verified against
Table A-1 2025 values in the benchmark suite; plain tier selections (e.g.
`"02"`) remain available and are labeled by lexicon semantics.

## Reproducibility

Every analysis is a serializable spec (`AnalysisSpec.to_dict()/from_dict()`),
runnable from JSON via `atus analyze run --spec file.json`, and every result
embeds: the exact spec, weight variable, variance method, D, sample sizes,
`analytics_version`, and methodological warnings. The same engine call
(`AnalysisEngine.estimate/trend/compare`) is the interface Phase 3's API will
consume — nothing statistical lives in the CLI.

## Errors instead of silent fallbacks

`InvalidSpecError` (malformed request), `UnknownActivityError` (code not in
the lexicon), `UnsupportedAnalysisError` (methodologically unsupported, e.g.
2020 under multiyear weights), `InsufficientDataError` (empty population,
zero denominators, replicate breakdown). The engine never substitutes an
unweighted statistic, another weight, or an empty result for what was asked.

## CLI quick reference

```bash
atus analyze estimate --activity sleep --year 2025
atus analyze estimate --activity 120303,120304 --label "TV" --year 2024 --measure participation_rate
atus analyze estimate --activity sleep --year 2020 --weights pandemic --from-date 2020-05-10
atus analyze trend --activity leisure_and_sports_bls_table --start-year 2003 --end-year 2025
atus analyze compare --activity household_activities_bls_table --year 2025 \
    --group-a sex=male --group-b sex=female --label-a Men --label-b Women
atus analyze run --spec docs/examples/leisure_trend.json --trend --json
atus validate-analytics          # reproduce official BLS estimates
```

Add `--json` to any command for machine-readable output (the result object's
`to_dict()`).

## Performance baseline (full 2003–25 database, Sept 2026)

| Analysis | seconds |
| --- | --- |
| one activity, one year, point only | 0.3 |
| one activity, one year, with replicate SE | 0.9 |
| composite category, one year, with SE | 0.8 |
| demographic subgroup, with SE | 0.5 |
| pooled 22 years, with SE | 10.7 |
| 22-year trend with per-year SEs | 11.1 |

(`scripts/benchmark_analytics.py`.) The multi-year cases scan all 259k × 160
replicate values once; acceptable for Phase 2, and the obvious first target if
Phase 5 profiling demands caching or materialization.
