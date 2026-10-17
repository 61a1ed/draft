import { describe, expect, it } from 'vitest'
import type {
  ResultSubmission,
  TournamentMatch,
  TournamentRound,
  TournamentSnapshot,
  TournamentStage,
} from './tournamentTypes'
import { matchStatus, progression, roundByeTeamIds, suggestedWeek } from './tournamentView'

const stamp = '2026-10-19T12:00:00Z'

function stage(overrides: Partial<TournamentStage> = {}): TournamentStage {
  return {
    id: 10,
    tournament_id: 1,
    position: 1,
    name: 'Season',
    format: 'round_robin',
    best_of: 3,
    wins_required: 2,
    source_stage_id: null,
    qualifier_count: null,
    qualification_finalized_at: null,
    qualification_finalized_by: null,
    ...overrides,
  }
}

function round(overrides: Partial<TournamentRound> = {}): TournamentRound {
  return {
    id: 100,
    stage_id: 10,
    number: 1,
    purpose: 'normal',
    tiebreak_reason: null,
    created_by: 1,
    created_at: stamp,
    suggested_start_date: '2026-10-19',
    ...overrides,
  }
}

function match(overrides: Partial<TournamentMatch> = {}): TournamentMatch {
  return {
    id: 1000,
    round_id: 100,
    team_a_id: 1,
    team_b_id: 2,
    revision: 0,
    vod_url: null,
    team_a_submission_id: null,
    team_b_submission_id: null,
    current_decision_id: null,
    created_at: stamp,
    ...overrides,
  }
}

function report(overrides: Partial<ResultSubmission> = {}): ResultSubmission {
  return {
    id: 10000,
    match_id: 1000,
    reporting_side: 'a',
    submitted_by: 1,
    submitted_at: stamp,
    outcome_kind: 'played',
    team_a_game_wins: 2,
    team_b_game_wins: 1,
    awarded_winner_side: null,
    winner_side: 'a',
    ...overrides,
  }
}

function tournament(overrides: Partial<TournamentSnapshot> = {}): TournamentSnapshot {
  return {
    league_id: 1,
    revision: 0,
    published_at: stamp,
    published_by: 1,
    stages: [
      stage(),
      stage({
        id: 20,
        position: 2,
        name: 'Final',
        format: 'single_match',
        best_of: 5,
        wins_required: 3,
        source_stage_id: 10,
        qualifier_count: 2,
      }),
    ],
    entries: [1, 2, 3].map((id) => ({
      stage_id: 10,
      team_id: id,
      entered_by: 1,
      entered_at: stamp,
    })),
    rounds: [round()],
    matches: [match()],
    current_submissions: [],
    official_results: [],
    stage_records: [],
    round_records: [],
    withdrawals: [],
    dispositions: [],
    ...overrides,
  }
}

describe('tournament match status', () => {
  it('distinguishes unreported, one report, and conflicting scores in fixture orientation', () => {
    const state = tournament({
      current_submissions: [
        report(),
        report({ id: 10001, reporting_side: 'b', team_b_game_wins: 0 }),
      ],
    })
    expect(matchStatus(match(), state)).toEqual({
      label: 'Awaiting reports',
      tone: 'pending',
      unresolved: true,
    })
    expect(matchStatus(match({ team_a_submission_id: 10000 }), state).label).toBe(
      'Awaiting opponent',
    )
    expect(
      matchStatus(match({ team_a_submission_id: 10000, team_b_submission_id: 10001 }), state),
    ).toEqual({ label: 'Reports disagree', tone: 'review', unresolved: true })
  })

  it('compares complete forfeit claims and uses only current report pointers', () => {
    const state = tournament({
      current_submissions: [
        report({ id: 99, team_a_game_wins: 0, team_b_game_wins: 2, winner_side: 'b' }),
        report({
          outcome_kind: 'forfeit',
          team_a_game_wins: null,
          team_b_game_wins: null,
          awarded_winner_side: 'a',
        }),
        report({
          id: 10001,
          reporting_side: 'b',
          outcome_kind: 'forfeit',
          team_a_game_wins: null,
          team_b_game_wins: null,
          awarded_winner_side: 'a',
        }),
      ],
    })
    const fixture = match({ team_a_submission_id: 10000, team_b_submission_id: 10001 })
    expect(matchStatus(fixture, state).label).toBe('Reports match')
    state.current_submissions[2].awarded_winner_side = 'b'
    expect(matchStatus(fixture, state).tone).toBe('review')
  })

  it('classifies withdrawn fixtures by retained evidence and treats them as resolved', () => {
    const state = tournament({ dispositions: [{ match_id: 1000, withdrawal_team_id: 1 }] })
    expect(matchStatus(match(), state)).toEqual({
      label: 'Cancelled',
      tone: 'removed',
      unresolved: false,
    })
    expect(matchStatus(match({ team_b_submission_id: 10001 }), state).label).toBe(
      'Excluded evidence',
    )
    expect(matchStatus(match({ current_decision_id: 100000 }), state).label).toBe('Excluded result')
  })

  it('treats an official decision as settled even if participant reports disagree', () => {
    const state = tournament({
      current_submissions: [
        report(),
        report({ id: 10001, team_a_game_wins: 1, team_b_game_wins: 2, winner_side: 'b' }),
      ],
    })
    expect(
      matchStatus(
        match({
          current_decision_id: 100000,
          team_a_submission_id: 10000,
          team_b_submission_id: 10001,
        }),
        state,
      ),
    ).toEqual({ label: 'Official', tone: 'official', unresolved: false })
  })
})

describe('qualification and byes', () => {
  it('waits for every prior tiebreak attempt and ignores removed fixtures, regardless of records', () => {
    const state = tournament({
      rounds: [
        round(),
        round({ id: 101, purpose: 'tiebreak', number: 2, tiebreak_reason: 'Tie for second' }),
        round({ id: 200, stage_id: 20 }),
      ],
      matches: [
        match({ current_decision_id: 1 }),
        match({ id: 1001, round_id: 101 }),
        match({ id: 1002, round_id: 101, team_b_id: 3 }),
        match({ id: 2000, round_id: 200 }),
      ],
      dispositions: [{ match_id: 1002, withdrawal_team_id: 3 }],
      withdrawals: [{ team_id: 3, decided_by: 1, decided_at: stamp, reason: 'Unable to continue' }],
    })
    const result = progression(state)
    expect(result.unresolved.map((item) => item.id)).toEqual([1001])
    expect(result.eligibleTeamIds).toEqual([1, 2])
    expect(result.finalPublished).toBe(false)
    expect(result.source?.id).toBe(10)
    expect(result.final?.id).toBe(20)
  })

  it('retains final publication after subsequent withdrawal and does not remove active results', () => {
    const state = tournament({
      withdrawals: [{ team_id: 1, decided_by: 1, decided_at: stamp, reason: 'After final' }],
    })
    state.stages[0].qualification_finalized_at = stamp
    state.stages[0].qualification_finalized_by = 1
    expect(progression(state).finalPublished).toBe(true)
    expect(progression(state).unresolved.map((item) => item.id)).toEqual([1000])
    expect(matchStatus(state.matches[0], state).unresolved).toBe(true)
  })

  it('shows only unassigned active source entrants as byes in normal rounds', () => {
    const state = tournament({ dispositions: [{ match_id: 1000, withdrawal_team_id: 1 }] })
    expect(roundByeTeamIds(state.rounds[0], state)).toEqual([3])
    expect(roundByeTeamIds(round({ purpose: 'tiebreak', tiebreak_reason: 'Tie' }), state)).toEqual(
      [],
    )
    expect(roundByeTeamIds(round({ stage_id: 20 }), state)).toEqual([])
    state.withdrawals.push({ team_id: 3, decided_by: 1, decided_at: stamp, reason: 'Withdrawn' })
    expect(roundByeTeamIds(state.rounds[0], state)).toEqual([])
  })
})

describe('suggested calendar weeks', () => {
  it('retains the calendar date around daylight saving changes', () => {
    expect(suggestedWeek('2026-03-08')).toBe('Week of March 8, 2026')
    expect(suggestedWeek('2026-11-01')).toBe('Week of November 1, 2026')
    expect(suggestedWeek('2026-10-19')).toBe('Week of October 19, 2026')
  })

  it('supports an absent hint without displaying an invalid or rolled-over date', () => {
    expect(suggestedWeek(null)).toBe('No suggested week')
    expect(suggestedWeek('2026-02-30')).toBe('No suggested week')
    expect(suggestedWeek('invalid')).toBe('No suggested week')
  })
})
