/**
 * Quota governor Durable Object (ADR-011 "Quota governor"; finalized in
 * ADR-012). One SQLite-backed instance named "global" admits uncached
 * analyses against the daily, per-prefix and Neon budgets.
 *
 * Stage 1 declares the class so the binding and its SQLite migration deploy;
 * admission is implemented in stage 2. Until then the Worker never calls it.
 */

import { DurableObject } from 'cloudflare:workers'

export class Governor extends DurableObject<Env> {}
