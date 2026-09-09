# Data Lineage

Every canonical column traced to its BLS source variable and transformation.
The transformations live in `src/atus_pipeline/transformation/` (one explicit
line per column in `tables.py`); a unit test asserts each table's column list
and transformer stay in lockstep. Variable definitions: the
[2003–25 Interview Data Dictionary](https://www.bls.gov/tus/dictionaries/atusintcodebk0325.pdf)
for `T*` variables and the
[ATUS-CPS Data Dictionary](https://www.bls.gov/tus/dictionaries/atuscpscodebk0325.pdf)
for `P*/G*/H*` variables.

## Transformation vocabulary

| Rule | Meaning |
| --- | --- |
| *as-is* | integer/text value stored unchanged |
| *sentinel→NULL* | −1 (blank) / −2 (don't know) / −3 (refused) become SQL NULL; the three-way distinction remains recoverable from `data/staging/` |
| *yes/no→bool* | 1→true, 2→false, sentinels→NULL |
| *0/1→bool* | 0→false, 1→true (no missing allowed) |
| *implied ÷100* | BLS "2 implied decimals" earnings, divided by exactly 100 (`Decimal`); allocated values can carry fractional cents |
| *weight* | decimal preserved exactly; −1 (weight undefined) → NULL |
| *code, validated* | zero-padded digit string validated (width, digits) and stored as `char(n)` |

## atus.respondents ← Respondent file (`atusresp_0325.dat`)

| Canonical column | Source variable | Transformation |
| --- | --- | --- |
| tucaseid | TUCASEID | as-is (bigint) |
| data_year | TUYEAR | as-is |
| diary_date | TUDIARYDATE | YYYYMMDD → `date` |
| diary_day_of_week | TUDIARYDAY | as-is (1=Sunday … 7=Saturday) |
| is_holiday | TRHOLIDAY | 0/1→bool |
| labor_force_status | TELFS | as-is (1–5) |
| has_multiple_jobs | TEMJOT | yes/no→bool (universe: employed) |
| full_or_part_time | TRDPFTPT | sentinel→NULL |
| usual_weekly_hours | TEHRUSLT | sentinel→NULL; **−4 "hours vary" split out** |
| usual_hours_vary | TEHRUSLT | true iff source = −4 |
| class_of_worker | TEIO1COW | sentinel→NULL |
| major_industry | TRMJIND1 | sentinel→NULL (13-category recode) |
| major_occupation | TRMJOCC1 | sentinel→NULL (10-category recode; system changed 2011, 2020 — see methodology) |
| weekly_earnings | TRERNWA | implied ÷100 (topcode 2884.61 through May 2024, monthly top-3% after) |
| hourly_earnings | TRERNHLY | implied ÷100 |
| school_enrolled | TESCHENR | yes/no→bool (universe: age 15–49) |
| school_level | TESCHLVL | sentinel→NULL (1 high school, 2 college) |
| spouse_or_partner_present | TRSPPRES | as-is (1 spouse, 2 unmarried partner, 3 neither) |
| spouse_or_partner_employed | TESPEMPNOT | yes/no→bool |
| household_size | TRNUMHOU | as-is |
| household_children | TRCHILDNUM | as-is (household children < 18) |
| youngest_child_age | TRYHHCHILD | sentinel→NULL |
| provided_eldercare_on_diary_day | TUECYTD | yes/no→bool; **one undocumented `0` (single 2024 case) → NULL** |
| eldercare_minutes | TRTEC | sentinel→NULL (defined only when TUECYTD=1; NULL for all years < 2011) |
| secondary_childcare_minutes | TRTCCTOT | sentinel→NULL (all children < 13) |
| time_alone_minutes | TRTALONE | sentinel→NULL (BLS-computed from who codes; excludes work/sleep) |
| time_with_family_minutes | TRTFAMILY | sentinel→NULL (same caveat) |
| final_weight | TUFNWGTP | weight — **NULL exactly for 2020** (schema-enforced) |
| pandemic_weight | TU20FWGT | weight — defined only 2019–2020 |

Respondent-file variables *not* loaded (all 133 remain in staging): unedited
labor-force detail (TU\*), job-search method fields, allocation flags
(TRHERNAL/TRWERNAL), topcode flags (TTHR/TTOT/TTWK), module respondent flags,
detailed industry/occupation codes (TEIO1ICD/TEIO1OCD — classification systems
changed four times; the stable major recodes are loaded instead), and the
remaining TRT\* time-with-whom summaries. Adding one is a migration plus one
transformer line.

## atus.household_members ← Roster file (`atusrost_0325.dat`)

| Canonical column | Source variable | Transformation |
| --- | --- | --- |
| tucaseid | TUCASEID | as-is |
| lineno | TULINENO | as-is (respondent = 1) |
| relationship | TERRP | sentinel→NULL (18/19 self, 20 spouse … 40 own nonhousehold child < 18) |
| age | TEAGE | sentinel→NULL (topcode 80 in 2003–04, 85 from 2005) |
| sex | TESEX | as-is (1 male, 2 female) |

## atus.activities ← Activity file (`atusact_0325.dat`)

| Canonical column | Source variable | Transformation |
| --- | --- | --- |
| tucaseid | TUCASEID | as-is |
| activity_number | TUACTIVITY_N | as-is (diary order) |
| activity_code | TRCODEP | code, validated (6-digit harmonized multi-year code) |
| tier1_code | TRTIER1P | code, validated (2-digit) |
| tier2_code | TRTIER2P | code, validated (4-digit) |
| start_time | TUSTARTTIM | HH:MM:SS → `time` |
| stop_time | TUSTOPTIME | HH:MM:SS → `time` (may cross midnight; final episode may pass 04:00) |
| duration_minutes | TUACTDUR24 | as-is (capped at the 04:00 diary boundary; sums to 1440/day) |
| duration_uncapped_minutes | TUACTDUR | as-is (full reported duration) |
| cumulative_minutes | TUCUMDUR24 | as-is (running within-day total, validated) |
| location_code | TEWHERE | sentinel→NULL (−1 = not collected: sleeping/grooming/personal codes) |
| secondary_childcare_minutes | TRTCCTOT_LN | sentinel→NULL |
| eldercare_minutes | TRTEC_LN | sentinel→NULL (2011+) |

Not loaded: per-episode secondary-childcare detail by child type (TRTO_LN,
TRTOHH_LN, …), interviewer-path fields (TUCC5/TUCC7/TUCC8, TUDURSTOP),
well-being module eligibility (TRWBELIG).

## atus.activity_companions ← Who file (`atuswho_0325.dat`)

| Canonical column | Source variable | Transformation |
| --- | --- | --- |
| tucaseid | TUCASEID | as-is |
| activity_number | TUACTIVITY_N | as-is |
| who_lineno | TULINENO | **as-is including −1** (structural: not a roster member / not collected; part of PK) |
| who_code | TUWHO_CODE | **as-is including −1/−2/−3** (18–62 = companion category; part of PK) |
| who_not_asked | TRWHONA | 0/1→bool |

Documented cross-year caveats: codes 59–62 introduced and 55 dropped in 2010;
who codes not collected for working episodes before 2010. Empirical finding
preserved as-is: 1,208 episodes were asked but have code −1/−2/−3.

## atus.cps_persons ← ATUS-CPS file (`atuscps_0325.dat`)

Curated 23 of 265 columns; **rows filtered to households of ATUS respondents**
(681,624 of 1,583,133 source rows; the rest — households of sampled
nonrespondents — remain in staging).

| Canonical column | Source variable | Transformation |
| --- | --- | --- |
| tucaseid, lineno | TUCASEID, TULINENO | as-is |
| cps_year, cps_month | HRYEAR4, HRMONTH | sentinel→NULL (final CPS interview timing) |
| region, division | GEREG, GEDIV | sentinel→NULL (Census region/division) |
| state_fips | GESTFIPS | zero-padded to `char(2)`, plausibility-checked |
| gemetsta / gtmetsta | GEMETSTA / GTMETSTA | sentinel→NULL — era-split pair, BLS names kept |
| household_size | HRNUMHOU | sentinel→NULL |
| household_type | HRHTYPE | sentinel→NULL |
| tenure | HETENURE | sentinel→NULL (own/rent/no cash rent) |
| hufaminc / hefaminc | HUFAMINC / HEFAMINC | sentinel→NULL — era-split family-income bands, BLS names kept |
| relationship | PERRP | sentinel→NULL (CPS codes — **not** the same code set as roster TERRP) |
| age | PRTAGE | sentinel→NULL |
| sex | PESEX | sentinel→NULL |
| education | PEEDUCA | sentinel→NULL (31–46 attainment codes) |
| race | PTDTRACE | sentinel→NULL (category count grew across years) |
| is_hispanic | PEHSPNON | yes/no→bool |
| marital_status | PEMARITL | sentinel→NULL |
| citizenship | PRCITSHP | sentinel→NULL |
| cps_labor_force_status | PEMLR | sentinel→NULL (CPS-time status; ATUS-time status is `respondents.labor_force_status`) |

## atus.replicate_weights ← Replicate weights file (`atuswgts_0325.dat`)

`tucaseid` as-is; `tufnwgtp001`…`tufnwgtp160` ← TUFNWGTP001…160, *weight*
transformation (2020 rows are all −1 in the source → all NULL, validated).

## atus.pandemic_replicate_weights ← Pandemic replicate weights file (`atuswgtspan_1920.dat`)

`tucaseid` as-is; `tu20fwgt001`…`tu20fwgt160` ← TU20FWGT001…160, *weight*.
(The archive's `Replwgts1920_diarydate.xlsx` — diary dates for partial-period
estimation — is not staged; diary dates come from the Respondent file.)

## atus.activity_tier1 / activity_tier2 / activity_codes ← Coding lexicon PDF

Extracted from `lexiconnoex0325.pdf` by `scripts/extract_lexicon.py` into the
committed `data/reference/activity_lexicon_0325.csv` (18 / 107 / 431 entries);
`harmonization_note` preserves the BLS notes about absorbed retired codes.
Extraction was verified two independent ways: the 431 codes exactly match the
Activity Summary file's `t`-columns, and every `TRCODEP` in the 4,994,172
episodes resolves against it (enforced permanently as an FK).

## Provenance metadata

`data/manifest.json` (working copy) and `atus.source_files` (in-database, per
ingestion run) record for every source file: official URL, zip name, SHA-256,
size, download/staging timestamps, staged row count, loaded table and loaded
row count. BLS publishes no official checksums, so hashes document what this
pipeline saw rather than an external ground truth.
