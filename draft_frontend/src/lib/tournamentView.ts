import type {
  TournamentMatch,
  TournamentRound,
  TournamentSnapshot,
  TournamentStage,
} from './tournamentTypes'

export interface MatchStatus {
  label: string
  tone: 'pending' | 'review' | 'official' | 'removed'
  unresolved: boolean
}

export function matchStatus(match: TournamentMatch, state: TournamentSnapshot): MatchStatus {
  const official = match.current_decision_id !== null
  const hasReport = match.team_a_submission_id !== null || match.team_b_submission_id !== null
  if (state.dispositions.some((item) => item.match_id === match.id)) {
    return {
      label: official ? 'Excluded result' : hasReport ? 'Excluded evidence' : 'Cancelled',
      tone: 'removed',
      unresolved: false,
    }
  }
  if (official) return { label: 'Official', tone: 'official', unresolved: false }

  const a = state.current_submissions.find((item) => item.id === match.team_a_submission_id)
  const b = state.current_submissions.find((item) => item.id === match.team_b_submission_id)
  if (a && b) {
    const agrees =
      a.outcome_kind === b.outcome_kind &&
      a.team_a_game_wins === b.team_a_game_wins &&
      a.team_b_game_wins === b.team_b_game_wins &&
      a.awarded_winner_side === b.awarded_winner_side
    return {
      label: agrees ? 'Reports match' : 'Reports disagree',
      tone: agrees ? 'pending' : 'review',
      unresolved: true,
    }
  }
  return {
    label: hasReport ? 'Awaiting opponent' : 'Awaiting reports',
    tone: 'pending',
    unresolved: true,
  }
}

export function progression(state: TournamentSnapshot): {
  source: TournamentStage | undefined
  final: TournamentStage | undefined
  eligibleTeamIds: number[]
  unresolved: TournamentMatch[]
  finalPublished: boolean
} {
  const source = state.stages.find((stage) => stage.source_stage_id === null)
  const final = state.stages.find((stage) => stage.source_stage_id === source?.id)
  const withdrawn = new Set(state.withdrawals.map((item) => item.team_id))
  const removed = new Set(state.dispositions.map((item) => item.match_id))
  const sourceRoundIds = new Set(
    state.rounds.filter((round) => round.stage_id === source?.id).map((round) => round.id),
  )
  return {
    source,
    final,
    eligibleTeamIds: state.entries
      .filter((entry) => entry.stage_id === source?.id && !withdrawn.has(entry.team_id))
      .map((entry) => entry.team_id),
    unresolved: state.matches.filter(
      (match) =>
        sourceRoundIds.has(match.round_id) &&
        match.current_decision_id === null &&
        !removed.has(match.id),
    ),
    finalPublished: source?.qualification_finalized_at != null,
  }
}

export function roundByeTeamIds(round: TournamentRound, state: TournamentSnapshot): number[] {
  const source = state.stages.find((stage) => stage.source_stage_id === null)
  if (round.purpose !== 'normal' || round.stage_id !== source?.id) return []
  const assigned = new Set(
    state.matches
      .filter((match) => match.round_id === round.id)
      .flatMap((match) => [match.team_a_id, match.team_b_id]),
  )
  const withdrawn = new Set(state.withdrawals.map((item) => item.team_id))
  return state.entries
    .filter(
      (entry) =>
        entry.stage_id === source.id &&
        !assigned.has(entry.team_id) &&
        !withdrawn.has(entry.team_id),
    )
    .map((entry) => entry.team_id)
}

export function suggestedWeek(value: string | null): string {
  if (!value) return 'No suggested week'
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!parts) return 'No suggested week'
  const [, year, month, day] = parts.map(Number)
  // Construct a calendar date locally; parsing YYYY-MM-DD would interpret it as UTC.
  const date = new Date(0)
  date.setFullYear(year, month - 1, day)
  date.setHours(12, 0, 0, 0)
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day)
    return 'No suggested week'
  return `Week of ${date.toLocaleDateString('en-US', {
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })}`
}
