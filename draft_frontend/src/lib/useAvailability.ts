import { api } from './api'
import type { TeamAvailabilitySnapshot } from './availabilityTypes'
import { useResourceMutation, useResourceQuery, type ResourceReadOptions } from './useResourceQuery'

export interface UseAvailabilityOptions extends ResourceReadOptions {
  teamId: number
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}

const availabilityReader = {
  resource: 'availability' as const,
  read: (id: number, signal: AbortSignal) => api.teamAvailability(id, signal),
  accepts: (
    next: TeamAvailabilitySnapshot,
    previous: TeamAvailabilitySnapshot | null,
    teamId: number,
  ) => {
    if (next.team_id !== teamId || (next.availability && next.availability.team_id !== teamId)) {
      return false
    }
    // An absent header precedes revision zero. Once saved, it cannot become absent again.
    return (next.availability?.revision ?? -1) >= (previous?.availability?.revision ?? -1)
  },
}

export function useAvailability({ teamId, mutate, ...options }: UseAvailabilityOptions) {
  const read = useResourceQuery(teamId, options, availabilityReader)
  return useResourceMutation(read, mutate)
}
