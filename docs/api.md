# API Reference (Phase 3)

A read-only, versioned HTTP service around the Phase 2 analytical engine.
The API validates requests, calls `AnalysisEngine`, serializes its result
objects, maps domain errors to HTTP, and caches deterministic results — it
contains **no statistical logic**. Everything an estimate means (weights,
denominators, variance, 2020 rules) is defined in
[analytics.md](analytics.md) and computed by the engine the CLI also uses.

```text
CLI ────────┐
            ├──→ AnalysisEngine (Phase 2) ──→ PostgreSQL (Phase 1)
HTTP API ───┘
```

## Running locally

```bash
docker compose up -d          # PostgreSQL (see README for load steps)
atus api                      # http://127.0.0.1:8000
atus api --port 8080 --reload # dev mode
# equivalently: uvicorn --factory atus_pipeline.api.app:create_app
```

Interactive OpenAPI docs: <http://127.0.0.1:8000/docs> (spec at
`/openapi.json`). Startup is instant: the connection pool connects in the
background and `/ready` reports 503 until the database is reachable and
loaded. Readiness assumes the database passed `atus validate-db` at load
time; it does not re-run data-quality checks.

Configuration (same `.env` mechanism as the pipeline):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATUS_DATABASE_URL` | compose instance on :5434 | analytical database |
| `ATUS_API_CORS_ORIGINS` | *(empty — no cross-origin access)* | comma-separated allowed frontend origins, e.g. `http://localhost:3000` |
| `ATUS_API_CACHE_SIZE` | `256` | analysis-result cache entries (LRU) |

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /api/v1/analysis/estimate` | one (possibly pooled multi-year) weighted estimate |
| `POST /api/v1/analysis/trend` | per-year series with explicit unavailable points |
| `POST /api/v1/analysis/compare` | two populations + covariance-correct difference |
| `GET /api/v1/activities` | lexicon listing (`?level=`, `?parent=`, `?search=`) |
| `GET /api/v1/activities/presets` | named selections incl. BLS table categories |
| `GET /api/v1/activities/{code}` | one code with ancestry, children, leaf count |
| `GET /api/v1/population/metadata` | supported population filters + valid values |
| `GET /api/v1/meta` | API/analytics/data versions + capability discovery |
| `GET /health`, `GET /ready` | liveness / readiness (unversioned) |

The API is strictly read-only: no CRUD, no persistent analysis storage, no
authentication (public analytical access), no raw-microdata download. It
exposes domain concepts, never database tables.

## Analysis requests

The request body **is** the canonical Phase 2 analysis specification — the
`spec` field of every response can be resubmitted verbatim. Example files
live in [examples/api/](examples/api/) and are also embedded in the OpenAPI
schema.

```bash
curl -X POST http://localhost:8000/api/v1/analysis/estimate \
  -H "Content-Type: application/json" \
  -d @docs/examples/api/sleep_2025_estimate.json
```

```json
{
  "measure": "average_minutes_per_day",
  "activity": {"preset": "sleep"},
  "years": [2025],
  "population": {"age_min": 25, "age_max": 54},
  "weights": "multiyear",
  "variance": "replicate",
  "confidence_level": 0.95
}
```

Field notes (full semantics in [analytics.md](analytics.md)):

- `activity` — `{"preset": name}` (see `/activities/presets`) **or**
  `{"include": [...], "exclude": [...], "label": ...}` with 2/4/6-digit
  lexicon codes; a tier code means itself plus all descendants.
- `years` — order-insensitive (canonicalized ascending); duplicates rejected.
- `population` — any subset of the dimensions in `/population/metadata`;
  filtering on a dimension excludes respondents missing that dimension.
- `weights` — `multiyear` (TUFNWGTP; **refuses 2020**) or `pandemic`
  (TU20FWGT; 2019/2020 only, collection-window semantics).
- `variance: "none"` skips replicate SEs (faster; `standard_error` is null).

Limits: ≤ 50 years per request; ≤ 100 `include` and ≤ 100 `exclude` codes
per activity selection. Bodies with unknown fields are rejected (422) to
catch typos.

## Responses

Responses are the engine result objects' `to_dict()` — identical structure to
the CLI's `--json` output. Key semantics:

- `estimate.value` + `estimate.unit` (`minutes_per_day`,
  `proportion_of_population` 0–1, `minutes_per_day_of_participants`,
  `persons_per_day`) with full float precision — formatting is the client's job.
- `n_respondents` / `n_participants` are **unweighted sample sizes**;
  `weighted_population_per_day` is the persons represented on an average day.
  These are never collapsed into an ambiguous "count".
- `warnings` are methodological notes clients should display (2020 window
  semantics, CI truncation, harmonized coding) — cached responses carry
  exactly the same warnings.
- Trend points for years that cannot be estimated have `estimate: null` plus
  `unavailable_reason` — never zero.
- `spec` is the canonical analysis specification; with the same
  `analytics_version` and data release (see `/meta`), resubmitting it
  reproduces the result.

Response headers: `X-Request-ID` (log correlation), `X-Cache` (`hit`/`miss`),
and `X-Analysis-Key` — the deterministic identifier of the analysis
(SHA-256 of operation + canonical spec + analytics version + data version). It is a
reproducibility/cache key, not a stored record ID.

## Errors

One envelope everywhere:

```json
{"error": {"code": "unsupported_analysis", "message": "…", "details": null}}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `malformed_json` | body is not valid JSON |
| 422 | `validation_error` | schema violation (wrong types/enums/unknown fields); `details.errors` locates each problem |
| 422 | `invalid_spec` | structurally valid but domain-invalid (age_min > age_max, duplicate years, unknown preset) |
| 422 | `unknown_activity` | activity code not in the harmonized lexicon |
| 422 | `unsupported_analysis` | methodologically unsupported (e.g. 2020 under `multiyear`); the message explains the alternative |
| 422 | `insufficient_data` | empty population / subgroup too small for replicate variance |
| 404 | `activity_not_found` | `GET /activities/{code}` for a nonexistent code |
| 503 | `database_unavailable` | database unreachable (any database-backed endpoint) |
| 503 | `schema_not_initialized` | schema missing (any database-backed endpoint; also `/ready`) |
| 503 | `data_not_loaded` | `/ready` only: schema exists but no ATUS data is loaded |
| 500 | `internal_error` | unexpected failure — details are logged server-side, never returned |

The engine remains the authoritative validator: API-level validation exists
for fast, well-located client errors, and anything it cannot check cheaply
(lexicon membership, weight-scheme rules, population feasibility) flows
through to the engine and comes back as a structured 422.

## Caching

Server-side: complete successful analysis payloads are cached in an
in-process, thread-safe LRU keyed by

```text
sha256(operation + canonical spec + analytics_version + data_version)
```

where `data_version` = `<release>:run<latest successful ingestion run>` —
so reloading data or changing the statistical implementation invalidates
keys by construction (no TTLs needed for correctness; the LRU bounds
memory). Equivalent requests share keys (years and code lists are
canonicalized), errors are never cached, a cache hit returns the
byte-identical payload including warnings, and a cache failure degrades to
recomputation — never to an error. The backend is behind a small protocol so
a multi-process deployment can swap in a shared store (e.g. Redis) later;
in-process is documented as the single-process development default.

Concurrent-reload guard: if `atus load` finishes while a request is
computing, the data version is re-read before storing and the result is
returned uncached (`X-Cache: bypass`) instead of being stored under the
pre-reload key. Residual caveat: a request whose statements straddle the
reload commit can still *observe* mixed-snapshot data once (standard READ
COMMITTED behavior); such results are never cached, so they cannot persist.

HTTP: metadata endpoints (`/meta`, `/activities*`, `/population/metadata`)
send `Cache-Control: public, max-age=300`. Analysis responses are POST
(bodies too complex for GET) and rely on the server-side cache; ETags are
deliberately not implemented since conditional requests don't apply to POST
in practice.

Measured effect (in-process client, full 2003–25 database, Sept 2026):

| Request | uncached | cached |
| --- | --- | --- |
| estimate, 1 year, `variance: none` | 0.31 s | 0.006 s |
| estimate, 1 year, replicate SE | 0.97 s | 0.005 s |
| estimate, pooled 22 years, replicate SE | 11.0 s | 0.004 s |
| trend 2003–2025 with per-year SEs | 11.7 s | 0.003 s |
| compare (two groups, SEs) | 1.7 s | 0.005 s |

Expensive multi-year variance requests stay **synchronous** (no job queues,
polling, or websockets) — the worst case is ~12 s once per (spec × version),
then cached. Phase 5 can revisit async execution if production traffic
justifies it.

## Versioning

Three independent versions, all visible in `GET /api/v1/meta`:

| Version | Meaning | Changes when |
| --- | --- | --- |
| `api_version` (`v1`, path prefix) | HTTP contract | request/response semantics change incompatibly |
| `analytics_version` (`0.2`) | statistical implementation | computed numbers could differ |
| data release + ingestion run (`0325`, `run N`) | loaded BLS data | `atus load` rebuilds the database |

## Security posture

Read-only endpoints over parameterized queries only — client input is never
interpolated into SQL (activity codes travel as bound parameters; search
terms are LIKE-escaped; identifiers come from fixed internal vocabularies).
CORS is off by default and enabled per-origin via `ATUS_API_CORS_ORIGINS`
(no credentials). Errors never expose stack traces, SQL, paths, or
configuration. No authentication (public statistical data, no user state)
and no rate limiting — a public production deployment would need traffic
controls (Phase 5); none are claimed here. One deliberate exception to the
no-internal-hints rule: `/ready` failure bodies include operator remediation
hints (it is an infrastructure endpoint for supervisors, not an analytical
surface).

## Testing

```bash
pytest tests/unit/api                      # contract, error mapping, cache (no DB)
pytest tests/integration/api               # full stack against the fixture test DB
ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_api_benchmarks.py
                                           # BLS benchmarks replayed through HTTP
python scripts/benchmark_api.py            # performance baseline
```
