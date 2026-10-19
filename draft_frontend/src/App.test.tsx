import { act, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { LeagueSetup } from './components/LeagueSetup'
import type { DraftStatus, LeagueSnapshot } from './lib/types'
import { useDraft } from './lib/useDraft'

vi.mock('./lib/useDraft', () => ({ useDraft: vi.fn() }))
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
  Catalog: () => <section data-testid="catalog">Available Pokémon</section>,
  PokemonImage: () => <span>Pokémon image</span>,
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
  vi.stubGlobal('localStorage', { getItem: vi.fn(() => null), setItem: vi.fn() })
  renderer = null
  const snapshot = league()
  draft = {
    mode: 'live',
    session: {
      user: { id: 11, display_name: 'Alice' },
      csrf_token: 'csrf',
      expires_at: 2_000_000_000,
    },
    catalog: [],
    leagues: [snapshot.league, league('drafting', 8).league, league('setup', 9).league],
    users: [
      { id: 11, display_name: 'Alice' },
      { id: 22, display_name: 'Bob' },
    ],
    leagueId: snapshot.league_id,
    snapshot,
    connection: 'connected',
    tournamentRefreshKey: 0,
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
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
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
    },
  )

  it.each(['drafting', 'completed'] as const)(
    'opens a %s league in the draft room',
    async (status) => {
      draft.snapshot = league(status)
      await render()

      expect(heading()).toBe('Draft room')
      expect(renderer!.root.findByProps({ 'data-testid': 'catalog' })).toBeDefined()
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
})
