import { act, createElement } from 'react'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, subscribeAccount } from './api'
import type { AccountEventHandlers, LeagueSnapshot, Session } from './types'
import { useDraft } from './useDraft'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    api: Object.fromEntries(Object.keys(actual.api).map((key) => [key, vi.fn()])),
    subscribeAccount: vi.fn(),
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
let streams: { handlers: AccountEventHandlers; stop: ReturnType<typeof vi.fn> }[]

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
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  streams = []
  vi.mocked(api.session).mockResolvedValue(session)
  vi.mocked(api.draftables).mockResolvedValue([])
  vi.mocked(api.users).mockResolvedValue([session.user])
  vi.mocked(api.leagues).mockResolvedValue([room().league, room(8).league])
  vi.mocked(api.snapshot).mockImplementation(async (id) => room(id))
  vi.mocked(api.logout).mockResolvedValue(undefined)
  vi.mocked(subscribeAccount).mockImplementation((handlers) => {
    const stop = vi.fn()
    streams.push({ handlers, stop })
    handlers.onInvalidate()
    return stop
  })
})

afterEach(async () => {
  await act(async () => {
    renderer?.unmount()
  })
  renderer = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('draft request ownership and event ordering', () => {
  it('invalidates tournament reads even when the draft revision stays the same', async () => {
    await mount()
    const previous = current.tournamentRefreshKey
    const revision = current.snapshot?.revision
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.snapshot?.revision).toBe(revision)
    expect(current.tournamentRefreshKey).toBeGreaterThan(previous)
    expect(streams).toHaveLength(1)
  })

  it('keeps both snapshot and league metadata at the latest event revision', async () => {
    const initial = deferred<LeagueSnapshot>()
    vi.mocked(api.snapshot).mockReturnValueOnce(initial.promise)
    await mount()
    await act(async () => {
      vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 22))
      streams[0].handlers.onInvalidate()
    })
    await act(async () => {
      initial.resolve(room(7, 21))
    })

    expect(current.snapshot?.revision).toBe(22)
    expect(current.leagues.find((league) => league.id === 7)?.revision).toBe(22)
    expect(current.leagues.find((league) => league.id === 7)?.name).toBe('League 7, revision 22')
  })

  it('keeps the account stream when switching leagues and refreshes the new selection', async () => {
    await mount()
    const old = streams[0]
    await act(async () => {
      current.selectLeague(8)
      // The same stream remains authoritative across selection changes.
      old.handlers.onInvalidate()
    })

    expect(old.stop).not.toHaveBeenCalled()
    expect(streams).toHaveLength(1)
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
    expect(current.sessionCheckFailed).toBe(false)
  })

  it('keeps an authenticated stream and retries failed initial reads', async () => {
    vi.useFakeTimers()
    await mount()
    vi.mocked(api.draftables).mockRejectedValueOnce(new TypeError('backend offline'))
    await act(async () => {
      await current.connect(session)
    })

    expect(current.mode).toBe('live')
    expect(current.session).toEqual(session)
    expect(current.snapshot).toBeNull()
    expect(current.busy).toBe(false)
    expect(streams.at(-1)!.stop).not.toHaveBeenCalled()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(current.snapshot?.league_id).toBe(7)
    expect(current.error).toBe('')
  })

  it.each([
    [new TypeError('backend offline'), 'Could not reach the server'],
    [new ApiError(503, 'database_unavailable'), 'The database is unavailable'],
  ])('marks failed session checks for retry without fictional data: %s', async (cause, message) => {
    vi.mocked(api.session).mockRejectedValueOnce(cause)
    await mount()

    expect(current.mode).toBe('disconnected')
    expect(current.session).toBeNull()
    expect(current.snapshot).toBeNull()
    expect(current.catalog).toEqual([])
    expect(current.leagues).toEqual([])
    expect(current.error).toContain(message)
    expect(current.sessionCheckFailed).toBe(true)
    expect(current.busy).toBe(false)
  })

  it('clears the failed session check while retrying and restores the signed-in account', async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new TypeError('backend offline'))
    await mount()
    const retry = deferred<Session>()
    vi.mocked(api.session).mockReturnValueOnce(retry.promise)
    let connecting!: Promise<void>
    await act(async () => {
      connecting = current.connect()
    })

    expect(current.mode).toBe('loading')
    expect(current.sessionCheckFailed).toBe(false)
    await act(async () => {
      retry.resolve(session)
      await connecting
    })
    expect(current.mode).toBe('live')
    expect(current.session).toEqual(session)
    expect(current.sessionCheckFailed).toBe(false)
    expect(current.error).toBe('')
  })

  it('returns to ordinary signed-out state when a failed session check retries with no session', async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new TypeError('backend offline'))
    await mount()
    const signedOut = new ApiError(401, 'sign_in_required')
    vi.mocked(api.session).mockRejectedValueOnce(signedOut)
    await act(async () => {
      await expect(current.connect()).rejects.toBe(signedOut)
    })

    expect(current.mode).toBe('disconnected')
    expect(current.sessionCheckFailed).toBe(false)
    expect(current.error).toBe('')
  })

  it('stays signed out after logout or a missing session', async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new ApiError(401, 'sign_in_required'))
    await mount()
    expect(current.mode).toBe('disconnected')
    expect(current.error).toBe('')
    expect(current.sessionCheckFailed).toBe(false)

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
    expect(current.sessionCheckFailed).toBe(false)
  })

  it('refreshes all SQL data and preserves the selected league with fresh revision authority', async () => {
    await mount()
    await act(async () => {
      current.selectLeague(8)
    })
    const old = streams[streams.length - 1]
    vi.mocked(api.leagues).mockResolvedValue([room(7).league, room(8, 1).league, room(9).league])
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
      old.handlers.onInvalidate()
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
    vi.mocked(api.leagues).mockResolvedValue([room(10, 0).league])
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
    let writing!: Promise<boolean>
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
      old.handlers.onInvalidate()
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
    vi.useFakeTimers()
    await mount()
    vi.mocked(api.createLeague).mockResolvedValueOnce({ result: 9 })
    vi.mocked(api.leagues).mockRejectedValueOnce(new TypeError('list unavailable'))
    await act(async () => {
      expect(await current.createLeague('New room', 6, 60)).toBe(true)
    })

    expect(api.createLeague).toHaveBeenCalledOnce()
    expect(current.leagueId).toBe(9)
    expect(current.snapshot).toBeNull()
    expect(current.notice).toContain('Your league was created.')
    expect(current.error).toContain('Could not reach the server')
    vi.mocked(api.leagues).mockResolvedValue([room().league, room(9).league])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(current.snapshot?.league_id).toBe(9)
    expect(current.leagues.some((league) => league.id === 9)).toBe(true)
    expect(api.createLeague).toHaveBeenCalledOnce()
    expect(current.error).toBe('')
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
    let oldWrite!: Promise<boolean>
    let newWrite!: Promise<boolean>
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
    expect(current.sessionCheckFailed).toBe(false)
  })
})

describe('snapshot refresh failures', () => {
  it('retries a failed read without another event and accepts equal-revision newer time', async () => {
    vi.useFakeTimers()
    await mount()
    vi.mocked(api.snapshot).mockRejectedValueOnce(new TypeError('offline'))
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    const next = { ...room(), server_now: '2026-01-01T00:00:10Z' }
    vi.mocked(api.snapshot).mockResolvedValueOnce(next)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(current.snapshot?.server_now).toBe(next.server_now)
    expect(current.error).toBe('')
  })

  it.each([401, 404])('reacts to HTTP %s instead of a proactive stream message', async (status) => {
    await mount()
    vi.mocked(api.snapshot).mockRejectedValueOnce(new ApiError(status, 'denied'))
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.snapshot).toBeNull()
    if (status === 401) expect(streams[0].stop).toHaveBeenCalled()
    else expect(streams[0].stop).not.toHaveBeenCalled()
    expect(current.mode).toBe(status === 401 ? 'disconnected' : 'live')
    expect(current.sessionCheckFailed).toBe(false)
  })

  it('cancels pending read retries when changing leagues', async () => {
    vi.useFakeTimers()
    await mount()
    vi.mocked(api.snapshot).mockRejectedValueOnce(new TypeError('offline'))
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    await act(async () => {
      current.selectLeague(8)
    })
    const calls = vi.mocked(api.snapshot).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(api.snapshot).toHaveBeenCalledTimes(calls)
    expect(current.snapshot?.league_id).toBe(8)
  })
})

describe('account membership discovery and recovery', () => {
  it('discovers the first league from an empty account and follows draft turns', async () => {
    vi.mocked(api.leagues).mockResolvedValue([])
    await mount()
    expect(current.leagues).toEqual([])
    expect(current.leagueId).toBeNull()
    expect(streams).toHaveLength(1)
    expect(api.snapshot).not.toHaveBeenCalled()

    vi.mocked(api.leagues).mockResolvedValue([room().league])
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.leagueId).toBe(7)
    expect(current.snapshot?.league_id).toBe(7)
    expect(current.notice).toContain('You now have access to')
    expect(streams).toHaveLength(1)

    const started = room(7, 21)
    started.state = { ...started.state, status: 'drafting', next_pick_number: 1, next_team_id: 12 }
    vi.mocked(api.snapshot).mockResolvedValueOnce(started)
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.snapshot?.state.next_team_id).toBe(12)
    const picked = room(7, 22)
    picked.state = { ...started.state, revision: 22, next_pick_number: 2, next_team_id: 14 }
    vi.mocked(api.snapshot).mockResolvedValueOnce(picked)
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.snapshot?.state.next_pick_number).toBe(2)
    expect(current.snapshot?.state.next_team_id).toBe(14)
  })

  it('notifies about added access without replacing an accessible selection', async () => {
    await mount()
    await act(async () => {
      await current.selectLeague(8)
    })
    vi.mocked(api.leagues).mockResolvedValue([room().league, room(8).league, room(9).league])
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.leagueId).toBe(8)
    expect(current.notice).toContain(room(9).league.name)
    expect(streams).toHaveLength(1)
  })

  it('selects remaining access, clears removed private state, and discovers reassignment', async () => {
    await mount()
    vi.mocked(api.leagues).mockResolvedValue([room(8).league])
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.leagueId).toBe(8)
    expect(current.snapshot?.league_id).toBe(8)
    vi.mocked(api.leagues).mockResolvedValue([])
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.leagueId).toBeNull()
    expect(current.snapshot).toBeNull()
    expect(current.leagues).toEqual([])
    expect(streams[0].stop).not.toHaveBeenCalled()
    vi.mocked(api.leagues).mockResolvedValue([room(10).league])
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    expect(current.leagueId).toBe(10)
    expect(current.snapshot?.league_id).toBe(10)
    expect(streams).toHaveLength(1)
  })

  it('coalesces a burst during reads into one extra list and snapshot pass', async () => {
    await mount()
    const pendingList = deferred<ReturnType<typeof room>['league'][]>()
    vi.mocked(api.leagues).mockReturnValueOnce(pendingList.promise)
    const listCalls = vi.mocked(api.leagues).mock.calls.length
    const snapshotCalls = vi.mocked(api.snapshot).mock.calls.length
    await act(async () => {
      streams[0].handlers.onInvalidate()
      streams[0].handlers.onInvalidate()
      streams[0].handlers.onInvalidate()
    })
    expect(api.leagues).toHaveBeenCalledTimes(listCalls + 1)
    expect(api.snapshot).toHaveBeenCalledTimes(snapshotCalls)
    await act(async () => {
      pendingList.resolve([room().league])
    })
    expect(api.leagues).toHaveBeenCalledTimes(listCalls + 2)
    expect(api.snapshot).toHaveBeenCalledTimes(snapshotCalls + 2)
  })

  it('does not let a list begun before creation overwrite the newly selected league', async () => {
    await mount()
    const oldList = deferred<ReturnType<typeof room>['league'][]>()
    vi.mocked(api.leagues).mockReturnValueOnce(oldList.promise)
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    vi.mocked(api.createLeague).mockResolvedValueOnce({ result: 9 })
    vi.mocked(api.leagues).mockResolvedValue([room().league, room(9).league])
    let creating!: Promise<boolean>
    await act(async () => {
      creating = current.createLeague('New league', 6, 60)
    })
    expect(current.leagueId).toBe(9)
    await act(async () => {
      oldList.resolve([room().league])
      expect(await creating).toBe(true)
    })
    expect(current.leagueId).toBe(9)
    expect(current.snapshot?.league_id).toBe(9)
    expect(streams).toHaveLength(1)
  })

  it('rejects old snapshots even after selecting away and back to the same league', async () => {
    await mount()
    const oldSnapshot = deferred<LeagueSnapshot>()
    const freshSnapshot = deferred<LeagueSnapshot>()
    vi.mocked(api.snapshot)
      .mockReturnValueOnce(oldSnapshot.promise)
      .mockReturnValueOnce(freshSnapshot.promise)
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    await act(async () => {
      void current.selectLeague(8)
      void current.selectLeague(7)
    })
    await act(async () => {
      oldSnapshot.resolve(room(7, 99))
    })
    expect(current.snapshot).toBeNull()
    await act(async () => {
      freshSnapshot.resolve(room(7, 21))
    })
    expect(current.snapshot?.revision).toBe(21)
    expect(streams).toHaveLength(1)
  })

  it('ignores an old account list after switching accounts', async () => {
    await mount()
    const oldList = deferred<ReturnType<typeof room>['league'][]>()
    vi.mocked(api.leagues).mockReturnValueOnce(oldList.promise)
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    vi.mocked(api.leagues).mockResolvedValue([])
    const other = { ...session, user: { id: 43, display_name: 'Other manager' } }
    await act(async () => {
      await current.connect(other)
    })
    await act(async () => {
      oldList.resolve([room().league])
    })
    expect(current.session?.user.id).toBe(43)
    expect(current.leagues).toEqual([])
    expect(current.snapshot).toBeNull()
    expect(streams[0].stop).toHaveBeenCalled()
    expect(streams[1].stop).not.toHaveBeenCalled()
  })

  it('recovers a missed membership change after a failed list read without another event', async () => {
    vi.useFakeTimers()
    vi.mocked(api.leagues).mockResolvedValue([])
    await mount()
    vi.mocked(api.leagues).mockRejectedValueOnce(new ApiError(503, 'database_unavailable'))
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    vi.mocked(api.leagues).mockResolvedValue([room().league])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(current.leagueId).toBe(7)
    expect(current.snapshot?.league_id).toBe(7)
    expect(current.notice).toContain('You now have access')
    expect(current.error).toBe('')
  })

  it('expires a quiet offline session and cancels pending refresh retries', async () => {
    vi.useFakeTimers()
    vi.mocked(api.session).mockResolvedValue({ ...session, expires_at: Date.now() / 1000 + 2 })
    await mount()
    vi.mocked(api.leagues).mockRejectedValue(new TypeError('offline'))
    await act(async () => {
      streams[0].handlers.onInvalidate()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(current.mode).toBe('disconnected')
    expect(current.snapshot).toBeNull()
    expect(current.leagues).toEqual([])
    expect(current.sessionCheckFailed).toBe(false)
    expect(streams[0].stop).toHaveBeenCalled()
    const reads = vi.mocked(api.leagues).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(api.leagues).toHaveBeenCalledTimes(reads)
  })
})
