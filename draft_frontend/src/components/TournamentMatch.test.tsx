import { act, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { LeagueSnapshot } from '../lib/types'
import type { MatchDetail, ResultSubmission, TournamentSnapshot } from '../lib/tournamentTypes'
import { useTournamentMatch } from '../lib/useTournament'
import { TournamentMatch } from './TournamentMatch'

vi.mock('./Modal', () => ({
  Modal: ({ children }: { children: ReactNode }) => <div role="dialog">{children}</div>,
}))
vi.mock('../lib/useTournament', () => ({ useTournamentMatch: vi.fn() }))
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      submitResult: vi.fn(),
      recordResult: vi.fn(),
      correctResult: vi.fn(),
      setMatchVod: vi.fn(),
    },
  }
})

const now = '2026-10-17T00:00:00Z'
const league: LeagueSnapshot = {
  league_id: 7,
  revision: 8,
  server_now: now,
  league: {
    id: 7,
    name: 'Test league',
    commissioner_user_id: 99,
    roster_size: 1,
    pick_seconds: 60,
    revision: 8,
    created_at: now,
    started_at: now,
    turn_started_at: null,
  },
  state: {
    league_id: 7,
    revision: 8,
    status: 'completed',
    roster_size: 1,
    team_count: 2,
    active_pick_count: 2,
    required_pick_count: 2,
    can_start: false,
    next_pick_number: null,
    next_team_id: null,
    last_pick_id: 2,
    deadline: null,
  },
  teams: [
    {
      id: 1,
      league_id: 7,
      name: 'Alpha',
      manager_user_id: 11,
      draft_position: 1,
      point_budget: 100,
      points_spent: 1,
      points_remaining: 99,
    },
    {
      id: 2,
      league_id: 7,
      name: 'Beta',
      manager_user_id: 22,
      draft_position: 2,
      point_budget: 100,
      points_spent: 1,
      points_remaining: 99,
    },
  ],
  rosters: [],
  history: [],
  actions: [],
}

function fixture(): MatchDetail {
  return {
    id: 10,
    league_id: 7,
    stage_id: 1,
    round_id: 1,
    team_a_id: 1,
    team_b_id: 2,
    revision: 0,
    vod_url: null,
    team_a_submission_id: null,
    team_b_submission_id: null,
    current_decision_id: null,
    created_at: now,
    official_result: null,
    disposition: null,
    submissions: [],
    decisions: [],
  }
}

function report(side: 'a' | 'b', id = 51): ResultSubmission {
  return {
    id,
    match_id: 10,
    reporting_side: side,
    submitted_by: side === 'a' ? 11 : 22,
    submitted_at: now,
    outcome_kind: 'played',
    team_a_game_wins: 2,
    team_b_game_wins: 1,
    awarded_winner_side: null,
    winner_side: 'a',
  }
}

function tournament(): TournamentSnapshot {
  return {
    league_id: 7,
    revision: 0,
    published_at: now,
    published_by: 99,
    stages: [
      {
        id: 1,
        tournament_id: 7,
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
    ],
    rounds: [
      {
        id: 1,
        stage_id: 1,
        number: 1,
        purpose: 'normal',
        tiebreak_reason: null,
        created_by: 99,
        created_at: now,
        suggested_start_date: '2026-10-19',
      },
    ],
    matches: [detail],
    entries: [],
    current_submissions: detail.submissions,
    official_results: detail.official_result ? [detail.official_result] : [],
    stage_records: [],
    round_records: [],
    withdrawals,
    dispositions: detail.disposition ? [detail.disposition] : [],
  }
}

let renderer: ReactTestRenderer | null
let detail: MatchDetail
let userId: number
let withdrawals: TournamentSnapshot['withdrawals']
let refresh: ReturnType<typeof vi.fn<() => Promise<void>>>
let run: ReturnType<typeof vi.fn<(operation: () => Promise<unknown>) => Promise<boolean>>>

function screen() {
  return (
    <TournamentMatch
      matchId={10}
      tournament={tournament()}
      snapshot={league}
      userId={userId}
      users={[
        { id: 11, display_name: 'Alice' },
        { id: 22, display_name: 'Bob' },
        { id: 99, display_name: 'Commissioner' },
      ]}
      refreshKey={detail.revision}
      busy={false}
      run={run}
      onClose={() => undefined}
    />
  )
}

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

function button(label: string) {
  const found = renderer!.root.findAllByType('button').find((node) => text(node) === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}

async function click(label: string) {
  await act(async () => {
    button(label).props.onClick()
  })
}

async function change(label: string, value: string) {
  const field = renderer!.root.findAllByType('label').find((node) => text(node).includes(label))!
  const input = field.findAll((node) =>
    ['input', 'select', 'textarea'].includes(String(node.type)),
  )[0]
  await act(async () => {
    input.props.onChange({ target: { value } })
  })
}

async function submit() {
  await act(async () => {
    renderer!.root.findByType('form').props.onSubmit({ preventDefault: vi.fn() })
  })
}

async function mount() {
  await act(async () => {
    renderer = create(screen())
  })
}

async function rerender() {
  await act(async () => {
    renderer!.update(screen())
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  detail = fixture()
  userId = 11
  withdrawals = []
  refresh = vi.fn().mockResolvedValue(undefined)
  run = vi.fn(async (operation: () => Promise<unknown>) => {
    try {
      await operation()
      return true
    } catch {
      return false
    }
  })
  vi.mocked(useTournamentMatch).mockImplementation(() => ({
    match: detail,
    loading: false,
    error: '',
    refresh,
  }))
  vi.mocked(api.submitResult).mockResolvedValue({ result: 51 })
  vi.mocked(api.recordResult).mockResolvedValue({ result: 71 })
  vi.mocked(api.correctResult).mockResolvedValue({ result: 72 })
  vi.mocked(api.setMatchVod).mockResolvedValue({ result: null })
})

afterEach(async () => {
  await act(async () => {
    renderer?.unmount()
  })
  vi.unstubAllGlobals()
})

describe('TournamentMatch', () => {
  it('reports named A/B scores in fixture orientation even for the B manager', async () => {
    userId = 22
    await mount()
    await click('Report result')
    await change('Alpha (A) game wins', '2')
    await change('Beta (B) game wins', '1')
    await submit()
    expect(api.submitResult).toHaveBeenCalledWith({
      p_match: 10,
      p_expected_previous_submission_id: null,
      p_outcome_kind: 'played',
      p_team_a_game_wins: 2,
      p_team_b_game_wins: 1,
    })
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('keeps a first report valid when only the opponent report or VOD changes', async () => {
    await mount()
    await click('Report result')
    await change('Alpha (A) game wins', '2')
    await change('Beta (B) game wins', '1')
    detail = {
      ...detail,
      revision: 2,
      team_b_submission_id: 52,
      submissions: [report('b', 52)],
      vod_url: 'https://example.com/video',
    }
    await rerender()
    await submit()
    expect(api.submitResult).toHaveBeenCalledWith(
      expect.objectContaining({ p_expected_previous_submission_id: null }),
    )
    expect(text(renderer!.root)).not.toContain('changed while you were editing')
  })

  it('requires review when the same participant report pointer changes', async () => {
    await mount()
    await click('Report result')
    await change('Alpha (A) game wins', '2')
    await change('Beta (B) game wins', '1')
    detail = { ...detail, revision: 1, team_a_submission_id: 51, submissions: [report('a')] }
    await rerender()
    expect(text(renderer!.root)).toContain('changed while you were editing')
    expect(renderer!.root.findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.submitResult).not.toHaveBeenCalled()
    await click('Discard edit and review')
    await click('Replace your report')
    await submit()
    expect(api.submitResult).toHaveBeenCalledWith(
      expect.objectContaining({ p_expected_previous_submission_id: 51 }),
    )
  })

  it('replaces a report with a forfeit using the own pointer and no invented scores', async () => {
    detail = { ...detail, revision: 1, team_a_submission_id: 51, submissions: [report('a')] }
    await mount()
    await click('Replace your report')
    await change('Result type', 'forfeit')
    await change('Team awarded the win', 'b')
    await submit()
    expect(api.submitResult).toHaveBeenCalledWith({
      p_match: 10,
      p_expected_previous_submission_id: 51,
      p_outcome_kind: 'forfeit',
      p_awarded_winner_side: 'b',
    })
  })

  it('rejects incomplete and tied played scores before sending a report', async () => {
    await mount()
    await click('Report result')
    await change('Alpha (A) game wins', '1')
    await change('Beta (B) game wins', '1')
    await submit()
    expect(api.submitResult).not.toHaveBeenCalled()
    expect(text(renderer!.root)).toContain('Enter a completed series')
  })

  it('records a selected current report with a reason and without a second outcome', async () => {
    userId = 99
    detail = { ...detail, revision: 1, team_a_submission_id: 51, submissions: [report('a')] }
    await mount()
    await click('Record official result')
    await change('Decision source', '51')
    await submit()
    expect(api.recordResult).not.toHaveBeenCalled()
    await change('Reason for decision', 'Reviewed match recording')
    await submit()
    expect(api.recordResult).toHaveBeenCalledWith({
      p_match: 10,
      p_revision: 1,
      p_reason: 'Reviewed match recording',
      p_selected_submission: 51,
    })
  })

  it('allows correcting excluded official history without reopening participant reports', async () => {
    userId = 99
    detail = {
      ...detail,
      revision: 3,
      team_a_submission_id: 51,
      current_decision_id: 71,
      submissions: [report('a')],
      disposition: { match_id: 10, withdrawal_team_id: 1 },
      official_result: {
        match_id: 10,
        decision_id: 71,
        submission_id: 51,
        authority: 'commissioner',
        decided_by: 99,
        decided_at: now,
        reason: 'Original decision',
        outcome_kind: 'played',
        team_a_game_wins: 2,
        team_b_game_wins: 1,
        winner_side: 'a',
        counts_in_standings: false,
      },
      decisions: [
        {
          id: 71,
          match_id: 10,
          selected_submission_id: 51,
          supporting_submission_id: null,
          authority: 'commissioner',
          decided_by: 99,
          decided_at: now,
          reason: 'Original decision',
        },
      ],
    }
    withdrawals = [{ team_id: 1, decided_by: 99, decided_at: now, reason: 'Team withdrew' }]
    await mount()
    expect(text(renderer!.root)).toContain('Excluded result')
    expect(text(renderer!.root)).toContain('Original decision')
    await click('Correct official result')
    expect(text(renderer!.root)).toContain('will remain excluded')
    await change('Alpha (A) game wins', '1')
    await change('Beta (B) game wins', '2')
    await change('Reason for correction', 'Corrected reversed score')
    await submit()
    expect(api.correctResult).toHaveBeenCalledWith({
      p_match: 10,
      p_revision: 3,
      p_reason: 'Corrected reversed score',
      p_outcome_kind: 'played',
      p_team_a_game_wins: 1,
      p_team_b_game_wins: 2,
    })
    userId = 11
    await rerender()
    expect(
      renderer!.root.findAllByType('button').some((node) => text(node) === 'Replace your report'),
    ).toBe(false)
  })

  it('leaves reporting available for a withdrawn team when the fixture was not removed', async () => {
    withdrawals = [
      { team_id: 1, decided_by: 99, decided_at: now, reason: 'After final publication' },
    ]
    await mount()
    expect(button('Report result')).toBeDefined()
    expect(text(renderer!.root)).toContain('This fixture remains in place')
  })

  it('uses frozen match revisions for VOD edits and permits explicitly clearing a link', async () => {
    detail = { ...detail, vod_url: 'https://example.com/video', revision: 2 }
    await mount()
    await click('Edit or remove video link')
    await change('Video link (VOD)', '')
    detail = { ...detail, revision: 3 }
    await rerender()
    await submit()
    expect(api.setMatchVod).not.toHaveBeenCalled()
    await click('Discard edit and review')
    await click('Edit or remove video link')
    await change('Video link (VOD)', '')
    await submit()
    expect(api.setMatchVod).toHaveBeenCalledWith({ p_match: 10, p_revision: 3, p_vod_url: null })
  })

  it('never renders an unsafe VOD link and prevents saving one', async () => {
    detail = { ...detail, vod_url: 'javascript:alert(1)' }
    await mount()
    expect(
      renderer!.root.findAllByType('a').some((node) => node.props.href.startsWith('javascript:')),
    ).toBe(false)
    await click('Edit or remove video link')
    await change('Video link (VOD)', 'https://user:password@example.com/video')
    await submit()
    expect(api.setMatchVod).not.toHaveBeenCalled()
    expect(text(renderer!.root)).toContain('without spaces or sign-in credentials')
  })

  it('refreshes after an interrupted write and shows the error inside the dialog without replay', async () => {
    vi.mocked(api.submitResult).mockRejectedValue(new TypeError('Network failed'))
    await mount()
    await click('Report result')
    await change('Alpha (A) game wins', '2')
    await change('Beta (B) game wins', '1')
    await submit()
    expect(api.submitResult).toHaveBeenCalledOnce()
    expect(refresh).toHaveBeenCalledOnce()
    expect(text(renderer!.root)).toContain('Could not reach the server')
  })

  it('hides mutation controls from unrelated signed-in spectators', async () => {
    userId = 33
    await mount()
    expect(renderer!.root.findAllByType('button').map(text)).toEqual([''])
  })
})
