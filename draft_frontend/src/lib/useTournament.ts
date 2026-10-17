import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage } from './api'
import type { MatchDetail, TournamentSnapshot } from './tournamentTypes'

interface ReadOptions {
  refreshKey: number
  /** Change this when a mounted view changes account; a keyed parent also isolates accounts. */
  identityKey?: string | number
}

interface Reader<T> {
  read: (id: number, signal: AbortSignal) => Promise<T>
  accepts: (value: T, previous: T | null, id: number) => boolean
  isEmpty: (cause: unknown) => boolean
}

interface ReadState<T> {
  key: string
  value: T | null
  loading: boolean
  error: string
}

interface ReadOwner {
  key: string
  active: boolean
  refresh: () => Promise<void>
}

const tournamentReader: Reader<TournamentSnapshot> = {
  read: (id, signal) => api.tournament(id, signal),
  accepts: (next, previous, id) => {
    if (next.league_id !== id || (previous && next.revision < previous.revision)) return false
    // VOD changes advance only the match revision, so equal tournament revisions
    // must still be accepted. Never regress any already-observed match either.
    return !previous?.matches.some((old) => {
      const current = next.matches.find((match) => match.id === old.id)
      return !current || current.revision < old.revision
    })
  },
  isEmpty: (cause) =>
    cause instanceof ApiError && cause.status === 404 && cause.code === 'tournament_not_found',
}

const matchReader: Reader<MatchDetail> = {
  read: (id, signal) => api.match(id, signal),
  accepts: (next, previous, id) =>
    next.id === id && (!previous || next.revision >= previous.revision),
  isEmpty: () => false,
}

/** Serialize reads and coalesce wake-ups into one subsequent authoritative read. */
function useTournamentRead<T>(id: number, options: ReadOptions, reader: Reader<T>) {
  const key = JSON.stringify([options.identityKey ?? null, id])
  const selected = useRef(key)
  selected.current = key
  const owner = useRef<ReadOwner | null>(null)
  const [result, setResult] = useState<ReadState<T>>({
    key,
    value: null,
    loading: true,
    error: '',
  })

  useEffect(() => {
    const abort = new AbortController()
    let pending = false
    let running: Promise<void> | null = null
    let latest: T | null = null
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
          const next = await reader.read(id, abort.signal)
          if (!valid()) return
          if (reader.accepts(next, latest, id)) latest = next
          failures = 0
          setResult({ key, value: latest, loading: false, error: '' })
        } catch (cause) {
          if (!valid() || abort.signal.aborted) return
          if (reader.isEmpty(cause) && latest === null) {
            failures = 0
            setResult({ key, value: null, loading: false, error: '' })
          } else {
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
  }, [id, key, reader])

  const refresh = useCallback(async () => {
    if (selected.current === key && owner.current?.key === key) await owner.current.refresh()
  }, [key])

  useEffect(() => {
    void refresh()
  }, [refresh, options.refreshKey])

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
    key,
  }
}

export interface UseTournamentOptions extends ReadOptions {
  leagueId: number
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

export function useTournament({ leagueId, mutate, ...options }: UseTournamentOptions) {
  const read = useTournamentRead(leagueId, options, tournamentReader)
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
    const token = Symbol('tournament mutation')
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
      // Even a lost response may have committed. Fetch state without replaying the write.
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

  return {
    state: read.state,
    loading: read.loading,
    error: (writeError.owner === read.captureOwner() ? writeError.message : '') || read.error,
    readError: read.error,
    writeError: writeError.owner === read.captureOwner() ? writeError.message : '',
    clearError,
    refresh,
    run,
  }
}

export interface UseTournamentMatchOptions extends ReadOptions {
  matchId: number
}

export function useTournamentMatch({ matchId, ...options }: UseTournamentMatchOptions) {
  const read = useTournamentRead(matchId, options, matchReader)
  return { match: read.state, loading: read.loading, error: read.error, refresh: read.refresh }
}
