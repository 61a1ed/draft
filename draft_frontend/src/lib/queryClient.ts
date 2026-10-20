import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './api'

export const QUERY_POLL_INTERVAL = 30_000
type Identity = string | number | null | undefined

// Include the account in every private key, even when resource IDs are globally unique.
export const queryKeys = {
  session: ['session'] as const,
  account: (identity: Identity) => ['account', identity ?? null] as const,
  leagues: (identity: Identity) => ['account', identity ?? null, 'leagues'] as const,
  catalog: (identity: Identity) => ['account', identity ?? null, 'catalog'] as const,
  users: (identity: Identity) => ['account', identity ?? null, 'users'] as const,
  draft: (identity: Identity, leagueId: number | null) =>
    ['account', identity ?? null, 'draft', leagueId] as const,
  favorites: (identity: Identity) => ['account', identity ?? null, 'favorites'] as const,
  favoritesLists: (identity: Identity) =>
    ['account', identity ?? null, 'favorites', 'lists'] as const,
  favoritesList: (identity: Identity, listId: number | null) =>
    ['account', identity ?? null, 'favorites', 'list', listId] as const,
  tournament: (identity: Identity, leagueId: number) =>
    ['account', identity ?? null, 'tournament', leagueId] as const,
  match: (identity: Identity, matchId: number) =>
    ['account', identity ?? null, 'match', matchId] as const,
  availability: (identity: Identity, teamId: number) =>
    ['account', identity ?? null, 'availability', teamId] as const,
}

export function queryRetry(failures: number, cause: unknown) {
  return (
    failures < 2 &&
    (cause instanceof TypeError ||
      (cause instanceof ApiError && (cause.status >= 500 || [408, 429].includes(cause.status))))
  )
}

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: queryRetry,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
      },
      // A response can be lost after the transaction commits. Never replay a write.
      mutations: { retry: false, networkMode: 'always' },
    },
  })
}
