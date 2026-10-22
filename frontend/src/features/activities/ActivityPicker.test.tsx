import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { renderWithProviders } from '../../test/render'
import { ActivityPicker } from './ActivityPicker'

async function openPicker() {
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Change activity' }))
  await waitFor(() => expect(screen.getByLabelText('Search activities')).toBeInTheDocument())
  return user
}

describe('ActivityPicker', () => {
  it('shows the current selection with descendant semantics', async () => {
    renderWithProviders(
      <ActivityPicker value={{ include: ['0101'], label: 'Sleeping' }} onChange={() => {}} />,
    )
    await waitFor(() =>
      // Sleeping and sleeplessness; the "n.e.c." catch-all is not offered.
      expect(screen.getByText(/includes all 2 activities under it/)).toBeInTheDocument(),
    )
  })

  it('searches the lexicon and selects with the keyboard', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    await user.type(screen.getByLabelText('Search activities'), 'television')
    const options = await screen.findAllByRole('option')
    expect(options[0]).toHaveTextContent('Television and movies (not religious)')
    expect(options[0]).toHaveTextContent('Socializing, Relaxing, and Leisure')

    await user.keyboard('{ArrowDown}{Enter}')
    expect(onChange).toHaveBeenCalledWith({
      include: ['120304'],
      label: 'Television (religious)',
    })
  })

  it('offers presets from the API under plain names and selects one', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    const presets = screen.getByRole('list', { name: 'Preset activity selections' })
    expect(presets).not.toHaveTextContent(/BLS table/)
    // Two presets are both "Eating and drinking": only the BLS-table one is offered.
    expect(
      within(presets).getAllByRole('button', { name: 'Eating and drinking' }),
    ).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Eating and drinking' }))
    expect(onChange).toHaveBeenCalledWith({ preset: 'eating_and_drinking_bls_table' })
    await user.click(screen.getByRole('button', { name: 'Change activity' }))
    await user.click(screen.getByRole('button', { name: 'Leisure and sports' }))
    expect(onChange).toHaveBeenCalledWith({ preset: 'leisure_and_sports_bls_table' })
  })

  it('names a selected preset without its definition qualifier', async () => {
    renderWithProviders(
      <ActivityPicker value={{ preset: 'leisure_and_sports_bls_table' }} onChange={() => {}} />,
    )
    expect(
      await screen.findByText('Leisure and sports', { selector: '.activity-picker__name' }),
    ).toBeInTheDocument()
  })

  it('never offers the "n.e.c." catch-alls or the data codes', async () => {
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={() => {}} />)
    const user = await openPicker()
    await user.type(screen.getByLabelText('Search activities'), 'n.e.c')
    expect(await screen.findByText(/No activities match/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Search activities'))
    const tree = screen.getByRole('list', { name: 'Activity categories' })
    expect(tree).not.toHaveTextContent(/Data Codes/)
  })

  it('browses the hierarchy and communicates category semantics', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    expect(
      screen.getByText(/Selecting a category includes every activity beneath it/),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Expand Personal Care Activities/ }))
    await user.click(screen.getByRole('button', { name: /Sleeping.*2 activities/ }))
    expect(onChange).toHaveBeenCalledWith({ include: ['0101'], label: 'Sleeping' })
  })

  it('shows an empty state for hopeless searches', async () => {
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={() => {}} />)
    const user = await openPicker()
    await user.type(screen.getByLabelText('Search activities'), 'quidditch')
    expect(await screen.findByText(/No activities match/)).toBeInTheDocument()
  })
})
