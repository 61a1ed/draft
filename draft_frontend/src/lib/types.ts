/** Public HTTP shapes from draft_backend/sql/schema.sql and its FastAPI gateway. */
export interface User {
  id: number
  display_name: string
}

export interface Session {
  user: User
  csrf_token: string
  /** Absolute Unix expiry, in seconds. */
  expires_at: number
}

export interface Draftable {
  id: number
  name: string
  point_cost: number
  dex: number | null
  species: string | null
  form: string
  image_url: string | null
}

export interface League {
  id: number
  name: string
  commissioner_user_id: number
  roster_size: number
  pick_seconds: number
  revision: number
  created_at: string
  started_at: string | null
  turn_started_at: string | null
}

export type DraftStatus = 'setup' | 'drafting' | 'completed'

export interface DraftState {
  league_id: number
  revision: number
  status: DraftStatus
  roster_size: number
  team_count: number
  active_pick_count: number
  required_pick_count: number
  can_start: boolean
  next_pick_number: number | null
  next_team_id: number | null
  last_pick_id: number | null
  deadline: string | null
}

export interface Team {
  id: number
  league_id: number
  name: string | null
  /** Opaque sRGB encoded as 0xRRGGBB; zero is a valid black color. */
  color_rgb: number | null
  metadata_complete: boolean
  /** Whether the current account may edit this team's name and color. */
  can_set_metadata: boolean
  manager_user_id: number
  draft_position: number | null
  point_budget: number
  points_spent: number
  points_remaining: number
}

export interface RosterPick {
  league_id: number
  team_id: number
  draftable_id: number
  pick_id: number
  pick_number: number
  selected_by: number
  selected_at: string
  /** The amount charged at selection, which may differ from today's catalog price. */
  point_cost: number
}

export interface Pick {
  id: number
  league_id: number
  pick_number: number
  draftable_id: number
  selected_by: number
  selected_at: string
  undone_by: number | null
  undone_at: string | null
  point_cost: number
}

export type ActionKind = 'configure' | 'start' | 'pick' | 'undo'

export interface PermittedAction {
  league_id: number
  revision: number
  actor_user_id: number
  kind: ActionKind
  pick_number: number | null
  target_pick_id: number | null
}

export interface LeagueSnapshot {
  league_id: number
  revision: number
  server_now: string
  league: League
  state: DraftState
  teams: Team[]
  rosters: RosterPick[]
  history: Pick[]
  /** Already filtered by the server to the signed-in user's permitted actions. */
  actions: PermittedAction[]
}

export interface LeagueRevision {
  p_league: number
  p_revision: number
}

export interface CreateLeagueInput {
  p_name: string
  p_roster_size: number
  p_pick_seconds?: number
}

export interface UpdateLeagueInput extends LeagueRevision {
  p_name: string
  p_roster_size: number
  p_pick_seconds: number
}

export interface AddTeamInput extends LeagueRevision {
  p_name?: string | null
  p_manager: number
  p_point_budget: number
}

export interface TeamRevision extends LeagueRevision {
  p_team: number
}

export interface UpdateTeamInput extends TeamRevision {
  p_name: string | null
  p_manager: number
  p_point_budget: number
}

export interface SetTeamMetadataInput extends TeamRevision {
  p_name: string
  p_color_rgb: number
}

export interface SetDraftOrderInput extends LeagueRevision {
  p_teams: number[]
}

export type DraftActionInput = LeagueRevision &
  (
    | { p_kind: 'pick'; p_draftable: number; p_charge_points?: boolean }
    | { p_kind: 'start' | 'undo'; p_draftable?: null; p_charge_points?: true }
  )

export interface RpcResult<T> {
  result: T
}

export type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface AccountEventHandlers {
  onInvalidate: () => void
  onStatus?: (status: ConnectionStatus) => void
}
