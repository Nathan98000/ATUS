# Frontend (Phase 4, hardened in Phase 5) — ATUS Explorer

A client-side React application through which a nontechnical user can explore
ATUS time-use estimates. It consumes the Phase 3 HTTP API as its **single
source of numerical truth**:

```text
UI components
    ↓
feature/state layer (builder state, TanStack Query server state)
    ↓
endpoints.ts → static snapshot (exact API responses, written at deploy)
            ↘ API client (src/api/client.ts — the only fetch to the API)
    ↓
Phase 3 HTTP API  →  Phase 2 analytical engine  →  PostgreSQL
```

The static snapshot holds byte-for-byte copies of earlier API responses for
the same data release; it never computes anything either.

The frontend presents analyses; it never performs them. No statistical
formula, no interpolation, no client-side difference or CI arithmetic exists
in this codebase — presentation-only unit formatting (`528.75` → “8h 49m”)
is the entire extent of numeric transformation (`src/utils/format.ts`).

## Technology

| Concern | Choice | Why (details in ADRs) |
| --- | --- | --- |
| Build/dev | Vite 8, TypeScript (strict) | standard, fast, no SSR needed ([ADR-007](adr/ADR-007-frontend-stack.md)) |
| UI | React 19 | ecosystem default, function components only |
| Routing | react-router 8 (declarative) | four routes; analyses live in URLs |
| Server state | TanStack Query 5 | caching, cancellation, race safety ([ADR-008](adr/ADR-008-server-state.md)) |
| Charts | hand-rolled SVG components | 2020-gap correctness, CI bands, keyboard access, tiny bundle ([ADR-010](adr/ADR-010-visualization.md)) |
| Styling | plain CSS, design tokens in `src/styles/global.css` | no framework needed at this size |
| Unit/component tests | Vitest + Testing Library + MSW | fixtures are captured real API payloads |
| E2E tests | Playwright | real API on the fixture database |
| Lint/format | oxlint (with jsx-a11y rules) + Prettier | template default, fast |

## Layout

```text
frontend/src/
  api/        generated.ts (OpenAPI-generated types), types.ts (aliases),
              apiBase.ts (same-origin default), client.ts (single HTTP
              boundary), compat.ts (API version check), resilience.ts
              (time-budgeted retries), serviceStatus.ts (waking + data
              version stores), snapshot.ts (static snapshot), prewake.ts,
              endpoints.ts, queries.ts
  domain/     urlSpec.ts (shareable-URL encoding), describe.ts (titles,
              labels, plain-language interpretation), presentError.ts,
              prewarm.ts (starter examples + prewarmed snapshot specs)
  features/
    activities/  lexicon.ts (client index), ActivityPicker
    builder/     state.ts (builder ⇄ request spec), BuilderControls
    population/  PopulationFields (generated from population metadata)
    results/     EstimateView, TrendView + TrendChart, CompareView +
                 ComparisonChart, MethodologyPanel, ShareButton, chartScale
  components/ AppShell, ErrorBoundary, LoadingPanel, ErrorPanel, WarningBanner,
              ServiceNotice (waking / reload notices), ServiceInterruption
              (full-page paused-quota and incompatible-API states)
  pages/      Home, Explore (builder), Analysis (result), About, NotFound
  utils/      format.ts (precision policy), stableStringify.ts
  test/       MSW server (+ snapshot publisher), captured fixtures, render helpers
frontend/scripts/
  snapshot-requests.mjs  prints the prewarmed analysis requests for the snapshot
  check-bundle.mjs       fails if a build still points at a development API
frontend/public/boot.css  styles the boot shell in index.html
```

## API integration

- **Types are generated from the backend's own OpenAPI schema.**
  `python scripts/export_openapi.py` writes `frontend/openapi.json`;
  `npm run generate:api-types` regenerates `src/api/generated.ts`.
  Both artifacts are committed; regenerate and commit after any API schema
  change. `src/api/types.ts` re-exports friendly aliases — the only names
  the app imports.
- **One client.** `apiRequest` owns the base URL, JSON handling, protocol
  headers and error translation.
  - **Base URL.** Empty by default: the app calls `/api/v1/...` on its own
    origin, which is how the public deployment works (one origin serves the
    SPA and proxies `/api`, ADR-011). `VITE_API_BASE_URL` opts into another
    origin and must be an absolute http(s) URL; any other value fails the
    build. There is no fallback to a developer address (audit F24).
  - **Errors.** An error status becomes `ApiError`
    (status/code/message/details/request id/`retryAfterSeconds`). With the
    API's envelope the code is the API's; an error page that is not the
    API's JSON gets a synthesized code: `edge_daily_limit` for a 429 (the
    edge's daily request allowance is used up), `upstream_unavailable` for a
    5xx (a host or proxy page), `malformed_response` otherwise. A request
    that gets no answer, exceeds its time limit, or returns a success status
    whose body is not JSON becomes `NetworkError`. `Retry-After` (seconds or
    an HTTP date; else `details.retry_after_seconds`) is kept on the error.
  - **Headers read.** `X-Analysis-Key`, `X-Cache` and `X-Data-Version`.
    Across origins a browser only exposes them if the API lists them in
    `Access-Control-Expose-Headers`; same-origin (production) always does.
  - **Bodies.** Analysis specs are serialized once with `stableStringify`
    (sorted keys) and those exact bytes are sent, so one specification is
    always one byte sequence — the key of the edge cache and the snapshot.
- **Discovery-driven UI.** Years, measures, weight schemes, population
  dimensions and values, activity taxonomy, and presets all come from
  `/meta`, `/population/metadata`, and `/activities*` — nothing analytical
  is hard-coded. A capability the UI does not recognize is ignored (never a
  crash); one the server stops reporting disappears from the UI.
- The 556-entry lexicon (~70 KB) is fetched once per session and searched
  client-side — activity search performs zero network requests.

## Server state and caching

Metadata queries load once per page (`staleTime: Infinity`) and never
refetch in the background — not on window focus, not on reconnect — because
a focus refetch could wake a sleeping free-tier host for nothing (audit
F62). `/meta` is always fetched with `cache: "no-cache"`, so a reload never
pairs a `/meta` from the browser's HTTP cache with newer data (F27). TanStack
Query's own retries are off; `resilience.ts` retries instead (below).

**API compatibility.** `/meta` must report API major version `v1` and the
shape this build reads (`compat.ts`). Anything else — `v2`, a missing
version, a renamed field — is an `IncompatibleApiError`: never retried,
never papered over with defaults, and shown as one full-page state
("This page and the analysis service are out of step", with a reload
button) instead of crashing a route into the error boundary (F25).

**Data version.** Every page is tagged with the data version its metadata
came from: `X-Data-Version` from the API (or the static snapshot's
`data_version`), else a `data_version` field in the `/meta` body, else the
legacy `<release>:run<id>` tag of APIs that report neither. Analysis results
are cached under

```text
['analysis', operation, analytics_version:data_version, canonicalSpecJSON]
```

— the same versioning rule as the server's own cache, so results of
different releases never share an entry. A loaded page does not learn of a
data swap by itself: every later response's `X-Data-Version` is compared
with the loaded one (`serviceStatus.ts`), and a difference shows a polite
"different data release — reload" notice. (Before Phase 5 the docs said the
key invalidated "by construction"; that only held after a reload, and only
with a globally unique data version — audit F06/F27.)

Analyses are retried automatically only in the cold-start cases below;
superseded requests are aborted via TanStack Query's signal, and stale
responses cannot overwrite newer ones because every spec is its own key.
A failed background refetch never unmounts the builder or hides a displayed
result: pages show a page-level error only while nothing is loaded (F26),
and Explore's "Try again" refetches every failed query (F63).

## Cold start, retries and service states

The free API host sleeps when idle and takes about a minute to wake
(ADR-011). The client therefore treats a slow or failing start as a wait,
not an error (`resilience.ts`, audit F11, F12, F64):

| Failure | Metadata GET | Analysis POST |
| --- | --- | --- |
| No answer / connection error, `service_waking`, `upstream_unavailable`, other 502/503/504/52x | retried until 150 s have passed | same |
| `overloaded`, `database_unavailable` (503) | retried until 150 s | retried **once** |
| `rate_limited` (429) with `Retry-After` | retried until 150 s | never (visitor decides) |
| client time-out (60 s per metadata attempt, 150 s per analysis) | retried until 150 s | never |
| anything else (4xx, quota states, `analysis_timeout`, `data_version_mismatch`, incompatible API) | never | never |

Every wait honours `Retry-After` (at least 1 s); without it the backoff is
1, 2, 4, 8, 8, … s. Being offline is not a cold start and is not retried.

**Waking notice.** "Waking up the analysis service — free hosting sleeps
when idle; this can take up to a minute." appears when a metadata request
has been pending for 3 s, while an attempt has just failed as "waking", or
while the edge's prewake said the origin is waking and an API call is
pending. It lives in a persistent `<output>` (polite live region) at the top
of the page, never moves focus, and comes with no progress bar — the service
reports no progress, so none is invented. A shared link whose analysis is
still waiting for `/meta` says "Preparing the analysis…", never
"Calculating…": no analysis has been sent yet.

**Prewake.** On user intent only — entering Explore, or opening an analysis
the static snapshot does not hold — the app sends `GET /api/v1/wake`
(fire-and-forget, at most once per 10 minutes) so the host's spin-up
overlaps with the time spent building a spec. The edge Worker answers it
without touching the database. It is only sent where that Worker exists,
which a deployed snapshot indicates; development and E2E never send it.

**Presentations** (`domain/presentError.ts`, F13). Every code of the v1
contract and of the edge has its own public-facing copy:

| Code | Presentation |
| --- | --- |
| `invalid_spec`, `validation_error`, `malformed_json`, `unknown_activity`, `unsupported_analysis`, `insufficient_data`, `payload_too_large` | the request is the problem: the API's own explanation, "Adjust analysis" |
| `rate_limited` | "Too many new analyses in a short time" with a `Retry-After` countdown on "Try again" (the ticking seconds are hidden from assistive technology; the message states the wait) |
| `daily_capacity_reached` | today's capacity is used up (per visitor or for everyone, from `details.scope`); resets at 00:00 UTC, shown in local time |
| `overloaded`, `analysis_timeout`, `capacity_unavailable`, `data_version_mismatch`, `service_waking`, `upstream_unavailable`, `database_unavailable`, `data_not_loaded` | specific temporary-problem copy with "Try again" (honouring `Retry-After`); no more "the service is up" |
| `database_paused`, `capacity_paused`, `edge_daily_limit` | **full-page** "paused" state: free hosting quota used up, when it resets, what still works (starter examples and earlier analyses), a link home |
| incompatible API | **full-page** state with a reload button |

**Boot fallback** (F65). `index.html` ships a shell inside `#root` that React
replaces when it starts. If the hashed bundle never loads (a stale page after
a deploy, a network failure, blocked scripts), the shell stays and
`public/boot.css` — unhashed, so it still loads — reveals "ATUS Explorer did
not start" with a reload link after 10 s. CSS only: no inline script or style,
so the strict CSP is unchanged.

## Static snapshot

Each deploy publishes, next to the SPA, the exact API responses for all
metadata and about 130 prewarmed analyses (ADR-011, tier L0). Static files
never invoke the edge Worker, so they keep working when the API sleeps, when
a quota is exhausted, and when the Worker's daily allowance runs out.

```text
/snap/current.json                  no-cache pointer: snapshot_id, data_version, api_version, …
/snap/<snapshot_id>/manifest.json   immutable: { get: {"/api/v1/meta": file, …}, post: {k: entry} }
/snap/<snapshot_id>/<file>          exact API response bodies
```

`snapshot.ts` loads the pointer once per page. Missing, not JSON (the SPA
fallback answers a missing file with `index.html` and a 200), for another
API major version, or malformed → no snapshot, everything goes to the API.
Otherwise GET metadata listed in `get` comes from the snapshot, tagged with
the snapshot's data version. An analysis is served from it when `post` lists

```text
k = sha256_hex(path + "?" + query + "\n" + body)   (WebCrypto, UTF-8)
```

for the exact bytes the SPA would send — the same key the edge cache uses.
The analysis key shown in the methodology panel is then the manifest's
`analysis_key`. The manifest is always consulted first; files are never
probed. Any failure reading a listed file falls back to the API.

`scripts/snapshot-requests.mjs` prints the request list the snapshot builder
(`scripts/build_snapshot.py`) sends, as `[{name, path, body, key}]`, with
`body` already serialized by the SPA's own `stableStringify` (send it
unchanged) and `key` as above. The specs come from `domain/prewarm.ts`:
the four home-page starters, then — built by the analysis builder's own
`toRequest`, so the bytes match what visitors send — every preset under every
measure, a full-period trend per preset and per tier-1 category, a
latest-year estimate per tier-1 category, men vs women per preset and per
tier-1 category, and each preset under the pandemic weights in 2019 and
2020 (130 for the 2003–2025 release). Two passes:

```bash
node scripts/snapshot-requests.mjs --meta meta.json --activities activities.json
node scripts/snapshot-requests.mjs --canonical responses.json   # [{path, response}] of pass 1
```

The second pass prints each estimate/trend response's canonical `spec`: the
result page rewrites its URL to that spec, so share links copied from a
prewarmed result send those bytes. Needs Node ≥ 22.18 (it imports the SPA's
TypeScript modules directly).

## Shareable analyses

An analysis is fully described by `(operation, request spec)` — the API is
stateless. The URL encodes exactly that:

```text
/analysis/<operation>?spec=<base64url(UTF-8 JSON, keys sorted)>
```

- The builder never touches the URL while editing; pressing **Analyze**
  commits one history entry.
- Opening a share link: decode → structural validation (nonsense links get a
  friendly error page) → POST → render. Refresh reproduces the analysis.
- **Canonicalization:** after a successful estimate/trend run, the page
  rewrites the URL (history *replace*) using the API's canonical `spec` from
  the response, and seeds the query cache under the canonical key so the
  rewrite never refetches. Equivalent requests (differently ordered years or
  code lists, expanded presets) therefore converge on one canonical link —
  the same equivalence the backend's `X-Analysis-Key` hashes. Compare
  responses carry no single top-level spec, so compare links keep the
  sorted-keys request encoding.
- `X-Analysis-Key` is displayed in the methodology panel as the analysis's
  deterministic identifier; the spec in the URL is what reconstructs it.
  The key covers the data release, so it changes after a data swap; a result
  from the static snapshot shows the key recorded when the snapshot was
  generated; and a cross-origin API must expose the header (CORS
  `Access-Control-Expose-Headers`) or the panel omits it.
- “Adjust analysis” opens `/explore?op=…&spec=…`, which rebuilds builder
  state from the spec (`features/builder/state.ts: fromRequest`).

## Statistical presentation rules

- `estimate: null` (unavailable trend years) renders as a labeled gap band;
  the line never crosses it, nothing is interpolated, and the data table
  says “Unavailable” with the API's reason — never zero.
- SEs and confidence intervals are shown wherever the API provides them
  (big-number view, chart band, tooltips, tables); `variance: "none"`
  results say explicitly that no uncertainty was requested.
- API `warnings` always render in a visible note banner.
- Unweighted sample sizes (“Survey sample”) and the weighted population
  (“Represents”) are always separate statements.
- Formatting precision policy (`utils/format.ts`): durations as h + m
  rounded to the minute (detailed form: one decimal of minutes);
  percentages one decimal (small nonzero values keep two significant digits
  rather than collapsing to “0.0”); SEs/CIs in the estimate's own scale and
  precision; a **difference** of two proportions is formatted in percentage
  points (pp), matching its SE — never as “%”. Estimate and comparison
  pages expose the raw full-precision API values under “View exact values”;
  the trend page's “View data” table uses display precision. Raw API values
  are never mutated in state.
- The one-sentence interpretation on result pages is a deterministic
  template over response fields — descriptive wording only, no causal
  claims, no significance language.

## Accessibility

- Semantic landmarks, skip link, labeled controls, `:focus-visible` styles.
- Activity search is a WAI-ARIA combobox (arrow keys + Enter, Escape);
  the category browser is plain nested lists of buttons.
- The trend chart is an interactive widget: one tab stop, arrow keys move a
  cursor through years (including unavailable ones), announced via a live
  region; the comparison chart is `role="img"` with a data summary; every
  chart has a data table alternative.
- Loading states are `<output>` (status) elements; errors are
  `role="alert"`; group colors are always paired with text labels.
- Service notices (waking, data release changed) live in persistent
  `<output>` regions, so text added later is announced politely; nothing
  moves focus. A `Retry-After` countdown is visual only (`aria-hidden`); the
  alert's text states the wait once.
- Playwright runs axe checks on the five main screens, the waking state, the
  incompatible-API and paused-quota pages and the edge-limit page, plus
  keyboard checks of the primary workflow and of the new pages' actions.

## Testing

```bash
npm test          # 273 unit/component tests (Vitest + Testing Library + MSW)
npm run test:e2e  # 29 Playwright tests (27 desktop + 2 mobile)
```

- Unit: formatters, URL spec round-trips (including non-ASCII labels and
  malformed links), builder state ⇄ spec conversion, lexicon index, API
  client error mapping (envelopes, non-JSON 429/5xx, `Retry-After`,
  `X-Data-Version`, time-outs, exact body bytes), the API base and build
  configuration, the API version check, the retry policy and budget (fake
  clock), the waking and data-version stores, every error code's
  presentation, the snapshot (hit, miss, SPA-fallback, unsafe manifest, key
  equal to Node's sha256), the prewarm list (equal to the builder's own
  output), `snapshot-requests.mjs` and `check-bundle.mjs` run as scripts,
  and the boot shell (no inline script/style; replaced by React).
- Component: ActivityPicker (search/keyboard/presets/browse), warning and
  error panels (incl. the `Retry-After` countdown), TrendChart 2020-gap
  rendering and keyboard access.
- Integration (MSW, fixtures = captured real payloads): result pages render
  estimates/uncertainty/samples/warnings/methodology; URL canonicalization;
  API errors; invalid share links; the race test (a slow superseded response
  can never overwrite a newer one); and, through the whole app shell, the
  Phase 5 states — a delayed `/meta` (waking notice after 3 s, focus
  unmoved), `service_waking` and busy-pool starts that recover by
  themselves, "Preparing" instead of "Calculating", the incompatible API,
  paused quotas and the edge limit as single full pages, rate limits,
  one-retry transient 503s, timeouts, the data-release reload notice,
  no metadata refetch on focus or reconnect after 30 minutes, background
  refetch failures, "Try again" for every failed query, prewake, and
  snapshot-served metadata.
- E2E (real API + fixture database via `scripts/e2e_backend.py`; every
  expected number hand-computable; production build served same-origin
  under the production CSP): the full first-time-user scenario, the
  comparison scenario, share-link reproduction in a fresh browser context,
  refresh, the 2020 gap, error handling incl. backend-down recovery after the
  cold-start budget (page clock), axe checks, keyboard operation, mobile
  viewport; and the Phase 5 states with page.route: delayed `/meta`,
  incompatible API, paused database, edge daily limit, rate-limit countdown,
  the boot fallback with the bundle blocked, and zero CSP violations with
  every API request on the page's own origin.

E2E ports default to 4173 (frontend) and 8137 (backend);
`E2E_FRONTEND_PORT` / `E2E_BACKEND_PORT` override them, and
`ATUS_E2E_DATABASE_URL` selects the disposable database.
- An adversarial review fleet (96 agents: 8 code-dimension reviewers,
  3 live browser probes, an aggregator, and two independent refuters per
  finding) audited the implementation; 37 confirmed findings — including
  proportion-difference units, a day-type-unaware interpretation sentence,
  and line-drawing across non-contiguous trend years — were all fixed with
  regression tests (see docs/roadmap.md).

## Local development

```bash
docker compose up -d                      # PostgreSQL (once)
# load data: see README quickstart (atus download/extract/load)
atus api                                  # API on :8000

cd frontend
npm install
npm run dev                               # http://localhost:5173, /api proxied to :8000
```

The dev server (and `npm run preview`) forwards `/api` to
`ATUS_DEV_API_ORIGIN` (default `http://localhost:8000`), so the browser
talks to one origin and no CORS setup is needed. `frontend/.env.example`
documents both optional settings; use `.env.local` for machine-specific
development overrides. To call an API on another origin instead, set
`VITE_API_BASE_URL` to its absolute URL and list the frontend origin in the
API's `ATUS_API_CORS_ORIGINS` — do not work around CORS by disabling browser
security.

## Production build and deployment shape

`npm run build` type-checks and emits static assets into `frontend/dist/`
(Oct 2026, Phase 5: 381.6 kB JS / 117.1 kB gzip, 15.6 kB CSS / 3.9 kB gzip —
one chunk, no SSR, no server runtime; Sept 2026, Phase 4: 365.0 / 110.8 kB
JS). The deployment (ADR-011) serves `dist/` as Cloudflare Workers static
assets on one origin, whose Worker proxies `/api/*` to the API — so the
build calls `/api/v1` on its own origin:

- **Configuration.** Builds read `VITE_*` variables from the process
  environment only; no `.env` file (not even `.env.local`) reaches a
  deployable bundle. A `VITE_API_BASE_URL` that is not an absolute http(s)
  URL fails the build. `npm run check:bundle` fails if `dist/` still
  contains a development API address (`localhost:<port>`, `127.0.0.1:<port>`,
  `[::1]:<port>`); react-router's internal portless `http://localhost` URL
  base is not one.
- **CSP.** The app needs nothing inline and nothing third-party.
  `vite preview` serves the build under the production policy
  (`CONTENT_SECURITY_POLICY` in `vite.config.ts`: `default-src 'none';
  script-src 'self'; style-src 'self'; … connect-src 'self'; frame-ancestors
  'none'`), so every E2E run checks it.
- **Deploys.** Hashed bundles may disappear after a new deploy while an old
  `index.html` is still open or cached; the boot shell then explains instead
  of showing a blank page, and a reload fetches the new `index.html`. A data
  release switch shows the reload notice described above.
- **Static snapshot.** The deploy places `snap/` (generated by
  `scripts/build_snapshot.py`) next to the SPA's files; the SPA works
  without it.

## Performance (measured, production build, Sept 2026)

These are the Phase 4 measurements against a local API; they were not
re-measured for Phase 5. Since then the page also requests `/boot.css` and
`/snap/current.json` (plus, when a snapshot is deployed, its manifest, which
then replaces the metadata API calls).

| Measurement | Value |
| --- | --- |
| Home page visually complete (local preview, full-DB API) | ~170 ms |
| DOMContentLoaded | ~54 ms |
| Bytes transferred for the home page | ~114 kB |
| Requests for the home page | 4 total, 1 API call (`/meta`) |
| Route transition to /explore | ~97 ms, 3 API calls (population metadata, lexicon, presets — then cached for the session) |
| Activity search keystrokes | 0 network requests |
| Analysis requests | only on **Analyze** — never per keystroke |

Slow analyses (~11 s uncached full-period variance) show an explanatory
loading state; repeated runs hit the server cache (milliseconds).
