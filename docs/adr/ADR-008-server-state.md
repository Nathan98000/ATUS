# ADR-008: Server state via TanStack Query, version-tagged analysis keys

**Status:** accepted (Phase 4)

## Context

The frontend has two very different kinds of state: server data (metadata,
lexicon, analysis results — owned by the API) and UI state (the analysis
being assembled, open panels). Mixing them in one store breeds bugs: stale
results overwriting new ones, refetch storms, cache entries surviving data
reloads.

## Decision

- **TanStack Query owns all server state.** The population metadata,
  lexicon, and preset queries are fetched once and cached for the session;
  `/meta` uses a 5-minute staleTime (matching the API's `Cache-Control`) so
  a data reload is noticed without polling. The ~70 KB lexicon is searched
  client-side, so typing performs zero requests.
- **Analysis query keys embed the backend's version triple**
  (`analytics_version:release:runN` from `/meta`) plus the canonical spec
  JSON. This mirrors the server's own cache key, so a data reload or
  analytics change invalidates frontend entries *by construction* — a cached
  result can never be presented as coming from a newer version. Within one
  version the result is immutable (`staleTime: Infinity`).
- **Race safety for free:** each spec is its own key, and TanStack Query
  aborts superseded requests via `AbortSignal` — a slow response for an old
  spec can never overwrite a newer result (covered by an integration test
  with controlled MSW delays).
- **Retry policy by error class:** metadata retries twice on
  `NetworkError`/503 only; analyses never auto-retry (they can cost ~11 s
  server-side) — the error state offers an explicit "Try again".
- **UI state stays local:** the builder is one `useState` reducer-style
  object in the Explore page; committed analyses live in the URL, not in a
  global store. No Redux/Zustand — nothing is genuinely shared.

## Consequences

- No hand-written fetch lifecycles, deduplication, or cancellation code.
- The version-tagged key means "stale after reload" needs no invalidation
  calls anywhere in the app.
- A page refresh refetches (or hits the server cache in milliseconds) —
  results are deliberately not persisted client-side; the URL is the
  persistence mechanism.
