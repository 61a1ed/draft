import { act, createElement } from 'react'
import { QueryClientProvider, notifyManager } from '@tanstack/react-query'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, subscribeDraft } from './api'
import { createQueryClient, QUERY_POLL_INTERVAL, queryKeys } from './queryClient'
import type { DraftEventHandlers, LeagueSnapshot, Session } from './types'
import { useDraft } from './useDraft'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    api: Object.fromEntries(Object.keys(actual.api).map((key) => [key, vi.fn()])),
    subscribeDraft: vi.fn(),
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
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

let client: ReturnType<typeof createQueryClient>
let renderer: ReactTestRenderer | null
let current: ReturnType<typeof useDraft>
let streams: { leagueId: number; handlers: DraftEventHandlers; stop: ReturnType<typeof vi.fn> }[]
function Harness() {
  current = useDraft()
  return null
}
async function mount() {
  await act(async () => {
    renderer = create(createElement(QueryClientProvider, { client }, createElement(Harness)))
  })
}
async function wake() {
  await act(async () => streams.at(-1)!.handlers.onInvalidate())
}
async function refreshMembership() {
  await act(async () => {
    await client.invalidateQueries({ queryKey: queryKeys.leagues(42) })
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  notifyManager.setScheduler(queueMicrotask)
  client = createQueryClient()
  client.setDefaultOptions({
    queries: { ...client.getDefaultOptions().queries, retry: false },
    mutations: client.getDefaultOptions().mutations,
  })
  renderer = null
  streams = []
  vi.mocked(api.session).mockResolvedValue(session)
  vi.mocked(api.draftables).mockResolvedValue([])
  vi.mocked(api.users).mockResolvedValue([session.user])
  vi.mocked(api.leagues).mockResolvedValue([room().league, room(8).league])
  vi.mocked(api.snapshot).mockImplementation(async (id) => room(id))
  vi.mocked(api.logout).mockResolvedValue(undefined)
  vi.mocked(subscribeDraft).mockImplementation((leagueId, handlers) => {
    const stop = vi.fn()
    streams.push({ leagueId, handlers, stop })
    handlers.onStatus?.('connected')
    return stop
  })
})
afterEach(async () => {
  await act(async () => renderer?.unmount())
  client.clear()
  notifyManager.setScheduler((callback) => setTimeout(callback, 0))
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('query-backed draft lifecycle', () => {
  it('loads independent queries and scopes realtime invalidation to the selected draft', async () => {
    await mount()
    const initialLists = vi.mocked(api.leagues).mock.calls.length
    const initialCatalog = vi.mocked(api.draftables).mock.calls.length
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await wake()
    expect(current.snapshot?.revision).toBe(21)
    expect(current.leagues[0].revision).toBe(21)
    expect(api.leagues).toHaveBeenCalledTimes(initialLists)
    expect(api.draftables).toHaveBeenCalledTimes(initialCatalog)
    expect(streams.map((stream) => stream.leagueId)).toEqual([7])
  })

  it('retires a read started before the draft event and ignores its late result', async () => {
    const old = deferred<LeagueSnapshot>()
    vi.mocked(api.snapshot).mockReturnValueOnce(old.promise)
    await mount()
    const signal = vi.mocked(api.snapshot).mock.calls[0][1]
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 22))
    await wake()
    expect(signal?.aborted).toBe(true)
    await act(async () => old.resolve(room(7, 99)))
    expect(current.snapshot?.revision).toBe(22)
  })

  it('never regresses snapshot revisions or equal-revision server time', async () => {
    await mount()
    const newer = { ...room(7, 22), server_now: '2026-01-01T00:00:10Z' }
    vi.mocked(api.snapshot).mockResolvedValueOnce(newer)
    await wake()
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await wake()
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 22))
    await wake()
    expect(current.snapshot).toEqual(newer)
    expect(current.leagues[0].revision).toBe(22)
  })

  it('switches the draft stream and ignores callbacks from the old league', async () => {
    await mount()
    const old = streams[0]
    await act(async () => current.selectLeague(8))
    const calls = vi.mocked(api.snapshot).mock.calls.length
    await act(async () => old.handlers.onInvalidate())
    expect(old.stop).toHaveBeenCalled()
    expect(streams.at(-1)?.leagueId).toBe(8)
    expect(current.snapshot?.league_id).toBe(8)
    expect(api.snapshot).toHaveBeenCalledTimes(calls)
  })

  it('starts with a selected league and keeps completed drafts live for undo', async () => {
    vi.mocked(api.leagues).mockResolvedValue([])
    await mount()
    expect(streams).toHaveLength(0)
    vi.mocked(api.leagues).mockResolvedValue([room().league])
    await refreshMembership()
    expect(current.notice).toContain('You now have access')
    expect(streams).toHaveLength(1)
    const completed = room(7, 21)
    completed.state.status = 'completed'
    vi.mocked(api.snapshot).mockResolvedValueOnce(completed)
    await wake()
    expect(streams[0].stop).not.toHaveBeenCalled()
    expect(current.connection).toBe('connected')
    const resumed = room(7, 22)
    resumed.state.status = 'drafting'
    vi.mocked(api.snapshot).mockResolvedValueOnce(resumed)
    await wake()
    expect(current.snapshot?.state.status).toBe('drafting')
    expect(streams).toHaveLength(1)
  })

  it('restores the stream after selecting away and back before React renders', async () => {
    await mount()
    const old = streams[0]
    await act(async () => {
      current.selectLeague(8)
      current.selectLeague(7)
    })
    expect(old.stop).toHaveBeenCalled()
    expect(streams).toHaveLength(2)
    expect(streams.at(-1)?.leagueId).toBe(7)
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await wake()
    expect(current.snapshot?.revision).toBe(21)
  })

  it('polls membership without SSE and replaces removed selections', async () => {
    vi.useFakeTimers()
    // QueryObserver enables intervals in a browser; vitest's node environment is server-like.
    await mount()
    vi.mocked(api.leagues).mockResolvedValue([room(8).league])
    await refreshMembership()
    expect(current.leagueId).toBe(8)
    expect(current.snapshot?.league_id).toBe(8)
    expect(client.getQueryCache().find({ queryKey: queryKeys.leagues(42) })?.options).toMatchObject(
      { refetchInterval: QUERY_POLL_INTERVAL },
    )
    vi.mocked(api.leagues).mockResolvedValue([])
    await refreshMembership()
    expect(current.snapshot).toBeNull()
    expect(current.leagueId).toBeNull()
    expect(streams.at(-1)?.stop).toHaveBeenCalled()
  })

  it('clears private caches immediately on logout and rejects late writes and events', async () => {
    await mount()
    client.setQueryData(queryKeys.favoritesLists(42), [{ id: 1 }])
    const write = deferred<void>()
    const leaving = deferred<void>()
    vi.mocked(api.logout).mockReturnValueOnce(leaving.promise)
    let pending!: Promise<boolean>
    let logout!: Promise<void>
    await act(async () => {
      pending = current.mutate(() => write.promise)
    })
    const old = streams[0]
    await act(async () => {
      logout = current.logout()
    })
    expect(current.session).toBeNull()
    expect(current.snapshot).toBeNull()
    expect(client.getQueryData(queryKeys.favoritesLists(42))).toBeUndefined()
    await act(async () => {
      old.handlers.onInvalidate()
      write.resolve()
      expect(await pending).toBe(false)
    })
    expect(current.busy).toBe(true)
    await act(async () => {
      leaving.resolve()
      await logout
    })
    expect(current.mode).toBe('disconnected')
    expect(current.busy).toBe(false)
  })

  it('isolates a new login from the old account and its in-flight list', async () => {
    await mount()
    const old = deferred<LeagueSnapshot['league'][]>()
    vi.mocked(api.leagues).mockReturnValueOnce(old.promise)
    await act(async () => {
      void client.invalidateQueries({ queryKey: queryKeys.leagues(42) })
    })
    vi.mocked(api.leagues).mockResolvedValue([])
    await act(async () => current.connect({ ...session, user: { id: 43, display_name: 'Other' } }))
    await act(async () => old.resolve([room().league]))
    expect(current.session?.user.id).toBe(43)
    expect(current.leagues).toEqual([])
    expect(current.snapshot).toBeNull()
    expect(client.getQueryData(queryKeys.leagues(42))).toBeUndefined()
  })

  it('rejects a pending creation and stale actions after a session refetch changes accounts', async () => {
    await mount()
    const oldMutate = current.mutate
    const creation = deferred<{ result: number }>()
    vi.mocked(api.createLeague).mockReturnValueOnce(creation.promise)
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.createLeague('Old account', 6, 60)
    })
    vi.mocked(api.session).mockResolvedValue({
      ...session,
      user: { id: 43, display_name: 'Other' },
    })
    vi.mocked(api.leagues).mockResolvedValue([])
    await act(async () => {
      await client.invalidateQueries({ queryKey: queryKeys.session })
    })
    await act(async () => {
      creation.resolve({ result: 9 })
      expect(await pending).toBe(false)
    })
    const operation = vi.fn()
    await act(async () => {
      expect(await oldMutate(operation)).toBe(false)
    })
    expect(operation).not.toHaveBeenCalled()
    expect(current.session?.user.id).toBe(43)
    expect(current.leagueId).toBeNull()
    expect(current.notice).not.toContain('Your league was created')
  })

  it('refreshes session authentication when the draft stream cannot reconnect', async () => {
    await mount()
    vi.mocked(api.session).mockRejectedValue(new ApiError(401, 'sign_in_required'))
    await act(async () => streams[0].handlers.onStatus?.('reconnecting'))
    expect(current.mode).toBe('disconnected')
    expect(current.snapshot).toBeNull()
    expect(streams[0].stop).toHaveBeenCalled()
  })

  it('ignores an old session lookup after a newer sign-in', async () => {
    const old = deferred<Session>()
    vi.mocked(api.session).mockReturnValueOnce(old.promise)
    await mount()
    await act(async () => current.connect(session))
    await act(async () => old.reject(new TypeError('offline')))
    expect(current.mode).toBe('live')
    expect(current.snapshot?.league_id).toBe(7)
  })

  it.each([401, 503])(
    'distinguishes signed out from failed session checks (%s)',
    async (status) => {
      vi.mocked(api.session).mockRejectedValue(new ApiError(status, 'sign_in_required'))
      await mount()
      expect(current.mode).toBe('disconnected')
      expect(current.sessionCheckFailed).toBe(status !== 401)
      vi.mocked(api.session).mockResolvedValue(session)
      await act(async () => current.connect())
      expect(current.mode).toBe('live')
    },
  )

  it('expires quiet sessions and catches authorization failures from feature queries', async () => {
    vi.useFakeTimers()
    vi.mocked(api.session).mockResolvedValue({ ...session, expires_at: Date.now() / 1000 + 2 })
    await mount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(current.mode).toBe('disconnected')
    expect(current.snapshot).toBeNull()
    await act(async () => current.connect(session))
    await act(async () => {
      await client
        .fetchQuery({
          queryKey: queryKeys.favoritesLists(42),
          queryFn: () => Promise.reject(new ApiError(401, 'sign_in_required')),
        })
        .catch(() => {})
    })
    expect(current.mode).toBe('disconnected')
    expect(current.error).toContain('Your session ended')
  })

  it('refreshes uncertain mutations without replaying the write', async () => {
    await mount()
    const operation = vi.fn().mockRejectedValue(new TypeError('response lost'))
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await act(async () => {
      expect(await current.mutate(operation)).toBe(false)
    })
    expect(operation).toHaveBeenCalledOnce()
    expect(current.snapshot?.revision).toBe(21)
    expect(current.error).toContain('Could not reach the server')
  })

  it('retires an initial snapshot read when a write commits before it finishes', async () => {
    const old = deferred<LeagueSnapshot>()
    vi.mocked(api.snapshot).mockReturnValueOnce(old.promise)
    await mount()
    const signal = vi.mocked(api.snapshot).mock.calls[0][1]
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await act(async () => {
      expect(await current.mutate(async () => {})).toBe(true)
    })
    expect(signal?.aborted).toBe(true)
    await act(async () => old.resolve(room(7, 99)))
    expect(current.snapshot?.revision).toBe(21)
  })

  it('rejects a stale pick closure against newer cached draft data', async () => {
    await mount()
    const staleAction = current.draftAction
    vi.mocked(api.snapshot).mockResolvedValueOnce(room(7, 21))
    await wake()
    await act(async () => {
      expect(await staleAction('pick', 25)).toBe(false)
    })
    expect(api.draftAction).not.toHaveBeenCalled()
    expect(current.error).toContain('The draft changed')
  })

  it('selects a confirmed creation and does not replay it if list refresh fails', async () => {
    await mount()
    vi.mocked(api.createLeague).mockResolvedValue({ result: 9 })
    vi.mocked(api.leagues).mockRejectedValueOnce(new TypeError('offline'))
    await act(async () => {
      expect(await current.createLeague('New', 6, 60)).toBe(true)
    })
    expect(api.createLeague).toHaveBeenCalledOnce()
    expect(current.leagueId).toBe(9)
    expect(current.notice).toContain('Your league was created')
    vi.mocked(api.leagues).mockResolvedValue([room().league, room(9).league])
    await refreshMembership()
    expect(current.snapshot?.league_id).toBe(9)
  })

  it('ignores league creation after sign-out', async () => {
    await mount()
    const created = deferred<{ result: number }>()
    vi.mocked(api.createLeague).mockReturnValueOnce(created.promise)
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.createLeague('New', 6, 60)
    })
    await act(async () => current.logout())
    await act(async () => {
      created.resolve({ result: 9 })
      expect(await pending).toBe(false)
    })
    expect(current.leagues).toEqual([])
  })

  it('resets revision authority on explicit refresh and preserves selection', async () => {
    await mount()
    await act(async () => current.selectLeague(8))
    const old = streams.at(-1)!
    vi.mocked(api.leagues).mockResolvedValue([room(7, 1).league, room(8, 1).league])
    vi.mocked(api.snapshot).mockImplementation(async (id) => room(id, 1))
    await act(async () => current.refreshData())
    expect(old.stop).toHaveBeenCalled()
    expect(current.leagueId).toBe(8)
    expect(current.snapshot?.revision).toBe(1)
    expect(api.session).toHaveBeenCalledTimes(2)
  })

  it('leaves private state closed when logout cannot be confirmed', async () => {
    await mount()
    vi.mocked(api.logout).mockRejectedValue(new TypeError('offline'))
    await act(async () => current.logout())
    expect(current.mode).toBe('disconnected')
    expect(current.error).toContain('Sign-out could not be confirmed')
    expect(current.snapshot).toBeNull()
  })
})
