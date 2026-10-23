/**
 * Prewake (ADR-011). On user intent only — entering Explore, or opening an
 * analysis that the static snapshot does not hold — ask the edge to start
 * waking the API host, so its spin-up (about a minute on free hosting)
 * overlaps with the time spent building a specification.
 *
 * `GET /api/v1/wake` is answered by the edge Worker itself, which never
 * touches the database; its `{origin: "warm" | "waking"}` answer only feeds
 * the waking notice. The request is fire-and-forget, sent at most once per
 * `PREWAKE_INTERVAL_MS` per page, and only where that Worker exists — which a
 * deployed static snapshot indicates. Development and E2E servers have
 * neither, so they never see the request.
 */
import { apiRequest } from './client'
import { setOriginState } from './serviceStatus'
import { loadSnapshot } from './snapshot'

export const PREWAKE_INTERVAL_MS = 10 * 60 * 1000
const PREWAKE_TIMEOUT_MS = 10_000

let lastPrewakeAt = Number.NEGATIVE_INFINITY

export async function prewake(): Promise<void> {
  const now = Date.now()
  if (now - lastPrewakeAt < PREWAKE_INTERVAL_MS) return
  lastPrewakeAt = now
  try {
    if (!(await loadSnapshot())) return
    const { data } = await apiRequest<{ origin?: unknown }>('/wake', {
      timeoutMs: PREWAKE_TIMEOUT_MS,
    })
    if (data.origin === 'warm' || data.origin === 'waking') setOriginState(data.origin)
  } catch {
    // Best effort: a failed prewake changes nothing the visitor sees.
  }
}

/** Test isolation: allow the next prewake immediately. */
export function resetPrewake(): void {
  lastPrewakeAt = Number.NEGATIVE_INFINITY
}
