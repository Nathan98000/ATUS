# Example Analyses

Real analyses run against the loaded 2003–2025 database (outputs captured
September 2026). Each shows the CLI form; every one is equally expressible as
a JSON spec (`atus analyze run --spec …`) or a direct
`AnalysisEngine.estimate/trend/compare` call — same engine, same numbers.
Definitions and caveats for every term: [analytics.md](analytics.md).

---

## 1. Average daily sleep, adults 25–54, 2025

```bash
atus analyze estimate --activity sleep --year 2025 --age-min 25 --age-max 54
```

```text
average_minutes_per_day — Sleeping
Population: age 25-54 | Years: 2025 | Weight: TUFNWGTP (multiyear)
Estimate: 528.75 min/day (8.81 h)   SE 2.938   95% CI [523.00, 534.51]
Sample: 2,597 respondents (2,594 participants) · represents 131,100,442 persons on an average day
```

Estimator: Σ(w·x)/Σ(w) over all respondents aged 25–54 (including the three
with zero sleep recorded); replicate-weight SE. Interpretation caveat: this is
the average across all days of the week (weights are day-balanced), not a
"typical weeknight".

## 2. Leisure and sports by year, 2003–2025 (trend)

```bash
atus analyze trend --activity leisure_and_sports_bls_table --start-year 2003 --end-year 2025
```

```text
  2003:   306.6 min ( 5.11 h)  SE 1.592   n=20,720
  2004:   311.2 min ( 5.19 h)  SE 1.879   n=13,973
  ...
  2019:   311.1 min ( 5.19 h)  SE 2.688   n=9,435
  2020: unavailable — TUFNWGTP is undefined for 2020 (pandemic collection
        suspension); estimate 2020 separately with weights='pandemic'.
  2021:   316.1 min ( 5.27 h)  SE 2.329   n=9,087
  ...
  2025:   309.6 min ( 5.16 h)  SE 3.404   n=6,146
```

The activity preset matches the published Table A-1 category (tiers 12+13
plus related travel 1812/1813). 2020 appears as an explicit gap by design
([ADR-004](adr/ADR-004-2020-handling.md)). The JSON form of this exact
analysis is committed at [examples/leisure_trend.json](examples/leisure_trend.json).

## 3. Household activities: household children present vs not, 2025

```bash
atus analyze compare --activity household_activities_bls_table --year 2025 \
    --group-a has_household_children=true --group-b has_household_children=false \
    --label-a "Household children present" --label-b "No household children"
```

```text
[Household children present]  112.31 min/day (1.87 h)  SE 3.314  95% CI [105.82, 118.81]  n=1,699
[No household children]       123.40 min/day (2.06 h)  SE 2.397  95% CI [118.71, 128.10]  n=4,447
Difference:                   -11.09 min/day           SE 4.239  95% CI [-19.40, -2.79]
```

The difference SE comes from per-replicate differences (covariance-correct).
Caveat: `has_household_children` means *any* household child under 18
(`TRCHILDNUM > 0`), which is close to but not identical to "parents".

## 4. Daily participation rate: watching TV, 2024

```bash
atus analyze estimate --activity 120303,120304 --label "Watching TV" \
    --measure participation_rate --year 2024
```

```text
Estimate: 72.78% of the population   SE 0.728pp   95% CI [71.35%, 74.20%]
Sample: 7,669 respondents (5,917 participants) · represents 272,912,483 persons on an average day
```

A *daily* rate: the share watching TV on an average day — not the share who
ever watch TV (which time diaries cannot measure).

## 5. 2020 under pandemic weights (the BLS-published reference period)

```bash
atus analyze estimate --activity sleep --year 2020 --weights pandemic --from-date 2020-05-10
```

```text
Estimate: 540.53 min/day (9.01 h)   SE 2.053   95% CI [536.50, 544.55]
Sample: 6,666 respondents · represents 264,814,835 persons on an average day
note: Pandemic weights (TU20FWGT): estimates represent only the comparable
      collection windows Jan 1 - Mar 17 and May 10 - Dec 31; annual estimates
      for 2020 are not possible.
```

Matches the published 2020-results value (9.01 h) — this exact reproduction is
part of `atus validate-analytics`. Asking for 2020 without `--weights pandemic`
is refused with an explanation, never approximated.

## 6. Machine-readable output (for the future API)

```bash
atus analyze estimate --activity sleep --year 2025 --json
```

```json
{
  "measure": "average_minutes_per_day",
  "estimate": {
    "value": 541.9642003795799,
    "unit": "minutes_per_day",
    "standard_error": 2.166215578745084,
    "confidence_level": 0.95,
    "ci_lower": 537.7184958624899,
    "ci_upper": 546.2099048966699
  },
  "years": [2025],
  "activity": {"label": "Sleeping", "include": ["0101"], "exclude": [], "leaf_code_count": 3},
  "population": "civilian noninstitutional population age 15+",
  "n_respondents": 6146,
  "n_participants": 6139,
  "weighted_population_per_day": 277984657.483597,
  "days_in_period": 365,
  "weight": {"scheme": "multiyear", "bls_variable": "TUFNWGTP", "column": "respondents.final_weight"},
  "variance_method": "replicate",
  "analytics_version": "0.1",
  "spec": { "...": "the exact serialized AnalysisSpec, reproducible verbatim" },
  "warnings": ["Activity codes use the BLS-harmonized 2003-25 multi-year lexicon (cross-year comparable by construction)."]
}
```
