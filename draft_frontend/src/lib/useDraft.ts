import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage, subscribeAccount } from './api'
import type { ConnectionStatus, Draftable, League, LeagueSnapshot, Session, User } from './types'

export function useDraft() {
  const [mode, setMode] = useState<'loading' | 'live' | 'disconnected'>('loading')
  const [session, setSession] = useState<Session | null>(null)
  const [catalog, setCatalog] = useState<Draftable[]>([])
  const [leagues, setLeagues] = useState<League[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [leagueId, setLeagueId] = useState<number | null>(null)
  const [snapshot, setSnapshot] = useState<LeagueSnapshot | null>(null)
  const [connection, setConnection] = useState<ConnectionStatus>('closed')
  const [error, setError] = useState('')
  const [sessionCheckFailed, setSessionCheckFailed] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [streamEpoch, setStreamEpoch] = useState(0)
  const generation = useRef(0)
  const selectionGeneration = useRef(0)
  const mounted = useRef(true)
  const currentMode = useRef(mode)
  const currentLeague = useRef<number | null>(null)
  const currentSnapshot = useRef<LeagueSnapshot | null>(null)
  const currentLeagues = useRef<League[]>([])
  const referenceDataReady = useRef(false)
  const leagueListReady = useRef(false)
  const refreshAccount = useRef<(() => Promise<void>) | null>(null)
  const stopStream = useRef<(() => void) | null>(null)
  const cancelConnect = useRef<AbortController | null>(null)
  const mutationLock = useRef<symbol | null>(null)

  const isCurrent = useCallback(
    (token: number) => mounted.current && token === generation.current,
    [],
  )

  // Invalidate synchronously: old requests must stop being authoritative before React renders.
  const invalidate = useCallback(() => {
    const token = ++generation.current
    cancelConnect.current?.abort()
    cancelConnect.current = null
    stopStream.current?.()
    stopStream.current = null
    mutationLock.current = null
    refreshAccount.current = null
    setBusy(false)
    setConnection('closed')
    return token
  }, [])

  const enterGuest = useCallback(() => {
    invalidate()
    currentMode.current = 'disconnected'
    currentLeague.current = null
    currentSnapshot.current = null
    currentLeagues.current = []
    setMode('disconnected')
    setSession(null)
    setCatalog([])
    setLeagues([])
    setUsers([])
    setLeagueId(null)
    setSnapshot(null)
    setError('')
    setSessionCheckFailed(false)
    setNotice('')
  }, [invalidate])

  const connect = useCallback(
    async (authenticated?: Session, preferredLeague?: number | null) => {
      const token = invalidate()
      const abort = new AbortController()
      cancelConnect.current = abort
      currentMode.current = 'loading'
      currentLeague.current = null
      currentSnapshot.current = null
      currentLeagues.current = []
      referenceDataReady.current = false
      leagueListReady.current = false
      setMode('loading')
      setSession(null)
      setSnapshot(null)
      setLeagueId(null)
      setLeagues([])
      setCatalog([])
      setUsers([])
      setNotice('')
      setSessionCheckFailed(false)
      setBusy(true)
      try {
        const current = authenticated ?? (await api.session(abort.signal))
        if (!isCurrent(token)) return
        currentMode.current = 'live'
        currentLeague.current = preferredLeague ?? null
        setSession(current)
        setLeagueId(currentLeague.current)
        setMode('live')
        setError('')
        setNotice('')
        // Reconnecting the same account and league still needs a new event stream.
        setStreamEpoch((value) => value + 1)
      } catch (cause) {
        if (!isCurrent(token)) return
        enterGuest()
        if (!(cause instanceof ApiError && cause.status === 401)) {
          setSessionCheckFailed(true)
          setError(errorMessage(cause))
        }
        throw cause
      } finally {
        if (isCurrent(token)) {
          cancelConnect.current = null
          setBusy(false)
        }
      }
    },
    [enterGuest, invalidate, isCurrent],
  )

  useEffect(() => {
    mounted.current = true
    void connect().catch(() => {
      /* connect already displayed the signed-out state. */
    })
    return () => {
      mounted.current = false
      generation.current++
      cancelConnect.current?.abort()
      stopStream.current?.()
      stopStream.current = null
    }
  }, [connect])

  const acceptSnapshot = useCallback(
    (next: LeagueSnapshot, token: number, id: number, selection: number) => {
      if (
        !isCurrent(token) ||
        currentMode.current !== 'live' ||
        selectionGeneration.current !== selection ||
        currentLeague.current !== id ||
        next.league_id !== id
      )
        return
      const previous = currentSnapshot.current
      if (
        previous?.league_id === id &&
        (previous.revision > next.revision ||
          (previous.revision === next.revision &&
            Date.parse(previous.server_now) > Date.parse(next.server_now)))
      )
        return
      currentSnapshot.current = next
      setSnapshot(next)
      currentLeagues.current = currentLeagues.current.map((item) =>
        item.id === id && item.revision <= next.revision ? next.league : item,
      )
      setLeagues(currentLeagues.current)
    },
    [isCurrent],
  )

  const expireSession = useCallback(() => {
    enterGuest()
    setError('Your session ended. Sign in again to return to your league.')
  }, [enterGuest])

  useEffect(() => {
    if (mode !== 'live' || currentMode.current !== 'live' || !session) return
    const token = generation.current
    const abort = new AbortController()
    let active = true
    const valid = () => active && isCurrent(token)
    let retry: ReturnType<typeof setTimeout> | undefined
    let expiry: ReturnType<typeof setTimeout> | undefined
    let refreshError = ''
    let failures = 0
    let pending = false
    let running: Promise<void> | null = null
    const choose = (id: number | null) => {
      if (currentLeague.current === id) return
      selectionGeneration.current++
      currentLeague.current = id
      currentSnapshot.current = null
      setLeagueId(id)
      setSnapshot(null)
    }
    const scheduleRetry = () => {
      clearTimeout(retry)
      retry = setTimeout(
        () => void refresh(),
        Math.min(1000 * 2 ** Math.min(failures++, 5), 30_000),
      )
    }
    const drain = async () => {
      while (pending && valid()) {
        pending = false
        clearTimeout(retry)
        let id: number | null = null
        let selection = selectionGeneration.current
        try {
          const [list, reference] = await Promise.all([
            api.leagues(abort.signal),
            referenceDataReady.current
              ? null
              : Promise.all([api.draftables(abort.signal), api.users(abort.signal)]),
          ])
          if (!valid()) return
          // A list begun before a selection (including a just-created league) may
          // omit it. Let the queued pass decide access using a fresh transaction.
          if (selection !== selectionGeneration.current) {
            pending = true
            continue
          }
          if (reference) {
            setCatalog(reference[0])
            setUsers(reference[1])
            referenceDataReady.current = true
          }
          const previous = currentLeagues.current
          const added = list.filter((item) => !previous.some((old) => old.id === item.id))
          currentLeagues.current = list.map((item) => {
            const newer = previous.find((old) => old.id === item.id && old.revision > item.revision)
            return newer ?? item
          })
          setLeagues(currentLeagues.current)
          if (leagueListReady.current && added.length) {
            setNotice(
              added.length === 1
                ? `You now have access to ${added[0].name}.`
                : `You now have access to ${added.length} new leagues.`,
            )
          }
          leagueListReady.current = true
          if (!list.some((item) => item.id === currentLeague.current)) {
            choose(list[0]?.id ?? null)
          }
          id = currentLeague.current
          selection = selectionGeneration.current
          if (id !== null) {
            const next = await api.snapshot(id, abort.signal)
            if (valid()) acceptSnapshot(next, token, id, selection)
          }
          if (!valid()) return
          failures = 0
          setError((previousError) => (previousError === refreshError ? '' : previousError))
        } catch (cause) {
          if (!valid() || abort.signal.aborted) return
          if (cause instanceof ApiError && cause.status === 401) {
            expireSession()
            return
          }
          if (selection !== selectionGeneration.current) continue
          if (cause instanceof ApiError && cause.status === 404 && id !== null) {
            // Membership can change between the list and snapshot transactions.
            currentLeagues.current = currentLeagues.current.filter((item) => item.id !== id)
            setLeagues(currentLeagues.current)
            choose(currentLeagues.current[0]?.id ?? null)
            setNotice('Your league access changed. The available leagues have been updated.')
            scheduleRetry()
          } else {
            refreshError = errorMessage(cause)
            setError(refreshError)
            if (
              !(cause instanceof ApiError) ||
              cause.status >= 500 ||
              cause.status === 408 ||
              cause.status === 429
            ) {
              scheduleRetry()
            }
          }
        }
      }
    }
    const refresh = (): Promise<void> => {
      if (!valid()) return Promise.resolve()
      pending = true
      clearTimeout(retry)
      // A burst during a read becomes one additional pass. No concurrent list/snapshot reads.
      if (!running) {
        running = drain().finally(() => {
          running = null
          if (pending && valid()) void refresh()
        })
      }
      return running
    }
    refreshAccount.current = refresh
    const stop = subscribeAccount({
      onInvalidate: () => void refresh(),
      onStatus: (status) => {
        if (valid()) setConnection(status)
      },
    })
    const checkExpiry = () => {
      if (!valid()) return
      const remaining = session.expires_at * 1000 - Date.now()
      if (remaining <= 0) expireSession()
      else expiry = setTimeout(checkExpiry, Math.min(remaining, 2_147_483_647))
    }
    checkExpiry()
    const cleanup = () => {
      active = false
      clearTimeout(retry)
      clearTimeout(expiry)
      abort.abort()
      stop()
      if (refreshAccount.current === refresh) refreshAccount.current = null
    }
    stopStream.current = cleanup
    return () => {
      cleanup()
      if (stopStream.current === cleanup) stopStream.current = null
    }
  }, [mode, session, streamEpoch, acceptSnapshot, expireSession, isCurrent])

  const selectLeague = (id: number) => {
    if (currentMode.current !== 'live' || currentLeague.current === id) return
    selectionGeneration.current++
    mutationLock.current = null
    setBusy(false)
    currentLeague.current = id
    currentSnapshot.current = null
    setSnapshot(null)
    setError('')
    setLeagueId(id)
    return refreshAccount.current?.()
  }

  const mutate = async (operation: () => Promise<unknown>) => {
    if (mutationLock.current || currentMode.current !== 'live') return
    const owner = Symbol('mutation')
    mutationLock.current = owner
    setBusy(true)
    setError('')
    const token = generation.current
    const selection = selectionGeneration.current
    try {
      await operation()
      if (isCurrent(token)) await refreshAccount.current?.()
    } catch (cause) {
      if (!isCurrent(token)) return
      if (cause instanceof ApiError && cause.status === 401) expireSession()
      else if (selectionGeneration.current === selection) {
        setError(errorMessage(cause))
        // Refresh even after an unknown outcome. Never replay a write automatically.
        await refreshAccount.current?.()
      }
    } finally {
      if (mutationLock.current === owner) {
        mutationLock.current = null
        if (mounted.current) setBusy(false)
      }
    }
  }

  const draftAction = async (kind: 'start' | 'pick' | 'undo', id?: number, chargePoints = true) => {
    const current = currentSnapshot.current
    if (!current) return
    // Use the turn the UI rendered, never silently move an action to a newer turn.
    if (
      !snapshot ||
      snapshot.league_id !== current.league_id ||
      snapshot.revision !== current.revision
    ) {
      setError('The draft changed. Review the latest turn before making this selection.')
      return
    }
    const revision = { p_league: snapshot.league_id, p_revision: snapshot.revision }
    await mutate(() =>
      kind === 'pick'
        ? api.draftAction({
            ...revision,
            p_kind: kind,
            p_draftable: id!,
            p_charge_points: chargePoints,
          })
        : api.draftAction({ ...revision, p_kind: kind }),
    )
  }

  const refreshData = async () => {
    if (mutationLock.current || currentMode.current !== 'live') return
    try {
      // A SQL fixture may add/remove leagues or replace their state. Reconnect
      // all reads, including the stream, rather than retaining old revisions.
      await connect(undefined, currentLeague.current)
    } catch {
      /* connect has already displayed the failure. */
    }
  }

  const logout = async () => {
    if (currentMode.current !== 'live') return
    const token = invalidate()
    currentMode.current = 'loading'
    currentLeague.current = null
    currentSnapshot.current = null
    currentLeagues.current = []
    setMode('loading')
    setBusy(true)
    setSession(null)
    setSnapshot(null)
    setLeagueId(null)
    setLeagues([])
    setCatalog([])
    setUsers([])
    setNotice('')
    try {
      await api.logout()
      if (isCurrent(token)) enterGuest()
    } catch (cause) {
      if (!isCurrent(token)) return
      enterGuest()
      if (!(cause instanceof ApiError && cause.status === 401)) {
        setError(
          `Sign-out could not be confirmed. Your browser session may still be active. ${errorMessage(cause)}`,
        )
      }
    }
  }

  const createLeague = async (name: string, size: number, seconds: number) => {
    if (mutationLock.current || currentMode.current !== 'live') return false
    const owner = Symbol('create')
    mutationLock.current = owner
    setBusy(true)
    setError('')
    const token = generation.current
    const selection = selectionGeneration.current
    try {
      const result = await api.createLeague({
        p_name: name,
        p_roster_size: size,
        p_pick_seconds: seconds,
      })
      if (!isCurrent(token)) return false
      // A confirmed creation remains a success even when the subsequent list read fails.
      setNotice('Your league was created.')
      if (selectionGeneration.current === selection) await selectLeague(result.result)
      else await refreshAccount.current?.()
      return isCurrent(token)
    } catch (cause) {
      if (!isCurrent(token)) return false
      if (cause instanceof ApiError && cause.status === 401) expireSession()
      else if (selectionGeneration.current === selection) {
        setError(`${errorMessage(cause)} Check the league list before trying again.`)
        await refreshAccount.current?.()
      }
      return false
    } finally {
      if (mutationLock.current === owner) {
        mutationLock.current = null
        if (mounted.current) setBusy(false)
      }
    }
  }

  return {
    mode,
    session,
    catalog,
    leagues,
    users,
    leagueId,
    snapshot,
    connection,
    error,
    sessionCheckFailed,
    setError,
    notice,
    setNotice,
    busy,
    connect,
    selectLeague,
    mutate,
    draftAction,
    refreshData,
    logout,
    createLeague,
  }
}
