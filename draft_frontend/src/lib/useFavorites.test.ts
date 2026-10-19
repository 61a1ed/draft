import { act, createElement } from 'react'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import type { FavoritesList, FavoritesListSnapshot } from './favoritesTypes'
import { useFavorites, type FavoritesController, type UseFavoritesOptions } from './useFavorites'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      favoritesLists: vi.fn(),
      favoritesList: vi.fn(),
      createFavoritesList: vi.fn(),
    },
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

function list(id = 7, userId = 11, revision = 0): FavoritesList {
  return { id, owner_user_id: userId, name: `List ${id}`, revision }
}

function snapshot(id = 7, userId = 11, revision = 0): FavoritesListSnapshot {
  return { ...list(id, userId, revision), entries: [{ draftable_id: 25, ordinal: 0 }] }
}

let renderer: ReactTestRenderer | null
let current: FavoritesController
let props: UseFavoritesOptions
let renders: { userId: number | null; value: FavoritesController }[]

function Harness(options: UseFavoritesOptions) {
  current = useFavorites(options)
  renders.push({ userId: options.userId, value: current })
  return null
}

async function mount() {
  await act(async () => {
    renderer = create(createElement(Harness, props))
  })
}

async function update(changes: Partial<UseFavoritesOptions>) {
  props = { ...props, ...changes }
  await act(async () => {
    renderer!.update(createElement(Harness, props))
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  renders = []
  props = {
    userId: 11,
    refreshKey: 0,
    mutate: vi.fn(async (operation) => {
      try {
        await operation()
        return true
      } catch {
        return false
      }
    }),
  }
  vi.mocked(api.favoritesLists).mockImplementation(async (userId) => [list(7, userId)])
  vi.mocked(api.favoritesList).mockImplementation(async (id) => snapshot(id, props.userId!))
  vi.mocked(api.createFavoritesList).mockResolvedValue({ result: 9 })
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('account favorites reads', () => {
  it('does not read or allow writes while signed out', async () => {
    props.userId = null
    await mount()
    expect(api.favoritesLists).not.toHaveBeenCalled()
    expect(current.lists).toEqual([])
    expect(current.state).toBeNull()
    expect(current.loading).toBe(false)
    expect(await current.create('My list')).toBe(false)
    expect(api.createFavoritesList).not.toHaveBeenCalled()
  })

  it('requests the current owner and rejects a snapshot belonging to another account', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(8, 22), list()])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 22))
    await mount()
    expect(api.favoritesLists).toHaveBeenCalledWith(11, expect.any(AbortSignal))
    expect(current.lists).toEqual([list()])
    expect(current.state).toBeNull()
    expect(current.error).toContain('unexpected response')
  })

  it('serializes reads and coalesces account wake-ups while a read is pending', async () => {
    const initial = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(initial.promise)
    await mount()
    await update({ refreshKey: 1 })
    await update({ refreshKey: 2 })
    expect(api.favoritesLists).toHaveBeenCalledTimes(1)
    await act(async () => initial.resolve([list()]))
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    expect(api.favoritesList).toHaveBeenCalledTimes(2)
    expect(current.state?.id).toBe(7)
    expect(current.loading).toBe(false)
  })

  it('runs a refresh queued between a completed read and its promise cleanup', async () => {
    vi.mocked(api.favoritesList)
      .mockImplementationOnce(() => {
        const response = Promise.resolve(snapshot(7, 11, 1))
        // Queue the wake-up immediately after drain resumes, before its finally runs.
        void response.then(() => queueMicrotask(() => void current.refresh()))
        return response
      })
      .mockResolvedValueOnce(snapshot(7, 11, 2))
    await mount()
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    expect(current.state?.revision).toBe(2)
    expect(current.loading).toBe(false)
  })

  it('keeps the selected list across account wake-ups and ignores a late selection read', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(8)])
    await mount()
    const delayed = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesList).mockReturnValueOnce(delayed.promise)
    await act(async () => current.selectList(8))
    expect(current.selectedId).toBe(8)
    expect(current.state).toBeNull()
    await act(async () => current.selectList(7))
    await act(async () => delayed.resolve(snapshot(8, 11, 4)))
    expect(current.selectedId).toBe(7)
    expect(current.state?.id).toBe(7)
    await act(async () => current.selectList(8))
    await update({ refreshKey: 1 })
    expect(current.selectedId).toBe(8)
    expect(current.state?.revision).toBe(4)
  })

  it('never regresses a header or snapshot and blocks edits after a stale snapshot response', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 3)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 3))
    await mount()
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 1)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 1))
    await update({ refreshKey: 1 })
    expect(current.lists[0].revision).toBe(3)
    expect(current.state?.revision).toBe(3)
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 4)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 2))
    await update({ refreshKey: 2 })
    expect(current.state?.revision).toBe(3)
    expect(current.readError).toContain('data changed')
    const blocked = vi.fn()
    expect(await current.run(blocked)).toBe(false)
    expect(blocked).not.toHaveBeenCalled()
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 4))
    await update({ refreshKey: 3 })
    expect(current.state?.revision).toBe(4)
    expect(current.error).toBe('')
  })

  it('retains the selected snapshot while a newer header loads and blocks edits until refreshed', async () => {
    await mount()
    const previous = current.state
    const refreshed = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 1)])
    vi.mocked(api.favoritesList).mockReturnValueOnce(refreshed.promise)
    await update({ refreshKey: 1 })
    expect(current.lists[0].revision).toBe(1)
    expect(current.state).toBe(previous)
    expect(current.loading).toBe(true)
    const blocked = vi.fn()
    expect(await current.run(blocked)).toBe(false)
    expect(blocked).not.toHaveBeenCalled()
    await act(async () => refreshed.resolve(snapshot(7, 11, 1)))
    expect(current.state?.revision).toBe(1)
    expect(current.loading).toBe(false)
    expect(current.readError).toBe('')
    await act(async () => expect(await current.run(async () => null)).toBe(true))
  })

  it('selects the next owned list when a list is deleted between the header and detail reads', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(8)])
    vi.mocked(api.favoritesList).mockRejectedValueOnce(
      new ApiError(404, 'favorites_list_not_found'),
    )
    await mount()
    expect(current.lists.map((item) => item.id)).toEqual([8])
    expect(current.selectedId).toBe(8)
    expect(current.state?.id).toBe(8)
    expect(current.error).toBe('')
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([])
    await update({ refreshKey: 1 })
    expect(current.selectedId).toBeNull()
    expect(current.state).toBeNull()
  })

  it('clears data on the first account-change render and ignores responses from an earlier login', async () => {
    await mount()
    const old = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesList).mockReturnValueOnce(old.promise)
    await update({ refreshKey: 1 })
    const signal = vi.mocked(api.favoritesList).mock.calls.at(-1)![1]
    await update({ userId: 22 })
    expect(signal?.aborted).toBe(true)
    const firstNewAccount = renders.find((render) => render.userId === 22)!
    expect(firstNewAccount.value.lists).toEqual([])
    expect(firstNewAccount.value.state).toBeNull()
    await update({ userId: 11 })
    await act(async () => old.resolve(snapshot(7, 11, 90)))
    expect(current.state?.revision).toBe(0)
    await update({ userId: null })
    expect(
      renders
        .filter((render) => render.userId === null)
        .every(
          (render) =>
            render.value.state === null && render.value.lists.length === 0 && !render.value.busy,
        ),
    ).toBe(true)
  })

  it('retries transient reads with bounded backoff and recovers without another event', async () => {
    vi.useFakeTimers()
    vi.mocked(api.favoritesLists)
      .mockRejectedValueOnce(new ApiError(503, 'database_unavailable'))
      .mockRejectedValueOnce(new TypeError('offline'))
    await mount()
    expect(current.readError).not.toBe('')
    await act(async () => vi.advanceTimersByTimeAsync(1000))
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(1999))
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(current.state?.id).toBe(7)
    expect(current.readError).toBe('')

    vi.mocked(api.favoritesLists).mockRejectedValue(new TypeError('offline'))
    await update({ refreshKey: 1 })
    for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
      const reads = vi.mocked(api.favoritesLists).mock.calls.length
      await act(async () => vi.advanceTimersByTimeAsync(delay - 1))
      expect(api.favoritesLists).toHaveBeenCalledTimes(reads)
      await act(async () => vi.advanceTimersByTimeAsync(1))
      expect(api.favoritesLists).toHaveBeenCalledTimes(reads + 1)
    }
  })

  it('cancels scheduled retries after recovery or account logout', async () => {
    vi.useFakeTimers()
    const delayed = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(delayed.promise)
    await mount()
    await update({ refreshKey: 1 })
    await act(async () => delayed.reject(new TypeError('offline')))
    expect(current.error).toBe('')
    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    vi.mocked(api.favoritesLists).mockRejectedValueOnce(new TypeError('offline'))
    await update({ refreshKey: 2 })
    await update({ userId: null })
    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(api.favoritesLists).toHaveBeenCalledTimes(3)
  })
})

describe('favorites mutations', () => {
  it('selects a created list after an authoritative refresh', async () => {
    await mount()
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(), list(9)])
    await act(async () => expect(await current.create('My shortlist')).toBe(true))
    expect(api.createFavoritesList).toHaveBeenCalledWith({ p_name: 'My shortlist' })
    expect(props.mutate).toHaveBeenCalledTimes(1)
    expect(current.selectedId).toBe(9)
    expect(current.state?.id).toBe(9)
  })

  it('keeps writes and list selection locked until their follow-up read finishes', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(8)])
    await mount()
    const refreshed = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(refreshed.promise)
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(async () => ({ result: null }))
    })
    expect(current.busy).toBe(true)
    await act(async () => current.selectList(8))
    expect(current.selectedId).toBe(7)
    const duplicate = vi.fn()
    expect(await current.run(duplicate)).toBe(false)
    expect(duplicate).not.toHaveBeenCalled()
    await act(async () => refreshed.resolve([list(), list(8)]))
    expect(await pending).toBe(true)
    expect(current.busy).toBe(false)
    await act(async () => current.selectList(8))
    expect(current.selectedId).toBe(8)
  })

  it('refreshes uncertain writes without replaying them and preserves the error across wake-ups', async () => {
    await mount()
    vi.mocked(api.favoritesList).mockResolvedValue(snapshot(7, 11, 1))
    const operation = vi.fn().mockRejectedValue(new TypeError('lost response'))
    await act(async () => expect(await current.run(operation)).toBe(false))
    expect(operation).toHaveBeenCalledTimes(1)
    expect(current.state?.revision).toBe(1)
    expect(current.writeError).toContain('Check your connection')
    expect(current.readError).toBe('')
    await update({ refreshKey: 1 })
    expect(current.writeError).toContain('Check your connection')
    await act(async () => current.refresh())
    expect(current.error).toBe('')
    expect(operation).toHaveBeenCalledTimes(1)
  })

  it('refreshes a stale revision failure once and uses the latest snapshot for the next action', async () => {
    await mount()
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 3))
    const operation = vi.fn().mockRejectedValue(new ApiError(409, 'stale_revision'))
    await act(async () => expect(await current.run(operation)).toBe(false))
    expect(operation).toHaveBeenCalledTimes(1)
    expect(current.state?.revision).toBe(3)
    expect(current.error).toContain('data changed')
  })

  it('does not restore data, errors, or a busy lock when an old account mutation settles', async () => {
    await mount()
    const old = deferred<unknown>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => old.promise)
    })
    await update({ userId: 22 })
    await update({ userId: 11 })
    expect(current.busy).toBe(false)
    const reads = vi.mocked(api.favoritesLists).mock.calls.length
    await act(async () => old.reject(new ApiError(409, 'stale_revision')))
    expect(await pending).toBe(false)
    expect(api.favoritesLists).toHaveBeenCalledTimes(reads)
    expect(current.error).toBe('')
  })

  it('rejects a deferred operation before sending it after the account changes', async () => {
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
    await update({ userId: 22 })
    await expect(execute()).rejects.toMatchObject({ code: 'session_changed' })
    await act(async () => guarded.resolve(false))
    expect(await pending).toBe(false)
    expect(operation).not.toHaveBeenCalled()
  })
})
