import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, errorMessage, subscribeDraft } from './api'
import { QUERY_POLL_INTERVAL, queryKeys } from './queryClient'
import type { ConnectionStatus, League, LeagueSnapshot, Session } from './types'

export function useDraft() {
  const client = useQueryClient()
  const [authEnabled, setAuthEnabled] = useState(true)
  const [authPending, setAuthPending] = useState(false)
  const [authEpoch, setAuthEpoch] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [selectionEpoch, setSelectionEpoch] = useState(0)
  const [connection, setConnection] = useState<ConnectionStatus>('closed')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const generation = useRef(0)
  const selectionGeneration = useRef(0)
  const mounted = useRef(true)
  const selected = useRef<number | null>(null)
  const active = useRef(false)
  const stopStream = useRef<(() => void) | null>(null)
  const lock = useRef<symbol | null>(null)
  const [busy, setBusy] = useState(false)
  const knownLeagues = useRef<League[] | null>(null)

  const sessionQuery = useQuery({
    queryKey: queryKeys.session,
    queryFn: ({ signal }) => api.session(signal),
    enabled: authEnabled,
    staleTime: 60_000,
    refetchInterval: 60_000,
  })
  const session = authEnabled && !authPending ? (sessionQuery.data ?? null) : null
  const mode =
    authPending || (authEnabled && sessionQuery.isPending)
      ? 'loading'
      : session
        ? 'live'
        : 'disconnected'
  active.current = mode === 'live'
  const userId = session?.user.id ?? null
  const owner = useMemo(() => ({ active: true }), [userId, authEpoch])
  const currentOwner = useRef(owner)
  currentOwner.current = owner
  useEffect(() => {
    owner.active = true
    lock.current = null
    setBusy(false)
    return () => {
      owner.active = false
    }
  }, [owner])
  const ownsAccount = () => owner.active && currentOwner.current === owner && active.current
  const enabled = mode === 'live'
  const leaguesQuery = useQuery({
    queryKey: queryKeys.leagues(userId),
    queryFn: ({ signal }) => api.leagues(signal),
    enabled,
    refetchInterval: QUERY_POLL_INTERVAL,
    structuralSharing: (old, next) => {
      const previous = old as League[] | undefined
      return (next as League[]).map((league) => {
        const cached = previous?.find((item) => item.id === league.id)
        return cached && cached.revision > league.revision ? cached : league
      })
    },
  })
  const catalogQuery = useQuery({
    queryKey: queryKeys.catalog(userId),
    queryFn: ({ signal }) => api.draftables(signal),
    enabled,
    staleTime: 300_000,
    refetchInterval: 300_000,
  })
  const usersQuery = useQuery({
    queryKey: queryKeys.users(userId),
    queryFn: ({ signal }) => api.users(signal),
    enabled,
    refetchInterval: QUERY_POLL_INTERVAL,
  })
  const leagueId = enabled ? selectedId : null
  const snapshotQuery = useQuery({
    queryKey: queryKeys.draft(userId, leagueId),
    queryFn: async ({ signal }) => {
      const next = await api.snapshot(leagueId!, signal)
      if (next.league_id !== leagueId) throw new ApiError(502, 'invalid_response')
      return next
    },
    enabled: enabled && leagueId !== null,
    refetchInterval: QUERY_POLL_INTERVAL,
    structuralSharing: (old, next) => {
      const previous = old as LeagueSnapshot | undefined
      const value = next as LeagueSnapshot
      return previous &&
        (previous.revision > value.revision ||
          (previous.revision === value.revision &&
            Date.parse(previous.server_now) > Date.parse(value.server_now)))
        ? previous
        : value
    },
  })
  const snapshot =
    enabled &&
    leagueId !== null &&
    !(snapshotQuery.error instanceof ApiError && [401, 404].includes(snapshotQuery.error.status))
      ? (snapshotQuery.data ?? null)
      : null

  const invalidateAuthority = useCallback(() => {
    generation.current++
    selectionGeneration.current++
    active.current = false
    stopStream.current?.()
    stopStream.current = null
    lock.current = null
    knownLeagues.current = null
    setBusy(false)
    setConnection('closed')
    setAuthEpoch(generation.current)
    void client.cancelQueries()
    client.removeQueries({ queryKey: ['account'] })
    client.getMutationCache().clear()
    return generation.current
  }, [client])

  const enterGuest = useCallback(
    (message = '') => {
      invalidateAuthority()
      setAuthEnabled(false)
      setAuthPending(false)
      client.removeQueries({ queryKey: queryKeys.session })
      selected.current = null
      setSelectedId(null)
      setError(message)
      setNotice('')
    },
    [client, invalidateAuthority],
  )
  const expireSession = useCallback(() => {
    enterGuest('Your session ended. Sign in again to return to your league.')
  }, [enterGuest])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      generation.current++
      stopStream.current?.()
    }
  }, [])

  // Any authenticated query can discover expiry, including another feature's reads.
  useEffect(
    () =>
      client.getQueryCache().subscribe((event) => {
        if (event.type !== 'updated' || event.action.type !== 'error' || !active.current) return
        const cause = event.query.state.error
        if (cause instanceof ApiError && cause.status === 401) expireSession()
      }),
    [client, expireSession],
  )

  useEffect(() => {
    if (!session) return
    let timer: ReturnType<typeof setTimeout>
    const check = () => {
      const remaining = session.expires_at * 1000 - Date.now()
      if (remaining <= 0) expireSession()
      else timer = setTimeout(check, Math.min(remaining, 2_147_483_647))
    }
    check()
    return () => clearTimeout(timer)
  }, [session, expireSession])

  const previousUser = useRef(userId)
  useEffect(() => {
    if (previousUser.current !== userId) {
      const oldUser = previousUser.current
      previousUser.current = userId
      if (oldUser !== null) {
        client.removeQueries({ queryKey: queryKeys.account(oldUser) })
        client.getMutationCache().clear()
        knownLeagues.current = null
      }
    }
  }, [client, userId])

  useEffect(() => {
    if (!enabled || !leaguesQuery.data) return
    const list = leaguesQuery.data
    const previous = knownLeagues.current
    if (previous) {
      const added = list.filter((item) => !previous.some((old) => old.id === item.id))
      if (added.length)
        setNotice(
          added.length === 1
            ? `You now have access to ${added[0].name}.`
            : `You now have access to ${added.length} new leagues.`,
        )
    }
    knownLeagues.current = list
    if (!list.some((item) => item.id === selected.current)) {
      selectionGeneration.current++
      selected.current = list[0]?.id ?? null
      setSelectedId(selected.current)
    }
  }, [enabled, leaguesQuery.data])

  useEffect(() => {
    if (!snapshot) return
    if (
      !client
        .getQueryData<League[]>(queryKeys.leagues(userId))
        ?.some((item) => item.id === snapshot.league_id && item.revision < snapshot.revision)
    )
      return
    client.setQueryData<League[]>(queryKeys.leagues(userId), (list) =>
      list?.map((item) =>
        item.id === snapshot.league_id && item.revision < snapshot.revision
          ? snapshot.league
          : item,
      ),
    )
  }, [client, snapshot, userId])

  useEffect(() => {
    if (
      !enabled ||
      !(snapshotQuery.error instanceof ApiError) ||
      snapshotQuery.error.status !== 404
    )
      return
    setNotice('Your league access changed. The available leagues have been updated.')
    void client.invalidateQueries({ queryKey: queryKeys.leagues(userId) })
  }, [client, enabled, snapshotQuery.error, userId])

  // A stream is only a draft wake-up. HTTP remains the authoritative, cancellable query.
  // A completed draft can reopen through undo, so its participants still need immediate wake-ups.
  const streamEnabled = enabled && leagueId !== null
  useEffect(() => {
    if (!streamEnabled || leagueId === null) {
      setConnection('closed')
      return
    }
    const token = generation.current
    const selection = selectionGeneration.current
    let listening = true
    const valid = () =>
      listening &&
      mounted.current &&
      active.current &&
      currentOwner.current === owner &&
      owner.active &&
      generation.current === token &&
      selectionGeneration.current === selection
    const key = queryKeys.draft(userId, leagueId)
    const stop = subscribeDraft(leagueId, {
      onInvalidate: () => {
        if (!valid()) return
        // Cancel even a first read: an event may have committed after that read began.
        void client.cancelQueries({ queryKey: key, exact: true }).then(() => {
          if (valid()) return client.invalidateQueries({ queryKey: key, exact: true })
        })
      },
      onStatus: (status) => {
        if (!valid()) return
        setConnection(status)
        // EventSource hides HTTP status codes; a session read can explain a failed open.
        if (status === 'reconnecting')
          void client.invalidateQueries({ queryKey: queryKeys.session, exact: true })
      },
    })
    const cleanup = () => {
      listening = false
      stop()
    }
    stopStream.current = cleanup
    return () => {
      cleanup()
      if (stopStream.current === cleanup) stopStream.current = null
    }
  }, [client, streamEnabled, leagueId, userId, authEpoch, selectionEpoch, owner])

  const connect = async (authenticated?: Session, preferredLeague?: number | null) => {
    const token = invalidateAuthority()
    setAuthEnabled(false)
    setAuthPending(true)
    setError('')
    setNotice('')
    selected.current = preferredLeague ?? null
    setSelectedId(selected.current)
    client.removeQueries({ queryKey: queryKeys.session })
    try {
      if (authenticated) client.setQueryData(queryKeys.session, authenticated)
      else
        await client.fetchQuery({
          queryKey: queryKeys.session,
          queryFn: ({ signal }) => api.session(signal),
          staleTime: 0,
        })
      if (!mounted.current || generation.current !== token) return
      setAuthEnabled(true)
    } catch (cause) {
      if (!mounted.current || generation.current !== token) return
      // Keep the failure so the signed-out page offers retry for network failures.
      setAuthEnabled(true)
      throw cause
    } finally {
      if (mounted.current && generation.current === token) setAuthPending(false)
    }
  }

  const selectLeague = (id: number) => {
    if (!ownsAccount() || selected.current === id) return
    selectionGeneration.current++
    setSelectionEpoch(selectionGeneration.current)
    stopStream.current?.()
    stopStream.current = null
    void client.cancelQueries({ queryKey: queryKeys.draft(userId, selected.current), exact: true })
    selected.current = id
    setSelectedId(id)
    lock.current = null
    setBusy(false)
    setError('')
  }

  const mutation = useMutation({ mutationFn: (operation: () => Promise<unknown>) => operation() })
  const refreshLeague = async (id: number | null) => {
    await Promise.all([
      client.cancelQueries({ queryKey: queryKeys.leagues(userId) }),
      client.cancelQueries({ queryKey: queryKeys.draft(userId, id), exact: true }),
    ])
    if (!ownsAccount()) return
    await Promise.all([
      client.invalidateQueries({ queryKey: queryKeys.leagues(userId) }),
      client.invalidateQueries({ queryKey: queryKeys.draft(userId, id), exact: true }),
    ])
  }
  const mutate = async (operation: () => Promise<unknown>) => {
    if (lock.current || !ownsAccount()) return false
    const owner = Symbol('write')
    lock.current = owner
    const token = generation.current
    const selection = selectionGeneration.current
    const id = selected.current
    const valid = () => mounted.current && generation.current === token && ownsAccount()
    setBusy(true)
    setError('')
    try {
      await mutation.mutateAsync(() => {
        if (!valid() || selectionGeneration.current !== selection)
          throw new ApiError(409, 'session_changed')
        return operation()
      })
      if (valid()) await refreshLeague(id)
      return valid() && selectionGeneration.current === selection
    } catch (cause) {
      if (!valid()) return false
      if (cause instanceof ApiError && cause.status === 401) expireSession()
      else if (selectionGeneration.current === selection) {
        setError(errorMessage(cause))
        await refreshLeague(id)
      }
      return false
    } finally {
      if (lock.current === owner) {
        lock.current = null
        if (mounted.current) setBusy(false)
      }
    }
  }

  const draftAction = async (kind: 'start' | 'pick' | 'undo', id?: number, chargePoints = true) => {
    const latest = client.getQueryData<LeagueSnapshot>(queryKeys.draft(userId, selected.current))
    if (
      !snapshot ||
      !latest ||
      snapshot.league_id !== selected.current ||
      snapshot.revision !== latest.revision
    ) {
      setError('The draft changed. Review the latest turn before making this selection.')
      return false
    }
    const revision = { p_league: snapshot.league_id, p_revision: snapshot.revision }
    return mutate(() =>
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

  const logoutMutation = useMutation({ mutationFn: () => api.logout() })
  const logout = async () => {
    if (!ownsAccount()) return
    enterGuest()
    const token = generation.current
    setAuthPending(true)
    try {
      await logoutMutation.mutateAsync()
    } catch (cause) {
      if (
        mounted.current &&
        generation.current === token &&
        !(cause instanceof ApiError && cause.status === 401)
      )
        setError(
          `Sign-out could not be confirmed. Your browser session may still be active. ${errorMessage(cause)}`,
        )
    } finally {
      if (mounted.current && generation.current === token) setAuthPending(false)
    }
  }

  const createLeague = async (name: string, size: number, seconds: number) => {
    const token = generation.current
    const selection = selectionGeneration.current
    let created = false
    const success = await mutate(async () => {
      const result = await api.createLeague({
        p_name: name,
        p_roster_size: size,
        p_pick_seconds: seconds,
      })
      if (!ownsAccount() || generation.current !== token) return
      created = true
      setNotice('Your league was created.')
      // Retire an in-flight list from before the insert before changing selection.
      await client.cancelQueries({ queryKey: queryKeys.leagues(userId) })
      if (!ownsAccount() || generation.current !== token) return
      if (selectionGeneration.current === selection) {
        selected.current = result.result
        setSelectedId(result.result)
      }
    })
    return created && success
  }

  const refreshData = async () => {
    if (lock.current || !ownsAccount()) return
    try {
      await connect(undefined, selected.current)
    } catch {
      /* Query exposes the error. */
    }
  }

  const readError = [
    sessionQuery.error,
    leaguesQuery.error,
    snapshotQuery.error,
    catalogQuery.error,
    usersQuery.error,
  ].find(Boolean)
  const sessionCheckFailed =
    !session &&
    !authPending &&
    authEnabled &&
    sessionQuery.isError &&
    !(sessionQuery.error instanceof ApiError && sessionQuery.error.status === 401)
  return {
    mode,
    session,
    catalog: enabled ? (catalogQuery.data ?? []) : [],
    leagues: enabled ? (leaguesQuery.data ?? []) : [],
    users: enabled ? (usersQuery.data ?? []) : [],
    leagueId,
    snapshot,
    connection,
    error:
      error ||
      (sessionCheckFailed
        ? errorMessage(sessionQuery.error)
        : enabled && readError
          ? errorMessage(readError)
          : ''),
    sessionCheckFailed,
    setError,
    notice,
    setNotice,
    busy: busy || authPending,
    connect,
    selectLeague,
    mutate,
    draftAction,
    refreshData,
    logout,
    createLeague,
  }
}
