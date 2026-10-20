/**
 * The /api allowlist (ADR-011 "Request flow", step 3.1).
 *
 * Only these routes exist at the edge. Anything else under /api gets a 404
 * envelope without an origin call, so `/ready`, `/docs`, `/redoc`,
 * `/openapi.json` and `/metrics` are never reachable through the Worker.
 * Paths are matched exactly as sent (no trailing-slash or case folding).
 */

export type RouteKind =
  /** Forwarded to the origin path of the same name. */
  | 'proxy'
  /** Forwarded to an origin path with a different name (`/api/health` -> `/health`). */
  | 'origin-health'
  /** Answered by the Worker: prewake. */
  | 'wake'
  /** Answered by the Worker without any origin call. */
  | 'edge-health'

export interface Route {
  /** Stable name used in logs. */
  name: string
  method: 'GET' | 'POST'
  kind: RouteKind
  /** Origin path when it differs from the request path. */
  upstreamPath?: string
  /** Analysis POSTs: JSON-only bodies, edge cache and governor (stage 2). */
  analysis?: boolean
}

export type RouteMatch =
  | { route: Route }
  | { error: 'not_found' }
  | { error: 'method_not_allowed'; allow: Route['method'] }

const ROUTES: ReadonlyMap<string, Route> = new Map<string, Route>([
  ['/api/v1/meta', { name: 'meta', method: 'GET', kind: 'proxy' }],
  ['/api/v1/activities', { name: 'activities', method: 'GET', kind: 'proxy' }],
  ['/api/v1/activities/presets', { name: 'activities.presets', method: 'GET', kind: 'proxy' }],
  [
    '/api/v1/population/metadata',
    { name: 'population.metadata', method: 'GET', kind: 'proxy' },
  ],
  [
    '/api/v1/analysis/estimate',
    { name: 'analysis.estimate', method: 'POST', kind: 'proxy', analysis: true },
  ],
  [
    '/api/v1/analysis/trend',
    { name: 'analysis.trend', method: 'POST', kind: 'proxy', analysis: true },
  ],
  [
    '/api/v1/analysis/compare',
    { name: 'analysis.compare', method: 'POST', kind: 'proxy', analysis: true },
  ],
  [
    '/api/health',
    { name: 'health', method: 'GET', kind: 'origin-health', upstreamPath: '/health' },
  ],
  ['/api/v1/wake', { name: 'wake', method: 'GET', kind: 'wake', upstreamPath: '/health' }],
  ['/api/_edge/health', { name: 'edge.health', method: 'GET', kind: 'edge-health' }],
])

/**
 * `/api/v1/activities/<code>`. Lexicon codes are 2, 4 or 6 digits; any other
 * plain segment is still forwarded so the API answers with its own
 * `activity_not_found`. Encoded characters (`%2F`, ...) and dots never are.
 */
const ACTIVITY_DETAIL = /^\/api\/v1\/activities\/[A-Za-z0-9_-]{1,32}$/

const ACTIVITY_DETAIL_ROUTE: Route = { name: 'activities.detail', method: 'GET', kind: 'proxy' }

/** True for the paths `run_worker_first` sends to the Worker. */
export function isApiPath(pathname: string): boolean {
  return pathname === '/api' || pathname.startsWith('/api/')
}

/** Resolve a method and URL path against the allowlist. */
export function matchRoute(method: string, pathname: string): RouteMatch {
  const route =
    ROUTES.get(pathname) ?? (ACTIVITY_DETAIL.test(pathname) ? ACTIVITY_DETAIL_ROUTE : undefined)
  if (route === undefined) return { error: 'not_found' }
  if (method !== route.method) return { error: 'method_not_allowed', allow: route.method }
  return { route }
}

/** The origin path a matched route calls. */
export function upstreamPathFor(route: Route, pathname: string): string {
  return route.upstreamPath ?? pathname
}
