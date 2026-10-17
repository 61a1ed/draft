export type MatchSide = 'a' | 'b'
export type OutcomeKind = 'played' | 'forfeit'

export interface TournamentStage {
  id: number
  tournament_id: number
  position: number
  name: string
  format: string
  best_of: number
  wins_required: number
  source_stage_id: number | null
  qualifier_count: number | null
  qualification_finalized_at: string | null
  qualification_finalized_by: number | null
}

export interface StageEntry {
  stage_id: number
  team_id: number
  entered_by: number
  entered_at: string
}

export interface TournamentRound {
  id: number
  stage_id: number
  number: number
  purpose: 'normal' | 'tiebreak'
  tiebreak_reason: string | null
  created_by: number
  created_at: string
  suggested_start_date: string | null
}

export interface TournamentMatch {
  id: number
  round_id: number
  team_a_id: number
  team_b_id: number
  revision: number
  vod_url: string | null
  team_a_submission_id: number | null
  team_b_submission_id: number | null
  current_decision_id: number | null
  created_at: string
}

export interface ResultSubmission {
  id: number
  match_id: number
  reporting_side: MatchSide | null
  submitted_by: number
  submitted_at: string
  outcome_kind: OutcomeKind
  team_a_game_wins: number | null
  team_b_game_wins: number | null
  awarded_winner_side: MatchSide | null
  winner_side: MatchSide
}

export interface MatchDecision {
  id: number
  match_id: number
  selected_submission_id: number
  supporting_submission_id: number | null
  authority: 'agreement' | 'commissioner'
  decided_by: number | null
  decided_at: string
  reason: string | null
}

export interface OfficialResult {
  match_id: number
  decision_id: number
  authority: 'agreement' | 'commissioner'
  decided_by: number | null
  decided_at: string
  reason: string | null
  submission_id: number
  outcome_kind: OutcomeKind
  team_a_game_wins: number | null
  team_b_game_wins: number | null
  winner_side: MatchSide
  counts_in_standings: boolean
}

export interface StageRecord {
  stage_id: number
  team_id: number
  withdrawn: boolean
  series_completed: number
  series_wins: number
  series_losses: number
  played_series: number
  recorded_game_wins: number
  recorded_game_losses: number
}

export interface RoundRecord extends StageRecord {
  round_id: number
  purpose: 'normal' | 'tiebreak'
}

export interface TournamentWithdrawal {
  team_id: number
  decided_by: number
  decided_at: string
  reason: string
}

export interface MatchDisposition {
  match_id: number
  withdrawal_team_id: number
}

export interface TournamentSnapshot {
  league_id: number
  revision: number
  published_at: string
  published_by: number
  stages: TournamentStage[]
  entries: StageEntry[]
  rounds: TournamentRound[]
  matches: TournamentMatch[]
  current_submissions: ResultSubmission[]
  official_results: OfficialResult[]
  stage_records: StageRecord[]
  round_records: RoundRecord[]
  withdrawals: TournamentWithdrawal[]
  dispositions: MatchDisposition[]
}

export interface MatchDetail extends TournamentMatch {
  league_id: number
  stage_id: number
  official_result: OfficialResult | null
  disposition: MatchDisposition | null
  submissions: ResultSubmission[]
  decisions: MatchDecision[]
}

export type MatchSnapshot = MatchDetail

export interface PublishTournamentInput {
  p_league: number
  p_league_revision: number
  p_first_suggested_date: string
  p_season_best_of?: number
  p_final_best_of?: number
}

export interface SetRoundSuggestedDateInput {
  p_round: number
  p_revision: number
  p_suggested_start_date: string | null
}

export interface ResultOutcomeInput {
  p_outcome_kind: OutcomeKind
  p_team_a_game_wins?: number | null
  p_team_b_game_wins?: number | null
  p_awarded_winner_side?: MatchSide | null
}

export interface SubmitResultInput extends ResultOutcomeInput {
  p_match: number
  p_expected_previous_submission_id: number | null
}

export interface RecordResultInput {
  p_match: number
  p_revision: number
  p_reason: string
  p_selected_submission?: number | null
  p_outcome_kind?: OutcomeKind | null
  p_team_a_game_wins?: number | null
  p_team_b_game_wins?: number | null
  p_awarded_winner_side?: MatchSide | null
}

export interface CorrectResultInput extends ResultOutcomeInput {
  p_match: number
  p_revision: number
  p_reason: string
}

export interface SetMatchVodInput {
  p_match: number
  p_revision: number
  p_vod_url: string | null
}

export interface WithdrawTeamInput {
  p_league: number
  p_revision: number
  p_team: number
  p_reason: string
}

export interface CreateTiebreakRoundInput {
  p_stage: number
  p_revision: number
  p_teams: number[]
  p_reason: string
  p_suggested_start_date?: string | null
}

export interface PublishFinalInput {
  p_league: number
  p_revision: number
  p_qualifiers: number[]
  p_suggested_start_date?: string | null
}
