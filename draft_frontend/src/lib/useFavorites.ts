import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { queryOptions, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, errorMessage } from './api'
import type { FavoritesList, FavoritesListSnapshot } from './favoritesTypes'
import { QUERY_POLL_INTERVAL, queryKeys } from './queryClient'

export interface UseFavoritesOptions {
  userId: number | null
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

interface FavoritesOwner {
  userId: number
  active: boolean
  busy: boolean
  selectedId: number | null
  preferredId: number | null
  canWrite: boolean
}

/** Favorites belong to the account; league selection never participates in their lifecycle. */
export function useFavorites({ userId, mutate }: UseFavoritesOptions) {
  const client = useQueryClient()
  const owner = useMemo<FavoritesOwner | null>(
    () =>
      userId === null
        ? null
        : {
            userId,
            active: true,
            busy: false,
            selectedId: null,
            preferredId: null,
            canWrite: false,
          },
    [userId],
  )
  const identity = useRef(owner)
  identity.current = owner
  const [selection, setSelection] = useState<{ owner: FavoritesOwner; id: number } | null>(null)
  const [failure, setFailure] = useState<{ owner: FavoritesOwner; message: string } | null>(null)
  const isCurrent = useCallback(
    (captured: FavoritesOwner | null): captured is FavoritesOwner =>
      captured !== null && captured.active && identity.current === captured,
    [],
  )

  useEffect(() => {
    if (!owner) return
    owner.active = true
    return () => {
      owner.active = false
      // Cancel transports and discard private cached data when this login ends.
      void client.cancelQueries({ queryKey: queryKeys.favorites(owner.userId) })
      client.removeQueries({ queryKey: queryKeys.favorites(owner.userId) })
    }
  }, [client, owner])

  const listOptions = (accountId: number | null) =>
    queryOptions({
      queryKey: queryKeys.favoritesLists(accountId),
      enabled: accountId !== null,
      refetchInterval: QUERY_POLL_INTERVAL,
      queryFn: async ({ signal }) => {
        const next = await api.favoritesLists(accountId!, signal)
        signal.throwIfAborted()
        const previous = client.getQueryData<FavoritesList[]>(queryKeys.favoritesLists(accountId))
        return next
          .filter((list) => list.owner_user_id === accountId)
          .map((list) => {
            const old = previous?.find((candidate) => candidate.id === list.id)
            return old && old.revision > list.revision ? old : list
          })
      },
    })

  const snapshotOptions = (accountId: number | null, id: number | null) =>
    queryOptions({
      queryKey: queryKeys.favoritesList(accountId, id),
      enabled: accountId !== null && id !== null,
      refetchInterval: QUERY_POLL_INTERVAL,
      queryFn: async ({ signal }): Promise<FavoritesListSnapshot | null> => {
        let next: FavoritesListSnapshot
        try {
          next = await api.favoritesList(id!, signal)
        } catch (cause) {
          signal.throwIfAborted()
          if (
            cause instanceof ApiError &&
            cause.status === 404 &&
            cause.code === 'favorites_list_not_found'
          ) {
            client.setQueryData<FavoritesList[]>(queryKeys.favoritesLists(accountId), (lists) =>
              lists?.filter((list) => list.id !== id),
            )
            return null
          }
          throw cause
        }
        signal.throwIfAborted()
        if (next.id !== id || next.owner_user_id !== accountId) {
          throw new ApiError(502, 'invalid_response')
        }
        const previous = client.getQueryData<FavoritesListSnapshot | null>(
          queryKeys.favoritesList(accountId, id),
        )
        const header = client
          .getQueryData<FavoritesList[]>(queryKeys.favoritesLists(accountId))
          ?.find((list) => list.id === id)
        const minimumRevision = Math.max(previous?.revision ?? -1, header?.revision ?? -1)
        if (next.revision < minimumRevision) {
          if (previous && previous.revision >= minimumRevision) return previous
          throw new ApiError(409, 'stale_revision')
        }
        client.setQueryData<FavoritesList[]>(queryKeys.favoritesLists(accountId), (lists) =>
          lists?.map((list) =>
            list.id === id
              ? {
                  id: next.id,
                  owner_user_id: next.owner_user_id,
                  name: next.name,
                  revision: next.revision,
                }
              : list,
          ),
        )
        return next
      },
    })

  const listsQuery = useQuery(listOptions(userId))
  const lists = userId === null ? [] : (listsQuery.data ?? [])
  const requestedId = selection?.owner === owner ? selection.id : null
  const selectedId = lists.some((list) => list.id === requestedId)
    ? requestedId
    : (lists[0]?.id ?? null)
  if (owner) owner.selectedId = selectedId
  const snapshotQuery = useQuery(snapshotOptions(userId, selectedId))
  const state = userId === null || selectedId === null ? null : (snapshotQuery.data ?? null)
  const headerRevision = lists.find((list) => list.id === selectedId)?.revision

  useEffect(() => {
    if (!owner || selectedId === null || headerRevision === undefined) return
    const cached = client.getQueryData<FavoritesListSnapshot | null>(
      queryKeys.favoritesList(owner.userId, selectedId),
    )
    if (cached && cached.revision < headerRevision) {
      void client.invalidateQueries(
        { queryKey: queryKeys.favoritesList(owner.userId, selectedId) },
        { cancelRefetch: false },
      )
    }
  }, [client, owner, selectedId, headerRevision])

  // Reads after a write are ordered so the detail is checked against the latest header revision.
  const refreshReads = async (captured: FavoritesOwner) => {
    if (!isCurrent(captured)) return
    // A poll started before a write committed must not satisfy its follow-up refresh.
    await client.cancelQueries({ queryKey: [...queryKeys.favorites(captured.userId), 'list'] })
    if (!isCurrent(captured)) return
    await client.invalidateQueries({
      queryKey: queryKeys.favoritesLists(captured.userId),
      exact: true,
    })
    if (!isCurrent(captured)) return
    if (client.getQueryState(queryKeys.favoritesLists(captured.userId))?.status === 'error') return
    await client.invalidateQueries({
      queryKey: [...queryKeys.favorites(captured.userId), 'list'],
      refetchType: 'none',
    })
    const headers =
      client.getQueryData<FavoritesList[]>(queryKeys.favoritesLists(captured.userId)) ?? []
    const requested = captured.preferredId ?? captured.selectedId
    const id = headers.some((list) => list.id === requested) ? requested : (headers[0]?.id ?? null)
    if (id !== null && isCurrent(captured)) {
      // fetchQuery shares the same query and in-flight request as the selected useQuery observer.
      await client.fetchQuery(snapshotOptions(captured.userId, id)).catch(() => undefined)
    }
  }
  const mutation = useMutation({
    mutationKey: queryKeys.favorites(userId),
    retry: false,
    mutationFn: async ({
      captured,
      operation,
    }: {
      captured: FavoritesOwner
      operation: () => Promise<unknown>
    }) => {
      let completed = false
      let message = ''
      let success = false
      try {
        success = await mutate(async () => {
          if (!isCurrent(captured)) throw new ApiError(409, 'session_changed')
          try {
            const result = await operation()
            completed = true
            return result
          } catch (cause) {
            message = errorMessage(cause)
            throw cause
          }
        })
        if (!success && !message)
          message = 'The action could not complete. Review the latest state.'
      } catch (cause) {
        message = message || errorMessage(cause)
      }
      if (!isCurrent(captured)) return false
      setFailure({ owner: captured, message })
      // Even a lost response can have committed. Re-read without replaying the write.
      await refreshReads(captured)
      return isCurrent(captured) && success && completed
    },
  })

  const readCause =
    listsQuery.error ??
    listsQuery.failureReason ??
    snapshotQuery.error ??
    snapshotQuery.failureReason
  const readError = userId === null || !readCause ? '' : errorMessage(readCause)
  const writeError = failure?.owner === owner ? failure.message : ''
  const behind = headerRevision !== undefined && (state?.revision ?? -1) < headerRevision
  const loading =
    userId !== null &&
    (listsQuery.isPending ||
      listsQuery.isFetching ||
      (selectedId !== null &&
        (snapshotQuery.isPending || snapshotQuery.isFetching || (behind && !readError))))
  const busy = owner !== null && mutation.isPending && mutation.variables?.captured === owner
  if (owner) owner.canWrite = !loading && !readError

  const clearError = () => {
    if (isCurrent(owner)) setFailure({ owner, message: '' })
  }
  const refresh = async () => {
    if (!isCurrent(owner)) return
    clearError()
    await refreshReads(owner)
  }
  const run = async (operation: () => Promise<unknown>): Promise<boolean> => {
    if (
      !isCurrent(owner) ||
      owner.busy ||
      !owner.canWrite ||
      client.isFetching({ queryKey: queryKeys.favorites(owner.userId) }) > 0 ||
      owner.selectedId !== selectedId
    )
      return false
    owner.busy = true
    setFailure({ owner, message: '' })
    try {
      return await mutation.mutateAsync({ captured: owner, operation })
    } finally {
      owner.busy = false
    }
  }
  const create = (name: string) =>
    run(async () => {
      const created = await api.createFavoritesList({ p_name: name })
      if (isCurrent(owner)) {
        owner.preferredId = created.result
        setSelection({ owner, id: created.result })
      }
      return created
    })

  return {
    lists,
    selectedId,
    state,
    loading,
    busy,
    error: writeError || readError,
    readError,
    writeError,
    selectList: (id: number) => {
      if (!isCurrent(owner) || owner.busy || !lists.some((list) => list.id === id)) return
      owner.selectedId = id
      owner.preferredId = null
      setSelection({ owner, id })
      clearError()
    },
    refresh,
    clearError,
    run,
    create,
  }
}

export type FavoritesController = ReturnType<typeof useFavorites>
