/** Stored endpoints are actual instants, always including an offset or Z. */
export interface AvailabilityWindowInput {
  starts_at: string
  ends_at: string
}

export interface AvailabilityWindow extends AvailabilityWindowInput {
  id: number
  team_id: number
}

export interface TeamAvailability {
  team_id: number
  time_zone: string
  display_step_minutes: number
  revision: number
}

export interface TeamAvailabilitySnapshot {
  team_id: number
  /** A team has no header until its first edit; its first saved revision is zero. */
  availability: TeamAvailability | null
  windows: AvailabilityWindow[]
}

export interface SetTeamAvailabilityInput {
  p_team: number
  p_revision: number | null
  p_time_zone: string
  p_display_step_minutes: number
  /** The complete replacement, including windows outside the displayed week. */
  p_windows: AvailabilityWindowInput[]
}

export interface CopyTeamAvailabilityWeekInput {
  p_team: number
  p_revision: number
  /** Calendar dates in the saved availability time zone, not UTC timestamps. */
  p_source: string
  p_target: string
}
