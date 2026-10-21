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
[edge Worker, in the deployment]  →  Phase 3 HTTP API  →  Phase 2 analytical engine  →  PostgreSQL
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
              prewarm.ts (starter examples + prewarmed snapshot specs),
              exports.ts (CSV and JSON downloads of a result)
  deploy/     staticHeaders.ts (reads the _headers rules of the static host)
  features/
    activities/  lexicon.ts (client index), ActivityPicker
    builder/     state.ts (builder ⇄ request spec), BuilderControls
    population/  PopulationFields (generated from population metadata)
    results/     EstimateView, TrendView + TrendChart, CompareView +
                 ComparisonChart, MethodologyPanel, ShareButton,
                 ResultDownloads, chartScale
  components/ AppShell, ErrorBoundary, LoadingPanel, ErrorPanel, WarningBanner,
              ServiceNotice (waking / reload notices), ServiceInterruption
              (full-page paused-quota and incompatible-API states)
  pages/      Home, Explore (builder), Analysis (result), About, NotFound
  utils/      format.ts (precision policy), stableStringify.ts, download.ts
  styles/     global.css (design tokens)
  test/       MSW server (+ snapshot publisher), captured fixtures, render helpers
frontend/e2e/
  *.spec.ts   Playwright suites; fixtures.ts (one visitor per test), helpers.ts
  deployed/   starter.deployed.ts: the post-deploy browser smoke
              (playwright.deployed.config.ts)
frontend/scripts/
  snapshot-requests.mjs  prints the prewarmed analysis requests for the snapshot
  check-bundle.mjs       the deploy guard on dist/ (API address, inline code,
                         <meta> CSP, _headers, the published openapi.json)
  api-types.mjs          generates / checks src/api/generated.ts
frontend/public/
  _headers               response headers of the static site (CSP, HSTS, cache rules)
  boot.css               styles the boot shell in index.html
```

## API integration

- **Types are generated from the backend's own OpenAPI schema.**
  `python scripts/export_openapi.py` writes `frontend/openapi.json`;
  `npm run generate:api-types` regenerates `src/api/generated.ts`
  (`scripts/api-types.mjs`: openapi-typescript with
  `--default-non-nullable false`, so request properties that have a server
  default stay optional, then Prettier). Both artifacts are committed;
  regenerate and commit after any API schema change.
  `npm run check:api-types` fails when the committed file is not exactly what
  generation produces (audit F30: the old one-line script lacked the flag, and
  its output broke `tsc`). `src/api/types.ts` re-exports friendly aliases —
  the only names the app imports.
- **One client.** `apiRequest` owns the base URL, JSON handling, protocol
  headers and error translation.
  - **Base URL.** Empty by default: the app calls `/api/v1/...` on its own
    origin, which is how the public deployment works (one origin serves the
    SPA and proxies `/api`, ADR-011). `VITE_API_BASE_URL` opts into another
    origin and must be an absolute http(s) URL; any other value fails the
    build. So does an origin that the site's CSP (`connect-src` in
    `public/_headers`) does not allow: the built page enforces that policy
    and could never reach the API. There is no fallback to a developer
    address (audit F24). Only builds check the value: under `npm run dev`
    and in tests, a value that is not an absolute http(s) URL is ignored and
    the app calls its own origin (`resolveApiBase` in `src/api/apiBase.ts`).
  - **Errors** (`src/api/client.ts`). Every error status becomes an
    `ApiError` (status/code/message/details/request id/`retryAfterSeconds`),
    never a `NetworkError`. With the API's or the Worker's envelope the code
    is theirs (`unknown_error` if an envelope has a message but no code). An
    error body that is not JSON gets a synthesized code: `edge_daily_limit`
    for a 429 (the edge's daily request allowance is used up),
    `upstream_unavailable` for a 5xx (a host or proxy page),
    `malformed_response` for any other status; a JSON error body that is not
    an envelope is also `malformed_response`. `NetworkError` is only a request
    that gets no answer, exceeds its time limit, or returns a success status
    whose body is not JSON. `Retry-After` (seconds or an HTTP date; else
    `details.retry_after_seconds`; capped at 31 days) is kept on the error.
  - **Headers read.** `X-Analysis-Key`, `X-Cache` and `X-Data-Version` on
    success, `X-Request-ID` and `Retry-After` on errors. Across origins a
    browser only exposes them if the API lists them in
    `Access-Control-Expose-Headers` (the API does when CORS is on);
    same-origin, as in the planned deployment, it always does.
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

Metadata queries load once per page (`staleTime: Infinity`) and loaded
metadata never refetches in the background — not on window focus, not on
reconnect — because a focus refetch could wake a sleeping free-tier host for
nothing (audit F62). Coming back online refetches only a query whose last
attempt failed because the browser was offline; a final answer (an
`analysis_timeout`, a paused quota, a 422, an incompatible API) or a rate
limit whose `Retry-After` still runs is never re-sent by a reconnect — the
visitor decides (review round 2). Every query runs with `networkMode: "always"`: TanStack
Query's default pauses a query that starts while the browser is offline
before its function runs, which left a new analysis on "Calculating…" for as
long as the browser stayed offline, with no request and no message (review
round 1). The request now fails at once and is presented as offline. `/meta` is fetched with `cache: "no-cache"` when it comes
from the API, so a reload never pairs a `/meta` from the browser's HTTP cache
with newer data (F27); a deployed snapshot's `/meta` comes from an
immutable snapshot directory whose name includes a hash of the data version,
found through the `no-cache` pointer. TanStack
Query's own retries are off; `resilience.ts` retries instead (below).

**API compatibility.** `/meta` must report API major version `v1` and the
shape this build reads (`compat.ts`). Anything else — `v2`, a missing
version, a renamed field — is an `IncompatibleApiError`: never retried,
never papered over with defaults, and shown as one full-page state
("This page and the analysis service are out of step", with a reload
button) instead of crashing a route into the error boundary (F25). A `v1`
`/meta` that reports no loaded data (release `unloaded`, no years: the API's
answer before the first load) is not an incompatibility: it is shown as "No
survey data is loaded" with "Try again" (review round 2).

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
with a globally unique data version — audit F06/F27.) The statistical
implementation is tracked the same way (review round 2): the page's `/meta`
`analytics_version` is compared with every later `X-Analytics-Version` and
with every result's own `analytics_version`, so a release that changes only
`ANALYTICS_VERSION` shows a notice in its own words ("a different version of
its statistical methods"). The cache key keeps the page's versions; a result
of another release or implementation is not hidden by it but announced by
the notice, and each result still shows its own `analytics_version` (the
methodology panels of all three views, and the exports). The notice offers
"Reload page" only when a reload would load that release: when the snapshot
pointer, read again with `no-cache`, now names it, or, without a usable
pointer, when the site's own `/api/v1/meta` (also `no-cache`) already
reports it (`snapshot.ts reloadLoadsVersion`). That is what a reload reads:
through the site the edge Worker answers a plain `/api/v1/meta` from its own
deployed snapshot, not from the API, so "no snapshot loaded" does not mean
"the reload gets the API's release" (review round 2). During a release switch the API serves
the new release before the deploy job publishes the new snapshot (ADR-013
step 8); every page load, a reload or a new tab alike, then reads the old
snapshot, so the notice instead says the service is switching releases,
that new results already come from the new one and that reloading does not
change that yet, with no button (review round 1). While it says so, the page
reads the pointer again every minute (only while the tab is visible; a
static file, never the Worker or the API), so a tab left open through the
switch offers the reload within a minute of the deploy job publishing the
new snapshot.

Analyses are retried automatically only in the cold-start cases below;
superseded requests are aborted via TanStack Query's signal, and stale
responses cannot overwrite newer ones because every spec is its own key.
A failed background refetch never unmounts the builder or hides a displayed
result: pages show a page-level error only while nothing is loaded (F26),
and Explore's "Try again" refetches every failed query (F63). The activity
picker fetches a failed activity list or preset list again when it opens
and from its own "Try again" (review round 2).

## Cold start, retries and service states

The free API host sleeps when idle and takes about a minute to wake
(ADR-011). The client therefore treats a slow or failing start as a wait,
not an error (`resilience.ts`, audit F11, F12, F64):

| Failure | Metadata GET | Analysis POST |
| --- | --- | --- |
| No answer / connection error, `service_waking`, `upstream_unavailable` (incl. any non-JSON 5xx page), other 502/504/52x | retried until 150 s have passed | same |
| `upstream_unavailable` whose `details.reason` is `origin_timeout` (a Cloudflare 524) or `broken_response`: the API may have taken the analysis | retried until 150 s | retried **once** (it joins the running computation or hits the API's cache), never re-sent for the whole budget |
| `overloaded` (also the edge's mapping of uvicorn's own connection-limit 503), `database_unavailable`, a 503 with an unknown code | retried until 150 s | retried **once** |
| `rate_limited` (429) with `Retry-After` | retried until 150 s | never (visitor decides) |
| `rate_limited` with `details.budget: "prefix"` (the edge's per-network daily allowance, waiting until 00:00 UTC) | never | never |
| client time-out (60 s per metadata attempt, 150 s per analysis) | retried until 150 s | never |
| anything else: every other 4xx, `analysis_timeout`, `data_version_mismatch`, `data_not_loaded`, `schema_not_initialized`, `daily_capacity_reached`, `database_paused`, `capacity_paused`, `api_paused`, `capacity_unavailable`, `edge_daily_limit`, an incompatible API, being offline | never | never |

Every wait honours `Retry-After` (at least 1 s); without it the backoff is
1, 2, 4, 8, 8, … s. A retry is not started when its wait would end after the
150 s budget. Being offline (`navigator.onLine` false) is not a cold start
and is not retried; a query started offline runs too (`networkMode:
"always"`), so it fails at once with "check your internet connection"
instead of pausing silently.

**Waking notice.** "Waking up the analysis service — free hosting sleeps
when idle; this can take up to a minute." appears when a metadata request
has been pending for 3 s, while an attempt has just failed as "waking" (or
with `database_unavailable`, a database resuming), or for up to 90 s after
the edge's prewake said the origin is waking, while an API call is pending.
Both time limits are decided by their own timers firing, never by
re-reading the clock in the timer (`serviceStatus.ts`): Firefox reduces and
jitters the precision of `Date.now()`, so a 3 s timer can read 2999 ms, and
the earlier re-check then left a Firefox cold start without its notice
(found by the first Firefox E2E runs).
It lives in a persistent `<output>` (polite live region) at the top of the
page, never moves focus, and comes with no progress bar — the service
reports no progress, so none is invented. A shared link whose analysis is
still waiting for `/meta` says "Preparing the analysis…", never
"Calculating…": no analysis has been sent yet.

**Prewake.** On user intent only — entering Explore, or opening an analysis
the static snapshot does not hold — the app sends `GET /api/v1/wake`
(fire-and-forget, at most once per 10 minutes) so the host's spin-up
overlaps with the time spent building a spec. The edge Worker answers it
without touching the database. It is only sent where that Worker exists,
which a deployed snapshot indicates; development and E2E never send it.

**Presentations** (`domain/presentError.ts`, F13). The codes of the v1
contract and of the edge have public-facing copy:

| Code | Presentation |
| --- | --- |
| `invalid_spec`, `validation_error`, `malformed_json`, `invalid_analysis`, `unknown_activity`, `activity_not_found`, `unsupported_analysis`, `insufficient_data`, `payload_too_large` | the request is the problem: the API's own explanation, "Adjust analysis"; a 422's `details.errors[]` (`{location, message, type}`) become one line per field, e.g. "measure: Input should be …" |
| `rate_limited` | "Too many new analyses in a short time" with a `Retry-After` countdown on "Try again" (the ticking seconds are hidden from assistive technology; the message states the wait). The edge's per-network daily allowance (`details.budget: "prefix"`) instead says that the network's new analyses for today are used up and that the limit resets at 00:00 UTC (in local time), with no countdown and no "Try again"; any other wait over 15 minutes names the local time it ends |
| `daily_capacity_reached` | the service's shared capacity for new analyses is used up for today; resets at 00:00 UTC, shown in local time. A shared allowance: the API sends `details.scope: "daily"`, the edge governor `"global"` or `"daily"`; a per-network daily denial is `rate_limited` with budget `prefix` (above). The governor's heavy-only cap (`details.budget: "heavy"`) instead says that today's long analyses (8 or more year queries: 8+ years, 4+ for a comparison) are used up while analyses of up to 7 years (3 for a comparison) still run, and offers "Adjust analysis" (review round 2) |
| `overloaded`, `analysis_timeout`, `capacity_unavailable`, `data_version_mismatch`, `service_waking`, `upstream_unavailable`, `database_unavailable`, `schema_not_initialized`, `data_not_loaded` | specific temporary-problem copy with "Try again" (honouring `Retry-After`); `analysis_timeout` also offers "Adjust analysis". `upstream_unavailable` follows `details.reason`: `connect_failed`/`circuit_open` "could not be reached", `origin_timeout` "did not answer in time", `broken_response` "broke off", `not_configured` "not set up"; only `platform_error` (or a non-JSON 5xx) says the hosting provider answered. `service_waking` and the waking-class `upstream_unavailable` are shown only after the 150 s budget, so they say the service has not answered for more than 2 minutes and that its free hosting may be paused, for example until next month, instead of promising a temporary problem: a Render suspension looks exactly like this until the owner sets the edge's pause switch (`api_paused`, below; [security.md R13](security.md#accepted-risks)) |
| `database_paused`, `capacity_paused`, `api_paused`, `edge_daily_limit` | **full-page** "paused" state: free hosting quota used up, when it resets, what still works (starter examples and earlier analyses), a link home. A date is named only when the service says when the pause ends (`capacity_paused`: `resumes_at`/`Retry-After`; `api_paused`: `details.until`); `database_paused`'s fixed `Retry-After: 3600` only paces retries, so its page names no date. The governor's monthly compute budget (`capacity_paused` with `details.budget: "cu_month"`) also pauses while usage runs ahead of its pacing line, often for an hour or two early in the month, so its page says the pause spreads the month's free allowance and names when it lifts (a local time within the day, else a date), not "nearly used up … until it resets" (review round 2). `api_paused` is the owner's pause switch at the edge: "The analysis service is paused", worded by `details.reason` (the host used up this month's allowance, the host suspended it, or maintenance), and "It is not known yet when it will be back" without a date (review round 2) |
| `not_found`, `method_not_allowed`, `unsupported_media_type` | a request outside the HTTP contract, which the page never sends on purpose: most likely a page and a service from different versions, so the copy says to reload the page; no "Try again" (the retry policy treats these as final) |
| `forbidden_origin` | through the site the Worker always adds the edge secret, so this means the API does not accept it: the copy says the site's connection to the analysis service is misconfigured, a problem in the deployment and not in the visitor's analysis, and what still works; no "Try again" |
| incompatible API | **full-page** state with a reload button; another major version is named, and a `v1` `/meta` in a shape this build cannot read says so instead of "works with v1, but the service reported v1" |
| no data loaded (a `v1` `/meta` with release `unloaded` or no years, as the API answers before the first load) | the `data_not_loaded` copy ("No survey data is loaded") with "Try again", not the incompatible-API page |
| no answer (`NetworkError`) | "could not be reached" or "did not answer in time", with "Try again" |
| any other code (`internal_error`, `malformed_response`, `unknown_error`, a code this build does not know) | one generic "unexpected problem in the service" copy with "Try again" (honouring `Retry-After`) |

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
/snap/<snapshot_id>/manifest.json   immutable: { get: {"/api/v1/meta": file, …}, post: {k: entry}, files: {…} }
/snap/<snapshot_id>/get/…, post/<k>.json   exact API response bodies
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
probed. Any failure reading a listed file falls back to the API, and so does
every analysis on an origin without WebCrypto (plain http other than
localhost), where the key cannot be computed. `snapshot_id` is
`<api build[:12]>-<sha256(data_version)[:16]>` (`scripts/build_snapshot.py`).

The host answers a missing `/snap/<snapshot_id>/…` file with `index.html`
and the snapshot's one-year `immutable` Cache-Control, because `_headers`
rules match the request path, not the file that answers (observed under
`wrangler dev`, whose asset worker is Cloudflare's; review round 1). A
browser that asked for a file of a snapshot that was not deployed at that
moment — a tab left open across a deploy — would keep that HTML for a
year, also after a rollback deploys that snapshot id again. So a manifest
or data file whose body is not JSON is fetched once more with `cache:
"reload"`, which bypasses and replaces the cached copy, before the request
falls back to the API. The pointer is `no-cache` and is never re-fetched
this way.

`scripts/snapshot-requests.mjs` prints the request list the snapshot builder
(`scripts/build_snapshot.py`) sends, as `[{name, path, body, key}]`, with
`body` already serialized by the SPA's own `stableStringify` (send it
unchanged) and `key` as above. The specs come from `domain/prewarm.ts`:
the four home-page starters, then — built by the analysis builder's own
`toRequest`, so the bytes match what visitors send — every preset under every
measure, a full-period trend per preset and per tier-1 category, a
latest-year estimate per tier-1 category, men vs women per preset and per
tier-1 category, each preset under the pandemic weights in 2019 and
2020, and each starter as the builder sends it after "Adjust analysis"
without a change (every default stated; for the two comparison starters
these bytes are otherwise in no list; review round 2) — 132 for the
2003–2025 release. Two passes:

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
  Every field the page shows must have the JSON type the page reads it as,
  population values included (`has_household_children` a boolean, ages and
  `region` numbers, the rest strings): the API reads `"no"`, `"false"` or
  `"0"` as `false`, which the page would have labelled "with household
  children" (review round 4).
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
- **No short links** ([ADR-016](adr/ADR-016-short-share-links.md)): they
  would need server-side state — a store the edge Worker must reach to
  resolve every link, written from the same 1,000-writes/day KV quota as the
  result cache — whereas a long link keeps working on static files alone.
  Deferred; stateless compression of the spec is the first step if length
  ever matters.

## Exports

Every result page offers **Download CSV** and **Download JSON**
(`features/results/ResultDownloads.tsx`, built by `domain/exports.ts`). Both
files are made in the browser from the response already on screen: no
request, and no statistic is computed, rounded or derived. The file is a Blob
behind an object URL, opened through a temporary `<a download>`, so it needs
no CSP exception (no inline script, no `data:` URL, no connection).

**CSV** — the result table (RFC 4180: one header row, comma separators,
CRLF line endings, quotes only where needed, quotes doubled; UTF-8 with a
byte-order mark so spreadsheet programs read non-ASCII labels correctly):

| Operation | Rows | Columns |
| --- | --- | --- |
| estimate | one | activity (label, included and excluded codes), population, years, measure, `unit`, `estimate`, `standard_error`, `ci_lower`, `ci_upper`, `confidence_level`, respondents, participants, weighted population per day, days in period |
| trend | one per year | `year`, activity, population, measure, `unit`, the estimate columns, sample columns, `unavailable_reason` |
| compare | group A, group B, difference (A − B) | `series`, `label` (the difference row's is "<A> minus <B>"), population (`NA` on the difference row), activity, years, measure, `unit`, the estimate columns, sample columns and days in period (`NA` on the difference row) |

Every row ends with its provenance: `weights` (scheme), `weight_variable`
(e.g. `TUFNWGTP`), `variance_method`, `analytics_version`, `data_version`,
`warnings` (the API's warnings, joined with ` | `) and `source` (the BLS
attribution), so a row stays interpretable on its own. Numbers are the API's
own values in JavaScript's shortest round-trip form — the digits JSON uses —
so parsing a cell gives back exactly the API's double. A value the API did
not or could not provide — the 2020 point of a multi-year trend, a standard
error that was not requested, a difference's sample size — is the literal
`NA`, never `0` or blank. Lists (years, codes) are joined with `;`. A text
cell that a spreadsheet would run as a formula (leading `=`, `+`, `-`, `@`,
tab or carriage return; group labels come from share links, so they are
untrusted) gets a leading apostrophe. So does such text after a `;`, a tab
or a line break inside a cell (past any spaces or double quotes):
spreadsheet programs whose list separator is `;`, such as Excel in much of
Europe, start a new cell there, and to them the quotes of a field in the
middle of a line are ordinary characters, so a line break inside a trend's
activity label or a comparison's group label starts a new row (review
round 4).

**JSON** — the whole analysis, `format: "atus-explorer/analysis-export"`,
`format_version: 1`:

```text
exported_at, operation, link (the page's canonical share link),
spec + spec_source ("api_canonical" for estimate/trend; "request" for compare,
  which has no single canonical spec — each group's is inside the response),
analysis_key, api_version, analytics_version, data_version, data_release,
unit, warnings, attribution {source, source_url, notice, tool_url},
response (the API's response, unchanged: every estimate, SE and CI)
```

`data_version` is the response's `X-Data-Version` (or the static
snapshot's), else the version the page's metadata reported — the legacy
`<release>:run<id>` tag for an API that reports none. `data_release` is the
release that data version names (every format starts with `<release>:`), so
one export never names two releases — during a release switch a response
can come from a newer release than the page's metadata; only without a
data version does it fall back to the release `/meta` reported. Unknown
facts are `null`, never guessed. File names are
`atus-<operation>-<activity>-<years>.<csv|json>`, e.g.
`atus-trend-sleeping-2019-2023.csv`.

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
  points (pp), matching its SE — never as “%”. Confidence levels keep every
  digit (0.975 is “97.5%”, 0.999 “99.9%” — never rounded to another level),
  and the builder adds a shared link's non-standard level to its 90/95/99%
  choices rather than showing one level and sending another (review round
  3). Estimate and comparison pages expose the raw full-precision API
  values under “View exact values”; the trend page's “View data” table uses
  display precision. Raw API values are never mutated in state.
- Filters are never active yet invisible. A comparison's shared filters
  restrict both groups: in the builder their section opens and is marked
  “(active)” when set (from a link, or carried over from an estimate or
  trend), and the result names them in its title and in a “Both groups: …”
  line, not only inside the collapsed panels (review round 3).
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
  `role="alert"`; group colors are always paired with text labels. The
  analysis page announces through one persistent polite region of its own,
  so its loading panel is a plain container there, and the waking state is
  announced by the site-wide notice alone: each state is read once, not two
  or three times (review round 1).
- Service notices (waking, data release changed) live in persistent
  `<output>` regions, so text added later is announced politely; nothing
  moves focus. A `Retry-After` countdown is visual only (`aria-hidden`); the
  alert's text states the wait once.
- "Try again" replaces the panel that holds it, so `useRetryFocus` moves
  focus to the page's heading (`<h1 tabIndex={-1}>`; on Home, the starter
  examples' heading) before the retry and, until it settles, back to the
  heading the page then shows whenever focus fell to `<body>` (review
  round 1). Explore keeps its "Build an analysis" heading mounted in the
  loading and error states too, so focus stays on it while a retry runs and
  after one that fails again (review round 2).
- After an in-app navigation (a link, Analyze, Back/Forward) focus moves to
  the new page's `<h1>` (`AppShell`), so the change of page is announced and
  the next Tab continues from it. Not on the first page of a visit, and not
  when the analysis page only replaces its query string with the canonical
  spec (review round 2). When a full-page state (a paused quota or API, the
  edge's daily limit) then replaces the page and its focused heading, focus
  moves to the state's own heading instead of falling to `<body>`
  (`useKeepNavigationFocus`; review round 4).
- The download buttons are ordinary buttons; a persistent `<output>` says
  which file was started, and focus stays on the button. Downloading the
  same file again empties the region and says it again 100 ms later, so the
  repeat is announced too (review round 2).
- Playwright runs axe checks (no serious or critical violations) on the five
  main screens, the About page, the waking state, the incompatible-API, paused-quota and
  edge-limit pages, the data-release reload notice, the
  `data_version_mismatch` and rate-limit panels and the downloads, plus
  keyboard checks of the primary workflow and of every new state's action.
  WebKit on macOS follows Safari's default of tabbing only between text
  fields, so Tab-order checks are skipped there (`tabReachesAllControls`);
  the controls are still operated from the keyboard. WebKit on Linux (CI,
  and the container browsers below) tabs to every control, so the checks run
  there.

## Testing

```bash
npm test          # unit/component tests (Vitest + Testing Library + MSW)
npm run test:e2e  # 84 Playwright runs: 53 tests in Chromium desktop, 3 mobile,
                  # and the 14 @core journeys again in Firefox and in WebKit
                  # (one desktop test runs only in stack mode)
npx playwright install firefox webkit   # once, for the cross-browser projects
../scripts/e2e_in_container.sh --project firefox   # Firefox where it cannot launch
                  # natively (macOS 27): browsers in the official Linux image
```

The E2E counts are the tests declared in `e2e/*.spec.ts` and the projects in
`playwright.config.ts`, as `npx playwright test --list` prints them; the CI
test report gives the per-run counts.
`tests/deploy/test_docs_accuracy_r2.py` compares the comment above with that
listing wherever `frontend/node_modules` is installed.

- Unit: formatters, URL spec round-trips (including non-ASCII labels and
  malformed links), builder state ⇄ spec conversion, lexicon index, API
  client error mapping (envelopes, non-JSON 429/5xx, `Retry-After`,
  `X-Data-Version`, time-outs, exact body bytes), the API base and build
  configuration, the API version check, the retry policy and budget (fake
  clock), the waking and data-version stores (including a `Date.now()`
  that reads short when a timer fires, as Firefox's does), every error code's
  presentation, the snapshot (hit, miss, SPA-fallback, unsafe manifest, key
  equal to Node's sha256), the prewarm list (equal to the builder's own
  output), `snapshot-requests.mjs`, `check-bundle.mjs` and `api-types.mjs`
  run as scripts (the last also against a real production build: no inline
  code, the `<meta>` CSP first, `_headers` shipped; and `generated.ts`
  reproduced byte for byte), the `_headers` reader (Cloudflare matching,
  joining and detaching rules), the site's actual header and cache rules and
  `vite preview` applying them, the exports (exact CSV text, quoting, `NA`,
  formula guard, every number equal to the API's, the JSON record), the
  About page's logging statement, and the boot shell (no inline
  script/style; replaced by React).
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
  snapshot-served metadata, and the downloads (the saved file is built from
  the response on screen, through a Blob URL, with the page's analysis key,
  data version and canonical link).
- New survey year (brief §15, `src/pages/newSurveyYear.test.tsx`): with a
  `/meta` (or a static snapshot) announcing a release 0326 with years
  2003–2026 and its own data version, and no code change, the year pickers
  (single, pooled, trend) offer 2026 and default to it, the default trend is
  2003–2026, the home page's starters, the footer, the "View … trend" link
  and the prewarm list use 2026, results are cached under the 0326 version
  tag, and a shared link through 2026 opens unchanged (under the 0325
  release it loses 2026, with the note). The 2020 rules still hold: the
  builder's gap note follows the range and the weights, a trend through 2026
  shows the 2020 gap, pandemic weights stay 2019–2020 only.
- E2E (real API + fixture database via `scripts/e2e_backend.py`; every
  expected number hand-computable; production build served same-origin
  under the production CSP): the full first-time-user scenario, the
  comparison scenario, share-link reproduction in a fresh browser context,
  refresh, the 2020 gap, error handling incl. backend-down recovery after the
  cold-start budget (page clock), axe checks, keyboard operation, mobile
  viewport; and the Phase 5 states with page.route: delayed `/meta`,
  incompatible API, a data release switch (reload notice), a
  `data_version_mismatch` 503, paused database, edge daily limit, rate-limit
  countdown, the boot fallback with the bundle blocked; downloads read back
  from disk (CSV values equal to the JSON response's, `NA` for 2020, keyboard
  operation); a release announcing 2026 through `/meta` (the year controls
  and starters offer it, the default estimate asks the real API for 2026,
  and the real 2019–2023 trend still shows the 2020 gap); and the headers to
  be deployed: every page carries the `_headers` rules and the `<meta>` CSP,
  bundles are served immutable, `/openapi.json` is served with its rules and
  equals `frontend/openapi.json`, and the core journeys (including a
  download) run with every API request on the page's own origin. Every E2E
  test, in every project and in stack mode, collects the CSP violations and
  Permissions-Policy errors of every page in its browser context and fails
  on one (`e2e/fixtures.ts` `policyErrors`; the share test checks its fresh
  context too). Before review round 2 only the core-journeys test and the
  deployed smoke collected them, so the compare view, the share and JSON
  actions, the error, quota, waking and version pages, the boot fallback and
  the phone layout ran unchecked.
- Cross-browser: tests tagged `@core` — the first-time-user estimate, the
  comparison, share links (copy, refresh, invalid), the 2020 gap and chart
  keyboard access, the exports, the waking notice, and the headers/CSP
  journey — also run in Firefox and WebKit (`playwright.config.ts`). On the
  development machine (macOS 27) Playwright's Firefox build exits at launch
  with "Could not find profile folder", so Firefox runs there in the
  official Playwright Linux image (container browsers, below). On
  2026-10-21 every project passed that way: 63 of 63 runs (Chromium desktop
  35, mobile 2, Firefox 13, WebKit 13), and, as an extra check outside the
  configured projects, all 35 tests passed in Firefox and in WebKit. The
  first Firefox runs found one product bug, fixed with regression tests: the
  waking notice could fail to appear in Firefox (see "Cold start, retries
  and service states" — its 3 s timer re-read a `Date.now()` that Firefox
  reduces in precision and jitters; 8 failures in 200 runs of the waking
  journey before the fix, none in 200 after).
- Post-deploy smoke: `playwright.deployed.config.ts` runs `e2e/deployed/`
  (one Chromium journey, no retries, against `PUBLIC_BASE_URL`; `deploy.yml`
  runs it after a deploy). It has never run against a public URL, because
  nothing is deployed yet.

E2E ports default to 4173 (frontend) and 8137 (backend);
`E2E_FRONTEND_PORT` / `E2E_BACKEND_PORT` override them,
`ATUS_E2E_DATABASE_URL` selects the disposable database, and
`E2E_BACKEND_PYTHON` the backend's Python (default `../.venv/bin/python`).
`npx playwright test --project=desktop` runs Chromium only.

**Container browsers** (Firefox on macOS 27). `scripts/e2e_in_container.sh`
runs the same suite with its browsers in the official Playwright Linux image,
`mcr.microsoft.com/playwright:v<version>-noble`, where `<version>` is
`@playwright/test` in `frontend/package-lock.json` (now 1.63.0); the image is
pulled on first use. Run `npm ci` in `frontend/` first, then from the repo
root `scripts/e2e_in_container.sh --project firefox` (Firefox),
`scripts/e2e_in_container.sh` (every project), or any other `playwright
test` arguments. The container runs only `playwright run-server`, published
on 127.0.0.1, using the checkout's own Playwright (`frontend/node_modules`,
mounted read-only), so client, server and browser builds are one version; the
script refuses a `node_modules` or an image (`E2E_PLAYWRIGHT_IMAGE`, for
example pinned by digest) of another version. The test runner, the build,
`vite preview` and `scripts/e2e_backend.py` stay on the host exactly as in
`npm run test:e2e`: `E2E_BROWSER_SERVER` makes `playwright.config.ts` connect
every project to the container (`e2e/browserServer.ts`) with
`exposeNetwork: '<loopback>'`, so the browsers' requests for `localhost`
come back through the test runner and the app is served from the same
origin as in every other run (no `host.docker.internal`). The container is
removed on exit; it carries a per-run label, so a container that another run
started under the same name is never removed. `E2E_BROWSER_SERVER_PORT` (default 8139) and
`E2E_BROWSER_CONTAINER` (default `atus-e2e-browsers`) move it; the usual
`E2E_FRONTEND_PORT`, `E2E_BACKEND_PORT` and `ATUS_E2E_DATABASE_URL` pass
through, for example:

```bash
E2E_FRONTEND_PORT=4174 E2E_BACKEND_PORT=8138 E2E_BROWSER_SERVER_PORT=8140 \
  ATUS_E2E_DATABASE_URL=postgresql://atus:atus@localhost:5434/atus_e2e_firefox \
  scripts/e2e_in_container.sh --project firefox
```

Downloads are read through `download.createReadStream()`, which works for
local and remote browsers alike (`download.path()` throws when the browser is remote).
One known Playwright-Firefox limitation shows in long repeated runs: in a
Firefox instance that has made many navigations into documents served with
`Cross-Origin-Opener-Policy: same-origin`, `page.goto` sometimes never
resolves although the page has loaded (`document.readyState` is
`complete`) — rarely at first, then for about half of all loads after a few
hundred. It reproduces with a static page that has only that header, served
inside the container, without the app, the tunnel or the SPA's code (1,000
fresh-context loads: Firefox 526 hangs, the first at load 142; Chromium 0;
WebKit 1; the same pages without the header: Firefox 0 in 2,000), so it is
not a product defect, and the header stays. A normal run makes about 20 such
navigations per Firefox instance; across 1,365 Firefox test runs with
`--repeat-each` (5 to 30) it caused 2 failures.

**Stack mode** (wave 2). With `E2E_BASE_URL` set, the same suite runs
against the production-mode stack through its edge and starts no server:
`scripts/smoke_stack.sh --edge --e2e` (docs/deployment.md, step 0) builds
this SPA same-origin, publishes it with the static snapshot under the Worker
(`wrangler dev`, local mode) in front of the production API on a serving
database built from the same fixture, and runs `desktop`, `mobile` and
`webkit`. Everything the deployment adds is in the path: the snapshot (most
metadata comes from `/snap/`), the KV cache, the governor, the Worker's
headers and the production limits. Two settings make the suite valid in both
modes (`e2e/fixtures.ts`): each test is its own visitor (`wrangler dev`
takes `CF-Connecting-IP` from the request when one is sent, so every test
sends its own address from 198.19.0.0/16, and the per-visitor limits apply
per test rather than to the whole suite), and tests that inject failures
into `/api/v1/meta` first hide the snapshot (`withoutSnapshot`), since the
deployed SPA would otherwise read `/meta` from `/snap/`. On 2026-10-21, at
46293d1, all 70 runs passed in stack mode (Chromium desktop 53, mobile 3,
WebKit 14), with the review-round-4 test of `e2e/service-states.spec.ts`
that reaches a paused page through Analyze and checks that its heading has
focus, after the stack smoke (15/15), the edge probe (13/13) and
`deploy_smoke.py` (13/13); the API image was built from 3dffe1a, whose API
that commit does not change. Earlier that day, at 3a984fa, 68 of 68 had
passed (Chromium desktop 52, mobile 3, WebKit 13), after the review-round-2
merges: the edge's `api_paused` test of `e2e/service-codes.spec.ts`, and the
frontend's fixture that fails every test on a CSP or Permissions-Policy
violation, with its own test of that fixture, included; at 9188bb6, 67 of 67 had passed
(Chromium desktop 51, mobile 3, WebKit 13), with the `api_paused` test; at
5beb6a4, 66 of 66 had passed (Chromium desktop 50, mobile 3, WebKit 13),
with the review-round-1 browser tests (`e2e/service-codes.spec.ts` and
others), and at f208719, 52 of 52 had
passed (Chromium desktop 37, mobile 2, WebKit 13), with
`e2e/new-survey-year.spec.ts` and the static `/openapi.json` test of
`e2e/service-states.spec.ts`; the first stack-mode run, on 2026-10-20,
passed 50 of 50 before those two tests existed. Firefox cannot launch on macOS 27 and ran in vite-preview mode through the
container browsers (above); a stack-mode Firefox run has not been made yet.
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
security. That works under `npm run dev`, which has no CSP; a build for
another origin also needs that origin in `connect-src` in `public/_headers`,
or the build fails (and `npm run check:bundle` rejects such a policy for
deployment).

## Production build and deployment shape

`npm run build` type-checks and emits static assets into `frontend/dist/`:
one JS chunk, no SSR, no server runtime. Measured sizes: 390.4 kB JS /
120.0 kB gzip and 15.8 kB CSS / 4.0 kB gzip at commit 6411d73 (2026-10-20,
Phase 5 with exports; later commits changed the bundled code and it was
not re-measured); 365.0 / 110.8 kB JS in Sept 2026 (Phase 4). The planned
deployment (ADR-011) serves `dist/` as Cloudflare Workers static assets on
one origin, whose Worker proxies `/api/*` to the API — so the build calls
`/api/v1` on its own origin:

- **Configuration.** Builds read `VITE_*` variables from the process
  environment only; no `.env` file (not even `.env.local`) reaches a
  deployable bundle. A `VITE_API_BASE_URL` that is not an absolute http(s)
  URL fails the build, as does an absolute one whose origin the CSP's
  `connect-src` does not list (with the shipped `'self'`, any other origin):
  the built page enforces that policy, so it could never reach the API.
- **Response headers: `public/_headers`.** The static host applies this file
  (copied into `dist/`) to every static file; it does not apply to `/api`,
  whose headers the edge Worker sets. It is the single source of the site's
  header rules (`src/deploy/staticHeaders.ts` reads it):

  | Path | Headers |
  | --- | --- |
  | every static file | `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `Strict-Transport-Security: max-age=31536000`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` denying every powerful feature the site does not use (camera, microphone, geolocation, payment, USB, clipboard reads, …), `Cross-Origin-Opener-Policy: same-origin` |
  | `/assets/*` (hashed bundles) | `Cache-Control: public, max-age=31536000, immutable` |
  | `/`, `/index.html`, `/explore`, `/about`, `/analysis/*` | `Cache-Control: no-cache` |
  | `/snap/current.json`, `/openapi.json` | `Cache-Control: no-cache` |
  | `/snap/:snapshot/*` (a snapshot directory) | `Cache-Control: public, max-age=31536000, immutable` |

  The host joins a header set by two matching rules with a comma, so no two
  rules set the same header for one path (`/snap/*` would have joined
  `immutable` onto the pointer's `no-cache`). That a splat may follow a
  placeholder (`/snap/:snapshot/*`) is inferred from Cloudflare's docs, not
  stated; check it with curl after deploying (if the rule never matched,
  snapshot files would just be revalidated, never served stale). Other unhashed files
  (`boot.css`, icons) keep the host's default `max-age=0, must-revalidate`.
  The app needs nothing inline, no `data:` URL and nothing third-party;
  clipboard writes (the Share button) stay allowed for the page itself.
- **`<meta>` CSP backstop.** The build injects the same policy, minus
  `frame-ancestors` (which a `<meta>` CSP cannot carry; the header enforces
  it), as the first element of `index.html`, in case the host does not apply
  `_headers` to the SPA-fallback `index.html` (an unverified detail of
  ADR-011; check a deep link with curl after deploying). The dev server does
  not get it: Vite injects inline scripts there.
- **Published OpenAPI schema: `/openapi.json`.** The build copies
  `frontend/openapi.json` (written by `scripts/export_openapi.py`; the API
  types are generated from it) byte for byte to `dist/openapi.json` (the
  `atus-publish-openapi` plugin in `vite.config.ts`), so the static host
  serves the schema while the API's own `/openapi.json` stays off in
  production (ADR-011). It is a static file at the site root, not under
  `/api`: the edge Worker runs first only for `/api` and `/api/*`, so its
  route allowlist is never involved (a unit test checks `run_worker_first`
  in `edge/wrangler.jsonc`). Being unhashed, it gets `Cache-Control:
  no-cache`, so a deploy that changes the API is seen at once.
  `check:bundle` and an E2E test compare it with `frontend/openapi.json`.
- **Every E2E run uses the deployed headers.** `vite preview` applies the
  built `dist/_headers` to every non-`/api` response (a plugin in
  `vite.config.ts`), so the browser tests run under the exact CSP,
  Permissions-Policy and cache rules that will be deployed.
- **`npm run check:bundle`** (run on the deploy build) fails if `dist/`:
  still contains a development API address (`localhost:<port>`,
  `127.0.0.1:<port>`, `[::1]:<port>`; react-router's internal portless
  `http://localhost` URL base is not one); has an inline script, `<style>`,
  `style` attribute, inline event handler or `javascript:` URL in its HTML;
  lacks the `<meta>` CSP, or has one that differs from `_headers` or comes
  after a script or stylesheet; or lacks `_headers`, a security header on
  any HTML route, a strict CSP (`'none'` default, `'self'` scripts and
  connections, no `unsafe-*`, `data:` or wildcard, `frame-ancestors 'none'`),
  HSTS of at least a year, or the cache rules above; or if `openapi.json`
  is missing or differs from `frontend/openapi.json` by a single byte, or a
  `dist/api/` directory exists (the Worker answers every `/api` path first).
- **Deploys.** Hashed bundles may disappear after a new deploy while an old
  `index.html` is still open or cached; the boot shell then explains instead
  of showing a blank page, and a reload fetches the new `index.html` (never
  cached without revalidation). A data release switch shows the reload
  notice described above.
- **Static snapshot.** The deploy places `snap/` (generated by
  `scripts/build_snapshot.py`) next to the SPA's files; the SPA works
  without it.
- **Privacy.** No cookies, no browser storage, no third-party analytics or
  trackers. The About page ("Privacy and logs") states what the service
  logs per request — request ID, route, status, timing, cache status,
  analysis key, the client IP shortened to /24 (IPv4) or /48 (IPv6), never
  request bodies — and the retention of the providers' free plans (Render
  7 days; Cloudflare Workers Logs 3 days, 7 days from 1 December 2026), per
  ADR-011 and ADR-014.

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

Slow analyses show an explanatory loading state. Uncached full-period
variance took about 11 s on the Phase 4 laptop; the Phase 5 audit measured
tens of seconds for the pre-Phase-5 code under free-tier emulation
([`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md)).
For the current code under the same emulation (2026-10-21,
[`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)):
an uncached single-year estimate with replicate SEs took 0.99 s at p50 at
Neon's 0.25 CU floor and 79 ms at 2 CU; pooled 22-year and 23-year trend
worst cases took 21.4–21.8 s and 2.03–2.12 s; and the costliest request, the F66
broad selector compared by sex, 69–71 s at 0.25 CU. A cold container took
5.8 s at p50 to its first `/health` at 0.1 CPU, before Render's own spin-up
([cold-start A/B](../loadtests/results/2026-10-21-cold-start-ab.md)). In the
emulation runs, before the image precompiled its standard library, it took
13.1 s.
Repeated runs hit a cache (the static snapshot, the edge, or the API's
in-process cache).
