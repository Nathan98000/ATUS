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

/** `Retry-After` for `overloaded` mapped from uvicorn's own 503 (the API's own value). */
export const RETRY_AFTER_OVERLOADED_S = 10

/** Prewake: how long the reply waits for origin /health before answering "waking". */
export const WAKE_RACE_MS = 1_500

/** Prewake: at most one origin wake per isolate in this window. */
export const WAKE_INTERVAL_MS = 10 * 60 * 1000

// --- Edge result cache, tier L1 (ADR-015) ------------------------------------

/** Version prefix of every KV key (`r1/<CACHE_SCOPE>/<k>`); bump if the key format changes. */
export const KV_KEY_VERSION = 'r1'

/** Largest analysis body stored in KV (measured maximum 8,383 B). */
export const KV_MAX_VALUE_BYTES = 16 * 1024

/** KV entry lifetime. */
export const KV_TTL_SECONDS = 30 * 24 * 60 * 60

// --- Quota governor (ADR-011 "Quota governor", ADR-015) ---------------------

/** Uncached analyses admitted per UTC day, all clients together. */
export const GLOBAL_DAILY_ANALYSES = 600

/** Uncached analyses admitted per UTC day per client IP prefix (/24 or /48). */
export const PREFIX_DAILY_ANALYSES = 60

/**
 * Admissions per UTC day per client IP prefix, refunded or not. Analyses the
 * API never computed (Render waking, the API shedding) are refunded, so they
 * do not count toward the 60; but each admission and its settle write up to
 * 12 governor rows, and Workers Free allows 100,000 a day. This caps one
 * network at 300 x 12 = 3,600 rows. The SPA retries a waking analysis at most
 * ~30 times (150 s at `Retry-After: 5`).
 */
export const PREFIX_DAILY_ADMISSIONS = 300

/** Heavy uncached analyses admitted per UTC day. */
export const HEAVY_DAILY_ANALYSES = 300

/**
 * Year queries at which an analysis is heavy: `len(years)`, doubled for a
 * compare. The API's lane classifier uses the same rule.
 */
export const HEAVY_YEAR_QUERIES = 8

/** The Neon compute's autoscaling maximum: every charged second is charged at this size. */
export const NEON_MAX_CU = 2

/** Neon suspends after this much idle time; the whole tail is charged. */
export const NEON_IDLE_TAIL_MS = 300_000

/**
 * DB-busy time assumed for an admitted analysis until it settles: the API's
 * analysis deadline (90 s), the longest an analysis can keep the DB busy.
 */
export const PROVISIONAL_BUSY_MS = 90_000

/** Slack before a settled analysis's DB time, for the connection and a Neon wake. */
export const DB_START_MARGIN_MS = 5_000

/** A lease never settled (the Worker invocation died) is charged in full after this. */
export const LEASE_TTL_MS = 10 * 60 * 1000

/** Neon free compute quota per project per month. */
export const NEON_MONTHLY_CU_HOURS = 100

/** Daily CU-hour ceiling while ground truth from the Neon API is available. */
export const DAILY_CU_HOURS_GROUND_TRUTH = 10

/** Daily CU-hour ceiling without ground truth: no 30- or 31-day window can pass 90% of the quota. */
export const DAILY_CU_HOURS_CONSERVATIVE = (NEON_MONTHLY_CU_HOURS * 0.9) / 31

/** Month-to-date pacing: admit while usage < min(CAP, FLOOR + SLOPE x elapsed share). */
export const PACING_FLOOR_CU_HOURS = 10
export const PACING_SLOPE_CU_HOURS = 75
export const PACING_CAP_CU_HOURS = 85

/** Neon egress month-to-date at which uncached analyses stop (of 5 GB). */
export const EGRESS_STOP_BYTES = 4.5e9

/**
 * Neon egress charged per computed analysis between polls (estimates, not
 * measured: ~15 KB single-year, up to ~230 KB multi-year; docs/deployment.md).
 */
export const EGRESS_ESTIMATE_BYTES = { light: 32 * 1024, heavy: 256 * 1024 } as const

/** Neon usage poll: at most this often, and only while analyses arrived within it. */
export const NEON_POLL_INTERVAL_MS = 5 * 60 * 1000

/** Ground truth older than this no longer earns the larger daily ceiling. */
export const GROUND_TRUTH_MAX_AGE_MS = 24 * 60 * 60 * 1000

/** Timeout on the Neon usage poll (an alarm, never on a request's path). */
export const NEON_POLL_TIMEOUT_MS = 10_000

/** The Neon API's project endpoint (project-scoped key, read only). */
export const NEON_API_BASE = 'https://console.neon.tech/api/v2'

/** How long a request waits for the governor before failing closed. */
export const GOVERNOR_TIMEOUT_MS = 5_000

/** `Retry-After` for `capacity_unavailable` (the governor could not be reached). */
export const RETRY_AFTER_GOVERNOR_S = 60
