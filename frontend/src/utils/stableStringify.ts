/**
 * Deterministic JSON serialization: object keys are sorted recursively, so
 * structurally equal values always produce the same string. Used for share
 * URLs and query-cache identity. Arrays keep their order — the API itself
 * canonicalizes order-insensitive lists (years, activity codes), and the
 * frontend never reorders analytical data.
 */
export function stableStringify(value: unknown): string {
  return JSON.stringify(sortKeysDeep(value))
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep)
  if (value !== null && typeof value === 'object') {
    const source = value as Record<string, unknown>
    const sorted: Record<string, unknown> = {}
    for (const key of Object.keys(source).sort()) {
      sorted[key] = sortKeysDeep(source[key])
    }
    return sorted
  }
  return value
}
