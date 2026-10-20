/**
 * Fixed edge parameters (ADR-011). Values that change per deployment are
 * Worker vars in wrangler.jsonc, not constants here.
 */

/** Reported as `User-Agent: atus-edge/<version>` upstream and by /api/_edge/health. */
export const EDGE_VERSION = '0.1.0'

/** Request-body cap, equal to the API's `ATUS_API_MAX_BODY_BYTES` default (64 KiB). */
export const MAX_BODY_BYTES = 64 * 1024

/**
 * Cap on an upstream error body read to check for the API envelope. API error
 * bodies are small (422 details are capped); anything larger is not ours.
 */
export const MAX_ERROR_BODY_BYTES = 256 * 1024

/** Consecutive connection failures that open the per-isolate circuit breaker. */
export const BREAKER_THRESHOLD = 2

/** How long an open breaker skips the origin. */
export const BREAKER_OPEN_MS = 30_000

/** `Retry-After` for `service_waking` (the origin is spinning up). */
export const RETRY_AFTER_WAKING_S = 5

/** `Retry-After` for `upstream_unavailable` produced by a failed or non-API response. */
export const RETRY_AFTER_UNAVAILABLE_S = 10

/** Prewake: how long the reply waits for origin /health before answering "waking". */
export const WAKE_RACE_MS = 1_500

/** Prewake: at most one origin wake per isolate in this window. */
export const WAKE_INTERVAL_MS = 10 * 60 * 1000
