import { api, ApiError } from './api'
import type { MatchDetail, TournamentSnapshot } from './tournamentTypes'
import { useResourceMutation, useResourceQuery, type ResourceReadOptions } from './useResourceQuery'

const tournamentReader = {
  resource: 'tournament' as const,
  read: (id: number, signal: AbortSignal) => api.tournament(id, signal),
  accepts: (next: TournamentSnapshot, previous: TournamentSnapshot | null, id: number) => {
    if (next.league_id !== id || (previous && next.revision < previous.revision)) return false
    // VOD changes advance only the match revision, so equal tournament revisions
    // must still be accepted. Never regress any already-observed match either.
    return !previous?.matches.some((old) => {
      const current = next.matches.find((match) => match.id === old.id)
      return !current || current.revision < old.revision
    })
  },
  isEmpty: (cause: unknown) =>
    cause instanceof ApiError && cause.status === 404 && cause.code === 'tournament_not_found',
}

const matchReader = {
  resource: 'match' as const,
  read: (id: number, signal: AbortSignal) => api.match(id, signal),
  accepts: (next: MatchDetail, previous: MatchDetail | null, id: number) =>
    next.id === id && (!previous || next.revision >= previous.revision),
}

export interface UseTournamentOptions extends ResourceReadOptions {
  leagueId: number
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

export function useTournament({ leagueId, mutate, ...options }: UseTournamentOptions) {
  const read = useResourceQuery(leagueId, options, tournamentReader)
  return useResourceMutation(read, mutate)
}

export interface UseTournamentMatchOptions extends ResourceReadOptions {
  matchId: number
}

export function useTournamentMatch({ matchId, ...options }: UseTournamentMatchOptions) {
  const read = useResourceQuery(matchId, options, matchReader)
  return { match: read.state, loading: read.loading, error: read.error, refresh: read.refresh }
}
