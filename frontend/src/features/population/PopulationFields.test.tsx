/**
 * The generic renderer for API-discovered population dimensions — the one
 * place where an unknown future dimension type must degrade gracefully.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { PopulationDimension } from '../../api/types'
import { populationMetadataFixture } from '../../test/fixtures'
import { PopulationFields } from './PopulationFields'

const dimensions = populationMetadataFixture.dimensions

describe('PopulationFields (driven by real population metadata)', () => {
  it('renders every category dimension with human labels and an Any default', async () => {
    const onChange = vi.fn()
    render(<PopulationFields dimensions={dimensions} value={{}} onChange={onChange} />)

    const sex = screen.getByLabelText('Sex')
    expect(sex).toHaveValue('')
    expect(screen.getByRole('option', { name: 'Not in the labor force' })).toBeInTheDocument()

    await userEvent.selectOptions(sex, 'female')
    expect(onChange).toHaveBeenCalledWith({ sex: 'female' })
  })

  it('renders booleans as Any/Yes/No without leaking variable names', () => {
    render(<PopulationFields dimensions={dimensions} value={{}} onChange={() => {}} />)
    expect(screen.getByLabelText('Children in household')).toBeInTheDocument()
    expect(screen.queryByText(/TRCHILDNUM/)).not.toBeInTheDocument()
    expect(screen.queryByText(/TELFS/)).not.toBeInTheDocument()
  })

  it('parses age input and never emits NaN', async () => {
    const onChange = vi.fn()
    render(<PopulationFields dimensions={dimensions} value={{}} onChange={onChange} />)
    const min = screen.getByLabelText('Minimum age')
    await userEvent.type(min, '2')
    expect(onChange).toHaveBeenLastCalledWith({ age_min: 2 })
    // A non-numeric value degrades to unset, not NaN.
    onChange.mockClear()
    render(<PopulationFields dimensions={dimensions} value={{}} onChange={onChange} />)
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

  it('auto-opens the secondary section when a hidden filter is active', () => {
    render(
      <PopulationFields
        dimensions={dimensions}
        value={{ state_fips: '06' }}
        onChange={() => {}}
      />,
    )
    // The details section must be open so the active filter is visible.
    const details = screen.getByText(/More filters/).closest('details') as HTMLDetailsElement
    expect(details.open).toBe(true)
    expect(screen.getByLabelText('State (FIPS code)')).toHaveValue('06')
  })

  it('keeps the secondary section collapsed when nothing in it is set', () => {
    render(
      <PopulationFields dimensions={dimensions} value={{ sex: 'male' }} onChange={() => {}} />,
    )
    const details = screen.getByText('More filters').closest('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
  })

  it('renders an unknown future dimension type as a plain text input', () => {
    const future: PopulationDimension = {
      name: 'quantile_group',
      type: 'ordinal-bucket', // a type this UI has never heard of
      description: 'A future dimension.',
    }
    render(
      <PopulationFields dimensions={[...dimensions, future]} value={{}} onChange={() => {}} />,
    )
    expect(screen.getByLabelText('Quantile group')).toBeInTheDocument()
  })
})
