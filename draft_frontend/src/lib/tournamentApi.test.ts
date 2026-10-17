import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => vi.resetModules())
afterEach(() => vi.unstubAllGlobals())

describe('tournament API contract', () => {
  it('uses the tournament and match reads with cancellation and same-origin credentials', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ league_id: 7 }))
      .mockResolvedValueOnce(json({ id: 12 }))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const abort = new AbortController()
    await api.tournament(7, abort.signal)
    await api.match(12, abort.signal)
    expect(fetch.mock.calls).toEqual([
      ['/api/leagues/7/tournament', { credentials: 'same-origin', signal: abort.signal }],
      ['/api/matches/12', { credentials: 'same-origin', signal: abort.signal }],
    ])
  })

  it('sends every competitive operation with its exact independent freshness token', async () => {
    const fetch = vi.fn().mockImplementation(async () => json({ result: null }))
    fetch.mockResolvedValueOnce(
      json({
        user: { id: 1, display_name: 'Commissioner' },
        csrf_token: 'token',
        expires_at: 2_000_000_000,
      }),
    )
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const operations = [
      [
        'publish_tournament',
        () =>
          api.publishTournament({
            p_league: 7,
            p_league_revision: 42,
            p_first_suggested_date: '2026-10-19',
            p_season_best_of: 3,
            p_final_best_of: 5,
          }),
        {
          p_league: 7,
          p_league_revision: 42,
          p_first_suggested_date: '2026-10-19',
          p_season_best_of: 3,
          p_final_best_of: 5,
        },
      ],
      [
        'set_round_suggested_date',
        () =>
          api.setRoundSuggestedDate({ p_round: 2, p_revision: 5, p_suggested_start_date: null }),
        { p_round: 2, p_revision: 5, p_suggested_start_date: null },
      ],
      [
        'submit_result',
        () =>
          api.submitResult({
            p_match: 12,
            p_expected_previous_submission_id: null,
            p_outcome_kind: 'played',
            p_team_a_game_wins: 2,
            p_team_b_game_wins: 1,
          }),
        {
          p_match: 12,
          p_expected_previous_submission_id: null,
          p_outcome_kind: 'played',
          p_team_a_game_wins: 2,
          p_team_b_game_wins: 1,
        },
      ],
      [
        'submit_result',
        () =>
          api.submitResult({
            p_match: 12,
            p_expected_previous_submission_id: 31,
            p_outcome_kind: 'forfeit',
            p_awarded_winner_side: 'b',
          }),
        {
          p_match: 12,
          p_expected_previous_submission_id: 31,
          p_outcome_kind: 'forfeit',
          p_awarded_winner_side: 'b',
        },
      ],
      [
        'record_result',
        () =>
          api.recordResult({
            p_match: 12,
            p_revision: 9,
            p_reason: 'Reviewed evidence',
            p_selected_submission: 31,
          }),
        { p_match: 12, p_revision: 9, p_reason: 'Reviewed evidence', p_selected_submission: 31 },
      ],
      [
        'correct_result',
        () =>
          api.correctResult({
            p_match: 12,
            p_revision: 10,
            p_reason: 'Corrected score',
            p_outcome_kind: 'played',
            p_team_a_game_wins: 1,
            p_team_b_game_wins: 2,
          }),
        {
          p_match: 12,
          p_revision: 10,
          p_reason: 'Corrected score',
          p_outcome_kind: 'played',
          p_team_a_game_wins: 1,
          p_team_b_game_wins: 2,
        },
      ],
      [
        'set_match_vod',
        () => api.setMatchVod({ p_match: 12, p_revision: 11, p_vod_url: null }),
        { p_match: 12, p_revision: 11, p_vod_url: null },
      ],
      [
        'withdraw_team',
        () =>
          api.withdrawTeam({ p_league: 7, p_revision: 8, p_team: 3, p_reason: 'Cannot continue' }),
        { p_league: 7, p_revision: 8, p_team: 3, p_reason: 'Cannot continue' },
      ],
      [
        'create_tiebreak_round',
        () =>
          api.createTiebreakRound({
            p_stage: 1,
            p_revision: 9,
            p_teams: [2, 4, 5],
            p_reason: 'Tied for qualification',
            p_suggested_start_date: null,
          }),
        {
          p_stage: 1,
          p_revision: 9,
          p_teams: [2, 4, 5],
          p_reason: 'Tied for qualification',
          p_suggested_start_date: null,
        },
      ],
      [
        'publish_final',
        () => api.publishFinal({ p_league: 7, p_revision: 12, p_qualifiers: [2, 5] }),
        { p_league: 7, p_revision: 12, p_qualifiers: [2, 5] },
      ],
    ] as const
    for (const [name, call, body] of operations) {
      await call()
      expect(fetch).toHaveBeenLastCalledWith(`/api/rpc/${name}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'token' },
        body: JSON.stringify(body),
      })
    }
    expect(fetch).toHaveBeenCalledTimes(operations.length + 1)
  })

  it('keeps a stale report conflict readable and never retries the submission', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ csrf_token: 'token' }))
      .mockResolvedValueOnce(json({ detail: 'stale_submission' }, 409))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    await expect(
      api.submitResult({
        p_match: 12,
        p_expected_previous_submission_id: 31,
        p_outcome_kind: 'forfeit',
        p_awarded_winner_side: 'a',
      }),
    ).rejects.toMatchObject({
      code: 'stale_submission',
      message: expect.stringContaining('Review'),
    })
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
