# ADR-007: Frontend stack — Vite + React + TypeScript, client-side only

**Status:** accepted (Phase 4)

## Context

Phase 4 needs an interactive web application over the Phase 3 read-only API.
The application is fundamentally a client of one HTTP service: it holds no
secrets, renders no personalized content, and all data access already lives
behind `/api/v1`.

## Decision

- **Vite + React 19 + TypeScript (strict, `noUncheckedIndexedAccess`)** as a
  purely client-side single-page application in `frontend/`.
- **No Next.js / SSR.** Server rendering would add a Node runtime and a
  second server without any benefit: there is no SEO requirement, no
  per-user content, and the statistical boundary must stay in the API.
- **react-router 8 in declarative mode** with four routes (`/`, `/explore`,
  `/analysis/:operation`, `/about`) — analyses are represented in URLs, not
  in a route hierarchy.
- **Plain CSS with design tokens** (one global stylesheet) instead of
  Tailwind or CSS-in-JS: the app has one designed surface and a junior
  reader should be able to find any style by class name.
- **API types generated from the backend's OpenAPI schema**
  (`openapi-typescript`), committed, with a documented regeneration script —
  the frontend contract cannot silently drift from the backend.
- **oxlint + Prettier** (the current Vite template's toolchain) with the
  jsx-a11y rule set enabled.

## Consequences

- The production artifact is a static `dist/` (110.9 kB gzip JS measured);
  any static host works, and FastAPI static mounting stays an option.
- Type errors, not runtime surprises, when the API schema changes: regenerate
  types and the compiler lists every affected call site.
- No server-side rendering means the app requires JavaScript; acceptable for
  an analytical tool.
