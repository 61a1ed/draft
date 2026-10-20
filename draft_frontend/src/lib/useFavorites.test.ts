import { act, createElement, StrictMode } from 'react'
import { create, type ReactTestRenderer } from 'react-test-renderer'
import { QueryClientProvider, notifyManager, type QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import type { FavoritesList, FavoritesListSnapshot } from './favoritesTypes'
import { useFavorites, type FavoritesController, type UseFavoritesOptions } from './useFavorites'
import { createQueryClient, queryKeys } from './queryClient'

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
let client: QueryClient
let current: FavoritesController
let props: UseFavoritesOptions
let renders: { userId: number | null; value: FavoritesController }[]

function Harness(options: UseFavoritesOptions) {
  current = useFavorites(options)
  renders.push({ userId: options.userId, value: current })
  return null
}

function tree() {
  return createElement(QueryClientProvider, { client }, createElement(Harness, props))
}

async function mount() {
  await act(async () => {
    renderer = create(tree())
  })
}

async function update(changes: Partial<UseFavoritesOptions>) {
  props = { ...props, ...changes }
  await act(async () => {
    renderer!.update(tree())
  })
}

async function invalidateFavorites() {
  await act(async () => {
    // Do not await the network: individual tests also exercise in-flight requests.
    void client.invalidateQueries({ queryKey: queryKeys.favorites(props.userId) })
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  client = createQueryClient()
  notifyManager.setScheduler(queueMicrotask)
  renders = []
  props = {
    userId: 11,
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
  client.clear()
  notifyManager.setScheduler((callback) => setTimeout(callback, 0))
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('account favorites reads', () => {
  it('loads successfully through Strict Mode effect cleanup and replay', async () => {
    await act(async () => {
      renderer = create(createElement(StrictMode, null, tree()))
    })
    expect(current.state?.id).toBe(7)
    expect(current.loading).toBe(false)
  })

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

  it('shares in-flight reads when several refreshes request the same account data', async () => {
    const initial = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(initial.promise)
    await mount()
    await invalidateFavorites()
    await invalidateFavorites()
    expect(api.favoritesLists).toHaveBeenCalledTimes(1)
    await act(async () => initial.resolve([list()]))
    expect(api.favoritesLists).toHaveBeenCalledTimes(1)
    expect(api.favoritesList).toHaveBeenCalledTimes(1)
    expect(current.state?.id).toBe(7)
    expect(current.loading).toBe(false)
  })

  it('refreshes the query when a completed snapshot is explicitly invalidated', async () => {
    vi.mocked(api.favoritesList)
      .mockImplementationOnce(() => {
        const response = Promise.resolve(snapshot(7, 11, 1))
        // Invalidate just as the first response completes.
        void response.then(() => queueMicrotask(() => void current.refresh()))
        return response
      })
      .mockResolvedValueOnce(snapshot(7, 11, 2))
    await mount()
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    expect(current.state?.revision).toBe(2)
    expect(current.loading).toBe(false)
  })

  it('keeps selection across refreshes and cancels a previous selection read', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(8)])
    await mount()
    const delayed = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesList).mockReturnValueOnce(delayed.promise)
    await act(async () => current.selectList(8))
    const signal = vi.mocked(api.favoritesList).mock.calls.at(-1)![1]
    expect(current.selectedId).toBe(8)
    expect(current.state).toBeNull()
    await act(async () => current.selectList(7))
    expect(signal?.aborted).toBe(true)
    await act(async () => delayed.resolve(snapshot(8, 11, 4)))
    expect(current.selectedId).toBe(7)
    expect(current.state?.id).toBe(7)
    expect(client.getQueryData(queryKeys.favoritesList(11, 8))).toBeUndefined()
    vi.mocked(api.favoritesList).mockResolvedValue(snapshot(8, 11, 1))
    await act(async () => current.selectList(8))
    await invalidateFavorites()
    expect(current.selectedId).toBe(8)
    expect(current.state?.revision).toBe(1)
  })

  it('never regresses a header or snapshot and blocks edits after a stale snapshot response', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 3)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 3))
    await mount()
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 1)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 1))
    await invalidateFavorites()
    expect(current.lists[0].revision).toBe(3)
    expect(current.state?.revision).toBe(3)
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 4)])
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 2))
    await invalidateFavorites()
    expect(current.state?.revision).toBe(3)
    expect(current.readError).toContain('data changed')
    const blocked = vi.fn()
    expect(await current.run(blocked)).toBe(false)
    expect(blocked).not.toHaveBeenCalled()
    vi.mocked(api.favoritesList).mockResolvedValueOnce(snapshot(7, 11, 4))
    await invalidateFavorites()
    expect(current.state?.revision).toBe(4)
    expect(current.error).toBe('')
  })

  it('retains the selected snapshot while a newer header loads and blocks edits until refreshed', async () => {
    await mount()
    const previous = current.state
    const refreshed = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesLists).mockResolvedValueOnce([list(7, 11, 1)])
    vi.mocked(api.favoritesList).mockReturnValueOnce(refreshed.promise)
    await invalidateFavorites()
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
    await invalidateFavorites()
    expect(current.selectedId).toBeNull()
    expect(current.state).toBeNull()
  })

  it('clears data on the first account-change render and ignores responses from an earlier login', async () => {
    await mount()
    const old = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesList).mockReturnValueOnce(old.promise)
    await invalidateFavorites()
    const signal = vi.mocked(api.favoritesList).mock.calls.at(-1)![1]
    await update({ userId: 22 })
    expect(signal?.aborted).toBe(true)
    expect(client.getQueryData(queryKeys.favoritesLists(11))).toBeUndefined()
    expect(client.getQueryData(queryKeys.favoritesList(11, 7))).toBeUndefined()
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
    await invalidateFavorites()
    for (const delay of [1000, 2000]) {
      const reads = vi.mocked(api.favoritesLists).mock.calls.length
      await act(async () => vi.advanceTimersByTimeAsync(delay - 1))
      expect(api.favoritesLists).toHaveBeenCalledTimes(reads)
      await act(async () => vi.advanceTimersByTimeAsync(1))
      expect(api.favoritesLists).toHaveBeenCalledTimes(reads + 1)
    }
    const reads = vi.mocked(api.favoritesLists).mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(api.favoritesLists).toHaveBeenCalledTimes(reads)
    expect(current.loading).toBe(false)
    expect(current.readError).toContain('Check your connection')
  })

  it('cancels scheduled retries after recovery or account logout', async () => {
    vi.useFakeTimers()
    const delayed = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(delayed.promise)
    await mount()
    await invalidateFavorites()
    await act(async () => delayed.reject(new TypeError('offline')))
    expect(current.error).toContain('Check your connection')
    await act(async () => vi.advanceTimersByTimeAsync(1000))
    expect(api.favoritesLists).toHaveBeenCalledTimes(2)
    expect(current.error).toBe('')
    vi.mocked(api.favoritesLists).mockRejectedValueOnce(new TypeError('offline'))
    await invalidateFavorites()
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

  it('waits for a newly created list snapshot before releasing the write lock', async () => {
    await mount()
    const detail = deferred<FavoritesListSnapshot>()
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(9)])
    vi.mocked(api.favoritesList).mockImplementation((id) =>
      id === 9 ? detail.promise : Promise.resolve(snapshot(id)),
    )
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.create('New list')
    })
    expect(current.selectedId).toBe(9)
    expect(current.busy).toBe(true)
    expect(current.loading).toBe(true)
    await act(async () => detail.resolve(snapshot(9)))
    expect(await pending).toBe(true)
    expect(current.state?.id).toBe(9)
    expect(current.busy).toBe(false)
  })

  it('rejects an older action callback after selection or read readiness changes', async () => {
    vi.mocked(api.favoritesLists).mockResolvedValue([list(), list(8)])
    await mount()
    const previousRun = current.run
    const blocked = vi.fn()
    await act(async () => current.selectList(8))
    expect(await previousRun(blocked)).toBe(false)
    const currentRun = current.run
    const refresh = deferred<FavoritesList[]>()
    vi.mocked(api.favoritesLists).mockReturnValueOnce(refresh.promise)
    await invalidateFavorites()
    expect(await currentRun(blocked)).toBe(false)
    expect(blocked).not.toHaveBeenCalled()
    await act(async () => refresh.resolve([list(), list(8)]))
  })

  it('refreshes uncertain writes without replaying them and preserves the error across query invalidations', async () => {
    await mount()
    vi.mocked(api.favoritesList).mockResolvedValue(snapshot(7, 11, 1))
    const operation = vi.fn().mockRejectedValue(new TypeError('lost response'))
    await act(async () => expect(await current.run(operation)).toBe(false))
    expect(operation).toHaveBeenCalledTimes(1)
    expect(current.state?.revision).toBe(1)
    expect(current.writeError).toContain('Check your connection')
    expect(current.readError).toBe('')
    await invalidateFavorites()
    expect(current.writeError).toContain('Check your connection')
    await act(async () => current.refresh())
    expect(current.error).toBe('')
    expect(operation).toHaveBeenCalledTimes(1)
  })

  it('cancels a poll started during a write before reading its committed state', async () => {
    await mount()
    const write = deferred<unknown>()
    const poll = deferred<FavoritesListSnapshot>()
    let pending!: Promise<boolean>
    await act(async () => {
      pending = current.run(() => write.promise)
    })
    vi.mocked(api.favoritesList)
      .mockReturnValueOnce(poll.promise)
      .mockResolvedValueOnce(snapshot(7, 11, 1))
    await act(async () => {
      void client.refetchQueries({ queryKey: queryKeys.favoritesList(11, 7) })
    })
    const signal = vi.mocked(api.favoritesList).mock.calls.at(-1)![1]
    await act(async () => write.resolve({ result: null }))
    expect(await pending).toBe(true)
    expect(signal?.aborted).toBe(true)
    expect(current.state?.revision).toBe(1)
    await act(async () => poll.resolve(snapshot(7, 11, 0)))
    expect(current.state?.revision).toBe(1)
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
