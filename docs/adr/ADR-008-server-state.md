# ADR-008: Server state via TanStack Query, version-tagged analysis keys

**Status:** accepted (Phase 4); amended 2026-10-21 (Phase 5, below)

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

## Amendment (Phase 5, 2026-10-21)

The decision stands: TanStack Query owns server state, and analysis query
keys embed the versions the page was loaded with. The Phase 4 text above
about **what the version is** and **why invalidation is automatic** is
superseded (audit F06, F27):

- **The data version.** It is no longer `analytics_version:release:runN`.
  Since migration `0003_data_version.sql`, every load and every serving-DB
  build writes a globally unique `ingestion_runs.data_version` in the same
  commit as its data: `<release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>`
  for `atus load`, and `<release>:serving-<build_id>:<content_hash[:12]>` for
  a serving build (formatters and reader in
  `src/atus_pipeline/database/versions.py`; legacy rows fall back to
  `<release>:run<id>:<started_at epoch micros>`). The run id alone repeated
  across rebuilt databases, so two different datasets could share a key.
  See [ADR-013, Decision 2](ADR-013-data-releases-and-serving-db.md#decision-2-a-globally-unique-data_version-written-with-the-data-f06-f20).
- **The browser's cache key.** Analysis results are cached under
  `['analysis', operation, analytics_version:data_version, canonicalSpecJSON]`,
  where `data_version` is the `X-Data-Version` header of the page's `/meta`
  (or the static snapshot's `data_version`): the pair the API's own cache
  keys on, and both are part of the edge's `CACHE_SCOPE`
  ([frontend.md, "Server state and caching"](../frontend.md#server-state-and-caching);
  `frontend/src/api/queries.ts`).
- **Not "by construction".** A loaded page keeps its version tag until it is
  reloaded, so a data swap under an open page does not invalidate anything
  by itself. Every later response's `X-Data-Version` is compared with the
  loaded one, and a difference shows a "different data release — reload"
  notice (`frontend/src/api/serviceStatus.ts`).
- **Metadata freshness and retries.** `/meta` no longer uses a 5-minute
  `staleTime`: metadata is fetched once per page (`staleTime: Infinity`, no
  refetch on focus) so a background refetch cannot wake a sleeping free-tier
  host (F62). Analyses are now retried automatically in the cold-start
  cases: within a 150 s budget, and once for some transient 503s
  ([frontend.md, "Cold start, retries and service states"](../frontend.md#cold-start-retries-and-service-states)).
