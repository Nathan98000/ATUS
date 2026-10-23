# ATUS Explorer (frontend)

The Phase 4 web application: a React + TypeScript client of the Phase 3
analytical API, hardened for free-tier production hosting in Phase 5.
Architecture, API integration, cold-start and error handling, the static
snapshot, shareable URLs, exports, response headers, accessibility, and
testing are documented in [../docs/frontend.md](../docs/frontend.md).

```bash
npm install
npm run dev            # http://localhost:5173; /api is proxied to http://localhost:8000
npm test               # unit/component tests (Vitest + MSW)
npm run test:e2e       # Playwright: production build, same origin, deployed _headers;
                       # Chromium, plus the @core journeys in Firefox and WebKit
                       # (once: npx playwright install firefox webkit)
../scripts/e2e_in_container.sh --project firefox
                       # the same, browsers in the official Playwright Linux
                       # image (Firefox does not launch on macOS 27)
npm run build          # typecheck + static production build in dist/ (same-origin /api),
                       # with the API schema published at /openapi.json
npm run check:bundle   # deploy guard on dist/: no dev API address, no inline code,
                       # <meta> CSP, _headers rules, openapi.json = ./openapi.json
npm run generate:api-types  # src/api/generated.ts from openapi.json
npm run check:api-types     # fails if generated.ts is not what generation produces
```

The app calls `/api/v1` on its own origin; see `.env.example` for the two
optional settings (`ATUS_DEV_API_ORIGIN` for the dev proxy target,
`VITE_API_BASE_URL` for a cross-origin API).
