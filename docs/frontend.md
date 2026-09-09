# Frontend (Phase 4) — ATUS Explorer

A client-side React application through which a nontechnical user can explore
ATUS time-use estimates. It consumes the Phase 3 HTTP API as its **single
source of numerical truth**:

```text
UI components
    ↓
feature/state layer (builder state, TanStack Query server state)
    ↓
API client (src/api/client.ts — the only fetch in the app)
    ↓
Phase 3 HTTP API  →  Phase 2 analytical engine  →  PostgreSQL
```

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
              client.ts (single HTTP boundary), endpoints.ts, queries.ts
  domain/     urlSpec.ts (shareable-URL encoding), describe.ts (titles,
              labels, plain-language interpretation), presentError.ts
  features/
    activities/  lexicon.ts (client index), ActivityPicker
    builder/     state.ts (builder ⇄ request spec), BuilderControls
    population/  PopulationFields (generated from population metadata)
    results/     EstimateView, TrendView + TrendChart, CompareView +
                 ComparisonChart, MethodologyPanel, ShareButton, chartScale
  components/ AppShell, ErrorBoundary, LoadingPanel, ErrorPanel, WarningBanner
  pages/      Home, Explore (builder), Analysis (result), About, NotFound
  utils/      format.ts (precision policy), stableStringify.ts
  test/       MSW server, captured fixtures, render helpers
```

## API integration

- **Types are generated from the backend's own OpenAPI schema.**
  `python scripts/export_openapi.py` writes `frontend/openapi.json`;
  `npm run generate:api-types` regenerates `src/api/generated.ts`.
  Both artifacts are committed; regenerate and commit after any API schema
  change. `src/api/types.ts` re-exports friendly aliases — the only names
  the app imports.
- **One client.** `apiRequest` owns the base URL (`VITE_API_BASE_URL`, no
  trailing slash; the client appends `/api/v1`), JSON handling, and error
  translation: the API's error envelope becomes `ApiError`
  (status/code/message/details/request id); unreachable or non-JSON
  responses become `NetworkError`. The UI presents the two differently
  (`domain/presentError.ts`): analytical 422s show the API's own message
  verbatim; infrastructure failures get a retry.
- **Discovery-driven UI.** Years, measures, weight schemes, population
  dimensions and values, activity taxonomy, and presets all come from
  `/meta`, `/population/metadata`, and `/activities*` — nothing analytical
  is hard-coded. A capability the UI does not recognize is ignored (never a
  crash); one the server stops reporting disappears from the UI.
- The 556-entry lexicon (~70 KB) is fetched once per session and searched
  client-side — activity search performs zero network requests.

## Server state and caching

Metadata queries cache for the session (`staleTime: Infinity`; `/meta`
5 minutes, matching the API's `Cache-Control`). Analysis results are cached
under the key

```text
['analysis', operation, analytics_version:release:runN, canonicalSpecJSON]
```

— the same versioning rule as the server's own cache, so a data reload or
statistical change on the server invalidates frontend entries by
construction, and a cached result is never presented as coming from a newer
version. Analyses never auto-retry (they can be expensive); superseded
requests are aborted via TanStack Query's signal, and stale responses cannot
overwrite newer ones because every spec is its own key.

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
- Playwright runs axe checks on the five main screens and a keyboard-only
  pass of the primary workflow.

## Testing

```bash
npm test          # 119 unit/component tests (Vitest + Testing Library + MSW)
npm run test:e2e  # 20 Playwright tests (18 desktop + 2 mobile)
```

- Unit: formatters, URL spec round-trips (including non-ASCII labels and
  malformed links), builder state ⇄ spec conversion, lexicon index, API
  client error mapping, trend gap segmentation.
- Component: ActivityPicker (search/keyboard/presets/browse), warning and
  error panels, TrendChart 2020-gap rendering and keyboard access.
- Integration (MSW, fixtures = captured real payloads): result pages render
  estimates/uncertainty/samples/warnings/methodology; URL canonicalization;
  API errors; invalid share links; the race test (a slow superseded response
  can never overwrite a newer one).
- E2E (real API + fixture database via `scripts/e2e_backend.py`; every
  expected number hand-computable): the full first-time-user scenario, the
  comparison scenario, share-link reproduction in a fresh browser context,
  refresh, the 2020 gap, error handling incl. backend-down recovery, axe
  checks, keyboard operation, mobile viewport.
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
ATUS_API_CORS_ORIGINS=http://localhost:5173 atus api   # API on :8000

cd frontend
npm install
npm run dev                               # http://localhost:5173
```

`frontend/.env.example` documents `VITE_API_BASE_URL` (default
`http://localhost:8000`); use `.env.local` for machine-specific overrides.
The API must list the frontend origin in `ATUS_API_CORS_ORIGINS` — do not
work around CORS by disabling browser security.

## Production build and deployment shape

`npm run build` type-checks and emits static assets into `frontend/dist/`
(measured Sept 2026: 365.0 kB JS / 110.8 kB gzip, 15.0 kB CSS / 3.8 kB
gzip — one chunk, no SSR, no server runtime). The intended architecture is a static
host for `dist/` plus the FastAPI service on its own origin with CORS;
`VITE_API_BASE_URL` is baked at build time. Serving `dist/` from FastAPI
(static mount + SPA fallback) remains a Phase 5 option; nothing in the
frontend assumes either choice. Deployment itself is Phase 5.

## Performance (measured, production build, Sept 2026)

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
