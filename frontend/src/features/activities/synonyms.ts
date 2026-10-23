/**
 * Everyday words for the activities people ask about, so a search finds
 * "Watching TV" from "tv" and housework from "chores" (review round 4: the
 * survey's own wording was the only way in). Each word leads to presets
 * (by name) or to activity codes the picker already offers; a preset or
 * code a release does not have is simply not suggested. Presentation only:
 * a suggestion selects exactly what choosing it from the list would.
 */
export interface Synonym {
  words: readonly string[]
  presets?: readonly string[]
  codes?: readonly string[]
}

export const SYNONYMS: readonly Synonym[] = [
  {
    words: ['tv', 'television', 'telly', 'shows', 'streaming', 'netflix'],
    presets: ['watching_tv'],
  },
  { words: ['sleep', 'nap', 'naps', 'bed', 'rest'], presets: ['sleep'] },
  {
    words: ['chores', 'cleaning', 'laundry', 'tidying', 'housekeeping'],
    presets: ['housework', 'household_activities_bls_table'],
  },
  { words: ['cooking', 'meals', 'dishes', 'kitchen'], codes: ['0202'] },
  {
    words: ['job', 'jobs', 'work', 'working', 'employment', 'office'],
    presets: ['work_main_and_other_jobs', 'work_related_bls_table'],
  },
  { words: ['commute', 'commuting'], codes: ['1805'] },
  {
    words: ['kids', 'children', 'childcare', 'child care', 'parenting', 'babysitting'],
    codes: ['0301'],
  },
  {
    words: ['exercise', 'gym', 'workout', 'fitness', 'sport', 'sports', 'running'],
    codes: ['13'],
  },
  { words: ['volunteer', 'volunteering', 'charity'], codes: ['15'] },
  {
    words: ['eating', 'food', 'dinner', 'lunch', 'breakfast'],
    presets: ['eating_and_drinking_bls_table'],
  },
  {
    words: ['leisure', 'free time', 'relaxing', 'hobbies', 'fun'],
    presets: ['leisure_and_sports_bls_table'],
  },
  { words: ['friends', 'socializing', 'social', 'family time'], codes: ['1201'] },
  { words: ['shopping', 'shop', 'groceries', 'stores'], codes: ['0701'] },
  { words: ['school', 'study', 'studying', 'class', 'classes', 'education'], codes: ['06'] },
  { words: ['church', 'religion', 'praying', 'prayer', 'worship'], codes: ['14'] },
  { words: ['driving', 'travel', 'traveling', 'car', 'trips'], codes: ['18'] },
  { words: ['phone', 'calls', 'telephone'], codes: ['16'] },
  { words: ['computer', 'internet', 'online', 'social media'], codes: ['120308'] },
  { words: ['games', 'gaming', 'video games'], codes: ['120307'] },
  { words: ['reading', 'books'], codes: ['120312'] },
]

/**
 * The synonyms a search matches: a word that starts with the query (two
 * letters or more), or that the query starts with ("tv shows" → tv).
 */
export function matchingSynonyms(query: string): Synonym[] {
  const needle = query.trim().toLowerCase()
  if (needle.length < 2) return []
  return SYNONYMS.filter((synonym) =>
    synonym.words.some(
      (word) => word.startsWith(needle) || needle === word || needle.startsWith(`${word} `),
    ),
  )
}
