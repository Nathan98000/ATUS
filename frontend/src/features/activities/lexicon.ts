/**
 * Client-side index over the activity lexicon fetched from
 * GET /api/v1/activities. The API response is the source of truth; this
 * module only reorganizes it for instant browsing/search (counting and
 * grouping for presentation — no analytical logic).
 *
 * Catch-all codes are not offered: the "n.e.c." (not elsewhere classified)
 * entries and the "Data Codes" category (diaries that could not be coded)
 * are left out of browsing and search. They stay in `byCode`, so a link
 * that selects one still names it, and a selected category still includes
 * them (`leafCount` counts every code the analysis covers).
 */
import type { ActivityEntry } from '../../api/types'

export interface LexiconIndex {
  entries: ActivityEntry[]
  byCode: Map<string, ActivityEntry>
  /** Direct offered children per parent code ('' = top level), sorted by code. */
  childrenOf: Map<string, ActivityEntry[]>
  topLevel: ActivityEntry[]
  /** Number of 6-digit activities under a code (1 for a 6-digit code itself). */
  leafCount: (code: string) => number
  /** The same, counting only the activities the picker offers. */
  offeredLeafCount: (code: string) => number
  /** Ancestors of a code, outermost first (excluding the code itself). */
  pathOf: (code: string) => ActivityEntry[]
  search: (query: string, limit?: number) => ActivityEntry[]
}

/** The lexicon's catch-all "Data Codes" category (unable to code, missing). */
const DATA_CODES = '50'

/** Whether a picker offers this entry: not a catch-all code. */
export function isOffered(entry: ActivityEntry): boolean {
  return !/\bn\.e\.c\./i.test(entry.name) && !entry.code.startsWith(DATA_CODES)
}

export function buildLexiconIndex(entries: readonly ActivityEntry[]): LexiconIndex {
  const sorted = [...entries].sort((a, b) => a.code.localeCompare(b.code))
  const byCode = new Map<string, ActivityEntry>()
  const childrenOf = new Map<string, ActivityEntry[]>()
  const leafCounts = new Map<string, number>()
  const offeredLeafCounts = new Map<string, number>()

  for (const entry of sorted) {
    byCode.set(entry.code, entry)
    const parent = entry.parent_code ?? ''
    const siblings = childrenOf.get(parent)
    if (isOffered(entry)) {
      if (siblings) siblings.push(entry)
      else childrenOf.set(parent, [entry])
    }
    if (entry.level === 3) {
      for (const prefix of [entry.code.slice(0, 2), entry.code.slice(0, 4), entry.code]) {
        leafCounts.set(prefix, (leafCounts.get(prefix) ?? 0) + 1)
        if (isOffered(entry)) {
          offeredLeafCounts.set(prefix, (offeredLeafCounts.get(prefix) ?? 0) + 1)
        }
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
      if (!isOffered(entry)) continue
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
    offeredLeafCount: (code) => offeredLeafCounts.get(code) ?? 0,
    pathOf,
    search,
  }
}
