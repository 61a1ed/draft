import { act } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readableTextColor } from '../lib/pokemonTypes'
import type { BaseStats, Draftable } from '../lib/types'
import { PokemonDetails } from './PokemonDetails'

const stats = (...values: number[]): BaseStats => {
  const [hp, attack, defense, special_attack, special_defense, speed] = values
  return { hp, attack, defense, special_attack, special_defense, speed }
}

const charizard: Draftable = {
  id: 2,
  name: 'Charizard',
  point_cost: 22,
  dex: 6,
  species: 'Charizard',
  form: '',
  types: ['Fire', 'Flying'],
  abilities: ['Blaze'],
  hidden_ability: 'Solar Power',
  base_stats: stats(78, 84, 78, 109, 85, 100),
  image_url: '/images/charizard',
  megas: [
    {
      id: 2,
      name: 'Mega Charizard X',
      types: ['Fire', 'Dragon'],
      abilities: ['Tough Claws'],
      hidden_ability: null,
      base_stats: stats(78, 130, 111, 130, 85, 100),
      image_url: '/images/charizard-x',
    },
    {
      id: 3,
      name: 'Mega Charizard Y',
      types: ['Fire', 'Flying'],
      abilities: ['Drought'],
      hidden_ability: null,
      base_stats: stats(78, 104, 78, 159, 115, 100),
      image_url: null,
    },
  ],
  moves: [
    {
      name: 'Air Slash',
      type: 'Flying',
      category: 'Special',
      power: 75,
      accuracy: 95,
      pp: 16,
      description: '30% chance to make the target flinch.',
    },
    {
      name: 'Dragon Dance',
      type: 'Dragon',
      category: 'Status',
      power: null,
      accuracy: null,
      pp: 20,
      description: "Raises the user's Attack and Speed by 1.",
    },
    {
      name: 'Flare Blitz',
      type: 'Fire',
      category: 'Physical',
      power: 120,
      accuracy: 100,
      pp: 16,
      description: 'Has 33% recoil. 10% chance to burn.',
    },
  ],
}

let renderer: ReactTestRenderer | null

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}
const root = () => renderer!.root
const tabs = () => root().findAllByProps({ role: 'tab' })
const pills = () => root().findAllByProps({ className: 'type-pill' }).map(text)
const abilities = () =>
  root().findByProps({ className: 'ability-list' }).findAllByType('li').map(text)
const ribbon = () => text(root().findByProps({ className: 'points-ribbon' }))
const statRow = (label: string) =>
  root()
    .findAll((node) => node.type === 'div' && String(node.props.className).startsWith('stat-row'))
    .find((row) => text(row.findByType('dt')) === label)!

async function render(item: Draftable) {
  await act(async () => {
    renderer = create(<PokemonDetails item={item} />)
  })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
})

describe('Pokémon details', () => {
  it('shows the base form with points, types, one ability list, and stats', async () => {
    await render(charizard)
    expect(text(root().findByType('h2'))).toBe('Charizard')
    expect(ribbon()).toBe('22pts')
    expect(pills()).toEqual(['Fire', 'Flying'])
    expect(abilities()).toEqual(['Blaze', 'Solar Power'])
    expect(text(statRow('Sp. Atk'))).toBe('Sp. Atk109')
    expect(text(statRow('Total'))).toBe('Total534')
    expect(tabs().map(text)).toEqual([
      'Base form',
      'Mega Charizard X',
      'Mega Charizard Y',
      'Moves',
    ])
    expect(tabs()[0].props['aria-selected']).toBe(true)
    expect(tabs()[3].props.className).toContain('moves-tab')
  })

  it('switches to the Moves tab and filters moves by category, type, and name', async () => {
    await render(charizard)
    await act(async () => tabs()[3].props.onClick())
    expect(tabs()[3].props['aria-selected']).toBe(true)
    const moveRows = () =>
      root()
        .findByProps({ 'aria-label': 'Learned moves' })
        .findByType('tbody')
        .findAllByType('tr')
    expect(moveRows().map((row) => text(row.findByType('strong')))).toEqual([
      'Air Slash',
      'Dragon Dance',
      'Flare Blitz',
    ])
    expect(text(moveRows()[0])).toContain('75')
    expect(text(moveRows()[0])).toContain('95%')
    expect(text(moveRows()[0])).toContain('16')
    expect(text(moveRows()[0])).toContain('30% chance to make the target flinch.')

    const searchBox = root().findByProps({
      'aria-label': 'Search moves by name, type, or category',
    })
    await act(async () => searchBox.props.onChange({ target: { value: 'status' } }))
    const statusOption = root()
      .findAllByProps({ role: 'option' })
      .find((node) => text(node) === 'Status category')!
    await act(async () => statusOption.props.onClick())
    expect(moveRows().map((row) => text(row.findByType('strong')))).toEqual(['Dragon Dance'])

    await act(async () =>
      root()
        .findByProps({ 'aria-label': 'Remove Status category filter' })
        .props.onClick(),
    )
    expect(moveRows()).toHaveLength(3)

    await act(async () => searchBox.props.onChange({ target: { value: 'fire' } }))
    const fireOption = root()
      .findAllByProps({ role: 'option' })
      .find((node) => text(node) === 'Fire type')!
    await act(async () => fireOption.props.onClick())
    expect(moveRows().map((row) => text(row.findByType('strong')))).toEqual(['Flare Blitz'])

    await act(async () => searchBox.props.onChange({ target: { value: 'air' } }))
    expect(root().findAllByProps({ 'aria-label': 'Learned moves' })).toHaveLength(0)
    expect(text(root())).toContain('No moves match the current filter.')
  })

  it('switches to a Mega tab and shows its own data with changes from the base form', async () => {
    await render(charizard)
    await act(async () => tabs()[1].props.onClick())
    expect(text(root().findByType('h2'))).toBe('Mega Charizard X')
    expect(pills()).toEqual(['Fire', 'Dragon'])
    expect(abilities()).toEqual(['Tough Claws'])
    expect(text(statRow('Attack'))).toBe('Attack130+46')
    expect(text(statRow('HP'))).toBe('HP78')
    expect(text(statRow('Total'))).toBe('Total634+100')
    expect(root().findByType('img').props.src).toBe('/images/charizard-x')
    // Megas cost the same as their base Pokémon.
    expect(ribbon()).toBe('22pts')

    await act(async () => tabs()[2].props.onClick())
    expect(root().findByType('img').props.src).toBe('/pokeball.svg')
  })

  it('moves between form and Moves tabs with the arrow keys', async () => {
    await render(charizard)
    const list = root().findByProps({ role: 'tablist' })
    await act(async () => list.props.onKeyDown({ key: 'ArrowLeft', preventDefault: vi.fn() }))
    expect(tabs()[3].props['aria-selected']).toBe(true)
    await act(async () => list.props.onKeyDown({ key: 'ArrowRight', preventDefault: vi.fn() }))
    expect(tabs()[0].props['aria-selected']).toBe(true)
  })

  it('hides tabs without Megas or moves and explains missing details', async () => {
    await render({
      ...charizard,
      form: 'Alolan',
      types: null,
      abilities: null,
      hidden_ability: null,
      base_stats: null,
      megas: [],
      moves: [],
    })
    expect(tabs()).toHaveLength(0)
    expect(text(root())).toContain('POKÉDEX #006 · Alolan')
    expect(text(root())).toContain('Battle details have not been imported yet.')
  })

  it('picks readable text for every type color', () => {
    expect(readableTextColor('#C03028')).toBe('#ffffff')
    expect(readableTextColor('#7038F8')).toBe('#ffffff')
    expect(readableTextColor('#F8D030')).toBe('#1d1d1d')
    expect(readableTextColor('#F08030')).toBe('#1d1d1d')
  })
})
