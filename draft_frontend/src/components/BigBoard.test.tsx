import { act, type ComponentProps } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Draftable, LeagueSnapshot } from '../lib/types'
import { INITIAL_CATALOG_FILTERS, type CatalogFilterState } from '../lib/catalogFilters'
import { BigBoard } from './BigBoard'

const catalog: Draftable[] = [
  {
    id: 1,
    name: 'Bulbasaur',
    species: 'Bulbasaur',
    form: '',
    dex: 1,
    point_cost: 4,
    types: ['Grass', 'Poison'],
    abilities: ['Overgrow'],
    hidden_ability: 'Chlorophyll',
    base_stats: null,
    image_url: null,
    megas: [],
  },
  {
    id: 2,
    name: 'Pikachu',
    species: 'Pikachu',
    form: '',
    dex: 25,
    point_cost: 12,
    types: ['Electric'],
    abilities: ['Static'],
    hidden_ability: null,
    base_stats: null,
    image_url: null,
    megas: [],
  },
  {
    id: 3,
    name: 'Raichu',
    species: 'Raichu',
    form: '',
    dex: 26,
    point_cost: 12,
    types: ['Electric'],
    abilities: ['Static'],
    hidden_ability: null,
    base_stats: null,
    image_url: null,
    megas: [],
  },
  {
    id: 4,
    name: 'Charmander',
    species: 'Charmander',
    form: '',
    dex: 4,
    point_cost: 6,
    types: ['Fire'],
    abilities: ['Blaze'],
    hidden_ability: null,
    base_stats: null,
    image_url: null,
    megas: [],
  },
]

function league(): LeagueSnapshot {
  return {
    league_id: 20,
    revision: 99,
    server_now: '2026-10-19T12:00:00Z',
    league: {
      id: 20,
      name: 'Test league',
      commissioner_user_id: 11,
      roster_size: 2,
      pick_seconds: 60,
      revision: 99,
      created_at: '2026-10-19T12:00:00Z',
      started_at: '2026-10-19T12:00:00Z',
      turn_started_at: null,
    },
    state: {
      league_id: 20,
      revision: 99,
      status: 'drafting',
      roster_size: 2,
      team_count: 2,
      active_pick_count: 1,
      required_pick_count: 4,
      can_start: false,
      next_pick_number: 2,
      next_team_id: 8,
      last_pick_id: 50,
      deadline: null,
    },
    teams: [
      {
        id: 8,
        league_id: 20,
        name: 'Alpha',
        color_rgb: 0,
        metadata_complete: true,
        can_set_metadata: false,
        manager_user_id: 11,
        draft_position: 1,
        point_budget: 17,
        points_spent: 12,
        points_remaining: 5,
      },
    ],
    rosters: [
      {
        league_id: 20,
        team_id: 8,
        draftable_id: 2,
        pick_id: 50,
        pick_number: 1,
        selected_by: 11,
        selected_at: '2026-10-19T12:00:00Z',
        point_cost: 12,
      },
    ],
    history: [],
    actions: [],
  }
}

let renderer: ReactTestRenderer | null
let props: ComponentProps<typeof BigBoard>

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

const control = (label: string) => renderer!.root.findByProps({ 'aria-label': label })

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<BigBoard {...props} />)
    else renderer = create(<BigBoard {...props} />)
  })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  props = {
    catalog,
    snapshot: league(),
    onSelect: vi.fn(),
  }
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
})

describe('BigBoard', () => {
  it('groups Pokémon by point cost descending, marks drafted items as taken, and opens details on click', async () => {
    await render()
    const columns = renderer!.root.findAllByProps({ className: 'big-board-column  over-budget-tier' })
    expect(columns).toHaveLength(2) // 12pt and 6pt are over 5pt budget
    const tier12 = control('12 point tier')
    const tier6 = control('6 point tier')
    const tier4 = control('4 point tier')
    expect(text(tier12)).toContain('12pts1/2 left')
    expect(text(tier6)).toContain('6pts1/1 left')
    expect(text(tier4)).toContain('4pts1/1 left')

    const pikachu = control('View Pikachu, 12 points, already drafted')
    expect(pikachu.props.className).toContain('taken')
    const raichu = control('View Raichu, 12 points, over budget')
    expect(raichu.props.className).not.toContain('taken')

    await act(async () => raichu.props.onClick())
    expect(props.onSelect).toHaveBeenCalledWith(catalog[2])
  })

  it('accepts controlled filter state and clears all filters', async () => {
    let state: CatalogFilterState = {
      ...INITIAL_CATALOG_FILTERS,
      filters: [{ kind: 'type', value: 'Electric', label: 'Electric type' }],
    }
    props.filterState = state
    props.onFilterStateChange = (updater) => {
      state = typeof updater === 'function' ? updater(state) : updater
      props.filterState = state
    }
    await render()
    expect(renderer!.root.findAllByProps({ 'aria-label': '12 point tier' })).toHaveLength(1)
    expect(renderer!.root.findAllByProps({ 'aria-label': '4 point tier' })).toHaveLength(0)

    const clearBtn = renderer!.root
      .findAllByType('button')
      .find((node) => text(node).trim() === 'Clear filters')!
    await act(async () => clearBtn.props.onClick())
    await render()

    expect(state).toEqual(INITIAL_CATALOG_FILTERS)
    expect(renderer!.root.findAllByProps({ 'aria-label': '4 point tier' })).toHaveLength(1)
  })

  it('renders a collapsible equalizer minimap, jumps to a tier on click, and provides a top horizontal scrollbar', async () => {
    await render()
    // Top horizontal scrollbar is rendered right above the board
    const topScroll = control('Horizontal board scroll')
    expect(topScroll).toBeDefined()

    // Equalizer minimap shows stacks for each tier and can jump to a tier
    const jump12 = control('Jump to 12 point tier, 1 of 2 remaining')
    expect(jump12).toBeDefined()
    const pips12 = jump12.findAllByType('span').filter((s) => s.props.className?.includes('equalizer-pip'))
    expect(pips12).toHaveLength(2)
    expect(pips12[0].props.className).not.toContain('taken')
    expect(pips12[1].props.className).toContain('taken')

    await act(async () => jump12.props.onClick())

    // Collapse and re-expand the minimap
    const collapseBtn = control('Collapse pool minimap')
    expect(collapseBtn.props['aria-expanded']).toBe(true)
    expect(text(collapseBtn)).toContain('Collapse')
    await act(async () => collapseBtn.props.onClick())
    expect(renderer!.root.findAllByProps({ 'aria-label': 'Jump to point tier' })).toHaveLength(0)

    const expandBtn = control('Expand pool minimap')
    expect(expandBtn.props['aria-expanded']).toBe(false)
    expect(text(expandBtn)).toContain('Expand')
    await act(async () => expandBtn.props.onClick())
    expect(renderer!.root.findAllByProps({ 'aria-label': 'Jump to point tier' })).toHaveLength(1)
  })
})
