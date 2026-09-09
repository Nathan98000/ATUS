/**
 * Client-side index over the activity lexicon fetched from
 * GET /api/v1/activities. The API response is the source of truth; this
 * module only reorganizes it for instant browsing/search (counting and
 * grouping for presentation — no analytical logic).
 */
import type { ActivityEntry } from '../../api/types'

export interface LexiconIndex {
  entries: ActivityEntry[]
  byCode: Map<string, ActivityEntry>
  /** Direct children per parent code ('' = top level), sorted by code. */
  childrenOf: Map<string, ActivityEntry[]>
  topLevel: ActivityEntry[]
  /** Number of 6-digit activities under a code (1 for a 6-digit code itself). */
  leafCount: (code: string) => number
  /** Ancestors of a code, outermost first (excluding the code itself). */
  pathOf: (code: string) => ActivityEntry[]
  search: (query: string, limit?: number) => ActivityEntry[]
}

export function buildLexiconIndex(entries: readonly ActivityEntry[]): LexiconIndex {
  const sorted = [...entries].sort((a, b) => a.code.localeCompare(b.code))
  const byCode = new Map<string, ActivityEntry>()
  const childrenOf = new Map<string, ActivityEntry[]>()
  const leafCounts = new Map<string, number>()

  for (const entry of sorted) {
    byCode.set(entry.code, entry)
    const parent = entry.parent_code ?? ''
    const siblings = childrenOf.get(parent)
    if (siblings) siblings.push(entry)
    else childrenOf.set(parent, [entry])
    if (entry.level === 3) {
      for (const prefix of [entry.code.slice(0, 2), entry.code.slice(0, 4), entry.code]) {
        leafCounts.set(prefix, (leafCounts.get(prefix) ?? 0) + 1)
      }
    }
  }

  const pathOf = (code: string): ActivityEntry[] => {
    const path: ActivityEntry[] = []
    let current = byCode.get(code)?.parent_code ?? null
    while (current) {
      const parent = byCode.get(current)
      if (!parent) break
      path.unshift(parent)
      current = parent.parent_code ?? null
    }
    return path
  }

  const search = (query: string, limit = 30): ActivityEntry[] => {
    const needle = query.trim().toLowerCase()
    if (needle === '') return []
    const startsWith: ActivityEntry[] = []
    const contains: ActivityEntry[] = []
    for (const entry of sorted) {
      const name = entry.name.toLowerCase()
      if (name.startsWith(needle) || entry.code.startsWith(needle)) startsWith.push(entry)
      else if (name.includes(needle)) contains.push(entry)
      if (startsWith.length >= limit) break
    }
    return [...startsWith, ...contains].slice(0, limit)
  }

  return {
    entries: sorted,
    byCode,
    childrenOf,
    topLevel: childrenOf.get('') ?? [],
    leafCount: (code) => leafCounts.get(code) ?? 0,
    pathOf,
    search,
  }
}
