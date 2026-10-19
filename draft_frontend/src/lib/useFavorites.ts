import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { FavoritesList, FavoritesListSnapshot } from './favoritesTypes'

export interface UseFavoritesOptions {
  userId: number | null
  refreshKey: number
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

interface FavoritesOwner {
  userId: number
  active: boolean
  lists: FavoritesList[]
  selectedId: number | null
  preferredId: number | null
  loading: boolean
  busy: boolean
  readError: string
  writeError: string
  refresh: () => Promise<void>
  select: (id: number) => void
  publish: () => void
}

interface FavoritesState {
  owner: FavoritesOwner | null
  lists: FavoritesList[]
  selectedId: number | null
  state: FavoritesListSnapshot | null
  loading: boolean
  busy: boolean
  readError: string
  writeError: string
}

const empty: FavoritesState = {
  owner: null,
  lists: [],
  selectedId: null,
  state: null,
  loading: false,
  busy: false,
  readError: '',
  writeError: '',
}

function transient(cause: unknown) {
  return (
    cause instanceof TypeError ||
    (cause instanceof ApiError && (cause.status >= 500 || [408, 429].includes(cause.status)))
  )
}

/** Favorites belong to the account; league selection never participates in their lifecycle. */
export function useFavorites({ userId, refreshKey, mutate }: UseFavoritesOptions) {
  const identity = useRef(userId)
  identity.current = userId
  const owner = useRef<FavoritesOwner | null>(null)
  const [result, setResult] = useState<FavoritesState>(empty)

  useEffect(() => {
    if (userId === null) {
      owner.current = null
      setResult(empty)
      return
    }

    const abort = new AbortController()
    const snapshots = new Map<number, FavoritesListSnapshot>()
    let pending = false
    let running: Promise<void> | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const current: FavoritesOwner = {
      userId,
      active: true,
      lists: [],
      selectedId: null,
      preferredId: null,
      loading: true,
      busy: false,
      readError: '',
      writeError: '',
      refresh,
      select,
      publish,
    }
    owner.current = current
    const valid = () => current.active && identity.current === userId && owner.current === current

    function publish() {
      if (!valid()) return
      const header = current.lists.find((list) => list.id === current.selectedId)
      const snapshot = header ? snapshots.get(header.id) : undefined
      setResult({
        owner: current,
        lists: current.lists,
        selectedId: current.selectedId,
        // Preserve the selected list's cards and keyboard focus during refresh.
        // Loading and read errors prevent writes until its snapshot catches up.
        state: snapshot ?? null,
        loading: current.loading,
        busy: current.busy,
        readError: current.readError,
        writeError: current.writeError,
      })
    }

    async function drain() {
      while (pending && valid()) {
        pending = false
        clearTimeout(retry)
        current.loading = true
        publish()
        try {
          const headers = await api.favoritesLists(userId!, abort.signal)
          if (!valid()) return
          const previous = new Map(current.lists.map((list) => [list.id, list]))
          current.lists = headers
            .filter((list) => list.owner_user_id === userId)
            .map((list) => {
              const old = previous.get(list.id)
              return old && old.revision > list.revision ? old : list
            })
          if (current.lists.some((list) => list.id === current.preferredId)) {
            current.selectedId = current.preferredId
            current.preferredId = null
          }
          if (!current.lists.some((list) => list.id === current.selectedId)) {
            current.selectedId = current.lists[0]?.id ?? null
          }
          publish()

          // A list can be deleted between the header read and its snapshot read.
          // Remove that selection and continue with the next remaining owned list.
          while (current.selectedId !== null && valid()) {
            const listId = current.selectedId
            try {
              const next = await api.favoritesList(listId, abort.signal)
              if (!valid()) return
              if (next.id !== listId || next.owner_user_id !== userId) {
                throw new ApiError(502, 'invalid_response')
              }
              const previousSnapshot = snapshots.get(listId)
              const header = current.lists.find((list) => list.id === listId)
              const minimumRevision = Math.max(
                previousSnapshot?.revision ?? -1,
                header?.revision ?? -1,
              )
              if (next.revision >= minimumRevision) {
                snapshots.set(listId, next)
                current.lists = current.lists.map((list) =>
                  list.id === listId
                    ? {
                        id: next.id,
                        owner_user_id: next.owner_user_id,
                        name: next.name,
                        revision: next.revision,
                      }
                    : list,
                )
              } else if (!previousSnapshot || previousSnapshot.revision < minimumRevision) {
                throw new ApiError(409, 'stale_revision')
              }
              break
            } catch (cause) {
              if (!valid()) return
              if (
                cause instanceof ApiError &&
                cause.status === 404 &&
                cause.code === 'favorites_list_not_found'
              ) {
                current.lists = current.lists.filter((list) => list.id !== listId)
                snapshots.delete(listId)
                if (current.selectedId === listId) {
                  current.selectedId = current.lists[0]?.id ?? null
                }
                publish()
              } else {
                throw cause
              }
            }
          }
          failures = 0
          current.readError = ''
        } catch (cause) {
          if (!valid() || abort.signal.aborted) return
          current.readError = errorMessage(cause)
          if (transient(cause)) {
            retry = setTimeout(
              () => void refresh(),
              Math.min(1000 * 2 ** Math.min(failures++, 5), 30_000),
            )
          }
        }
        current.loading = pending
        publish()
      }
    }

    function refresh(): Promise<void> {
      if (!valid()) return Promise.resolve()
      clearTimeout(retry)
      pending = true
      if (!running) {
        running = drain().finally(() => {
          running = null
          // A wake-up can arrive after drain returns but before this cleanup runs.
          // Await its follow-up read too, so mutation locks cover the whole refresh.
          if (pending && valid()) return refresh()
        })
      }
      return running
    }

    function select(id: number) {
      if (!valid() || current.busy || !current.lists.some((list) => list.id === id)) return
      if (current.selectedId === id) return
      current.selectedId = id
      current.preferredId = null
      current.loading = true
      current.readError = ''
      current.writeError = ''
      publish()
      void refresh()
    }

    publish()
    return () => {
      current.active = false
      clearTimeout(retry)
      abort.abort()
      if (owner.current === current) owner.current = null
    }
  }, [userId])

  const captureOwner = useCallback(
    () =>
      identity.current === userId && owner.current?.userId === userId && owner.current.active
        ? owner.current
        : null,
    [userId],
  )

  useEffect(() => {
    void captureOwner()?.refresh()
  }, [captureOwner, refreshKey])

  const visible = result.owner === captureOwner() ? result : empty

  const clearError = () => {
    const current = captureOwner()
    if (!current) return
    current.writeError = ''
    current.publish()
  }

  const refresh = async () => {
    clearError()
    await captureOwner()?.refresh()
  }

  const run = async (operation: () => Promise<unknown>): Promise<boolean> => {
    const current = captureOwner()
    if (
      !current ||
      current.busy ||
      current.loading ||
      current.readError ||
      current.selectedId !== visible.selectedId
    )
      return false
    const isCurrent = () => captureOwner() === current
    current.busy = true
    current.writeError = ''
    current.publish()
    let completed = false
    let failure = ''
    try {
      const success = await mutate(async () => {
        if (!isCurrent()) throw new ApiError(409, 'session_changed')
        try {
          const value = await operation()
          completed = true
          return value
        } catch (cause) {
          failure = errorMessage(cause)
          throw cause
        }
      })
      if (!isCurrent()) return false
      if (!success && !failure) failure = 'The action could not complete. Review the latest state.'
      current.writeError = failure
      current.publish()
      // A lost response may still have committed. Refresh without replaying the mutation.
      await current.refresh()
      return isCurrent() && success && completed
    } catch (cause) {
      if (!isCurrent()) return false
      current.writeError = failure || errorMessage(cause)
      current.publish()
      await current.refresh()
      return false
    } finally {
      current.busy = false
      if (isCurrent()) current.publish()
    }
  }

  const create = async (name: string): Promise<boolean> => {
    const current = captureOwner()
    return run(async () => {
      const created = await api.createFavoritesList({ p_name: name })
      if (captureOwner() === current && current) current.preferredId = created.result
      return created
    })
  }

  return {
    lists: visible.lists,
    selectedId: visible.selectedId,
    state: visible.state,
    loading: userId !== null && (visible === empty || visible.loading),
    busy: visible.busy,
    error: visible.writeError || visible.readError,
    readError: visible.readError,
    writeError: visible.writeError,
    selectList: (id: number) => captureOwner()?.select(id),
    refresh,
    clearError,
    run,
    create,
  }
}

export type FavoritesController = ReturnType<typeof useFavorites>
