# HTTP equivalence report

**PASS**: 544 of 544 specs passed; 84 of 84 GET requests passed.

Generated 2026-10-21T02:45:26Z by scripts/equivalence (harness version 1). Spec directory: `$REPO/docs/evidence/phase5/equivalence/specs` (5 files).

## Servers

| Target | What | Code | Database | Data version |
|---|---|---|---|---|
| a | Phase 1-4 API (baseline commit) on the full database | `387fc2d29919ade9620c0ca23d2b72f89e0de8f8` | atus@localhost:5434/atus | `0325:run1:1788919495952073` |
| b | Phase 5 API on the full database | `283f8198e19d31ea733ace1171f94488b28b471f` | atus@localhost:5434/atus | `0325:run1:1788919495952073` |
| c | Phase 5 API on the serving database | `283f8198e19d31ea733ace1171f94488b28b471f` | atus_api@localhost:5466/atus | `0325:serving-20261020T224252Z-a7420a5c:de834e672474` |

## Criterion

Standard errors and CI bounds (`ci_lower`, `ci_upper`, `standard_error`): |a - b| <= REL_TOL * max(|a|, |b|, S); S = the estimate's point value (for a comparison's difference also both groups' values); every other field exactly equal (REL_TOL = 1e-12). Status codes identical; error bodies identical (b vs c); a-vs-b differences only where an intended Phase 5 change explains them exactly.

## Results by status

| Status | Specs |
|---|---|
| equivalent | 544 |
| intended_difference | 0 |
| mismatch | 0 |
| server_error | 0 |
| incomplete | 0 |

## Pairs

| Pair | Requests | Values compared | Outcomes | Max relative SE/CI diff | Max scaled diff (criterion ratio) |
|---|---|---|---|---|---|
| a_b (Phase 1-4 vs Phase 5 code, full database) | 628 | 34,333 | equivalent 621, intended_difference 7 | 1 (compare-edge-032) | 4.24e-15 (errors-weird-068) |
| b_c (full vs serving database, Phase 5 code) | 628 | 34,335 | equivalent 628 | 0.178 (pooled-trends-077) | 2.79e-15 (errors-weird-056) |

The relative difference can be large on zero-variance estimates (noise around 0); the criterion bounds the scaled one.

## By category

| Category | equivalent | intended_difference | mismatch | server_error | incomplete |
|---|---|---|---|---|---|
| compare-edge | 108 | 0 | 0 | 0 | 0 |
| errors-weird | 110 | 0 | 0 | 0 | 0 |
| measures-weights | 109 | 0 | 0 | 0 | 0 |
| pooled-trends | 108 | 0 | 0 | 0 | 0 |
| population | 109 | 0 | 0 | 0 | 0 |

## Failed specs (0)

None.

## GET endpoints

| Path | b vs c | a vs b | Answers |
|---|---|---|---|
| `/health` | equivalent | intended_difference (health_reports_build) | a: 200, b: 200, c: 200 |
| `/api/v1/meta` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/presets` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/population/metadata` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?level=1` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?level=2` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?level=3` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?parent=01` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?parent=0101` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?parent=18&level=2` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=sleep` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=care` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=%25%25` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=_l` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=caf%C3%A9` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities?search=x` | equivalent | equivalent | a: 422, b: 422, c: 422 |
| `/api/v1/activities?level=7` | equivalent | equivalent | a: 422, b: 422, c: 422 |
| `/api/v1/activities?parent=1` | equivalent | equivalent | a: 422, b: 422, c: 422 |
| `/api/v1/activities?search=ab%00` | equivalent | intended_difference (control_characters_rejected) | a: 500, b: 422, c: 422 |
| `/api/v1/activities/99` | equivalent | intended_difference (lexicon_wording) | a: 404, b: 404, c: 404 |
| `/api/v1/activities/9999` | equivalent | intended_difference (lexicon_wording) | a: 404, b: 404, c: 404 |
| `/api/v1/activities/999999` | equivalent | intended_difference (lexicon_wording) | a: 404, b: 404, c: 404 |
| `/api/v1/activities/abc` | equivalent | intended_difference (lexicon_wording) | a: 404, b: 404, c: 404 |
| `/api/v1/activities/0101011` | equivalent | intended_difference (lexicon_wording) | a: 404, b: 404, c: 404 |
| `/api/v1/no-such-route` | equivalent | equivalent | a: 404, b: 404, c: 404 |
| `/api/v1/activities/01` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/02` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/03` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/04` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/05` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/06` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/07` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/08` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/09` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/10` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/11` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/12` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/13` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/14` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/15` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/16` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/18` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/50` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0105` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0202` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0203` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0205` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0303` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0405` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0499` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0504` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0703` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0799` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/0904` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1002` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1099` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1102` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1201` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1506` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1809` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1813` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1815` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/1899` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/020203` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/020401` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/030103` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/030203` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/040102` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/040105` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/040506` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/049999` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/060104` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/060202` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/100102` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/120310` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/130116` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/130130` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/130231` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/150106` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/180501` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/180601` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/180903` | equivalent | equivalent | a: 200, b: 200, c: 200 |
| `/api/v1/activities/181081` | equivalent | equivalent | a: 200, b: 200, c: 200 |

## Intended Phase 5 differences (a vs b)

| Difference | Source | Requests | What changed |
|---|---|---|---|
| `control_characters_rejected` | audit F50 | 1 | Control characters in query parameters are refused with 422 `validation_error` (the baseline let a NUL reach PostgreSQL: 500). |
| `lexicon_wording` | audit F23 | 5 | `activity_not_found` names the loaded release's lexicon as 'the <span> activity lexicon' (release-neutral text); code and details unchanged. |
| `health_reports_build` | Phase 5 contract (ATUS_BUILD_SHA) | 1 | GET /health also reports `build`, which the deploy workflow polls. |

## Whitelisted fields

| Field | Pairs | Differed in | Why |
|---|---|---|---|
| body key `data_version` | a_b, b_c | 0 | The identity of a data build (`<release>:serving-<build_id>:<hash>` on the serving DB, `<release>:load-...` or the legacy `<release>:run<id>:...` on the full DB). It differs between every two databases by design and is not a statistic (docs/operations.md, equivalence criterion, 'What it does not cover'). No response body carries it today; the rule keeps the harness valid if /meta starts to. |
| body key `analysis_key` | a_b, b_c | 0 | sha256 of operation + canonical spec + analytics version + data version: it differs exactly when data_version does. Sent as the X-Analysis-Key header, not in bodies, today. |
| header `x-data-version` | all | (not compared) | data build identity (see the data_version rule); checked instead against the version each server reported at start |
| header `x-analysis-key` | all | (not compared) | hash including the data version (see the analysis_key rule) |
| header `x-request-id` | all | (not compared) | generated per request |
| header `x-cache` | all | (not compared) | cache state (hit/miss/join) depends on request order, not on data |
| header `server-timing` | all | (not compared) | timings |
| header `date` | all | (not compared) | time of the response |
| header `content-length` | all | (not compared) | follows the body, which is compared itself |

Considered and deliberately **not** whitelisted (compared exactly):

- /meta data.last_ingested_at, data.ingestion_run_id, data.release: Ingestion provenance. The serving builder copies the source run's id, release and finish time into the serving DB's ingestion_runs row (ADR-013; only data_version and notes are new), so full and serving answer the same. A builder that stopped doing so is caught here, and a rule must then be added deliberately.
- /meta data.respondents, data.years: Facts about the served data: equal by construction of the serving DB.

## Expectation misses (0)

Specs whose Phase 5 full-DB answer (b) was not what the spec expected (`ok` = 2xx). Not an equivalence failure.
