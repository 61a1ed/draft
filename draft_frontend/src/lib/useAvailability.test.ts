import { act, createElement } from 'react'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import type { TeamAvailabilitySnapshot } from './availabilityTypes'
import { useAvailability, type UseAvailabilityOptions } from './useAvailability'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, api: { ...actual.api, teamAvailability: vi.fn() } }
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

function availability(teamId = 7, revision: number | null = 0): TeamAvailabilitySnapshot {
  return {
    team_id: teamId,
    availability:
      revision === null
        ? null
        : { team_id: teamId, time_zone: 'UTC', display_step_minutes: 60, revision },
    windows: [],
  }
}

let renderer: ReactTestRenderer | null
let current: ReturnType<typeof useAvailability>
let props: UseAvailabilityOptions
function Harness(options: UseAvailabilityOptions) {
  current = useAvailability(options)
  return null
}
async function mount() {
  await act(async () => {
    renderer = create(createElement(Harness, props))
  })
}
async function update(changes: Partial<UseAvailabilityOptions>) {
  props = { ...props, ...changes }
  await act(async () => {
    renderer!.update(createElement(Harness, props))
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  props = {
    teamId: 7,
    refreshKey: 0,
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
  vi.mocked(api.teamAvailability).mockImplementation(async (id) => availability(id))
})
afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('availability reads and mutations', () => {
  it('distinguishes unsaved availability from a missing team', async () => {
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, null))
    await mount()
    expect(current.state).toEqual({ team_id: 7, availability: null, windows: [] })
    expect(current.error).toBe('')
    expect(current.loading).toBe(false)
    vi.mocked(api.teamAvailability).mockRejectedValueOnce(new ApiError(404, 'team_not_found'))
    await update({ refreshKey: 1 })
    expect(current.error).toContain('team no longer exists')
    expect(current.state?.team_id).toBe(7)
  })

  it('serializes reads and coalesces account invalidations', async () => {
    const initial = deferred<TeamAvailabilitySnapshot>()
    vi.mocked(api.teamAvailability)
      .mockReturnValueOnce(initial.promise)
      .mockResolvedValueOnce(availability(7, 2))
    await mount()
    await update({ refreshKey: 1 })
    await update({ refreshKey: 2 })
    expect(api.teamAvailability).toHaveBeenCalledTimes(1)
    await act(async () => initial.resolve(availability(7, 1)))
    expect(api.teamAvailability).toHaveBeenCalledTimes(2)
    expect(current.state?.availability?.revision).toBe(2)
    expect(current.loading).toBe(false)
  })

  it('accepts the first saved revision and never regresses to older or absent headers', async () => {
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, null))
    await mount()
    await update({ refreshKey: 1 })
    expect(current.state?.availability?.revision).toBe(0)
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, 2))
    await update({ refreshKey: 2 })
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, 1))
    await update({ refreshKey: 3 })
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, null))
    await update({ refreshKey: 4 })
    expect(current.state?.availability?.revision).toBe(2)
  })

  it('ignores a response belonging to a different team', async () => {
    await mount()
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(8, 99))
    await update({ refreshKey: 1 })
    expect(current.state).toEqual(availability(7))
  })

  it('retains saved windows during failed refreshes and retries transient reads with backoff', async () => {
    vi.useFakeTimers()
    const saved = availability(7, 1)
    saved.windows = [
      { id: 2, team_id: 7, starts_at: '2026-10-19T09:00:00Z', ends_at: '2026-10-19T12:00:00Z' },
    ]
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(saved)
    await mount()
    vi.mocked(api.teamAvailability)
      .mockRejectedValueOnce(new ApiError(503, 'database_unavailable'))
      .mockRejectedValueOnce(new TypeError('Connection interrupted'))
      .mockResolvedValueOnce(availability(7, 2))
    await update({ refreshKey: 1 })
    expect(current.state?.windows).toEqual(saved.windows)
    expect(current.readError).toContain('database is unavailable')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(api.teamAvailability).toHaveBeenCalledTimes(3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999)
    })
    expect(api.teamAvailability).toHaveBeenCalledTimes(3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(current.state?.availability?.revision).toBe(2)
    expect(current.readError).toBe('')
  })

  it('does not retry authorization or missing-team failures', async () => {
    vi.useFakeTimers()
    vi.mocked(api.teamAvailability).mockRejectedValueOnce(new ApiError(404, 'team_not_found'))
    await mount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api.teamAvailability).toHaveBeenCalledTimes(1)
    expect(current.state).toBeNull()
    expect(current.readError).not.toBe('')
  })

  it('cancels a retry if a queued invalidation recovers the read', async () => {
    vi.useFakeTimers()
    const pending = deferred<TeamAvailabilitySnapshot>()
    vi.mocked(api.teamAvailability).mockReturnValueOnce(pending.promise)
    await mount()
    await update({ refreshKey: 1 })
    await act(async () => pending.reject(new TypeError('Connection interrupted')))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api.teamAvailability).toHaveBeenCalledTimes(2)
    expect(current.error).toBe('')
  })

  it('aborts an old team read and ignores its late response', async () => {
    const old = deferred<TeamAvailabilitySnapshot>()
    vi.mocked(api.teamAvailability).mockReturnValueOnce(old.promise)
    await mount()
    const signal = vi.mocked(api.teamAvailability).mock.calls[0][1]
    await update({ teamId: 8 })
    expect(signal?.aborted).toBe(true)
    await act(async () => old.resolve(availability(7, 99)))
    expect(current.state?.team_id).toBe(8)
  })

  it('isolates account changes even when returning to the original account', async () => {
    const old = deferred<TeamAvailabilitySnapshot>()
    vi.mocked(api.teamAvailability).mockReturnValueOnce(old.promise)
    await mount()
    await update({ identityKey: 'account-b' })
    await update({ identityKey: 'account-a' })
    await act(async () => old.resolve(availability(7, 99)))
    expect(current.state?.availability?.revision).toBe(0)
  })

  it('cancels pending retries on team changes and aborts on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(api.teamAvailability).mockRejectedValueOnce(new TypeError('Network unavailable'))
    await mount()
    const pending = deferred<TeamAvailabilitySnapshot>()
    vi.mocked(api.teamAvailability).mockReturnValueOnce(pending.promise)
    await update({ teamId: 8 })
    const signal = vi.mocked(api.teamAvailability).mock.calls[1][1]
    await act(async () => {
      renderer!.unmount()
      renderer = null
    })
    expect(signal?.aborted).toBe(true)
    await act(async () => pending.resolve(availability(8, 99)))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api.teamAvailability).toHaveBeenCalledTimes(2)
  })

  it('refreshes after one successful mutation through the shared mutation guard', async () => {
    await mount()
    vi.mocked(api.teamAvailability).mockResolvedValueOnce(availability(7, 1))
    const operation = vi.fn().mockResolvedValue({ result: null })
    await act(async () => {
      expect(await current.run(operation)).toBe(true)
    })
    expect(operation).toHaveBeenCalledTimes(1)
    expect(props.mutate).toHaveBeenCalledTimes(1)
    expect(current.state?.availability?.revision).toBe(1)
  })

  it('refreshes a lost write response once and preserves its error across account wake-ups', async () => {
    await mount()
    vi.mocked(api.teamAvailability).mockResolvedValue(availability(7, 1))
    const operation = vi.fn().mockRejectedValue(new TypeError('Connection interrupted'))
    await act(async () => {
      expect(await current.run(operation)).toBe(false)
    })
    expect(operation).toHaveBeenCalledTimes(1)
    expect(current.state?.availability?.revision).toBe(1)
    expect(current.writeError).toContain('Check your connection')
    expect(current.readError).toBe('')
    await update({ refreshKey: 1 })
    expect(current.writeError).toContain('Check your connection')
    await act(async () => current.refresh())
    expect(current.error).toBe('')
  })

  it('does not refresh or restore errors after a mutation owner changes', async () => {
    await mount()
    const operation = deferred<unknown>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => operation.promise)
    })
    await update({ identityKey: 'account-b' })
    await update({ identityKey: 'account-a' })
    const reads = vi.mocked(api.teamAvailability).mock.calls.length
    await act(async () => operation.reject(new ApiError(409, 'stale_revision')))
    expect(await pending).toBe(false)
    expect(api.teamAvailability).toHaveBeenCalledTimes(reads)
    expect(current.error).toBe('')
  })

  it('rejects a delayed mutation before writing when its team changed', async () => {
    let execute!: () => Promise<unknown>
    const guarded = deferred<boolean>()
    props.mutate = vi.fn((operation) => {
      execute = operation
      return guarded.promise
    })
    await mount()
    const operation = vi.fn()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(operation)
    })
    await update({ teamId: 8 })
    await expect(execute()).rejects.toMatchObject({ code: 'session_changed' })
    await act(async () => guarded.resolve(false))
    expect(await pending).toBe(false)
    expect(operation).not.toHaveBeenCalled()
  })

  it('prevents duplicate mutations and releases the guard after completion', async () => {
    await mount()
    const operation = deferred<unknown>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => operation.promise)
    })
    const duplicate = vi.fn()
    expect(await current.run(duplicate)).toBe(false)
    expect(duplicate).not.toHaveBeenCalled()
    await act(async () => operation.resolve({ result: null }))
    expect(await pending).toBe(true)
    await act(async () => {
      expect(await current.run(async () => null)).toBe(true)
    })
  })
})
