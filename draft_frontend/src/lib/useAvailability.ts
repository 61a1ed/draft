import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { TeamAvailabilitySnapshot } from './availabilityTypes'

export interface UseAvailabilityOptions {
  teamId: number
  refreshKey: number
  /** Change this when the signed-in account changes, even for the same team. */
  identityKey?: string | number
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

interface ReadOwner {
  key: string
  active: boolean
  refresh: () => Promise<void>
}

interface ReadState {
  key: string
  value: TeamAvailabilitySnapshot | null
  loading: boolean
  error: string
}

function accepts(
  next: TeamAvailabilitySnapshot,
  previous: TeamAvailabilitySnapshot | null,
  teamId: number,
) {
  if (next.team_id !== teamId || (next.availability && next.availability.team_id !== teamId)) {
    return false
  }
  // An absent header precedes revision zero. Once saved, it cannot become absent again.
  return (next.availability?.revision ?? -1) >= (previous?.availability?.revision ?? -1)
}

/** Account events only wake this reader; all state comes from authoritative HTTP reads. */
function useAvailabilityRead({
  teamId,
  refreshKey,
  identityKey,
}: Omit<UseAvailabilityOptions, 'mutate'>) {
  const key = JSON.stringify([identityKey ?? null, teamId])
  const selected = useRef(key)
  selected.current = key
  const owner = useRef<ReadOwner | null>(null)
  const [result, setResult] = useState<ReadState>({
    key,
    value: null,
    loading: true,
    error: '',
  })

  useEffect(() => {
    const abort = new AbortController()
    let pending = false
    let running: Promise<void> | null = null
    let latest: TeamAvailabilitySnapshot | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const current: ReadOwner = { key, active: true, refresh }
    owner.current = current
    const valid = () => current.active && selected.current === key && owner.current === current
    setResult({ key, value: null, loading: true, error: '' })

    async function drain() {
      while (pending && valid()) {
        pending = false
        clearTimeout(retry)
        setResult((old) => ({ ...old, loading: true }))
        try {
          const next = await api.teamAvailability(teamId, abort.signal)
          if (!valid()) return
          if (accepts(next, latest, teamId)) latest = next
          failures = 0
          setResult({ key, value: latest, loading: false, error: '' })
        } catch (cause) {
          if (!valid() || abort.signal.aborted) return
          setResult({ key, value: latest, loading: false, error: errorMessage(cause) })
          if (
            cause instanceof TypeError ||
            (cause instanceof ApiError &&
              (cause.status >= 500 || [408, 429].includes(cause.status)))
          ) {
            retry = setTimeout(
              () => void refresh(),
              Math.min(1000 * 2 ** Math.min(failures++, 5), 30_000),
            )
          }
        }
      }
    }

    function refresh(): Promise<void> {
      if (!valid()) return Promise.resolve()
      clearTimeout(retry)
      pending = true
      if (!running) {
        running = drain().finally(() => {
          running = null
        })
      }
      return running
    }

    return () => {
      current.active = false
      clearTimeout(retry)
      abort.abort()
      if (owner.current === current) owner.current = null
    }
  }, [teamId, key])

  const refresh = useCallback(async () => {
    if (selected.current === key && owner.current?.key === key) await owner.current.refresh()
  }, [key])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshKey])

  const captureOwner = useCallback(
    () =>
      selected.current === key && owner.current?.key === key && owner.current.active
        ? owner.current
        : null,
    [key],
  )

  return {
    state: result.key === key ? result.value : null,
    loading: result.key === key ? result.loading : true,
    error: result.key === key ? result.error : '',
    refresh,
    captureOwner,
  }
}

export function useAvailability({ mutate, ...options }: UseAvailabilityOptions) {
  const read = useAvailabilityRead(options)
  const [writeError, setWriteError] = useState<{ owner: ReadOwner | null; message: string }>({
    owner: null,
    message: '',
  })
  const lock = useRef<{ token: symbol; owner: ReadOwner } | null>(null)
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
    const token = Symbol('availability mutation')
    lock.current = { token, owner }
    setWriteError({ owner, message: '' })
    let completed = false
    let failure = ''
    try {
      const success = await mutate(async () => {
        if (!isCurrent()) throw new ApiError(409, 'session_changed')
        try {
          const result = await operation()
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
      // A failed response can still mean a committed edit. Read again; never replay a write.
      await read.refresh()
      return isCurrent() && success && completed
    } catch (cause) {
      if (!isCurrent()) return false
      setWriteError({ owner, message: failure || errorMessage(cause) })
      await read.refresh()
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
