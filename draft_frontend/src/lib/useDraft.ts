import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, errorMessage, subscribeLeague } from './api'
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
  const [busy, setBusy] = useState(false)
  const [streamEpoch, setStreamEpoch] = useState(0)
  const generation = useRef(0)
  const mounted = useRef(true)
  const currentMode = useRef(mode)
  const currentLeague = useRef<number | null>(null)
  const currentSnapshot = useRef<LeagueSnapshot | null>(null)
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
    setBusy(false)
    setConnection('closed')
    return token
  }, [])

  const enterGuest = useCallback(() => {
    invalidate()
    currentMode.current = 'disconnected'
    currentLeague.current = null
    currentSnapshot.current = null
    setMode('disconnected')
    setSession(null)
    setCatalog([])
    setLeagues([])
    setUsers([])
    setLeagueId(null)
    setSnapshot(null)
    setError('')
  }, [invalidate])

  const connect = useCallback(
    async (authenticated?: Session, preferredLeague?: number | null) => {
      const token = invalidate()
      const abort = new AbortController()
      cancelConnect.current = abort
      currentMode.current = 'loading'
      setBusy(true)
      try {
        const current = authenticated ?? (await api.session(abort.signal))
        if (!isCurrent(token)) return
        const [items, leagueList, userList] = await Promise.all([
          api.draftables(abort.signal),
          api.leagues(abort.signal),
          api.users(abort.signal),
        ])
        if (!isCurrent(token)) return
        currentMode.current = 'live'
        currentLeague.current =
          leagueList.find((league) => league.id === preferredLeague)?.id ??
          leagueList[0]?.id ??
          null
        currentSnapshot.current = null
        setSession(current)
        setCatalog(items)
        setLeagues(leagueList)
        setUsers(userList)
        setSnapshot(null)
        setLeagueId(currentLeague.current)
        setMode('live')
        setError('')
        // Reconnecting the same account and league still needs a new event stream.
        setStreamEpoch((value) => value + 1)
      } catch (cause) {
        if (!isCurrent(token)) return
        enterGuest()
        if (!(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause))
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
    (next: LeagueSnapshot, token: number, id: number) => {
      if (
        !isCurrent(token) ||
        currentMode.current !== 'live' ||
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
      setLeagues((previous) =>
        previous.some((item) => item.id === id)
          ? previous.map((item) => (item.id === id ? next.league : item))
          : [...previous, next.league],
      )
    },
    [isCurrent],
  )

  const expireSession = useCallback(() => {
    enterGuest()
    setError('Your session ended. Sign in again to return to your league.')
  }, [enterGuest])

  useEffect(() => {
    if (mode !== 'live' || currentMode.current !== 'live' || leagueId === null) return
    const token = generation.current
    const abort = new AbortController()
    let active = true
    const valid = () => active && isCurrent(token)
    const revoked = () => {
      if (!valid()) return
      invalidate()
      currentLeague.current = null
      currentSnapshot.current = null
      setSnapshot(null)
      setLeagueId(null)
      setLeagues((previous) => previous.filter((item) => item.id !== leagueId))
      setError('You no longer have access to that league.')
    }
    const refresh = async () => {
      try {
        const next = await api.snapshot(leagueId, abort.signal)
        if (valid()) acceptSnapshot(next, token, leagueId)
      } catch (cause) {
        if (!valid() || abort.signal.aborted) return
        if (cause instanceof ApiError && cause.status === 401) expireSession()
        else if (cause instanceof ApiError && cause.status === 404) revoked()
        else setError(errorMessage(cause))
      }
    }
    void refresh()
    const stop = subscribeLeague(leagueId, {
      onSnapshot: (next) => {
        if (valid()) acceptSnapshot(next, token, leagueId)
      },
      onChanged: (event) => {
        if (valid() && event.league_id === leagueId) void refresh()
      },
      onStatus: (status) => {
        if (valid()) setConnection(status)
      },
      onSignedOut: () => {
        if (valid()) expireSession()
      },
      onAccessRevoked: revoked,
      onError: (cause) => {
        if (valid()) setError(errorMessage(cause))
      },
    })
    const cleanup = () => {
      active = false
      abort.abort()
      stop()
    }
    stopStream.current = cleanup
    return () => {
      cleanup()
      if (stopStream.current === cleanup) stopStream.current = null
    }
  }, [
    mode,
    leagueId,
    session?.user.id,
    streamEpoch,
    acceptSnapshot,
    expireSession,
    invalidate,
    isCurrent,
  ])

  const selectLeague = (id: number) => {
    if (currentMode.current !== 'live') return
    invalidate()
    currentLeague.current = id
    currentSnapshot.current = null
    setSnapshot(null)
    setError('')
    setLeagueId(id)
    setStreamEpoch((value) => value + 1)
  }

  const refreshLeagueList = async (token: number) => {
    const list = await api.leagues()
    if (!isCurrent(token)) return
    setLeagues((previous) =>
      list.map((item) => {
        const newer = previous.find((old) => old.id === item.id && old.revision > item.revision)
        return newer ?? item
      }),
    )
  }

  const mutate = async (operation: () => Promise<unknown>) => {
    if (mutationLock.current || currentMode.current !== 'live') return
    const owner = Symbol('mutation')
    mutationLock.current = owner
    setBusy(true)
    setError('')
    const token = generation.current
    const id = currentLeague.current
    try {
      await operation()
      if (isCurrent(token) && id !== null) acceptSnapshot(await api.snapshot(id), token, id)
    } catch (cause) {
      if (!isCurrent(token)) return
      if (cause instanceof ApiError && cause.status === 401) expireSession()
      else {
        setError(errorMessage(cause))
        // Refresh even after an unknown outcome. Never replay a write automatically.
        if (id !== null) {
          try {
            acceptSnapshot(await api.snapshot(id), token, id)
          } catch (refreshError) {
            if (isCurrent(token) && refreshError instanceof ApiError && refreshError.status === 401)
              expireSession()
          }
        }
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
    setMode('loading')
    setBusy(true)
    setSession(null)
    setSnapshot(null)
    setLeagueId(null)
    setLeagues([])
    setCatalog([])
    setUsers([])
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
    try {
      const result = await api.createLeague({
        p_name: name,
        p_roster_size: size,
        p_pick_seconds: seconds,
      })
      if (!isCurrent(token)) return false
      // A confirmed creation remains a success even when the subsequent list read fails.
      let refreshError = ''
      try {
        await refreshLeagueList(token)
      } catch (cause) {
        refreshError = `Your league was created. ${errorMessage(cause)}`
      }
      if (!isCurrent(token)) return false
      selectLeague(result.result)
      if (refreshError) setError(refreshError)
      return true
    } catch (cause) {
      if (!isCurrent(token)) return false
      if (cause instanceof ApiError && cause.status === 401) expireSession()
      else {
        setError(`${errorMessage(cause)} Check the league list before trying again.`)
        try {
          await refreshLeagueList(token)
        } catch {
          /* original error retained */
        }
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
    setError,
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
