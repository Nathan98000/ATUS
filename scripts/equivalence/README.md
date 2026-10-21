# HTTP equivalence harness (brief §9)

Replays the same requests against three API servers and diffs the answers:

| Target | Code | Database |
|---|---|---|
| `a` | Phase 1-4 API, a checkout of commit `387fc2d` | full database |
| `b` | Phase 5 API, this checkout | full database |
| `c` | Phase 5 API, this checkout | serving database (a restored release artifact) |

`b` vs `c` is the statistical-equivalence claim of the serving database at the HTTP
level. `a` vs `b` shows that Phase 5's code changed no statistic and no domain answer,
only what it lists as intended. The engine-level counterpart is `atus compare-serving-db`.
Both use the same criterion code (`atus_pipeline.serving.equivalence`).

## Running it

```sh
# once: a detached worktree of 387fc2d outside this checkout, with its own venv
# installed from requirements/api.lock.txt (the same library versions as Phase 5)
scripts/equivalence/run.sh setup

# the replay: starts a, b and c, replays, stops them, writes the report
scripts/equivalence/run.sh --specs DIR --out OUT \
    --serving-secrets-file ~/.config/atus/neon.env
```

`run.sh` prepares the baseline (at `$ATUS_EQ_BASELINE_DIR`, default
`~/.cache/atus/baseline-387fc2d`) and then runs `replay.py`, which you can also call
directly (`python scripts/equivalence/replay.py --help`). Useful options:
`--targets bc` (full vs serving only, no baseline needed), `--category NAME`, `--id ID`,
`--failed-from OLD/report.json` (replay only what failed before),
`--limit N`, `--jobs N` (specs in flight, default 1; each goes to all servers at once.
`a` and `b` share the full database, and 2 jobs once filled its container's 64 MiB
`/dev/shm`, audit F33),
`--ports 8641,8642,8643`, `--no-gets`, `--save-responses none|failures|all`,
`--fail-on-unexpected`.

Databases. Connection strings are never accepted on the command line, printed or written.
Reports name databases as `user@host:port/dbname`.

- Full: `$ATUS_EQ_FULL_DATABASE_URL`, default the local development database.
- Serving: `--serving-secrets-file`, the `KEY=value` file `atus restore-serving-db`
  writes. The harness uses `ATUS_API_DATABASE_URL` (the read-only API role) and falls
  back to `ATUS_OWNER_DATABASE_URL`. Alternatively, set `$ATUS_EQ_SERVING_DATABASE_URL`.
  If the serving database is the full database, the harness refuses to run, because
  comparing a database with itself proves nothing.

Every server connects with `default_transaction_read_only=on`. Each server runs `atus api`
from its own code base, on loopback, with a clean environment. Its working directory is
under `OUT/run/`, and the harness refuses to start if a `.env` file could reach a server.
The Phase 5 servers run in development mode without edge authentication. When edge
authentication is off, loopback clients are exempt from the per-client buckets. Their
limits are set so that no capacity answer replaces a result: daily cap 1,000,000, four
analyses at once, a 120 s lane wait, and a 10-minute statement timeout (the baseline has
none). All other settings keep their defaults. `servers.phase5_env` lists them, and the
report records them.

Retries:

- Capacity answers are retried up to `--retries` times: 429, and 503 `overloaded`,
  `database_unavailable` or `daily_capacity_reached`. `DiskFull` on the container's
  64 MiB `/dev/shm` arrives as `overloaded`.
- Lost connections are retried too.
- `analysis_timeout` and a request with no answer within `--request-timeout` are not
  retried. At a 10-minute statement timeout they mean a plan that would time out again,
  and they are reported as failures.

Nothing is timed.

Exit status: 0 everything passed, 1 a spec or GET failed (see `summary.md`), 2 a usage,
input or start-up error.

## Specs

Every `*.jsonl` (or `*.ndjson`) file in `--specs` holds one JSON object per line:

```json
{"id": "sleep-2024", "endpoint": "estimate", "category": "smoke", "expect": "ok",
 "body": {"activity": {"preset": "sleep"}, "years": [2024]}}
```

- `id`: unique across all files.
- `endpoint`: `estimate`, `trend` or `compare`, or the full path, for example
  `/api/v1/analysis/estimate`.
- `body`: serialized compactly with key order kept. A string body is sent verbatim, for
  malformed JSON.
- `expect`: what `b` should answer. It takes one of these forms:
  - `ok` (2xx), `error`, or `any` (not asserted);
  - `error:<code>`;
  - `{"outcome": "ok"|"error"|"any", "status": 422, "code": "..."}`, where `status` and
    `code` are optional and other keys are ignored.

  A miss is listed in the report and is not an equivalence failure, unless
  `--fail-on-unexpected` is set.
- `content_type` (optional): replaces `application/json`.
- `category` (optional): groups results in the report.
- `note` (optional): copied into the report.
- Any other key is ignored.

Spec sources:

- `specs/smoke.jsonl` (committed) has 28 quick specs. They cover the three operations,
  zero-variance and self-comparison estimates, pandemic weights, every kind of refusal,
  and the protocol cases.
- `make_specs.py --out DIR --random N` writes the engine-level diff's fixed extreme cases
  plus N seeded random specs, as API requests.
- `docs/evidence/phase5/equivalence/specs/` holds the 544 generated specs of the Phase 5
  run (5 categories), next to that run's report and summary.

The GET endpoints are always compared too, unless `--no-gets` is given:

- `/health`, `/api/v1/meta`, `/api/v1/activities` (and filtered or searched variants,
  including 422s, a non-ASCII term and a control character), `/presets`, and
  `/population/metadata`;
- unknown and malformed activity codes;
- every tier-1 code, plus a seeded sample of tier-2 and 6-digit codes
  (`--activity-sample`).

## Rules (`httpdiff.py`)

Status codes must be identical in both pairs. Then:

- **2xx bodies** follow the §9 criterion. Every field is exactly equal (same JSON type and
  value): point estimates, `n_respondents`, `n_participants`,
  `weighted_population_per_day`, `days_in_period`, labels, warnings, the echoed `spec`,
  and `analytics_version`. The only exceptions are `standard_error`, `ci_lower` and
  `ci_upper`, which must satisfy `|a - b| <= 1e-12 x max(|a|, |b|, S)`.
  - `S` is the point value of the estimate that the bound belongs to. For a comparison's
    difference, `S` also includes both groups' values.
  - This floor is what makes zero-variance estimates comparable. All 18 tier-1 codes add
    up to 1,440 minutes, and their "SE" is noise around 0: 7.83e-12 vs 7.93e-12.
  - Point values, counts and populations never get a tolerance.
- **Error bodies** (code, message, details) must be identical between `b` and `c`.
- **Headers**: between `b` and `c`, `Content-Type`, `Cache-Control`, `X-Analytics-Version`
  and `X-Response-Format` must be equal. Every Phase 5 answer's `X-Data-Version` must
  equal the version its server reported at start. Otherwise the data changed mid-run,
  and that is a mismatch that nothing explains away.
- **Whitelisted fields** (`WHITELIST`, `WHITELISTED_HEADERS`) are the only ones allowed
  to differ, and each has a justification in the code and in the report:
  - body keys `data_version` and `analysis_key`, at any depth (no body carries them
    today);
  - headers `X-Data-Version`, `X-Analysis-Key`, `X-Request-ID`, `X-Cache`,
    `Server-Timing`, `Date` and `Content-Length`.

  `/meta`'s `last_ingested_at` and `ingestion_run_id` are deliberately not whitelisted:
  the serving builder copies the source run's provenance, so they must match.
- **`a` vs `b`**: a difference passes only if exactly one of the intended Phase 5 changes
  below explains it. Domain errors (`invalid_spec`, `unknown_activity`, `invalid_analysis`,
  `unsupported_analysis`, `insufficient_data`) have no such entry, so their code and
  message must be identical.

| Intended difference | Source | Recognized when |
|---|---|---|
| `validation_details_capped` | F04 | Phase 5's own `validation_details()` applied to the baseline's 422/400 details reproduces Phase 5's details exactly; code and message are equal |
| `preset_max_length` | F04 | `activity.preset` is over 64 characters and Phase 5 answers 422 `string_too_long` at `body.activity.preset` |
| `payload_too_large` | F03 | The body is over 65,536 bytes and Phase 5 answers 413 |
| `unsupported_media_type` | ADR-012 | A POST that is not `application/json` gets 415 |
| `malformed_json_parser_limit` | error contract | The baseline answers `http_400` and Phase 5 answers `malformed_json` (the parser gave up, e.g. on deep nesting) |
| `control_characters_rejected` | F50 | The query has a control character and Phase 5 answers 422 (the baseline answered 500) |
| `lexicon_wording` | F23 | `activity_not_found` reads "... 2003-25 activity lexicon." instead of "... 2003-25 lexicon."; code and details are equal |
| `health_reports_build` | contract | `/health` adds `build` |

A request's status is one of the following. `equivalent` and `intended_difference`
pass.

- `equivalent`
- `intended_difference`
- `mismatch`
- `server_error`: a Phase 5 server answered 5xx. A baseline 5xx is judged by the a-b pair.
- `incomplete`: no answer, or only capacity answers, after the retries.

## Output

- `OUT/report.json` holds:
  - the inputs (spec files with their sha256);
  - for each server: commit (and whether it was dirty), interpreter, library versions,
    database (no password), the data version it holds and the version it served, its
    settings, and its `/meta` data;
  - the criterion, the whitelist and the intended differences;
  - a summary with counts by status, category and endpoint, the largest relative and
    scaled SE/CI differences per pair (and which spec produced them), and expectation
    misses;
  - one entry per spec and per GET: each target's status and error code, and for each
    pair the outcome, the values compared, `max_relative`, `max_scaled`, and mismatches
    as `{path, detail}` with JSON paths such as `$.points[3].estimate.standard_error`.
- `OUT/summary.md`: the same, for people.
- `OUT/logs/{a,b,c}.log`: the server logs. Connection-string passwords are masked when
  each server stops.
- `OUT/responses/`: the answers to failed requests, or to all of them with
  `--save-responses all`.

## Tests

- `tests/unit/test_equivalence_harness.py` covers the rules, including the zero-variance
  floor, the whitelist, every intended difference and its negative cases, the spec format,
  the report, the server environments (validated by the real `load_settings`), process
  start and stop with log redaction, retries, and set-up checks.
- `tests/integration/test_equivalence_harness.py` runs `b` and `c` as real servers on the
  hand-computed fixture and on a serving database built from it. It checks that they are
  equivalent, and that a serving copy with one changed episode and one replicate weight
  off by 1e-6 is caught at `$.estimate.value` and `$.estimate.standard_error`.
