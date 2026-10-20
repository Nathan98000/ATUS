# ATUS Explorer (frontend)

The Phase 4 web application: a React + TypeScript client of the Phase 3
analytical API, hardened for free-tier production hosting in Phase 5.
Architecture, API integration, cold-start and error handling, the static
snapshot, shareable URLs, accessibility, and testing are documented in
[../docs/frontend.md](../docs/frontend.md).

```bash
npm install
npm run dev            # http://localhost:5173; /api is proxied to http://localhost:8000
npm test               # unit/component tests (Vitest + MSW)
npm run test:e2e       # Playwright: production build, same origin, production CSP
npm run build          # typecheck + static production build in dist/ (same-origin /api)
npm run check:bundle   # fails if dist/ still points at a development API address
```

The app calls `/api/v1` on its own origin; see `.env.example` for the two
optional settings (`ATUS_DEV_API_ORIGIN` for the dev proxy target,
`VITE_API_BASE_URL` for a cross-origin API).
