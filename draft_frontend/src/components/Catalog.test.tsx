import { act, type ComponentProps, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { FavoritesListSnapshot } from '../lib/favoritesTypes'
import type { Draftable, LeagueSnapshot } from '../lib/types'
import { Catalog } from './Catalog'

vi.mock('./Modal', () => ({
  Modal: ({ title, children }: { title: string; children: ReactNode }) => (
    <section role="dialog" aria-label={title}>
      {children}
    </section>
  ),
}))
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      addFavoritesEntry: vi.fn(),
      removeFavoritesEntry: vi.fn(),
      setFavoritesOrder: vi.fn(),
      renameFavoritesList: vi.fn(),
      deleteFavoritesList: vi.fn(),
    },
  }
})

const catalog: Draftable[] = [
  { id: 1, name: 'Bulbasaur', dex: 1, point_cost: 4 },
  { id: 2, name: 'Pikachu', dex: 25, point_cost: 12 },
  { id: 3, name: 'Squirtle', dex: 7, point_cost: 3 },
  { id: 4, name: 'Charmander', dex: 4, point_cost: 6 },
].map((item) => ({
  ...item,
  species: item.name,
  form: '',
  types: null,
  abilities: null,
  hidden_ability: null,
  base_stats: null,
  image_url: null,
  megas: [],
}))

function savedList(): FavoritesListSnapshot {
  return {
    id: 7,
    owner_user_id: 11,
    name: 'Rain team',
    revision: 4,
    entries: [
      { draftable_id: 3, ordinal: 1 },
      { draftable_id: 1, ordinal: 3 },
      { draftable_id: 2, ordinal: 4 },
    ],
  }
}

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
let props: ComponentProps<typeof Catalog>

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

function button(label: string, scope = renderer!.root) {
  const found = scope.findAllByType('button').find((node) => text(node).trim() === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}

const control = (label: string) => renderer!.root.findByProps({ 'aria-label': label })
const dialog = () => renderer!.root.findByProps({ role: 'dialog' })
const cardNames = () =>
  renderer!.root.findAllByType('article').map((card) => text(card.findByType('h3')))

async function click(node: ReactTestInstance) {
  await act(async () => node.props.onClick())
}

async function change(node: ReactTestInstance, value: string) {
  await act(async () => node.props.onChange({ target: { value } }))
}

async function submit() {
  await act(async () => {
    await dialog().findByType('form').props.onSubmit({ preventDefault: vi.fn() })
  })
}

async function showFavorites() {
  await click(button(`Favorites${props.favorites.state?.entries.length ?? 0}`))
}

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<Catalog {...props} />)
    else renderer = create(<Catalog {...props} />)
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  props = {
    catalog,
    snapshot: league(),
    busy: false,
    onSelect: vi.fn(),
    favorites: {
      lists: [savedList(), { id: 9, owner_user_id: 11, name: 'Late picks', revision: 0 }],
      selectedId: 7,
      state: savedList(),
      loading: false,
      error: '',
      readError: '',
      writeError: '',
      busy: false,
      selectList: vi.fn(),
      refresh: vi.fn(async () => {}),
      clearError: vi.fn(),
      run: vi.fn(async (operation) => {
        await operation()
        return true
      }),
      create: vi.fn(async () => true),
    },
  }
  vi.mocked(api.addFavoritesEntry).mockResolvedValue({ result: null })
  vi.mocked(api.removeFavoritesEntry).mockResolvedValue({ result: null })
  vi.mocked(api.setFavoritesOrder).mockResolvedValue({ result: null })
  vi.mocked(api.renameFavoritesList).mockResolvedValue({ result: null })
  vi.mocked(api.deleteFavoritesList).mockResolvedValue({ result: null })
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
})

describe('catalog cards', () => {
  it('show the points ribbon, types, and Mega Evolutions', async () => {
    const stats = { hp: 1, attack: 1, defense: 1, special_attack: 1, special_defense: 1, speed: 1 }
    props.catalog = [
      {
        ...catalog[0],
        types: ['Grass', 'Poison'],
        megas: [
          {
            id: 5,
            name: 'Mega Bulbasaur',
            types: ['Grass', 'Fairy'],
            abilities: ['Thick Fat'],
            hidden_ability: null,
            base_stats: stats,
            image_url: null,
          },
        ],
      },
      { ...catalog[3], types: ['Fire'] },
    ]
    await render()
    const [bulbasaur, charmander] = renderer!.root.findAllByType('article')
    const pills = (card: ReactTestInstance) =>
      card.findAllByProps({ className: 'type-pill' }).map(text)
    expect(pills(bulbasaur)).toEqual(['Grass', 'Poison', 'Grass', 'Fairy'])
    expect(text(bulbasaur)).toContain('Mega Bulbasaur')
    expect(text(bulbasaur.findByProps({ className: 'points-ribbon' }))).toBe('4pts')
    expect(pills(charmander)).toEqual(['Fire'])
    expect(text(charmander)).not.toContain('Mega')
    expect(charmander.findAllByProps({ className: 'card-megas' })).toHaveLength(0)
    // Alpha has 5 points left, so Charmander (6) is flagged over budget.
    expect(charmander.findByProps({ className: 'points-ribbon over-budget' })).toBeDefined()
    expect(control('View Charmander, 6 points, over budget')).toBeDefined()
    expect(
      bulbasaur.findByProps({ className: 'bookmark-button card-bookmark saved' }),
    ).toBeDefined()
  })
})

describe('catalog search filters', () => {
  const searchBox = () => control('Search Pokémon by name or Pokédex number')
  const options = () => renderer!.root.findAllByProps({ role: 'option' }).map(text)
  const option = (label: string) =>
    renderer!.root.findAllByProps({ role: 'option' }).find((node) => text(node) === label)!

  beforeEach(() => {
    const mega = {
      id: 9,
      name: 'Mega Venusaur',
      types: ['Grass', 'Poison'],
      abilities: ['Thick Fat'],
      hidden_ability: null,
      base_stats: { hp: 1, attack: 1, defense: 1, special_attack: 1, special_defense: 1, speed: 1 },
      image_url: null,
    }
    const lightScreen = {
      name: 'Light Screen',
      type: 'Psychic',
      category: 'Status' as const,
      power: null,
      accuracy: null,
      pp: 20,
      description: 'Halves special damage.',
    }
    props.snapshot = null
    props.catalog = [
      {
        ...catalog[0],
        types: ['Grass', 'Poison'],
        abilities: ['Overgrow'],
        hidden_ability: 'Chlorophyll',
        megas: [mega],
        moves: [lightScreen],
      },
      { ...catalog[1], types: ['Electric'], abilities: ['Static'], moves: [lightScreen] },
      { ...catalog[2], types: ['Water'], abilities: ['Torrent'] },
      {
        ...catalog[3],
        name: 'Oddish',
        species: 'Oddish',
        types: ['Grass', 'Poison'],
        abilities: ['Chlorophyll'],
      },
    ]
  })

  it('suggests type, ability, move, and Mega filters and combines selected ones', async () => {
    await render()
    await change(searchBox(), 'light screen')
    expect(options()).toEqual(['Light Screen move'])
    await click(option('Light Screen move'))
    expect(searchBox().props.value).toBe('')
    expect(cardNames()).toEqual(['Bulbasaur', 'Pikachu'])
    await click(control('Remove Light Screen move filter'))
    expect(cardNames()).toHaveLength(4)

    await change(searchBox(), 'grass')
    expect(options()).toEqual(['Grass type'])
    await click(option('Grass type'))
    expect(searchBox().props.value).toBe('')
    expect(cardNames()).toEqual(['Bulbasaur', 'Oddish'])

    await change(searchBox(), 'chloro')
    expect(options()).toEqual(['Chlorophyll ability'])
    await click(option('Chlorophyll ability'))
    expect(cardNames()).toEqual(['Bulbasaur', 'Oddish'])

    await change(searchBox(), 'mega')
    expect(options()).toEqual(['Has Mega Evolution'])
    await click(option('Has Mega Evolution'))
    expect(cardNames()).toEqual(['Bulbasaur'])

    await change(searchBox(), 'grass')
    expect(options()).toEqual([])
    await change(searchBox(), '')

    await click(control('Remove Has Mega Evolution filter'))
    expect(cardNames()).toEqual(['Bulbasaur', 'Oddish'])
    await click(button('Clear filters'))
    expect(cardNames()).toHaveLength(4)
  })

  it('selects the highlighted suggestion with the keyboard', async () => {
    await render()
    await change(searchBox(), 'o')
    const key = (name: string) =>
      act(async () => searchBox().props.onKeyDown({ key: name, preventDefault: vi.fn() }))
    await key('ArrowDown')
    const second = options()[1]
    await key('Enter')
    expect(text(renderer!.root.findByProps({ className: 'search-filter-chip' }))).toBe(second)
    await key('Backspace')
    expect(renderer!.root.findAllByProps({ className: 'search-filter-chip' })).toHaveLength(0)
  })
})

describe('catalog favorites', () => {
  it('preserves the browser watchlist and copies an entry only after an explicit bookmark click', async () => {
    props.favorites.state = { ...savedList(), entries: [] }
    await render()
    expect(renderer!.root.findAllByType('button').map(text).join(' ')).not.toContain(
      'Browser watchlist',
    )

    props.legacyWatchlist = [2, 4]
    await render()
    await click(button('Browser watchlist 2'))
    expect(cardNames()).toEqual(['Charmander', 'Pikachu'])
    expect(control('View Pikachu, 12 points, already drafted')).toBeDefined()
    expect(text(renderer!.root)).toContain('Only you can view your account favorites.')
    expect(props.favorites.run).not.toHaveBeenCalled()
    expect(props.favorites.create).not.toHaveBeenCalled()
    expect(api.addFavoritesEntry).not.toHaveBeenCalled()
    expect(api.removeFavoritesEntry).not.toHaveBeenCalled()

    await click(control('Add Pikachu to Rain team'))
    expect(api.addFavoritesEntry).toHaveBeenCalledExactlyOnceWith({
      p_list: 7,
      p_revision: 4,
      p_draftable: 2,
    })
    expect(props.legacyWatchlist).toEqual([2, 4])
    expect(cardNames()).toEqual(['Charmander', 'Pikachu'])
    expect(api.removeFavoritesEntry).not.toHaveBeenCalled()
  })

  it('selects named lists by ID and shows the selected list membership', async () => {
    await render()
    expect(control('Favorites list').findAllByType('option').map(text)).toEqual([
      'Rain team',
      'Late picks',
    ])
    expect(control('Favorites list').props.value).toBe(7)
    await change(control('Favorites list'), '9')
    expect(props.favorites.selectList).toHaveBeenCalledWith(9)
    props.favorites = {
      ...props.favorites,
      selectedId: 9,
      state: { ...props.favorites.lists[1], entries: [{ draftable_id: 4, ordinal: 1 }] },
    }
    await render()
    await showFavorites()
    expect(cardNames()).toEqual(['Charmander'])
    expect(control('Remove Charmander from Late picks').props['aria-pressed']).toBe(true)
  })

  it('keeps drafted favorites visible in saved order and opens their details', async () => {
    await render()
    expect(cardNames()).not.toContain('Pikachu')
    await showFavorites()
    expect(cardNames()).toEqual(['Squirtle', 'Bulbasaur', 'Pikachu'])
    const drafted = control('View Pikachu, 12 points, already drafted')
    expect(text(drafted.parent!)).toContain('Drafted')
    await click(drafted)
    expect(props.onSelect).toHaveBeenCalledWith(catalog[1])
    expect(control('Remove Pikachu from Rain team').props.disabled).toBe(false)
  })

  it('reorders the complete saved list even while search and budget filters hide entries', async () => {
    await render()
    await showFavorites()
    await click(button('In budget'))
    await change(control('Search Pokémon by name or Pokédex number'), 'bulba')
    expect(cardNames()).toEqual(['Bulbasaur'])
    await click(control('Move Bulbasaur earlier'))
    expect(api.setFavoritesOrder).toHaveBeenCalledWith({
      p_list: 7,
      p_revision: 4,
      p_draftables: [1, 3, 2],
    })
    expect(props.favorites.run).toHaveBeenCalledOnce()
  })

  it('adds and removes entries using the selected list revision independently of drafting', async () => {
    await render()
    await click(control('Add Charmander to Rain team'))
    expect(api.addFavoritesEntry).toHaveBeenCalledWith({ p_list: 7, p_revision: 4, p_draftable: 4 })
    await showFavorites()
    await click(control('Remove Pikachu from Rain team'))
    expect(api.removeFavoritesEntry).toHaveBeenCalledWith({
      p_list: 7,
      p_revision: 4,
      p_draftable: 2,
    })
  })

  it('creates a named list without a league and requires a nonblank name', async () => {
    props.snapshot = null
    props.favorites = { ...props.favorites, lists: [], selectedId: null, state: null }
    await render()
    expect(control('Favorites list').props.disabled).toBe(true)
    await click(button('New list'))
    expect(dialog().props['aria-label']).toBe('Create favorites list')
    await change(control('Favorites list name'), '   ')
    expect(button('Create list', dialog()).props.disabled).toBe(true)
    await submit()
    expect(props.favorites.create).not.toHaveBeenCalled()
    await change(control('Favorites list name'), '  First-round targets  ')
    await submit()
    expect(props.favorites.create).toHaveBeenCalledWith('First-round targets')
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('keeps an unsuccessful create dialog open with its entered name', async () => {
    vi.mocked(props.favorites.create).mockResolvedValue(false)
    await render()
    await click(button('New list'))
    await change(control('Favorites list name'), 'First-round targets')
    await submit()
    props.favorites.error = 'The server could not save this list.'
    await render()
    expect(control('Favorites list name').props.value).toBe('First-round targets')
    expect(text(dialog())).toContain('The server could not save this list.')
  })

  it('renames with the captured list identity and revision', async () => {
    await render()
    await click(control('Rename favorites list'))
    expect(control('Favorites list name').props.value).toBe('Rain team')
    await change(control('Favorites list name'), '  Rain shortlist  ')
    await submit()
    expect(api.renameFavoritesList).toHaveBeenCalledWith({
      p_list: 7,
      p_revision: 4,
      p_name: 'Rain shortlist',
    })
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('requires confirmation before deleting the selected list', async () => {
    await render()
    await click(control('Delete favorites list'))
    expect(api.deleteFavoritesList).not.toHaveBeenCalled()
    expect(text(dialog())).toContain('Rain team')
    expect(text(dialog())).toContain('3 saved Pokémon')
    await click(button('Cancel', dialog()))
    expect(api.deleteFavoritesList).not.toHaveBeenCalled()
    await click(control('Delete favorites list'))
    await submit()
    expect(api.deleteFavoritesList).toHaveBeenCalledWith({ p_list: 7, p_revision: 4 })
  })

  it.each(['rename', 'delete'] as const)(
    'blocks a stale %s dialog after another list edit',
    async (kind) => {
      await render()
      await click(control(kind === 'rename' ? 'Rename favorites list' : 'Delete favorites list'))
      if (kind === 'rename') await change(control('Favorites list name'), 'My pending name')
      props.favorites.state = { ...savedList(), name: 'Changed elsewhere', revision: 5 }
      await render()
      expect(text(dialog())).toContain('This list changed.')
      expect(button(kind === 'rename' ? 'Save name' : 'Delete list', dialog()).props.disabled).toBe(
        true,
      )
      await submit()
      expect(api.renameFavoritesList).not.toHaveBeenCalled()
      expect(api.deleteFavoritesList).not.toHaveBeenCalled()
      if (kind === 'rename')
        expect(control('Favorites list name').props.value).toBe('My pending name')
    },
  )

  it('does not retarget an open confirmation when another list is selected', async () => {
    await render()
    await click(control('Delete favorites list'))
    props.favorites = {
      ...props.favorites,
      selectedId: 9,
      state: { ...savedList(), id: 9, name: 'Late picks' },
    }
    await render()
    expect(text(dialog())).toContain('Delete “Rain team”')
    expect(button('Delete list', dialog()).props.disabled).toBe(true)
    await submit()
    expect(api.deleteFavoritesList).not.toHaveBeenCalled()
  })

  it.each(['loading', 'busy', 'readError', 'draftBusy'] as const)(
    'locks list edits during %s',
    async (state) => {
      if (state === 'draftBusy') props.busy = true
      else if (state === 'readError') props.favorites.readError = 'Could not refresh favorites.'
      else props.favorites[state] = true
      await render()
      await showFavorites()
      for (const label of [
        'Rename favorites list',
        'Delete favorites list',
        'Remove Bulbasaur from Rain team',
        'Move Bulbasaur earlier',
        'Move Bulbasaur later',
      ]) {
        expect(control(label).props.disabled).toBe(true)
      }
      await click(control('Remove Bulbasaur from Rain team'))
      await click(control('Move Bulbasaur earlier'))
      expect(props.favorites.run).not.toHaveBeenCalled()
      if (state === 'busy' || state === 'draftBusy') {
        expect(control('Favorites list').props.disabled).toBe(true)
        expect(button('New list').props.disabled).toBe(true)
      }
      if (state !== 'readError') expect(control('Refresh favorites').props.disabled).toBe(true)
    },
  )
})
