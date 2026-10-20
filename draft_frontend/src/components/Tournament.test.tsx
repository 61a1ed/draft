import { act, type ComponentProps, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { LeagueSnapshot } from '../lib/types'
import type { OfficialResult, TournamentMatch, TournamentSnapshot } from '../lib/tournamentTypes'
import { useTournament } from '../lib/useTournament'
import { Tournament } from './Tournament'
import { TournamentMatch as MatchDetails } from './TournamentMatch'

vi.mock('../lib/useTournament', () => ({ useTournament: vi.fn() }))
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: Object.fromEntries(Object.keys(actual.api).map((key) => [key, vi.fn()])),
  }
})
vi.mock('./Modal', () => ({
  Modal: ({ title, children }: { title: string; children: ReactNode }) => (
    <section role="dialog" aria-label={title}>
      {children}
    </section>
  ),
}))
vi.mock('./TournamentMatch', () => ({
  TournamentMatch: ({ matchId }: { matchId: number }) => <section data-match-id={matchId} />,
}))

const stamp = '2026-10-19T12:00:00Z'
type Props = ComponentProps<typeof Tournament>
let renderer: ReactTestRenderer | null = null
let props: Props
let state: TournamentSnapshot | null
let hookError = ''
let run: ReturnType<typeof vi.fn<ReturnType<typeof useTournament>['run']>>

function room(): LeagueSnapshot {
  return {
    league_id: 1,
    revision: 13,
    server_now: stamp,
    league: {
      id: 1,
      revision: 13,
      name: 'Test league',
      commissioner_user_id: 42,
      roster_size: 1,
      pick_seconds: 60,
      created_at: stamp,
      started_at: stamp,
      turn_started_at: stamp,
    },
    state: {
      league_id: 1,
      revision: 13,
      status: 'completed',
      roster_size: 1,
      team_count: 3,
      active_pick_count: 3,
      required_pick_count: 3,
      can_start: false,
      next_pick_number: null,
      next_team_id: null,
      last_pick_id: 3,
      deadline: null,
    },
    teams: ['Alpha', 'Beta', 'Gamma'].map((name, index) => ({
      id: index + 1,
      league_id: 1,
      name,
      color_rgb: 0,
      metadata_complete: true,
      can_set_metadata: false,
      manager_user_id: 43 + index,
      draft_position: index + 1,
      point_budget: 100,
      points_spent: 10,
      points_remaining: 90,
    })),
    rosters: [],
    history: [],
    actions: [],
  }
}

function fixture(overrides: Partial<TournamentMatch> = {}): TournamentMatch {
  return {
    id: 1000,
    round_id: 100,
    team_a_id: 1,
    team_b_id: 2,
    revision: 2,
    vod_url: null,
    team_a_submission_id: null,
    team_b_submission_id: null,
    current_decision_id: 500,
    created_at: stamp,
    ...overrides,
  }
}

function official(match: TournamentMatch, overrides: Partial<OfficialResult> = {}): OfficialResult {
  return {
    match_id: match.id,
    decision_id: match.current_decision_id!,
    authority: 'commissioner',
    decided_by: 42,
    decided_at: stamp,
    reason: 'Verified result',
    submission_id: match.id + 1000,
    outcome_kind: 'played',
    team_a_game_wins: 2,
    team_b_game_wins: 1,
    winner_side: 'a',
    counts_in_standings: true,
    ...overrides,
  }
}

function tournament(): TournamentSnapshot {
  const matches = [
    fixture(),
    fixture({ id: 1001, round_id: 101, team_b_id: 3, current_decision_id: 501 }),
    fixture({ id: 1002, round_id: 102, team_a_id: 2, team_b_id: 3, current_decision_id: 502 }),
  ]
  return {
    league_id: 1,
    revision: 7,
    published_at: stamp,
    published_by: 42,
    stages: [
      {
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
      },
      {
        id: 20,
        tournament_id: 1,
        position: 2,
        name: 'Final',
        format: 'single_match',
        best_of: 5,
        wins_required: 3,
        source_stage_id: 10,
        qualifier_count: 2,
        qualification_finalized_at: null,
        qualification_finalized_by: null,
      },
    ],
    entries: [1, 2, 3].map((id) => ({
      stage_id: 10,
      team_id: id,
      entered_by: 42,
      entered_at: stamp,
    })),
    rounds: [100, 101, 102].map((id, index) => ({
      id,
      stage_id: 10,
      number: index + 1,
      purpose: 'normal',
      tiebreak_reason: null,
      created_by: 42,
      created_at: stamp,
      suggested_start_date: '2026-10-19',
    })),
    matches,
    current_submissions: [],
    official_results: matches.map((match) => official(match)),
    stage_records: [1, 2, 3].map((id) => ({
      stage_id: 10,
      team_id: id,
      withdrawn: false,
      series_completed: 2,
      series_wins: 3 - id,
      series_losses: id - 1,
      played_series: 2,
      recorded_game_wins: 5 - id,
      recorded_game_losses: id + 1,
    })),
    round_records: [],
    withdrawals: [],
    dispositions: [],
  }
}

function publishFinal(current: TournamentSnapshot, resolved = false) {
  current.stages[0].qualification_finalized_at = stamp
  current.stages[0].qualification_finalized_by = 42
  current.entries.push(
    ...[2, 3].map((id) => ({ stage_id: 20, team_id: id, entered_by: 42, entered_at: stamp })),
  )
  current.rounds.push({ ...current.rounds[0], id: 200, stage_id: 20, number: 1 })
  const final = fixture({
    id: 2000,
    round_id: 200,
    team_a_id: 2,
    team_b_id: 3,
    current_decision_id: resolved ? 900 : null,
  })
  current.matches.push(final)
  if (resolved)
    current.official_results.push(
      official(final, { winner_side: 'b', team_a_game_wins: 1, team_b_game_wins: 3 }),
    )
}

function text(node: ReactTestInstance | string | number): string {
  return typeof node === 'string' || typeof node === 'number'
    ? String(node)
    : node.children.map((child) => text(child)).join('')
}

function button(label: string, scope = renderer!.root) {
  const found = scope.findAllByType('button').find((node) => text(node) === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}

function control(
  label: string,
  kind: 'input' | 'select' | 'textarea' = 'input',
  scope = renderer!.root,
) {
  const found = scope.findAllByType('label').find((node) => text(node).startsWith(label))
  if (!found) throw new Error(`Control not found: ${label}`)
  return found.findByType(kind)
}

async function change(node: ReactTestInstance, value: string | boolean) {
  await act(async () =>
    node.props.onChange({ target: typeof value === 'boolean' ? { checked: value } : { value } }),
  )
}

async function click(node: ReactTestInstance) {
  await act(async () => node.props.onClick())
}

async function submit(scope = renderer!.root) {
  await act(async () => scope.findByType('form').props.onSubmit({ preventDefault: vi.fn() }))
}

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<Tournament {...props} />)
    else renderer = create(<Tournament {...props} />)
  })
}

const dialog = () => renderer!.root.findByProps({ role: 'dialog' })

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  state = tournament()
  hookError = ''
  run = vi.fn(async (operation) => {
    await operation()
    return true
  })
  props = {
    snapshot: room(),
    userId: 42,
    users: [{ id: 42, display_name: 'Commissioner' }],
    busy: false,
    mutate: run,
  }
  vi.mocked(useTournament).mockImplementation(() => ({
    state,
    loading: false,
    error: hookError,
    readError: '',
    writeError: hookError,
    clearError: vi.fn(),
    refresh: vi.fn(async () => {}),
    run,
  }))
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  renderer = null
  vi.unstubAllGlobals()
})

describe('tournament publication', () => {
  it('scopes cached tournament reads and related match invalidations to the signed-in user', async () => {
    await render()
    expect(useTournament).toHaveBeenCalledWith({
      leagueId: props.snapshot.league_id,
      identityKey: props.userId,
      mutate: props.mutate,
    })
  })

  it('passes availability navigation through to opened match details', async () => {
    props.onCompareAvailability = vi.fn()
    await render()
    await click(renderer!.root.findAllByProps({ className: 'tournament-fixture' })[0])
    const match = renderer!.root.findByType(MatchDetails)
    expect(match.props.matchId).toBe(1000)
    expect(match.props.onCompareAvailability).toBe(props.onCompareAvailability)
  })

  it('sends the reviewed draft revision and selected rules, with required date and odd-value controls', async () => {
    state = null
    await render()
    const date = control('First suggested week')
    expect(date.props.required).toBe(true)
    for (const label of ['Season best of', 'Final best of']) {
      expect(control(label).props).toMatchObject({
        type: 'number',
        required: true,
        min: '1',
        step: '2',
      })
    }
    expect(button('Publish tournament').props.disabled).toBe(true)
    await change(date, '2026-11-02')
    await change(control('Season best of'), '5')
    await change(control('Final best of'), '7')
    await change(control('I understand publication'), true)
    await submit()
    expect(api.publishTournament).toHaveBeenCalledWith({
      p_league: 1,
      p_league_revision: 13,
      p_first_suggested_date: '2026-11-02',
      p_season_best_of: 5,
      p_final_best_of: 7,
    })
  })

  it('does not silently adopt a changed draft revision before publication', async () => {
    state = null
    await render()
    await change(control('First suggested week'), '2026-11-02')
    await change(control('I understand publication'), true)
    props = { ...props, snapshot: { ...props.snapshot, revision: 14 } }
    await render()
    expect(button('Publish tournament').props.disabled).toBe(true)
    await submit()
    expect(api.publishTournament).not.toHaveBeenCalled()
    await click(button('Review latest draft'))
    expect(control('I understand publication').props.checked).toBe(false)
    await change(control('I understand publication'), true)
    await submit()
    expect(api.publishTournament).toHaveBeenCalledWith(
      expect.objectContaining({ p_league_revision: 14 }),
    )
  })

  it('keeps publication unavailable to a manager or before draft completion', async () => {
    state = null
    props.userId = 43
    await render()
    expect(text(renderer!.root)).toContain('Waiting for tournament publication')
    expect(renderer!.root.findAllByType('form')).toHaveLength(0)
    props.userId = 42
    props.snapshot.state.status = 'drafting'
    await render()
    expect(text(renderer!.root)).toContain('The season starts after the draft')
    expect(renderer!.root.findAllByType('form')).toHaveLength(0)
  })

  it('preserves the publication form alongside a failed write', async () => {
    state = null
    await render()
    await change(control('First suggested week'), '2026-11-02')
    hookError = 'The draft changed. Review the latest state.'
    await render()
    expect(text(renderer!.root)).toContain(hookError)
    expect(control('First suggested week').props.value).toBe('2026-11-02')
  })
})

describe('qualification management', () => {
  it('keeps progression disabled until an earlier tiebreak is resolved or excluded', async () => {
    state!.rounds.push({
      ...state!.rounds[0],
      id: 103,
      number: 4,
      purpose: 'tiebreak',
      tiebreak_reason: 'Tie for second',
    })
    state!.matches.push(fixture({ id: 1003, round_id: 103, current_decision_id: null }))
    await render()
    expect(button('Publish final').props.disabled).toBe(true)
    expect(button('Create tiebreak').props.disabled).toBe(true)
    const standings = renderer!.root.findByProps({ className: 'card tournament-standings' })
    expect(text(standings)).toContain('3 / 3 resolved')
    state!.dispositions.push({ match_id: 1003, withdrawal_team_id: 1 })
    state!.withdrawals.push({ team_id: 1, decided_by: 42, decided_at: stamp, reason: 'Withdrawn' })
    await render()
    expect(button('Publish final').props.disabled).toBe(false)
    expect(button('Create tiebreak').props.disabled).toBe(false)
  })

  it('publishes exactly the two selected eligible finalists with the reviewed tournament revision', async () => {
    state!.withdrawals.push({ team_id: 1, decided_by: 42, decided_at: stamp, reason: 'Withdrawn' })
    await render()
    await click(button('Publish final'))
    expect(text(dialog())).not.toContain('Alpha')
    await change(control('Gamma', 'input', dialog()), true)
    await change(control('I confirm these are', 'input', dialog()), true)
    expect(button('Publish the final', dialog()).props.disabled).toBe(true)
    await change(control('Beta', 'input', dialog()), true)
    expect(control('I confirm these are', 'input', dialog()).props.checked).toBe(false)
    await change(control('I confirm these are', 'input', dialog()), true)
    await submit(dialog())
    expect(api.publishFinal).toHaveBeenCalledWith({
      p_league: 1,
      p_revision: 7,
      p_qualifiers: [3, 2],
      p_suggested_start_date: null,
    })
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('requires a tiebreak reason and explicit certification, then sends every selected entrant', async () => {
    await render()
    await click(button('Create tiebreak'))
    for (const name of ['Alpha', 'Beta', 'Gamma'])
      await change(control(name, 'input', dialog()), true)
    await change(control('I confirm these entrants', 'input', dialog()), true)
    expect(button('Create qualification tiebreak', dialog()).props.disabled).toBe(true)
    await change(control('Reason', 'textarea', dialog()), '  Still tied for two places  ')
    await change(control('Suggested week', 'input', dialog()), '2026-12-07')
    expect(text(dialog())).toContain('3 new matches')
    await submit(dialog())
    expect(api.createTiebreakRound).toHaveBeenCalledWith({
      p_stage: 10,
      p_revision: 7,
      p_teams: [1, 2, 3],
      p_reason: 'Still tied for two places',
      p_suggested_start_date: '2026-12-07',
    })
  })

  it('blocks an already-open management form when competitive evidence changes', async () => {
    await render()
    await click(button('Publish final'))
    await change(control('Alpha', 'input', dialog()), true)
    await change(control('Beta', 'input', dialog()), true)
    await change(control('I confirm these are', 'input', dialog()), true)
    state = { ...state!, revision: 8 }
    await render()
    expect(text(dialog())).toContain('The tournament changed while this form was open')
    expect(button('Publish the final', dialog()).props.disabled).toBe(true)
    await submit(dialog())
    expect(api.publishFinal).not.toHaveBeenCalled()
  })

  it('lets a commissioner clear a suggested week using the tournament revision', async () => {
    await render()
    await click(renderer!.root.findAllByType('button').find((node) => text(node) === 'Edit week')!)
    await change(control('Suggested week', 'input', dialog()), '')
    await submit(dialog())
    expect(api.setRoundSuggestedDate).toHaveBeenCalledWith({
      p_round: 100,
      p_revision: 7,
      p_suggested_start_date: null,
    })
  })

  it('shows no commissioner management controls to a participant', async () => {
    props.userId = 43
    await render()
    const labels = renderer!.root.findAllByType('button').map(text)
    for (const label of ['Publish final', 'Create tiebreak', 'Withdraw a team', 'Edit week'])
      expect(labels).not.toContain(label)
  })
})

describe('withdrawals and championship', () => {
  it('previews only newly excluded source fixtures and records the reason with the reviewed revision', async () => {
    state!.matches[1].current_decision_id = null
    state!.dispositions.push({ match_id: 1001, withdrawal_team_id: 3 })
    state!.withdrawals.push({
      team_id: 3,
      decided_by: 42,
      decided_at: stamp,
      reason: 'Earlier withdrawal',
    })
    await render()
    await click(button('Withdraw a team'))
    await change(control('Team', 'select', dialog()), '1')
    expect(text(dialog())).toContain(
      '1 active fixtures will be removed from counted records, including 1 official results',
    )
    await change(control('Reason', 'textarea', dialog()), '  Unable to continue  ')
    await change(control('I have reviewed', 'input', dialog()), true)
    await submit(dialog())
    expect(api.withdrawTeam).toHaveBeenCalledWith({
      p_league: 1,
      p_revision: 7,
      p_team: 1,
      p_reason: 'Unable to continue',
    })
  })

  it('explains that post-final withdrawal keeps the finalists and results and requires explicit forfeits', async () => {
    publishFinal(state!)
    await render()
    await click(button('Withdraw a team'))
    await change(control('Team', 'select', dialog()), '2')
    expect(text(dialog())).toContain(
      'season results, finalists, and the final match remain in place',
    )
    expect(text(dialog())).toContain('Any forfeit must be recorded explicitly')
    expect(text(dialog())).not.toContain('active fixtures will be removed')
    await change(control('Reason', 'textarea', dialog()), 'Unavailable for final')
    await change(control('I have reviewed', 'input', dialog()), true)
    await submit(dialog())
    expect(api.withdrawTeam).toHaveBeenCalledWith(
      expect.objectContaining({ p_team: 2, p_revision: 7 }),
    )
  })

  it('shows the final winner as champion regardless of season ordering or later withdrawal', async () => {
    publishFinal(state!)
    await render()
    let hero = renderer!.root.findByProps({ className: 'tournament-hero ' })
    expect(text(hero.findByType('h2'))).toBe('Beta vs Gamma')
    expect(text(hero)).not.toContain('LEAGUE CHAMPION')
    const final = state!.matches.find((match) => match.id === 2000)!
    final.current_decision_id = 900
    state!.official_results.push(
      official(final, { winner_side: 'b', team_a_game_wins: 1, team_b_game_wins: 3 }),
    )
    state!.withdrawals.push({
      team_id: 3,
      decided_by: 42,
      decided_at: stamp,
      reason: 'After final',
    })
    await render()
    hero = renderer!.root.findByProps({ className: 'tournament-hero tournament-champion' })
    expect(text(hero.findByType('h2'))).toBe('Gamma')
    expect(text(hero)).toContain('LEAGUE CHAMPION')
    await click(button('View final'))
    expect(renderer!.root.findByProps({ 'data-match-id': 2000 })).toBeDefined()
  })
})
