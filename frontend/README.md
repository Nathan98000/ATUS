# ATUS Explorer (frontend)

The Phase 4 web application: a React + TypeScript client of the Phase 3
analytical API. Architecture, API integration, shareable-URL mechanism,
accessibility, testing, and measured performance are documented in
[../docs/frontend.md](../docs/frontend.md).

```bash
npm install
npm run dev        # http://localhost:5173 (API base: VITE_API_BASE_URL)
npm test           # unit/component tests (Vitest + MSW)
npm run test:e2e   # Playwright against the fixture-database API
npm run build      # typecheck + static production build in dist/
```

The API must allow this origin via `ATUS_API_CORS_ORIGINS`
(see the repository root `.env.example`).
