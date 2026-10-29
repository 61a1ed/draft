import { act, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { BigBoard } from './components/BigBoard'
import { Catalog } from './components/Catalog'
import { LeagueSetup } from './components/LeagueSetup'
import { api } from './lib/api'
import type { DraftStatus, LeagueSnapshot } from './lib/types'
import { useDraft } from './lib/useDraft'
import { useFavorites } from './lib/useFavorites'

vi.mock('./lib/useDraft', () => ({ useDraft: vi.fn() }))
vi.mock('./lib/useFavorites', () => ({ useFavorites: vi.fn() }))
vi.mock('./components/LeagueSetup', () => ({
  LeagueSetup: vi.fn(({ onStartDraft }: { onStartDraft?: () => void }) => (
    <section data-testid="league-setup">
      Team setup
      <button aria-label="Start draft from settings" onClick={onStartDraft}>
        Start draft
      </button>
    </section>
  )),
}))
vi.mock('./components/Catalog', () => ({
  Catalog: vi.fn(() => <section data-testid="catalog">Available Pokémon</section>),
  PokemonImage: () => <span>Pokémon image</span>,
}))
vi.mock('./components/BigBoard', () => ({
  BigBoard: vi.fn(() => <section data-testid="big-board">Draft Big Board</section>),
}))
vi.mock('./components/Tournament', () => ({ Tournament: () => <section>Tournament</section> }))
vi.mock('./components/Availability', () => ({
  Availability: () => <section>Availability</section>,
}))
vi.mock('./components/Modal', () => ({
  Modal: ({ title, children }: { title: string; children: ReactNode }) => (
    <section role="dialog" aria-label={title}>
      {children}
    </section>
  ),
}))

const stamp = '2026-10-21T12:00:00Z'

function league(status: DraftStatus = 'setup', id = 7): LeagueSnapshot {
  return {
    league_id: id,
    revision: 5,
    server_now: stamp,
    league: {
      id,
      name: `League ${id}`,
      commissioner_user_id: 11,
      roster_size: 2,
      pick_seconds: 60,
      revision: 5,
      created_at: stamp,
      started_at: status === 'setup' ? null : stamp,
      turn_started_at: status === 'drafting' ? stamp : null,
    },
    state: {
      league_id: id,
      revision: 5,
      status,
      roster_size: 2,
      team_count: 2,
      active_pick_count: 0,
      required_pick_count: 4,
      can_start: false,
      next_pick_number: status === 'drafting' ? 1 : null,
      next_team_id: status === 'drafting' ? 1 : null,
      last_pick_id: null,
      deadline: null,
    },
    teams: [null, 'Blackbirds'].map((name, index) => ({
      id: index + 1,
      league_id: id,
      name,
      color_rgb: index === 0 ? null : 0,
      metadata_complete: index !== 0,
      can_set_metadata: status === 'setup',
      manager_user_id: index === 0 ? 11 : 22,
      draft_position: index + 1,
      point_budget: 100,
      points_spent: 0,
      points_remaining: 100,
    })),
    rosters: [],
    history: [],
    actions: [],
  }
}

let renderer: ReactTestRenderer | null
let draft: ReturnType<typeof useDraft>
let favorites: ReturnType<typeof useFavorites>

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

function heading() {
  return text(renderer!.root.findByType('h1'))
}

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<App />)
    else renderer = create(<App />)
  })
}

async function navigate(label: string) {
  await act(async () => renderer!.root.findByProps({ 'aria-label': label }).props.onClick())
}

async function selectLeague(next: LeagueSnapshot) {
  await act(async () => {
    renderer!.root
      .findByProps({ 'aria-label': 'Choose league' })
      .props.onChange({ target: { value: String(next.league_id) } })
  })
  expect(draft.selectLeague).toHaveBeenLastCalledWith(next.league_id)
  draft = { ...draft, leagueId: next.league_id, snapshot: null }
  await render()
  draft = { ...draft, snapshot: next }
  await render()
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('window', {
    location: { search: '', href: 'http://localhost:5173/' },
    history: { replaceState: vi.fn() },
    scrollTo: vi.fn(),
    setInterval,
  })
  vi.stubGlobal('localStorage', {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  })
  renderer = null
  const snapshot = league()
  draft = {
    mode: 'live',
    session: {
      user: { id: 11, display_name: 'Alice' },
      csrf_token: 'csrf',
      expires_at: 2_000_000_000,
    },
    catalog: [
      {
        id: 25,
        name: 'Pikachu',
        point_cost: 12,
        dex: 25,
        species: 'Pikachu',
        form: '',
        types: null,
        abilities: null,
        hidden_ability: null,
        base_stats: null,
        image_url: null,
        megas: [],
      },
    ],
    leagues: [snapshot.league, league('drafting', 8).league, league('setup', 9).league],
    users: [
      { id: 11, display_name: 'Alice' },
      { id: 22, display_name: 'Bob' },
    ],
    leagueId: snapshot.league_id,
    snapshot,
    connection: 'connected',
    error: '',
    sessionCheckFailed: false,
    setError: vi.fn(),
    notice: '',
    setNotice: vi.fn(),
    busy: false,
    connect: vi.fn(async () => {}),
    selectLeague: vi.fn(),
    mutate: vi.fn(async () => true),
    draftAction: vi.fn(async () => true),
    refreshData: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
    createLeague: vi.fn(async () => true),
  }
  vi.mocked(useDraft).mockImplementation(() => draft)
  const list = { id: 31, owner_user_id: 11, name: 'My shortlist', revision: 8 }
  favorites = {
    lists: [list],
    selectedId: list.id,
    state: { ...list, entries: [] },
    loading: false,
    busy: false,
    error: '',
    readError: '',
    writeError: '',
    selectList: vi.fn(),
    refresh: vi.fn(async () => {}),
    clearError: vi.fn(),
    run: vi.fn(async () => true),
    create: vi.fn(async () => true),
  }
  vi.mocked(useFavorites).mockImplementation(() => favorites)
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('account catalog and favorites', () => {
  it('preserves valid browser watchlist IDs without rewriting or publishing them', async () => {
    vi.mocked(localStorage.getItem).mockReturnValue(
      JSON.stringify([25, 4, 25, '7', 0, -1, 2.5, null, true, {}, []]),
    )
    const createList = vi.spyOn(api, 'createFavoritesList').mockResolvedValue({ result: 32 })
    const addEntry = vi.spyOn(api, 'addFavoritesEntry').mockResolvedValue({ result: null })
    await render()
    await navigate('Catalog & favorites')

    expect(localStorage.getItem).toHaveBeenCalledExactlyOnceWith('draft-club:watchlist:live:11')
    expect(vi.mocked(Catalog).mock.lastCall?.[0].legacyWatchlist).toEqual([25, 4])
    await navigate('Draft room')
    expect(vi.mocked(Catalog).mock.lastCall?.[0].legacyWatchlist).toEqual([25, 4])
    expect(localStorage.setItem).not.toHaveBeenCalled()
    expect(localStorage.removeItem).not.toHaveBeenCalled()
    expect(localStorage.clear).not.toHaveBeenCalled()
    expect(favorites.create).not.toHaveBeenCalled()
    expect(favorites.run).not.toHaveBeenCalled()
    expect(draft.mutate).not.toHaveBeenCalled()
    expect(createList).not.toHaveBeenCalled()
    expect(addEntry).not.toHaveBeenCalled()
  })

  it('never exposes the previous account browser watchlist while switching accounts', async () => {
    vi.mocked(localStorage.getItem).mockImplementation((key) => {
      if (key === 'draft-club:watchlist:live:11') return '[25]'
      if (key === 'draft-club:watchlist:live:22') return '[4]'
      return null
    })
    draft = { ...draft, leagueId: null, leagues: [], snapshot: null }
    await render()
    await navigate('Catalog & favorites')
    expect(vi.mocked(Catalog).mock.lastCall?.[0].legacyWatchlist).toEqual([25])

    vi.mocked(Catalog).mockClear()
    draft = {
      ...draft,
      session: { ...draft.session!, user: { id: 22, display_name: 'Bob' } },
    }
    await render()

    expect(localStorage.getItem).toHaveBeenLastCalledWith('draft-club:watchlist:live:22')
    expect(vi.mocked(Catalog).mock.lastCall?.[0].legacyWatchlist).toEqual([4])
    for (const [props] of vi.mocked(Catalog).mock.calls) {
      expect(props.legacyWatchlist).not.toContain(25)
    }

    const connected = draft
    draft = { ...draft, mode: 'disconnected', session: null }
    await render()
    vi.mocked(Catalog).mockClear()
    draft = {
      ...connected,
      session: { ...connected.session!, user: { id: 99, display_name: 'Carol' } },
    }
    await render()
    expect(localStorage.getItem).toHaveBeenLastCalledWith('draft-club:watchlist:live:99')
    for (const [props] of vi.mocked(Catalog).mock.calls) {
      expect(props.legacyWatchlist).toEqual([])
    }
  })

  it.each(['invalid JSON', '{"25":true}', 'null'])(
    'ignores an invalid stored browser watchlist (%s)',
    async (saved) => {
      vi.mocked(localStorage.getItem).mockReturnValue(saved)
      await render()
      await navigate('Catalog & favorites')
      expect(vi.mocked(Catalog).mock.lastCall?.[0].legacyWatchlist).toEqual([])
      expect(vi.mocked(Catalog).mock.lastCall?.[0].favorites).toBe(favorites)
      expect(localStorage.setItem).not.toHaveBeenCalled()
    },
  )

  it('keeps account favorites available when browser storage is inaccessible', async () => {
    vi.mocked(localStorage.getItem).mockImplementation(() => {
      throw new Error('Storage access blocked')
    })
    await render()
    await navigate('Catalog & favorites')
    expect(vi.mocked(Catalog).mock.lastCall?.[0]).toMatchObject({
      legacyWatchlist: [],
      favorites,
    })
  })

  it('opens the catalog without a league and passes the account favorites', async () => {
    draft = { ...draft, leagueId: null, leagues: [], snapshot: null }
    await render()
    await navigate('Catalog & favorites')

    expect(heading()).toBe('Catalog & favorites')
    expect(renderer!.root.findByProps({ 'data-testid': 'catalog' })).toBeDefined()
    expect(vi.mocked(Catalog).mock.lastCall?.[0]).toMatchObject({
      catalog: draft.catalog,
      snapshot: null,
      favorites,
    })
    expect(vi.mocked(useFavorites).mock.lastCall?.[0]).toEqual({
      userId: 11,
      mutate: draft.mutate,
    })
  })

  it('shares the selected favorites controller and filter state between the draft room and catalog', async () => {
    await render()
    await navigate('Draft room')
    expect(vi.mocked(BigBoard).mock.lastCall?.[0].favorites).toBe(favorites)
    expect(vi.mocked(BigBoard).mock.lastCall?.[0].filterState).toEqual({
      search: '',
      filters: [],
      affordable: false,
    })

    await act(async () => {
      vi.mocked(BigBoard).mock.lastCall?.[0].onFilterStateChange?.({
        search: 'pika',
        filters: [{ kind: 'type', value: 'Electric', label: 'Electric type' }],
        affordable: true,
      })
    })
    await navigate('Catalog & favorites')
    expect(vi.mocked(Catalog).mock.lastCall?.[0].favorites).toBe(favorites)
    expect(vi.mocked(Catalog).mock.lastCall?.[0].snapshot).toBe(draft.snapshot)
    expect(vi.mocked(Catalog).mock.lastCall?.[0].filterState).toEqual({
      search: 'pika',
      filters: [{ kind: 'type', value: 'Electric', label: 'Electric type' }],
      affordable: true,
    })

    await selectLeague(league('drafting', 8))
    expect(vi.mocked(BigBoard).mock.lastCall?.[0].favorites).toBe(favorites)
    expect(vi.mocked(useFavorites).mock.lastCall?.[0].userId).toBe(11)
  })

  it('keeps account queries available even with no selected league', async () => {
    draft = { ...draft, leagueId: null, leagues: [], snapshot: null }
    await render()

    expect(vi.mocked(useFavorites).mock.lastCall?.[0]).toEqual({
      userId: 11,
      mutate: draft.mutate,
    })
  })

  it('clears the favorites owner when signed out and switches to the next account', async () => {
    await render()
    const connected = draft
    draft = { ...draft, mode: 'disconnected', session: null, snapshot: null }
    await render()
    expect(vi.mocked(useFavorites).mock.lastCall?.[0].userId).toBeNull()

    draft = {
      ...connected,
      session: { ...connected.session!, user: { id: 22, display_name: 'Bob' } },
    }
    await render()
    expect(vi.mocked(useFavorites).mock.lastCall?.[0].userId).toBe(22)
  })

  it.each([false, true])(
    'allows saving or removing a Pokémon from details without a league (saved: %s)',
    async (saved) => {
      draft = { ...draft, leagueId: null, leagues: [], snapshot: null }
      favorites.state!.entries = saved ? [{ draftable_id: 25, ordinal: 1 }] : []
      const save = vi.spyOn(api, 'addFavoritesEntry').mockResolvedValue({ result: null })
      const remove = vi.spyOn(api, 'removeFavoritesEntry').mockResolvedValue({ result: null })
      vi.mocked(favorites.run).mockImplementation(async (operation) => {
        await operation()
        return true
      })
      await render()
      await navigate('Catalog & favorites')
      await act(async () => vi.mocked(Catalog).mock.lastCall?.[0].onSelect(draft.catalog[0]))

      const dialog = renderer!.root.findByProps({ role: 'dialog', 'aria-label': 'Pokémon details' })
      expect(text(dialog)).toContain('Choose a league to draft this Pokémon.')
      expect(dialog.findAllByType('button').map(text)).not.toContain('Draft Pikachu')
      const toggle = dialog
        .findAllByType('button')
        .find((button) => text(button) === `${saved ? 'Remove from' : 'Save to'} My shortlist`)!
      expect(toggle.props.disabled).toBe(false)
      await act(async () => toggle.props.onClick())

      expect(saved ? remove : save).toHaveBeenCalledExactlyOnceWith({
        p_list: 31,
        p_revision: 8,
        p_draftable: 25,
      })
      expect(saved ? save : remove).not.toHaveBeenCalled()
      expect(draft.draftAction).not.toHaveBeenCalled()
    },
  )
})

describe('league navigation and team presentation', () => {
  it('returns to setup after signing back into the same account and league', async () => {
    await render()
    await navigate('Draft room')
    const connected = draft
    draft = { ...draft, mode: 'disconnected', session: null, snapshot: null }
    await render()
    draft = connected
    await render()
    expect(heading()).toBe('League settings')
  })

  it.each([11, 99])(
    'opens setup in settings for a manager with commissioner %s',
    async (commissioner) => {
      draft.snapshot!.league.commissioner_user_id = commissioner
      await render()

      expect(heading()).toBe('League settings')
      expect(renderer!.root.findByProps({ 'data-testid': 'league-setup' })).toBeDefined()
      expect(vi.mocked(LeagueSetup).mock.lastCall?.[0]).toMatchObject({
        snapshot: draft.snapshot,
        userId: 11,
      })
      expect(renderer!.root.findByProps({ 'aria-label': 'Team setup needed' })).toBeDefined()
      expect(renderer!.root.findAllByProps({ 'data-testid': 'catalog' })).toHaveLength(0)
      expect(renderer!.root.findAllByProps({ 'data-testid': 'big-board' })).toHaveLength(0)
    },
  )

  it.each(['drafting', 'completed'] as const)(
    'opens a %s league in the draft room',
    async (status) => {
      draft.snapshot = league(status)
      await render()

      expect(heading()).toBe('Draft room')
      expect(renderer!.root.findByProps({ 'data-testid': 'big-board' })).toBeDefined()
      expect(LeagueSetup).not.toHaveBeenCalled()
    },
  )

  it('preserves manual navigation through live updates, including draft start', async () => {
    await render()
    await navigate('Teams & rosters')
    expect(heading()).toBe('Teams & rosters')

    draft.snapshot = { ...league(), revision: 6, server_now: '2026-10-21T12:01:00Z' }
    await render()
    expect(heading()).toBe('Teams & rosters')

    draft.snapshot = { ...league('drafting'), revision: 7 }
    await render()
    expect(heading()).toBe('Teams & rosters')
  })

  it('chooses the appropriate initial screen after selecting another league', async () => {
    await render()
    await navigate('Teams & rosters')
    await selectLeague(league('drafting', 8))
    expect(heading()).toBe('Draft room')

    await navigate('Availability')
    await selectLeague(league('setup', 9))
    expect(heading()).toBe('League settings')
    expect(vi.mocked(LeagueSetup).mock.lastCall?.[0]).toMatchObject({
      snapshot: { league_id: 9 },
      userId: 11,
    })
  })

  it.each([true, false])(
    'starts from settings and navigates only on success (%s)',
    async (succeeded) => {
      draft.snapshot!.state.can_start = true
      draft.snapshot!.teams[0] = {
        ...draft.snapshot!.teams[0],
        name: 'Albatrosses',
        color_rgb: 0x123456,
        metadata_complete: true,
      }
      draft.snapshot!.actions = [
        {
          league_id: 7,
          revision: 5,
          actor_user_id: 11,
          kind: 'start',
          pick_number: null,
          target_pick_id: null,
        },
      ]
      vi.mocked(draft.draftAction).mockResolvedValue(succeeded)
      await render()
      await navigate('Start draft from settings')

      const dialog = renderer!.root.findByProps({ role: 'dialog', 'aria-label': 'Start draft?' })
      const start = dialog.findAllByType('button').find((node) => text(node) === 'Start draft')!
      expect(start.props.disabled).toBe(false)
      expect(draft.draftAction).not.toHaveBeenCalled()
      expect(heading()).toBe('League settings')

      await act(async () => start.props.onClick())

      expect(draft.draftAction).toHaveBeenCalledExactlyOnceWith('start')
      expect(heading()).toBe(succeeded ? 'Draft room' : 'League settings')
      expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
    },
  )

  it('renders an unnamed, uncolored team safely without metadata controls in the draft room', async () => {
    await render()
    await navigate('Draft room')
    const main = renderer!.root.findByType('main')
    const roster = main
      .findAllByType('section')
      .find((node) => node.props.className?.includes('roster-panel'))!

    expect(text(roster.findByType('h3'))).toBe('Team #1')
    expect(text(roster.findByProps({ className: 'team-avatar' }))).toBe('T#')
    expect(roster.findByProps({ className: 'team-avatar' }).props.style).toBeUndefined()
    expect(main.findAllByType('strong').map(text)).toContain('Team #1')
    expect(main.findAllByType('input')).toHaveLength(0)
    expect(main.findAllByType('button').map(text).join(' ')).not.toMatch(
      /edit team|save team|set up your team/i,
    )
    expect(main.findAllByProps({ 'data-testid': 'league-setup' })).toHaveLength(0)

    await navigate('Teams & rosters')
    const rosters = renderer!.root
      .findAllByType('section')
      .filter((node) => node.props.className?.includes('roster-panel'))
    expect(rosters.map((node) => text(node.findByType('h3')))).toEqual(['Team #1', 'Blackbirds'])
    expect(rosters[0].findByProps({ className: 'team-avatar' }).props.style).toBeUndefined()
    expect(rosters[1].findByProps({ className: 'team-avatar' }).props.style).toMatchObject({
      backgroundColor: '#000000',
      color: '#ffffff',
    })
  })

  it('places Draft board between Draft room and Catalog & favorites in the navigation and renders the snake grid', async () => {
    draft.snapshot = league('drafting')
    await render()

    const nav = renderer!.root.findByProps({ 'aria-label': 'Main navigation' })
    const labels = nav.findAllByType('button').map((btn) => btn.props['aria-label'])
    expect(labels.slice(0, 3)).toEqual(['Draft room', 'Draft board', 'Catalog & favorites'])

    await navigate('Draft board')
    expect(heading()).toBe('Draft board')
    expect(
      renderer!.root.findByProps({ 'aria-label': 'Draft picks by round and team' }),
    ).toBeDefined()
  })

  it('shows the floating corner draft tracker on non-draft views during an active draft and hides it in Draft room and Draft board', async () => {
    draft.snapshot = league('drafting')
    await render()

    // In Draft room: full TurnBanner is shown, floating tracker is hidden
    expect(heading()).toBe('Draft room')
    expect(
      renderer!.root.findAllByProps({ 'aria-label': 'Floating live draft tracker' }),
    ).toHaveLength(0)

    // In Draft board: full TurnBanner is shown, floating tracker is hidden
    await navigate('Draft board')
    expect(
      renderer!.root.findAllByProps({ 'aria-label': 'Floating live draft tracker' }),
    ).toHaveLength(0)

    // In Catalog & favorites: floating tracker appears with YOUR TURN state, minimize toggle, and Pick Now CTA
    await navigate('Catalog & favorites')
    const tracker = renderer!.root.findByProps({ 'aria-label': 'Floating live draft tracker' })
    expect(tracker.props.className).toContain('is-your-turn')
    expect(text(tracker)).toContain('YOUR TURN')
    expect(text(tracker)).toContain('Team #1 (You)')
    expect(text(tracker)).toContain('Next (1.02)')
    expect(text(tracker)).toContain('Blackbirds')
    expect(text(tracker)).toContain('Your subseq:')
    expect(text(tracker)).toContain('2.02 (#4 · in 3)')

    // Minimize and expand the floating tracker
    const minBtn = tracker.findByProps({ 'aria-label': 'Minimize live draft tracker' })
    await act(async () => minBtn.props.onClick())
    expect(tracker.props.className).toContain('is-minimized')
    expect(text(tracker)).not.toContain('Next (1.02)')

    const expBtn = tracker.findByProps({ 'aria-label': 'Expand live draft tracker' })
    await act(async () => expBtn.props.onClick())
    expect(tracker.props.className).not.toContain('is-minimized')

    // Clicking "Pick Now" returns to the Draft room
    const cta = tracker
      .findAllByType('button')
      .find((btn) => text(btn).includes('Pick Now'))!
    await act(async () => cta.props.onClick())
    expect(heading()).toBe('Draft room')
    expect(
      renderer!.root.findAllByProps({ 'aria-label': 'Floating live draft tracker' }),
    ).toHaveLength(0)
  })

  it('warns the commissioner and asks for confirmation when picking on another team’s behalf', async () => {
    const snap = league('drafting')
    snap.state.next_pick_number = 2
    snap.state.next_team_id = 2 // Blackbirds (manager_user_id: 22, Bob), while signed-in user is 11 (Alice, commissioner)
    snap.actions = [
      {
        league_id: 7,
        revision: 5,
        actor_user_id: 11,
        kind: 'pick',
        pick_number: 2,
        target_pick_id: null,
      },
    ]
    draft.snapshot = snap
    await render()

    const banner = renderer!.root.findByProps({ 'aria-label': 'On the clock status banner' })
    expect(banner.props.className).toContain('is-proxy-turn')
    expect(text(banner)).toContain('COMMISSIONER OVERRIDE — NOT YOUR TURN')
    expect(text(banner)).toContain('Blackbirds')

    // Open Pikachu pick modal
    await act(async () => vi.mocked(BigBoard).mock.lastCall?.[0].onSelect(draft.catalog[0]))

    const pickDialog = renderer!.root.findByProps({ role: 'dialog', 'aria-label': 'Pick details' })
    expect(text(pickDialog)).toContain('Commissioner override — Picking for Blackbirds')
    expect(text(pickDialog)).toContain('Bob’s (Blackbirds)')
    expect(text(pickDialog)).toContain('Blackbirds (Other manager)')

    const draftBtn = pickDialog
      .findAllByType('button')
      .find((btn) => text(btn).includes('Draft Pikachu for Blackbirds'))!
    expect(draftBtn.props.className).toContain('warning')

    // Clicking the draft button opens the "Are you sure?" confirmation modal instead of immediately submitting
    await act(async () => draftBtn.props.onClick())
    expect(draft.draftAction).not.toHaveBeenCalled()

    const confirmDialog = renderer!.root.findByProps({
      role: 'dialog',
      'aria-label': 'Are you sure?',
    })
    expect(text(confirmDialog)).toContain('You are picking on behalf of Blackbirds')
    expect(text(confirmDialog)).toContain('Pikachu')
    expect(text(confirmDialog)).toContain('12 pts')

    // Going back returns to Pick details without submitting
    const backBtn = confirmDialog
      .findAllByType('button')
      .find((btn) => text(btn).includes('Go back'))!
    await act(async () => backBtn.props.onClick())
    expect(draft.draftAction).not.toHaveBeenCalled()
    expect(renderer!.root.findByProps({ role: 'dialog', 'aria-label': 'Pick details' })).toBeDefined()

    // Open confirmation again and confirm the pick
    const draftBtnAgain = renderer!.root
      .findByProps({ role: 'dialog', 'aria-label': 'Pick details' })
      .findAllByType('button')
      .find((btn) => text(btn).includes('Draft Pikachu for Blackbirds'))!
    await act(async () => draftBtnAgain.props.onClick())

    const confirmBtn = renderer!.root
      .findByProps({ role: 'dialog', 'aria-label': 'Are you sure?' })
      .findAllByType('button')
      .find((btn) => text(btn).includes('Yes, draft for Blackbirds'))!
    await act(async () => confirmBtn.props.onClick())

    expect(draft.draftAction).toHaveBeenCalledExactlyOnceWith('pick', 25, true)
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('submits immediately without an extra confirmation popup when picking for your own team', async () => {
    const snap = league('drafting')
    snap.state.next_pick_number = 1
    snap.state.next_team_id = 1 // Team #1 (manager_user_id: 11, Alice)
    snap.actions = [
      {
        league_id: 7,
        revision: 5,
        actor_user_id: 11,
        kind: 'pick',
        pick_number: 1,
        target_pick_id: null,
      },
    ]
    draft.snapshot = snap
    await render()

    await act(async () => vi.mocked(BigBoard).mock.lastCall?.[0].onSelect(draft.catalog[0]))

    const pickDialog = renderer!.root.findByProps({ role: 'dialog', 'aria-label': 'Pick details' })
    expect(text(pickDialog)).not.toContain('Commissioner override')
    expect(text(pickDialog)).toContain('Team #1 (You)')

    const draftBtn = pickDialog
      .findAllByType('button')
      .find((btn) => text(btn) === 'Draft Pikachu')!
    expect(draftBtn.props.className).toContain('primary')

    await act(async () => draftBtn.props.onClick())
    expect(draft.draftAction).toHaveBeenCalledExactlyOnceWith('pick', 25, true)
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })
})
