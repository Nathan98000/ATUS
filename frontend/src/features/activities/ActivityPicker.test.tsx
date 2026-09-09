import { screen, waitFor } from '@testing-library/react'
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
      expect(screen.getByText(/includes all 3 specific activities/)).toBeInTheDocument(),
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

  it('offers presets from the API and selects one', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    await user.click(
      screen.getByRole('button', { name: 'Leisure and sports (BLS table A-1 definition)' }),
    )
    expect(onChange).toHaveBeenCalledWith({ preset: 'leisure_and_sports_bls_table' })
  })

  it('browses the hierarchy and communicates category semantics', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    expect(
      screen.getByText(/Selecting a category includes every activity beneath it/),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Expand Personal Care Activities/ }))
    await user.click(screen.getByRole('button', { name: /Sleeping.*3 activities/ }))
    expect(onChange).toHaveBeenCalledWith({ include: ['0101'], label: 'Sleeping' })
  })

  it('shows an empty state for hopeless searches', async () => {
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={() => {}} />)
    const user = await openPicker()
    await user.type(screen.getByLabelText('Search activities'), 'quidditch')
    expect(await screen.findByText(/No activities match/)).toBeInTheDocument()
  })
})
