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
  it('shows the current selection with what it includes', async () => {
    renderWithProviders(
      <ActivityPicker value={{ include: ['0101'], label: 'Sleeping' }} onChange={() => {}} />,
    )
    await waitFor(() =>
      // Sleeping and sleeplessness; the "n.e.c." catch-all is not offered.
      expect(screen.getByText('Includes sleeping; sleeplessness')).toBeInTheDocument(),
    )
  })

  it('says what a selected preset includes (review round 4)', async () => {
    renderWithProviders(
      <ActivityPicker value={{ preset: 'watching_tv' }} onChange={() => {}} />,
    )
    expect(
      await screen.findByText(
        'Includes television and movies (not religious); television (religious)',
      ),
    ).toBeInTheDocument()
  })

  it('searches the lexicon and selects with the keyboard', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    await user.type(screen.getByLabelText('Search activities'), 'television')
    const options = await screen.findAllByRole('option')
    // The preset first, then the lexicon's entries under plain names.
    expect(options[0]).toHaveTextContent('Watching TV')
    expect(options[1]).toHaveTextContent('Television and movies (not religious)')
    expect(options[1]).toHaveTextContent('Socializing, relaxing, and leisure')

    await user.keyboard('{ArrowDown}{Enter}')
    expect(onChange).toHaveBeenCalledWith({
      include: ['120303'],
      label: 'Television and movies (not religious)',
    })
  })

  it('finds activities by everyday words, presets included (review round 4)', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()
    const search = screen.getByLabelText('Search activities')

    await user.type(search, 'tv')
    expect((await screen.findAllByRole('option'))[0]).toHaveTextContent('Watching TV')
    await user.clear(search)
    await user.type(search, 'chores')
    const chores = await screen.findAllByRole('option')
    expect(chores[0]).toHaveTextContent('Housework')
    await user.click(chores[0] as HTMLElement)
    expect(onChange).toHaveBeenCalledWith({ preset: 'housework' })
  })

  it('offers presets from the API under plain names and selects one', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    const presets = screen.getByRole('list', { name: 'Popular activities' })
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

  it('offers popular categories as browsing selects them, and says what each includes', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()
    const volunteering = screen.getByRole('button', { name: 'Volunteering' })
    expect(volunteering).toHaveAccessibleDescription(/^Includes /)
    await user.click(volunteering)
    expect(onChange).toHaveBeenCalledWith({ include: ['15'], label: 'Volunteer Activities' })
  })

  it('never offers the "n.e.c." catch-alls, the data codes or an empty category', async () => {
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={() => {}} />)
    const user = await openPicker()
    await user.type(screen.getByLabelText('Search activities'), 'n.e.c')
    expect(await screen.findByText(/No activities match/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Search activities'))
    const tree = screen.getByRole('list', { name: 'Activity categories' })
    expect(tree).not.toHaveTextContent(/Data Codes/)
    // 0399's only activity is a catch-all: the category itself is not offered.
    await user.click(
      screen.getByRole('button', { name: 'Expand Caring for household members' }),
    )
    expect(tree).not.toHaveTextContent(/NonHH|non-household members/i)
  })

  it('browses the hierarchy and communicates category semantics', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ActivityPicker value={{ preset: 'sleep' }} onChange={onChange} />)
    const user = await openPicker()

    expect(
      screen.getByText(/Selecting a category includes every activity beneath it/),
    ).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /Expand Personal care, including sleep/ }),
    )
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
