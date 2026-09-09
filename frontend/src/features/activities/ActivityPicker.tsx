/**
 * Activity selection: search, curated presets, and hierarchical browsing of
 * the harmonized lexicon — all driven by the activities API. Selecting a
 * 2- or 4-digit tier means the tier and all of its descendants; the picker
 * says so explicitly rather than pretending every code is one activity.
 *
 * The search results implement the WAI-ARIA combobox/listbox pattern, which
 * a native <select> cannot express — the two rules below flag exactly that
 * pattern, so they are disabled for this file only.
 */
/* oxlint-disable jsx-a11y/prefer-tag-over-role, jsx-a11y/no-noninteractive-element-to-interactive-role */
import { useEffect, useId, useMemo, useRef, useState } from 'react'

import { useActivities, useActivityPresets } from '../../api/queries'
import type { ActivityEntry, ActivitySelection } from '../../api/types'
import { buildLexiconIndex, type LexiconIndex } from './lexicon'

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
  const presetLabels = useMemo(() => {
    const map = new Map<string, string>()
    for (const preset of presets.data?.presets ?? []) map.set(preset.name, preset.label)
    return map
  }, [presets.data])

  const select = (selection: ActivitySelection) => {
    onChange(selection)
    setOpen(false)
    toggleRef.current?.focus()
  }

  return (
    <div className="activity-picker">
      <div className="activity-picker__current">
        <SelectionSummary value={value} index={index} presetLabels={presetLabels} />
        <button
          ref={toggleRef}
          type="button"
          className="btn btn--small"
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? 'Close' : 'Change activity'}
        </button>
      </div>
      {error ? <p className="error-text">{error}</p> : null}
      {open ? (
        <div className="activity-picker__panel">
          {activities.isPending || presets.isPending ? (
            <output className="field-hint" style={{ display: 'block' }}>
              Loading the activity list…
            </output>
          ) : activities.isError || !index ? (
            <p className="error-text">
              The activity list could not be loaded. Close and reopen to retry.
            </p>
          ) : (
            <PickerPanel
              index={index}
              presets={presets.data?.presets ?? []}
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

function SelectionSummary({
  value,
  index,
  presetLabels,
}: {
  value: ActivitySelection
  index: LexiconIndex | null
  presetLabels: ReadonlyMap<string, string>
}) {
  if (value.preset != null) {
    return (
      <div>
        <span className="activity-picker__name">
          {presetLabels.get(value.preset) ?? value.preset}
        </span>
        <span className="field-hint"> — curated selection</span>
      </div>
    )
  }
  const codes = value.include ?? []
  if (codes.length === 1 && index) {
    const code = codes[0] as string
    const entry = index.byCode.get(code)
    const leaves = index.leafCount(code)
    return (
      <div>
        <span className="activity-picker__name">{entry?.name ?? `Activity ${code}`}</span>
        <span className="field-hint">
          {' '}
          — code {code}
          {leaves > 1 ? `, includes all ${leaves} specific activities under it` : ''}
        </span>
      </div>
    )
  }
  return (
    <div>
      <span className="activity-picker__name">{value.label ?? 'Custom selection'}</span>
      <span className="field-hint">
        {' '}
        — {codes.length} codes
        {value.exclude?.length ? `, ${value.exclude.length} excluded` : ''}
      </span>
    </div>
  )
}

function PickerPanel({
  index,
  presets,
  onSelect,
  onClose,
}: {
  index: LexiconIndex
  presets: { name: string; label: string; include: string[]; exclude: string[] }[]
  onSelect: (selection: ActivitySelection) => void
  onClose: () => void
}) {
  const listboxId = useId()
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const results = useMemo(() => index.search(query), [index, query])

  // Option ids are keyed by activity code (not list position) so
  // aria-activedescendant changes value as the results change — screen
  // readers then re-announce the active option while the user types.
  const optionId = (code: string) => `${listboxId}-option-${code}`
  const activeOptionId =
    query.trim() === ''
      ? undefined
      : results.length === 0
        ? `${listboxId}-no-results`
        : results[activeIndex]
          ? optionId(results[activeIndex].code)
          : undefined

  // Keep the active option visible inside the scrollable listbox.
  useEffect(() => {
    if (activeOptionId) {
      document.getElementById(activeOptionId)?.scrollIntoView?.({ block: 'nearest' })
    }
  }, [activeOptionId])

  const selectEntry = (entry: ActivityEntry) => {
    onSelect({ include: [entry.code], label: entry.name })
  }

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
      const entry = results[activeIndex]
      if (entry) selectEntry(entry)
    }
  }

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
          placeholder="e.g. television, sleep, cooking"
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
            results.map((entry, position) => {
              const path = index.pathOf(entry.code)
              const leaves = index.leafCount(entry.code)
              return (
                <li
                  key={entry.code}
                  role="presentation"
                  className={position === activeIndex ? 'is-active' : undefined}
                >
                  <button
                    type="button"
                    role="option"
                    id={optionId(entry.code)}
                    aria-selected={position === activeIndex}
                    tabIndex={-1}
                    onClick={() => selectEntry(entry)}
                  >
                    <span>
                      {path.map((ancestor) => `${ancestor.name} › `).join('')}
                      <b>{entry.name}</b>
                    </span>
                    <span className="field-hint">
                      {entry.level < 3
                        ? `category ${entry.code} · ${leaves} activities`
                        : `code ${entry.code}`}
                    </span>
                  </button>
                </li>
              )
            })
          )}
        </ul>
      ) : (
        <>
          <h3 className="activity-picker__heading">Common selections</h3>
          <ul className="chip-row" aria-label="Preset activity selections">
            {presets.map((preset) => (
              <li key={preset.name}>
                <button
                  type="button"
                  className="chip"
                  onClick={() => onSelect({ preset: preset.name })}
                >
                  {preset.label}
                </button>
              </li>
            ))}
          </ul>
          <h3 className="activity-picker__heading">Browse all activities</h3>
          <p className="field-hint">Selecting a category includes every activity beneath it.</p>
          <ul className="activity-tree" aria-label="Activity categories">
            {index.topLevel.map((entry) => (
              <TreeNode key={entry.code} entry={entry} index={index} onSelect={selectEntry} />
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
  const leaves = index.leafCount(entry.code)

  return (
    <li>
      <div className="activity-tree__row">
        {children.length > 0 ? (
          <button
            type="button"
            className="activity-tree__toggle"
            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${entry.name}`}
            onClick={() => setExpanded((current) => !current)}
          >
            <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
          </button>
        ) : (
          <span className="activity-tree__toggle" aria-hidden="true" />
        )}
        <button type="button" className="activity-tree__select" onClick={() => onSelect(entry)}>
          {entry.name}
          <span className="field-hint">
            {' '}
            {entry.level < 3 ? `· ${leaves} activities` : `· ${entry.code}`}
          </span>
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
