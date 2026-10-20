import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, errorMessage } from './api'
import { QUERY_POLL_INTERVAL, queryKeys } from './queryClient'

export interface ResourceReadOptions {
  /** Change this when a mounted view changes account. */
  identityKey?: string | number
}

interface ResourceReader<T> {
  resource: 'tournament' | 'match' | 'availability'
  read: (id: number, signal: AbortSignal) => Promise<T>
  accepts: (value: T, previous: T | null, id: number) => boolean
  isEmpty?: (cause: unknown) => boolean
}

interface ResourceOwner {
  active: boolean
}

/** Keep server state in the query cache, scoped to both account and resource. */
export function useResourceQuery<T>(
  id: number,
  { identityKey }: ResourceReadOptions,
  reader: ResourceReader<T>,
) {
  const client = useQueryClient()
  const queryKey = useMemo(
    () => queryKeys[reader.resource](identityKey, id),
    [identityKey, reader.resource, id],
  )
  // A new owner also distinguishes leaving and returning to the same resource.
  const owner = useMemo<ResourceOwner>(() => ({ active: true }), [queryKey])
  const selected = useRef(owner)
  selected.current = owner
  useEffect(() => {
    owner.active = true
    return () => {
      owner.active = false
    }
  }, [owner])

  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }): Promise<T | null> => {
      try {
        const next = await reader.read(id, signal)
        const previous = client.getQueryData<T | null>(queryKey) ?? null
        return reader.accepts(next, previous, id) ? next : previous
      } catch (cause) {
        if (reader.isEmpty?.(cause) && !client.getQueryData(queryKey)) return null
        throw cause
      }
    },
    refetchInterval: QUERY_POLL_INTERVAL,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  })

  const captureOwner = useCallback(
    () => (selected.current === owner && owner.active ? owner : null),
    [owner],
  )
  const refresh = useCallback(async () => {
    if (!captureOwner()) return
    // A write may have committed during an earlier read. Start a new request even
    // when the first read has not populated the cache yet.
    await client.cancelQueries({ queryKey, exact: true })
    if (captureOwner()) await client.invalidateQueries({ queryKey, exact: true })
  }, [client, queryKey, captureOwner])
  const refreshRelated = useCallback(async () => {
    if (!captureOwner()) return
    const related = {
      predicate: (query: { queryKey: readonly unknown[] }) =>
        query.queryKey[0] === 'account' &&
        query.queryKey[1] === (identityKey ?? null) &&
        (reader.resource === 'availability'
          ? query.queryKey[2] === 'availability'
          : ['tournament', 'match'].includes(String(query.queryKey[2]))),
    }
    await client.cancelQueries(related)
    if (captureOwner()) await client.invalidateQueries(related)
  }, [client, identityKey, reader.resource, captureOwner])
  return {
    state: query.data ?? null,
    loading: query.isPending || query.isFetching,
    error:
      query.error || query.failureReason ? errorMessage(query.error ?? query.failureReason) : '',
    refresh,
    refreshRelated,
    captureOwner,
  }
}

/** Mutations are never replayed; refresh even when the response was lost. */
export function useResourceMutation<T>(
  read: ReturnType<typeof useResourceQuery<T>>,
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>,
) {
  const mutation = useMutation({
    mutationFn: (operation: () => Promise<unknown>) => operation(),
    retry: false,
  })
  const [writeError, setWriteError] = useState<{
    owner: ResourceOwner | null
    message: string
  }>({ owner: null, message: '' })
  const lock = useRef<{ token: symbol; owner: ResourceOwner } | null>(null)
  const clearError = () => {
    const owner = read.captureOwner()
    if (owner) setWriteError({ owner, message: '' })
  }
  const refresh = async () => {
    clearError()
    await read.refresh()
  }
  const run = async (operation: () => Promise<unknown>): Promise<boolean> => {
    const owner = read.captureOwner()
    if (!owner || lock.current?.owner === owner) return false
    const isCurrent = () => read.captureOwner() === owner
    const token = Symbol('resource mutation')
    lock.current = { token, owner }
    setWriteError({ owner, message: '' })
    let completed = false
    let failure = ''
    try {
      const success = await mutate(async () => {
        if (!isCurrent()) throw new ApiError(409, 'session_changed')
        try {
          const result = await mutation.mutateAsync(() => {
            if (!isCurrent()) throw new ApiError(409, 'session_changed')
            return operation()
          })
          completed = true
          return result
        } catch (cause) {
          failure = errorMessage(cause)
          throw cause
        }
      })
      if (!isCurrent()) return false
      if (!success && !failure) failure = 'The action could not complete. Review the latest state.'
      setWriteError({ owner, message: failure })
      await read.refreshRelated()
      return isCurrent() && success && completed
    } catch (cause) {
      if (!isCurrent()) return false
      setWriteError({ owner, message: failure || errorMessage(cause) })
      await read.refreshRelated()
      return false
    } finally {
      if (lock.current?.token === token) lock.current = null
    }
  }

  const message = writeError.owner === read.captureOwner() ? writeError.message : ''
  return {
    state: read.state,
    loading: read.loading,
    error: message || read.error,
    readError: read.error,
    writeError: message,
    clearError,
    refresh,
    run,
  }
}
