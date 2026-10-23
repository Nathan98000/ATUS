/**
 * Activity selection: search, popular choices, and hierarchical browsing of
 * the harmonized lexicon — all driven by the activities API. Selecting a
 * 2- or 4-digit tier means the tier and all of its descendants; the picker
 * says so explicitly rather than pretending every code is one activity.
 *
 * Search finds the presets and everyday words too ("tv", "chores";
 * `synonyms.ts`), and every popular choice and selection says what it
 * includes, in plain names (review round 4).
 *
 * The search results implement the WAI-ARIA combobox/listbox pattern, which
 * a native <select> cannot express — the two rules below flag exactly that
 * pattern, so they are disabled for this file only.
 */
/* oxlint-disable jsx-a11y/prefer-tag-over-role, jsx-a11y/no-noninteractive-element-to-interactive-role */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'

import { refetchFailed, useActivities, useActivityPresets } from '../../api/queries'
import type { ActivityEntry, ActivitySelection } from '../../api/types'
import { activityName, offeredPresets } from '../../domain/describe'
import { buildLexiconIndex, type LexiconIndex } from './lexicon'
import { matchingSynonyms } from './synonyms'

interface PresetDefinition {
  name: string
  label: string
  include: string[]
  exclude: string[]
}

/**
 * Top-level categories people ask about that no preset covers, offered
 * beside the presets: volunteering, sports and exercise, caring for
 * household members, shopping. A choice selects the category exactly as
 * browsing to it does, which the snapshot holds for every top-level
 * category (its latest year and its full trend).
 */
const POPULAR_CATEGORIES = ['15', '13', '03', '07']

/** How many included activities a hint names before "and N more". */
const HINT_NAMES = 3

/** "Includes sleeping; sleeplessness", or '' when nothing is known. */
function includesHint(names: readonly string[]): string {
  if (names.length === 0) return ''
  const shown = names
    .slice(0, HINT_NAMES)
    .map((name) =>
      /^\p{Lu}{2,}\b/u.test(name) ? name : name.charAt(0).toLowerCase() + name.slice(1),
    )
  const more = names.length - shown.length
  return `Includes ${shown.join('; ')}${more > 0 ? `; and ${more} more` : ''}`
}

interface ActivityPickerProps {
  value: ActivitySelection
  onChange: (selection: ActivitySelection) => void
  error?: string
}

export function ActivityPicker({ value, onChange, error }: ActivityPickerProps) {
  const activities = useActivities()
  const presets = useActivityPresets()
  const [open, setOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)

  const index = useMemo(
    () => (activities.data ? buildLexiconIndex(activities.data.activities) : null),
    [activities.data],
  )
  const presetList = presets.data?.presets ?? []

  const select = (selection: ActivitySelection) => {
    onChange(selection)
    setOpen(false)
    toggleRef.current?.focus()
  }

  // The lists are fetched once per page and never refetched in the
  // background, so a failed one is fetched again only from here: when the
  // panel opens, and from its "Try again" (review round 2).
  const retryFailedLists = () => refetchFailed(activities, presets)

  return (
    <div className="activity-picker">
      <SelectionSummary value={value} index={index} presets={presetList}>
        <button
          ref={toggleRef}
          type="button"
          className="btn btn--small"
          aria-expanded={open}
          onClick={() => {
            if (!open) retryFailedLists()
            setOpen(!open)
          }}
        >
          {open ? 'Close' : 'Change activity'}
        </button>
      </SelectionSummary>
      {error ? <p className="error-text">{error}</p> : null}
      {open ? (
        <div className="activity-picker__panel">
          {activities.isPending || presets.isPending ? (
            <output className="field-hint" style={{ display: 'block' }}>
              Loading the activity list…
            </output>
          ) : activities.isError || !index ? (
            <>
              <p className="error-text">The activity list could not be loaded.</p>
              <button
                type="button"
                className="btn btn--small"
                onClick={() => {
                  // The button is replaced by the loading text: keep focus beside it.
                  toggleRef.current?.focus()
                  retryFailedLists()
                }}
              >
                Try again
              </button>
            </>
          ) : (
            <PickerPanel
              index={index}
              presets={presetList}
              onSelect={select}
              onClose={() => {
                setOpen(false)
                toggleRef.current?.focus()
              }}
            />
          )}
        </div>
      ) : null}
    </div>
  )
}

/**
 * The selected activity: its name with the button that changes it right
 * beside it (review round 5: at the card's far edge it read as unrelated),
 * then what it includes.
 */
function SelectionSummary({
  value,
  index,
  presets,
  children,
}: {
  value: ActivitySelection
  index: LexiconIndex | null
  presets: readonly PresetDefinition[]
  /** The "Change activity" button. */
  children: ReactNode
}) {
  const layout = (name: ReactNode, hint: string) => (
    <div className="activity-picker__current">
      <div className="activity-picker__line">
        {name}
        {children}
      </div>
      {hint ? <span className="field-hint activity-picker__includes">{hint}</span> : null}
    </div>
  )
  if (value.preset != null) {
    const preset = presets.find((candidate) => candidate.name === value.preset)
    const hint =
      preset && index ? includesHint(index.includedNames(preset.include, preset.exclude)) : ''
    return layout(
      <span className="activity-picker__name">
        {activityName(preset?.label ?? value.preset)}
      </span>,
      hint,
    )
  }
  const codes = value.include ?? []
  if (codes.length === 1 && index) {
    const code = codes[0] as string
    const entry = index.byCode.get(code)
    const hint =
      index.offeredLeafCount(code) > 1
        ? includesHint(index.includedNames(codes, value.exclude ?? []))
        : ''
    return layout(
      <span className="activity-picker__name">
        {entry ? activityName(entry.name) : `Activity ${code}`}
      </span>,
      hint,
    )
  }
  return layout(
    <span>
      <span className="activity-picker__name">
        {value.label != null ? activityName(value.label) : 'Custom selection'}
      </span>
      <span className="field-hint">
        {' '}
        — {codes.length} codes
        {value.exclude?.length ? `, ${value.exclude.length} excluded` : ''}
      </span>
    </span>,
    '',
  )
}

/** One choice in the search results: a preset or an entry of the lexicon. */
interface PickerOption {
  key: string
  name: string
  /** The path above an entry, outermost first. */
  path: string[]
  hint: string
  selection: ActivitySelection
}

function PickerPanel({
  index,
  presets,
  onSelect,
  onClose,
}: {
  index: LexiconIndex
  presets: readonly PresetDefinition[]
  onSelect: (selection: ActivitySelection) => void
  onClose: () => void
}) {
  const listboxId = useId()
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const offered = useMemo(() => offeredPresets(presets), [presets])

  const presetOption = (preset: PresetDefinition): PickerOption => ({
    key: `preset-${preset.name}`,
    name: activityName(preset.label),
    path: [],
    hint: includesHint(index.includedNames(preset.include, preset.exclude)),
    selection: { preset: preset.name },
  })
  const entryOption = (entry: ActivityEntry): PickerOption => ({
    key: `code-${entry.code}`,
    name: activityName(entry.name),
    path: index.pathOf(entry.code).map((ancestor) => activityName(ancestor.name)),
    hint: entry.level < 3 ? `Includes ${index.offeredLeafCount(entry.code)} activities` : '',
    selection: { include: [entry.code], label: entry.name },
  })

  // Presets first (by name, then by an everyday word), then entries an
  // everyday word points to, then the lexicon's own matches.
  const results = ((): PickerOption[] => {
    const needle = query.trim().toLowerCase()
    if (needle === '') return []
    const synonyms = matchingSynonyms(needle)
    const byName = offered.filter((preset) =>
      activityName(preset.label).toLowerCase().includes(needle),
    )
    // In the synonym's own order: "chores" means housework before all
    // household activities.
    const bySynonym = synonyms
      .flatMap((synonym) => synonym.presets ?? [])
      .flatMap((name) => offered.filter((preset) => preset.name === name))
    const entries = [
      ...synonyms
        .flatMap((synonym) => synonym.codes ?? [])
        .flatMap((code) => {
          const entry = index.byCode.get(code)
          return entry && index.offeredLeafCount(code) > 0 ? [entry] : []
        }),
      ...index.search(needle),
    ]
    const options = [...byName, ...bySynonym].map(presetOption)
    for (const entry of entries) options.push(entryOption(entry))
    const seen = new Set<string>()
    return options
      .filter((option) => {
        if (seen.has(option.key)) return false
        seen.add(option.key)
        return true
      })
      .slice(0, 30)
  })()

  // Option ids are keyed by what they select (not list position) so
  // aria-activedescendant changes value as the results change — screen
  // readers then re-announce the active option while the user types.
  const optionId = (key: string) => `${listboxId}-option-${key}`
  const activeOptionId =
    query.trim() === ''
      ? undefined
      : results.length === 0
        ? `${listboxId}-no-results`
        : results[activeIndex]
          ? optionId(results[activeIndex].key)
          : undefined

  // Keep the active option visible inside the scrollable listbox.
  useEffect(() => {
    if (activeOptionId) {
      document.getElementById(activeOptionId)?.scrollIntoView?.({ block: 'nearest' })
    }
  }, [activeOptionId])

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      if (query !== '') setQuery('')
      else onClose()
      return
    }
    if (results.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => Math.min(current + 1, results.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => Math.max(current - 1, 0))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const option = results[activeIndex]
      if (option) onSelect(option.selection)
    }
  }

  const popular = [
    ...offered.map(presetOption),
    ...POPULAR_CATEGORIES.flatMap((code) => {
      const entry = index.byCode.get(code)
      if (!entry || index.offeredLeafCount(code) === 0) return []
      const option = entryOption(entry)
      return [{ ...option, hint: includesHint(index.includedNames([code])) }]
    }),
  ]

  return (
    <div>
      <div className="field">
        <label htmlFor={`${listboxId}-input`}>Search activities</label>
        <input
          id={`${listboxId}-input`}
          type="search"
          role="combobox"
          aria-expanded={query.trim() !== ''}
          aria-controls={`${listboxId}-list`}
          aria-activedescendant={activeOptionId}
          aria-autocomplete="list"
          autoComplete="off"
          placeholder="e.g. TV, sleep, cooking, volunteering"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setActiveIndex(0)
          }}
          onKeyDown={onSearchKeyDown}
        />
      </div>

      {query.trim() !== '' ? (
        /* WAI-ARIA combobox pattern: the list is a listbox driven by the
           search input via aria-activedescendant (oxlint prefers native
           <select>, which cannot express this searchable hierarchy). */
        <ul
          className="activity-picker__results"
          role="listbox"
          id={`${listboxId}-list`}
          aria-label="Matching activities"
        >
          {results.length === 0 ? (
            /* A disabled option (not presentation) so the combobox's active
               descendant can announce the empty state to screen readers. */
            <li
              className="field-hint"
              role="option"
              aria-disabled="true"
              aria-selected={false}
              id={`${listboxId}-no-results`}
              style={{ padding: '0.4rem 0.6rem' }}
            >
              No activities match “{query}”.
            </li>
          ) : (
            results.map((option, position) => (
              <li
                key={option.key}
                role="presentation"
                className={position === activeIndex ? 'is-active' : undefined}
              >
                <button
                  type="button"
                  role="option"
                  id={optionId(option.key)}
                  aria-selected={position === activeIndex}
                  tabIndex={-1}
                  onClick={() => onSelect(option.selection)}
                >
                  <span>
                    {option.path.map((ancestor) => `${ancestor} › `).join('')}
                    <b>{option.name}</b>
                  </span>
                  {option.hint || option.key.startsWith('preset-') ? (
                    <span className="field-hint">
                      {option.key.startsWith('preset-')
                        ? ['Popular', option.hint].filter(Boolean).join(' · ')
                        : option.hint}
                    </span>
                  ) : null}
                </button>
              </li>
            ))
          )}
        </ul>
      ) : (
        <>
          <h3 className="activity-picker__heading">Popular activities</h3>
          <ul className="popular-options" aria-label="Popular activities">
            {popular.map((option) => (
              <li key={option.key} className="popular-option">
                <button
                  type="button"
                  className="popular-option__button"
                  aria-describedby={option.hint ? `${listboxId}-${option.key}` : undefined}
                  onClick={() => onSelect(option.selection)}
                >
                  {option.name}
                </button>
                {option.hint ? (
                  <span className="popular-option__hint" id={`${listboxId}-${option.key}`}>
                    {option.hint}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          <h3 className="activity-picker__heading">Browse all activities</h3>
          <p className="field-hint">Selecting a category includes every activity beneath it.</p>
          <ul className="activity-tree" aria-label="Activity categories">
            {index.topLevel.map((entry) => (
              <TreeNode
                key={entry.code}
                entry={entry}
                index={index}
                onSelect={(chosen) => onSelect({ include: [chosen.code], label: chosen.name })}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function TreeNode({
  entry,
  index,
  onSelect,
}: {
  entry: ActivityEntry
  index: LexiconIndex
  onSelect: (entry: ActivityEntry) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const children = index.childrenOf.get(entry.code) ?? []
  const leaves = index.offeredLeafCount(entry.code)
  const name = activityName(entry.name)

  return (
    <li>
      <div className="activity-tree__row">
        {children.length > 0 ? (
          <button
            type="button"
            className="activity-tree__toggle"
            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${name}`}
            onClick={() => setExpanded((current) => !current)}
          >
            <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
          </button>
        ) : (
          <span className="activity-tree__toggle" aria-hidden="true" />
        )}
        <button type="button" className="activity-tree__select" onClick={() => onSelect(entry)}>
          {name}
          {entry.level < 3 ? <span className="field-hint"> · {leaves} activities</span> : null}
        </button>
      </div>
      {expanded && children.length > 0 ? (
        <ul>
          {children.map((child) => (
            <TreeNode key={child.code} entry={child} index={index} onSelect={onSelect} />
          ))}
        </ul>
      ) : null}
    </li>
  )
}
