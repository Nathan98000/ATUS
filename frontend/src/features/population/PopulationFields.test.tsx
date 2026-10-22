/**
 * The population filters, driven by the API's real population metadata:
 * opt-in rows from a short list, and the one place where a dimension the
 * list does not offer (or an unknown future type) must still show when a
 * link sets it.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { PopulationDimension, PopulationRequest } from '../../api/types'
import { populationMetadataFixture } from '../../test/fixtures'
import { PopulationFields } from './PopulationFields'

const dimensions = populationMetadataFixture.dimensions

/** The component with its own state, as the builder holds it. */
function Harness({
  initial = {},
  onChange,
  extra = [],
}: {
  initial?: PopulationRequest
  onChange?: (value: PopulationRequest) => void
  extra?: PopulationDimension[]
}) {
  const [value, setValue] = useState<PopulationRequest>(initial)
  return (
    <PopulationFields
      dimensions={[...dimensions, ...extra]}
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange?.(next)
      }}
    />
  )
}

describe('PopulationFields (driven by real population metadata)', () => {
  it('starts as everyone, offering only the six plain filters', () => {
    render(<Harness />)
    expect(screen.getByText('Everyone age 15 and over.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Sex')).not.toBeInTheDocument()
    const add = screen.getByLabelText('Add a filter')
    expect(
      within(add)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual([
      '+ Add a filter',
      'Sex',
      'Age',
      'Employment',
      'Education',
      'Children under 18 at home',
      'Day of week',
    ])
    // Never offered: region, state, diary dates.
    expect(within(add).queryByRole('option', { name: /Region|State|Diaries/ })).toBeNull()
  })

  it('adds a filter as a row with an Any default, and removes it again', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    await user.selectOptions(screen.getByLabelText('Add a filter'), 'sex')
    const sex = screen.getByLabelText('Sex')
    expect(sex).toHaveValue('')
    expect(screen.queryByText('Everyone age 15 and over.')).not.toBeInTheDocument()
    // An added filter leaves the list.
    expect(
      within(screen.getByLabelText('Add a filter')).queryByRole('option', { name: 'Sex' }),
    ).toBeNull()

    await user.selectOptions(sex, 'female')
    expect(onChange).toHaveBeenLastCalledWith({ sex: 'female' })
    await user.click(screen.getByRole('button', { name: 'Remove the sex filter' }))
    expect(onChange).toHaveBeenLastCalledWith({})
    expect(screen.getByText('Everyone age 15 and over.')).toBeInTheDocument()
  })

  it('renders booleans as Any/Yes/No without leaking variable names', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.selectOptions(screen.getByLabelText('Add a filter'), 'has_household_children')
    expect(screen.getByLabelText('Children under 18 at home')).toBeInTheDocument()
    expect(screen.queryByText(/TRCHILDNUM/)).not.toBeInTheDocument()
    expect(screen.queryByText(/TELFS/)).not.toBeInTheDocument()
  })

  it('offers age as the comparison bands, and both ages at once (review round 4)', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    await user.selectOptions(screen.getByLabelText('Add a filter'), 'age')
    const age = screen.getByLabelText('Age')
    // Focus moves to the new filter's control once it renders.
    await waitFor(() => expect(age).toHaveFocus())
    expect(
      within(age)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual([
      'Choose ages',
      '15–24',
      '25–34',
      '35–44',
      '45–54',
      '55–64',
      '65 and over',
      'Other range…',
    ])
    await user.selectOptions(age, '25-34')
    expect(onChange).toHaveBeenLastCalledWith({ age_min: 25, age_max: 34 })
    await user.selectOptions(age, '65+')
    expect(onChange).toHaveBeenLastCalledWith({ age_min: 65, age_max: undefined })
    expect(screen.queryByLabelText('Minimum age')).not.toBeInTheDocument()
  })

  it('opens a range that is no band as "Other range", so a link’s ages show', () => {
    render(
      <PopulationFields
        dimensions={dimensions}
        value={{ age_min: 25, age_max: 54 }}
        onChange={() => {}}
      />,
    )
    expect(screen.getByLabelText('Age')).toHaveValue('other')
    expect(screen.getByLabelText('Minimum age')).toHaveValue(25)
    expect(screen.getByLabelText('Maximum age')).toHaveValue(54)
  })

  it('parses age input and never emits NaN', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    await user.selectOptions(screen.getByLabelText('Add a filter'), 'age')
    await user.selectOptions(screen.getByLabelText('Age'), 'other')
    await user.type(screen.getByLabelText('Minimum age'), '2')
    expect(onChange).toHaveBeenLastCalledWith({ age_min: 2 })
    await user.clear(screen.getByLabelText('Minimum age'))
    expect(onChange).toHaveBeenLastCalledWith({ age_min: undefined })
  })

  it('associates age errors with the inputs', () => {
    render(
      <PopulationFields
        dimensions={dimensions}
        value={{ age_min: 90, age_max: 20 }}
        onChange={() => {}}
        ageError="Minimum age cannot exceed maximum age."
      />,
    )
    const min = screen.getByLabelText('Minimum age')
    expect(min).toHaveAttribute('aria-invalid', 'true')
    expect(min).toHaveAccessibleDescription('Minimum age cannot exceed maximum age.')
  })

  it('shows a filter a link set even when it is not offered, so it is never invisible', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness initial={{ state_fips: '06', sex: 'male' }} onChange={onChange} />)
    expect(screen.getByLabelText('Sex')).toHaveValue('male')
    expect(screen.getByLabelText('State (FIPS code)')).toHaveValue('06')
    await user.click(
      screen.getByRole('button', { name: 'Remove the state (fips code) filter' }),
    )
    expect(onChange).toHaveBeenLastCalledWith({ sex: 'male' })
    expect(screen.queryByLabelText('State (FIPS code)')).not.toBeInTheDocument()
  })

  it('shows an unknown future dimension a link sets as a plain text input', () => {
    const future: PopulationDimension = {
      name: 'quantile_group',
      type: 'ordinal-bucket', // a type this UI has never heard of
      description: 'A future dimension.',
    }
    render(<Harness extra={[future]} initial={{ quantile_group: 'q1' } as PopulationRequest} />)
    expect(screen.getByLabelText('Quantile group')).toHaveValue('q1')
    expect(
      within(screen.getByLabelText('Add a filter')).queryByRole('option', {
        name: 'Quantile group',
      }),
    ).toBeNull()
  })
})
