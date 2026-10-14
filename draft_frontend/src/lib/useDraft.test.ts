import { act, createElement } from 'react'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, subscribeLeague } from './api'
import type { LeagueEventHandlers, LeagueSnapshot, Session } from './types'
import { useDraft } from './useDraft'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    api: Object.fromEntries(Object.keys(actual.api).map((key) => [key, vi.fn()])),
    subscribeLeague: vi.fn(),
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

const session: Session = {
  user: { id: 42, display_name: 'Live manager' },
  csrf_token: 'token',
  expires_at: 2_000_000_000,
}

function room(id = 7, revision = 20): LeagueSnapshot {
  const value: LeagueSnapshot = {
    league_id: id,
    revision,
    server_now: '2026-01-01T00:00:00Z',
    league: {
      id,
      revision,
      name: 'Test league',
      commissioner_user_id: 42,
      roster_size: 6,
      pick_seconds: 60,
      created_at: '2026-01-01T00:00:00Z',
      started_at: null,
      turn_started_at: null,
    },
    state: {
      league_id: id,
      revision,
      status: 'setup',
      roster_size: 6,
      team_count: 0,
      active_pick_count: 0,
      required_pick_count: 0,
      can_start: false,
      next_pick_number: null,
      next_team_id: null,
      last_pick_id: null,
      deadline: null,
    },
    teams: [],
    rosters: [],
    history: [],
    actions: [],
  }
  return {
    ...value,
    league_id: id,
    revision,
    league: {
      ...value.league,
      id,
      name: `League ${id}, revision ${revision}`,
      revision,
      commissioner_user_id: 42,
    },
    state: { ...value.state, league_id: id, revision },
  }
}

let renderer: ReactTestRenderer | null = null
let current: ReturnType<typeof useDraft>
let streams: { handlers: LeagueEventHandlers; stop: ReturnType<typeof vi.fn> }[]

function Harness() {
  current = useDraft()
  return null
}
async function mount() {
  await act(async () => {
    renderer = create(createElement(Harness))
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  streams = []
  vi.mocked(api.session).mockResolvedValue(session)
  vi.mocked(api.draftables).mockResolvedValue([])
  vi.mocked(api.users).mockResolvedValue([session.user])
  vi.mocked(api.leagues).mockResolvedValue([room().league, room(8).league])
  vi.mocked(api.snapshot).mockImplementation(async (id) => room(id))
  vi.mocked(api.logout).mockResolvedValue(undefined)
  vi.mocked(subscribeLeague).mockImplementation((_id, handlers) => {
    const stop = vi.fn()
    streams.push({ handlers, stop })
    return stop
  })
})

afterEach(async () => {
  await act(async () => {
    renderer?.unmount()
  })
  renderer = null
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('draft request ownership and event ordering', () => {
  it('keeps both snapshot and league metadata at the latest event revision', async () => {
    const initial = deferred<LeagueSnapshot>()
    vi.mocked(api.snapshot).mockReturnValueOnce(initial.promise)
    await mount()
    await act(async () => {
      streams[0].handlers.onSnapshot(room(7, 22))
    })
    await act(async () => {
      initial.resolve(room(7, 21))
    })

    expect(current.snapshot?.revision).toBe(22)
    expect(current.leagues.find((league) => league.id === 7)?.revision).toBe(22)
    expect(current.leagues.find((league) => league.id === 7)?.name).toBe('League 7, revision 22')
  })

  it('ignores old stream events immediately when switching leagues', async () => {
    await mount()
    const old = streams[0]
    await act(async () => {
      current.selectLeague(8)
      // These arrive before React has run the previous effect's cleanup.
      old.handlers.onSnapshot(room(7, 99))
      old.handlers.onSignedOut?.()
      old.handlers.onError?.(new Error('obsolete error'))
    })

    expect(old.stop).toHaveBeenCalled()
    expect(current.mode).toBe('live')
    expect(current.leagueId).toBe(8)
    expect(current.snapshot?.league_id).toBe(8)
    expect(current.error).toBe('')
  })

  it('restarts streaming when reconnecting the same account and league', async () => {
    await mount()
    const first = streams[0]
    await act(async () => {
      await current.connect(session)
    })

    expect(first.stop).toHaveBeenCalled()
    expect(streams).toHaveLength(2)
    expect(current.snapshot?.league_id).toBe(7)
    expect(current.mode).toBe('live')
  })

  it('does not let an old failed automatic connection replace a newer successful sign-in', async () => {
    const oldSession = deferred<Session>()
    vi.mocked(api.session).mockReturnValueOnce(oldSession.promise)
    await mount()
    await act(async () => {
      await current.connect(session)
    })
    await act(async () => {
      oldSession.reject(new TypeError('old request failed'))
    })

    expect(current.mode).toBe('live')
    expect(current.session?.user.id).toBe(42)
    expect(current.snapshot?.league_id).toBe(7)
    expect(current.error).toBe('')
  })

  it('returns connection failures to the sign-in UI and clears private state', async () => {
    await mount()
    vi.mocked(api.draftables).mockRejectedValueOnce(new TypeError('backend offline'))
    await act(async () => {
      await expect(current.connect(session)).rejects.toThrow('backend offline')
    })

    expect(current.mode).toBe('disconnected')
    expect(current.snapshot).toBeNull()
    expect(current.busy).toBe(false)
  })

  it('shows backend failure without fictional data', async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new TypeError('backend offline'))
    await mount()

    expect(current.mode).toBe('disconnected')
    expect(current.session).toBeNull()
    expect(current.snapshot).toBeNull()
    expect(current.catalog).toEqual([])
    expect(current.leagues).toEqual([])
    expect(current.error).toContain('Could not reach the server')
    expect(current.busy).toBe(false)
  })

  it('stays signed out after logout or a missing session', async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new ApiError(401, 'sign_in_required'))
    await mount()
    expect(current.mode).toBe('disconnected')
    expect(current.error).toBe('')

    await act(async () => {
      await current.connect(session)
    })
    expect(current.mode).toBe('live')
    await act(async () => {
      await current.logout()
    })
    expect(current.mode).toBe('disconnected')
    expect(current.session).toBeNull()
    expect(current.snapshot).toBeNull()
  })

  it('refreshes all SQL data and preserves the selected league with fresh revision authority', async () => {
    await mount()
    await act(async () => {
      current.selectLeague(8)
    })
    const old = streams[streams.length - 1]
    vi.mocked(api.leagues).mockResolvedValueOnce([
      room(7).league,
      room(8, 1).league,
      room(9).league,
    ])
    vi.mocked(api.users).mockResolvedValueOnce([{ id: 99, display_name: 'New fixture manager' }])
    vi.mocked(api.draftables).mockResolvedValueOnce([
      {
        id: 1,
        name: 'Test Pokemon',
        point_cost: 5,
        dex: 1,
        species: 'Test',
        form: '',
        image_url: '/images/test.png',
      },
    ])
    vi.mocked(api.snapshot).mockImplementation(async (id) => room(id, 1))
    await act(async () => {
      await current.refreshData()
      old.handlers.onSnapshot(room(8, 999))
    })

    expect(old.stop).toHaveBeenCalled()
    expect(current.leagueId).toBe(8)
    expect(current.leagues.map((league) => league.id)).toEqual([7, 8, 9])
    expect(current.users).toEqual([{ id: 99, display_name: 'New fixture manager' }])
    expect(current.catalog).toHaveLength(1)
    expect(current.snapshot?.revision).toBe(1)
    expect(api.session).toHaveBeenCalledTimes(2)
    expect(current.busy).toBe(false)
  })

  it('selects a replacement fixture after SQL removes the selected league', async () => {
    await mount()
    vi.mocked(api.leagues).mockResolvedValueOnce([room(10, 0).league])
    await act(async () => {
      await current.refreshData()
    })

    expect(current.leagueId).toBe(10)
    expect(current.snapshot?.league_id).toBe(10)
    expect(current.leagues.map((league) => league.id)).toEqual([10])
  })

  it('drops private state on logout and ignores pending mutations and old stream callbacks', async () => {
    await mount()
    const mutation = deferred<void>()
    const logout = deferred<void>()
    vi.mocked(api.logout).mockReturnValueOnce(logout.promise)
    let writing!: Promise<void>
    let leaving!: Promise<void>
    await act(async () => {
      writing = current.mutate(() => mutation.promise)
    })
    const old = streams[0]
    await act(async () => {
      leaving = current.logout()
    })

    expect(old.stop).toHaveBeenCalled()
    expect(current.session).toBeNull()
    expect(current.snapshot).toBeNull()
    expect(current.leagues).toEqual([])
    await act(async () => {
      old.handlers.onSnapshot(room(7, 99))
      mutation.resolve()
      await writing
    })
    expect(current.snapshot).toBeNull()
    expect(current.busy).toBe(true)
    await act(async () => {
      logout.resolve()
      await leaving
    })
    expect(current.mode).toBe('disconnected')
    expect(current.busy).toBe(false)
  })

  it('treats a confirmed creation as successful when only the list refresh fails', async () => {
    await mount()
    vi.mocked(api.createLeague).mockResolvedValueOnce({ result: 9 })
    vi.mocked(api.leagues).mockRejectedValueOnce(new TypeError('list unavailable'))
    await act(async () => {
      expect(await current.createLeague('New room', 6, 60)).toBe(true)
    })

    expect(api.createLeague).toHaveBeenCalledOnce()
    expect(current.leagueId).toBe(9)
    expect(current.snapshot?.league_id).toBe(9)
    expect(current.leagues.some((league) => league.id === 9)).toBe(true)
    expect(current.error).toContain('Your league was created.')
  })

  it('never applies a created league after the user signs out', async () => {
    await mount()
    const created = deferred<{ result: number }>()
    vi.mocked(api.createLeague).mockReturnValueOnce(created.promise)
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.createLeague('New room', 6, 60)
    })
    await act(async () => {
      await current.logout()
    })
    await act(async () => {
      created.resolve({ result: 9 })
      expect(await pending).toBe(false)
    })

    expect(current.mode).toBe('disconnected')
    expect(current.leagues).toEqual([])
    expect(current.busy).toBe(false)
  })

  it('keeps a newer mutation busy when an obsolete mutation finishes', async () => {
    await mount()
    const first = deferred<void>()
    const second = deferred<void>()
    let oldWrite!: Promise<void>
    let newWrite!: Promise<void>
    await act(async () => {
      oldWrite = current.mutate(() => first.promise)
    })
    await act(async () => {
      current.selectLeague(8)
    })
    await act(async () => {
      newWrite = current.mutate(() => second.promise)
    })
    await act(async () => {
      first.resolve()
      await oldWrite
    })

    expect(current.busy).toBe(true)
    expect(current.snapshot?.league_id).toBe(8)
    await act(async () => {
      second.resolve()
      await newWrite
    })
    expect(current.busy).toBe(false)
  })

  it('leaves private state closed and makes an unconfirmed logout explicit', async () => {
    await mount()
    vi.mocked(api.logout).mockRejectedValueOnce(new TypeError('network failed'))
    await act(async () => {
      await current.logout()
    })

    expect(current.mode).toBe('disconnected')
    expect(current.session?.user.id).not.toBe(42)
    expect(current.connection).toBe('closed')
    expect(current.error).toContain('Sign-out could not be confirmed')
    expect(current.error).toContain('browser session may still be active')
  })
})
