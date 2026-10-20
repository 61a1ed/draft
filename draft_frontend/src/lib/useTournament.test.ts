import { act, createElement } from 'react'
import {
  QueryClientProvider,
  focusManager,
  notifyManager,
  onlineManager,
} from '@tanstack/react-query'
import { createQueryClient, queryKeys, QUERY_POLL_INTERVAL } from './queryClient'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import type { MatchDetail, TournamentSnapshot } from './tournamentTypes'
import { useTournament, useTournamentMatch, type UseTournamentOptions } from './useTournament'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, api: { ...actual.api, tournament: vi.fn(), match: vi.fn() } }
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

function match(id = 12, revision = 0): MatchDetail {
  return {
    id,
    league_id: 7,
    stage_id: 1,
    round_id: 2,
    team_a_id: 3,
    team_b_id: 4,
    revision,
    vod_url: null,
    team_a_submission_id: null,
    team_b_submission_id: null,
    current_decision_id: null,
    created_at: '2026-10-17T00:00:00Z',
    official_result: null,
    disposition: null,
    submissions: [],
    decisions: [],
  }
}

function tournament(id = 7, revision = 0, matchRevision = 0): TournamentSnapshot {
  return {
    league_id: id,
    revision,
    published_at: '2026-10-17T00:00:00Z',
    published_by: 1,
    stages: [],
    entries: [],
    rounds: [],
    matches: [match(12, matchRevision)],
    current_submissions: [],
    official_results: [],
    stage_records: [],
    round_records: [],
    withdrawals: [],
    dispositions: [],
  }
}

let client: ReturnType<typeof createQueryClient>
let renderer: ReactTestRenderer | null
let current: ReturnType<typeof useTournament>
let detail: ReturnType<typeof useTournamentMatch>
let props: UseTournamentOptions
function Harness(options: UseTournamentOptions) {
  current = useTournament(options)
  return null
}
function MatchHarness(options: { matchId: number; identityKey?: string | number }) {
  detail = useTournamentMatch(options)
  return null
}
async function mount() {
  await act(async () => {
    renderer = create(createElement(QueryClientProvider, { client }, createElement(Harness, props)))
  })
}
async function update(changes: Partial<UseTournamentOptions>) {
  props = { ...props, ...changes }
  await act(async () => {
    renderer!.update(createElement(QueryClientProvider, { client }, createElement(Harness, props)))
  })
}
async function invalidate() {
  const queryKey = queryKeys.tournament(props.identityKey, props.leagueId)
  await act(async () => {
    await client.cancelQueries({ queryKey, exact: true })
    void client.invalidateQueries({ queryKey, exact: true })
  })
}

beforeEach(() => {
  client = createQueryClient()
  notifyManager.setScheduler(queueMicrotask)
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  props = {
    leagueId: 7,
    identityKey: 'account-a',
    mutate: vi.fn(async (operation) => {
      try {
        await operation()
        return true
      } catch {
        return false
      }
    }),
  }
  vi.mocked(api.tournament).mockImplementation(async (id) => tournament(id))
  vi.mocked(api.match).mockImplementation(async (id) => match(id))
})
afterEach(async () => {
  await act(async () => renderer?.unmount())
  client.clear()
  notifyManager.setScheduler((callback) => setTimeout(callback, 0))
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('tournament reads and mutation ownership', () => {
  it('cancels an older request when a fresh authoritative read is required', async () => {
    const initial = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament)
      .mockReturnValueOnce(initial.promise)
      .mockResolvedValueOnce(tournament(7, 2))
    await mount()
    const signal = vi.mocked(api.tournament).mock.calls[0][1]
    await invalidate()
    await invalidate()
    expect(signal?.aborted).toBe(true)
    expect(api.tournament).toHaveBeenCalledTimes(3)
    await act(async () => initial.resolve(tournament(7, 1)))
    expect(api.tournament).toHaveBeenCalledTimes(3)
    expect(current.state?.revision).toBe(2)
    expect(current.loading).toBe(false)
  })

  it('shares one cached request between views of the same tournament', async () => {
    const initial = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament).mockReturnValueOnce(initial.promise)
    await act(async () => {
      renderer = create(
        createElement(QueryClientProvider, { client }, [
          createElement(Harness, { ...props, key: 'first' }),
          createElement(Harness, { ...props, key: 'second' }),
        ]),
      )
    })
    expect(api.tournament).toHaveBeenCalledTimes(1)
    await act(async () => initial.resolve(tournament(7, 3)))
    expect(current.state?.revision).toBe(3)
    expect(client.getQueryData(queryKeys.tournament('account-a', 7))).toEqual(tournament(7, 3))
  })

  it('refreshes stale state on focus and reconnect, with polling enabled', async () => {
    vi.useFakeTimers()
    await mount()
    expect(
      client.getQueryCache().find({ queryKey: queryKeys.tournament('account-a', 7) })?.options,
    ).toMatchObject({ refetchInterval: QUERY_POLL_INTERVAL })
    await act(async () => {
      focusManager.setFocused(false)
      await vi.advanceTimersByTimeAsync(16_000)
      focusManager.setFocused(true)
    })
    expect(api.tournament).toHaveBeenCalledTimes(2)
    await act(async () => {
      onlineManager.setOnline(false)
      await vi.advanceTimersByTimeAsync(16_000)
      onlineManager.setOnline(true)
    })
    expect(api.tournament).toHaveBeenCalledTimes(3)
  })

  it('refreshes related match details after a tournament mutation without touching other accounts', async () => {
    const key = queryKeys.match('account-a', 12)
    const otherAccount = queryKeys.match('account-b', 12)
    client.setQueryData(key, match(12, 0))
    client.setQueryData(otherAccount, match(12, 0))
    await act(async () => {
      renderer = create(
        createElement(QueryClientProvider, { client }, [
          createElement(Harness, { ...props, key: 'tournament' }),
          createElement(MatchHarness, { matchId: 12, identityKey: 'account-a', key: 'match' }),
        ]),
      )
    })
    vi.mocked(api.match).mockResolvedValueOnce(match(12, 2))
    await act(async () => {
      await current.run(async () => null)
    })
    expect(api.match).toHaveBeenCalledTimes(1)
    expect(detail.match?.revision).toBe(2)
    expect(client.getQueryState(key)?.isInvalidated).toBe(false)
    expect(client.getQueryState(otherAccount)?.isInvalidated).toBe(false)
  })

  it('accepts VOD-only changes and rejects tournament or match revision rollback', async () => {
    vi.mocked(api.tournament).mockResolvedValueOnce(tournament(7, 4, 2))
    await mount()
    const changed = tournament(7, 4, 3)
    changed.matches[0].vod_url = 'https://example.com/replay'
    vi.mocked(api.tournament).mockResolvedValueOnce(changed)
    await invalidate()
    expect(current.state?.matches[0].vod_url).toBe('https://example.com/replay')
    vi.mocked(api.tournament).mockResolvedValueOnce(tournament(7, 3, 4))
    await invalidate()
    vi.mocked(api.tournament).mockResolvedValueOnce(tournament(7, 5, 2))
    await invalidate()
    expect(current.state).toEqual(changed)
  })

  it('distinguishes unpublished tournaments from failed reads', async () => {
    vi.mocked(api.tournament).mockRejectedValueOnce(new ApiError(404, 'tournament_not_found'))
    await mount()
    expect(current.state).toBeNull()
    expect(current.error).toBe('')
    expect(current.loading).toBe(false)
    vi.mocked(api.tournament).mockRejectedValueOnce(new ApiError(503, 'database_unavailable'))
    await invalidate()
    expect(current.error).toContain('database is unavailable')
    vi.mocked(api.tournament).mockRejectedValueOnce(new ApiError(404, 'league_not_found'))
    await invalidate()
    expect(current.error).toContain('league is unavailable')
  })

  it('retains published data and displays an error after a failed refresh', async () => {
    await mount()
    vi.mocked(api.tournament).mockRejectedValueOnce(new ApiError(404, 'tournament_not_found'))
    await invalidate()
    expect(current.state?.league_id).toBe(7)
    expect(current.error).not.toBe('')
  })

  it('retries transient read failures with backoff and recovers without another account event', async () => {
    vi.useFakeTimers()
    vi.mocked(api.tournament)
      .mockRejectedValueOnce(new ApiError(503, 'database_unavailable'))
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockResolvedValueOnce(tournament(7, 4))
    await mount()
    expect(current.readError).not.toBe('')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(api.tournament).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999)
    })
    expect(api.tournament).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(current.state?.revision).toBe(4)
    expect(current.readError).toBe('')
  })

  it('cancels retry timers when the selected league changes', async () => {
    vi.useFakeTimers()
    vi.mocked(api.tournament).mockRejectedValueOnce(new TypeError('network unavailable'))
    await mount()
    await update({ leagueId: 8 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api.tournament).toHaveBeenCalledTimes(2)
    expect(current.state?.league_id).toBe(8)
  })

  it('cancels a retry when a queued invalidation already recovers the failed read', async () => {
    vi.useFakeTimers()
    const pending = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament).mockReturnValueOnce(pending.promise)
    await mount()
    await invalidate()
    await act(async () => pending.reject(new TypeError('network interrupted')))
    expect(current.state?.league_id).toBe(7)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api.tournament).toHaveBeenCalledTimes(2)
  })

  it('aborts an old league read and ignores its late response', async () => {
    const old = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament).mockReturnValueOnce(old.promise)
    await mount()
    const signal = vi.mocked(api.tournament).mock.calls[0][1]
    await update({ leagueId: 8 })
    expect(signal?.aborted).toBe(true)
    await act(async () => old.resolve(tournament(7, 90)))
    expect(current.state?.league_id).toBe(8)
  })

  it('isolates reads across account changes even when returning to the same account', async () => {
    const old = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament).mockReturnValueOnce(old.promise)
    await mount()
    await update({ identityKey: 'account-b' })
    await update({ identityKey: 'account-a' })
    await act(async () => old.resolve(tournament(7, 99)))
    expect(current.state?.revision).toBe(0)
  })

  it('aborts reads on unmount without allowing a late state update', async () => {
    const pending = deferred<TournamentSnapshot>()
    vi.mocked(api.tournament).mockReturnValueOnce(pending.promise)
    await mount()
    const signal = vi.mocked(api.tournament).mock.calls[0][1]
    await act(async () => {
      renderer!.unmount()
      renderer = null
    })
    expect(signal?.aborted).toBe(true)
    await act(async () => pending.resolve(tournament(7, 99)))
    expect(api.tournament).toHaveBeenCalledTimes(1)
  })

  it('refreshes after a successful mutation through the shared mutation lock', async () => {
    await mount()
    vi.mocked(api.tournament).mockResolvedValueOnce(tournament(7, 1))
    const operation = vi.fn().mockResolvedValue({ result: 12 })
    let success = false
    await act(async () => {
      success = await current.run(operation)
    })
    expect(success).toBe(true)
    expect(operation).toHaveBeenCalledTimes(1)
    expect(props.mutate).toHaveBeenCalledTimes(1)
    expect(current.state?.revision).toBe(1)
  })

  it('refreshes an unknown write outcome without retrying or hiding the error', async () => {
    await mount()
    vi.mocked(api.tournament).mockResolvedValueOnce(tournament(7, 1))
    const operation = vi.fn().mockRejectedValue(new TypeError('network interrupted'))
    let success = true
    await act(async () => {
      success = await current.run(operation)
    })
    expect(success).toBe(false)
    expect(operation).toHaveBeenCalledTimes(1)
    expect(current.state?.revision).toBe(1)
    expect(current.error).toContain('Check your connection')
    await invalidate()
    expect(current.error).toContain('Check your connection')
  })

  it('does not refresh or report success into a newer account after an old write resolves', async () => {
    await mount()
    const operation = deferred<unknown>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => operation.promise)
    })
    await update({ identityKey: 'account-b' })
    await update({ identityKey: 'account-a' })
    const reads = vi.mocked(api.tournament).mock.calls.length
    await act(async () => operation.resolve({ result: null }))
    expect(await pending).toBe(false)
    expect(api.tournament).toHaveBeenCalledTimes(reads)
    expect(current.error).toBe('')
  })

  it('keeps publication errors separate from reads and clears them on manual refresh', async () => {
    vi.mocked(api.tournament).mockRejectedValue(new ApiError(404, 'tournament_not_found'))
    await mount()
    await act(async () => {
      await current.run(async () => {
        throw new ApiError(409, 'stale_revision')
      })
    })
    expect(current.state).toBeNull()
    expect(current.readError).toBe('')
    expect(current.writeError).toContain('changed while you were working')
    await act(async () => current.refresh())
    expect(current.error).toBe('')
    expect(current.writeError).toBe('')
  })

  it('does not restore old mutation errors when returning to a previously selected account', async () => {
    await mount()
    await act(async () => {
      await current.run(async () => {
        throw new ApiError(409, 'stale_revision')
      })
    })
    expect(current.writeError).not.toBe('')
    await update({ identityKey: 'account-b' })
    await update({ identityKey: 'account-a' })
    expect(current.writeError).toBe('')
  })

  it('does not run two tournament mutations at once', async () => {
    await mount()
    const operation = deferred<unknown>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => operation.promise)
    })
    const second = vi.fn()
    expect(await current.run(second)).toBe(false)
    expect(second).not.toHaveBeenCalled()
    await act(async () => operation.resolve({ result: null }))
    expect(await pending).toBe(true)
  })
})

describe('match detail reads', () => {
  it('refreshes history and VOD by match revision, and rejects older detail', async () => {
    await act(async () => {
      renderer = create(
        createElement(
          QueryClientProvider,
          { client },
          createElement(MatchHarness, { matchId: 12 }),
        ),
      )
    })
    vi.mocked(api.match).mockResolvedValueOnce({
      ...match(12, 2),
      vod_url: 'https://example.com/replay',
    })
    await act(async () => detail.refresh())
    expect(detail.match?.vod_url).toBe('https://example.com/replay')
    vi.mocked(api.match).mockResolvedValueOnce(match(12, 1))
    await act(async () => detail.refresh())
    expect(detail.match?.revision).toBe(2)
  })

  it('shows missing match errors rather than interpreting them as an unpublished tournament', async () => {
    vi.mocked(api.match).mockRejectedValueOnce(new ApiError(404, 'match_not_found'))
    await act(async () => {
      renderer = create(
        createElement(
          QueryClientProvider,
          { client },
          createElement(MatchHarness, { matchId: 12 }),
        ),
      )
    })
    expect(detail.match).toBeNull()
    expect(detail.error).toContain('match is no longer available')
  })
})
