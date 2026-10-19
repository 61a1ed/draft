import { act, type ComponentProps } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../lib/api'
import type { LeagueSnapshot } from '../lib/types'
import { LeagueSetup } from './LeagueSetup'
import { TeamMetadata } from './TeamMetadata'

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      setTeamMetadata: vi.fn(),
      setDraftOrder: vi.fn(),
      addTeam: vi.fn(),
      updateTeam: vi.fn(),
    },
  }
})

function league(): LeagueSnapshot {
  return {
    league_id: 7,
    revision: 5,
    server_now: '2026-10-21T12:00:00Z',
    league: {
      id: 7,
      name: 'Test league',
      commissioner_user_id: 99,
      roster_size: 1,
      pick_seconds: 60,
      revision: 5,
      created_at: '2026-10-21T12:00:00Z',
      started_at: null,
      turn_started_at: null,
    },
    state: {
      league_id: 7,
      revision: 5,
      status: 'setup',
      roster_size: 1,
      team_count: 2,
      active_pick_count: 0,
      required_pick_count: 2,
      can_start: false,
      next_pick_number: null,
      next_team_id: null,
      last_pick_id: null,
      deadline: null,
    },
    teams: [null, 'Beta'].map((name, index) => ({
      id: index + 1,
      league_id: 7,
      name,
      color_rgb: index === 0 ? null : 0,
      metadata_complete: index !== 0,
      can_set_metadata: index === 0,
      manager_user_id: index === 0 ? 11 : 22,
      draft_position: index + 1,
      point_budget: 100,
      points_spent: 0,
      points_remaining: 100,
    })),
    rosters: [],
    history: [],
    actions: [],
  }
}

let renderer: ReactTestRenderer | null
let props: ComponentProps<typeof LeagueSetup>

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

function own() {
  return renderer!.root.findByProps({ 'aria-labelledby': 'your-team-heading' })
}

function button(label: string, scope = renderer!.root) {
  const found = scope.findAllByType('button').find((node) => text(node).trim() === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}

function control(label: string, scope = own()) {
  const found = scope.findAllByType('label').find((node) => text(node).trim().startsWith(label))
  if (!found) throw new Error(`Control not found: ${label}`)
  return found.findAll((node) => ['input', 'select'].includes(String(node.type)))[0]
}

async function change(node: ReactTestInstance, value: string) {
  await act(async () => node.props.onChange({ target: { value } }))
}

async function click(node: ReactTestInstance) {
  await act(async () => node.props.onClick())
}

async function submit(scope = own()) {
  await act(async () => {
    scope.findByType('form').props.onSubmit({ preventDefault: vi.fn() })
  })
}

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<LeagueSetup {...props} />)
    else renderer = create(<LeagueSetup {...props} />)
  })
}

function commissioner(player = false) {
  props.userId = 99
  props.snapshot.actions = [
    {
      league_id: 7,
      revision: props.snapshot.revision,
      actor_user_id: 99,
      kind: 'configure',
      pick_number: null,
      target_pick_id: null,
    },
  ]
  props.snapshot.teams = props.snapshot.teams.map((team, index) => ({
    ...team,
    can_set_metadata: true,
    manager_user_id: player && index === 0 ? 99 : team.manager_user_id,
  }))
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  props = {
    snapshot: league(),
    userId: 11,
    users: [
      { id: 11, display_name: 'Alex' },
      { id: 22, display_name: 'Blair' },
      { id: 99, display_name: 'Commissioner' },
    ],
    busy: false,
    onMutation: vi.fn(async (operation) => {
      try {
        await operation()
        return true
      } catch {
        return false
      }
    }),
    onStartDraft: vi.fn(),
  }
  vi.mocked(api.setTeamMetadata).mockResolvedValue({ result: null })
  vi.mocked(api.setDraftOrder).mockResolvedValue({ result: null })
  vi.mocked(api.addTeam).mockResolvedValue({ result: 3 })
  vi.mocked(api.updateTeam).mockResolvedValue({ result: null })
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
})

describe('team setup in League settings', () => {
  it('puts the manager’s own editor first without exposing league administration', async () => {
    await render()
    expect(renderer!.root.findAllByType('h2').map(text)[0]).toBe('Your team')
    expect(renderer!.root.findAllByType(TeamMetadata)).toHaveLength(1)
    expect(text(own())).toContain('Team #1')
    expect(text(own())).toContain('No color chosen')
    expect(control('Team name').props.value).toBe('')
    expect(button('Save team details', own()).props.disabled).toBe(true)
    expect(renderer!.root.findAllByType('button').map(text)).not.toContain('Save settings')
    expect(text(renderer!.root)).toContain('Only the commissioner can change these settings.')
  })

  it('saves a first name and explicitly selected black as RGB zero', async () => {
    await render()
    await change(control('Team name'), '  Night Owls  ')
    await submit()
    expect(api.setTeamMetadata).not.toHaveBeenCalled()
    await click(own().findByProps({ 'aria-label': 'Use black' }))
    expect(text(own())).toContain('Unsaved preview')
    expect(text(own())).toContain('Saved status: Needs a name and color')
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledWith({
      p_league: 7,
      p_revision: 5,
      p_team: 1,
      p_name: 'Night Owls',
      p_color_rgb: 0,
    })
    expect(text(own())).toContain('Team saved.')
  })

  it('shows a commissioner-player’s editor once and supports other teams separately', async () => {
    commissioner(true)
    await render()
    expect(renderer!.root.findAllByType('h2').map(text)[0]).toBe('Your team')
    const editors = renderer!.root.findAllByType(TeamMetadata)
    expect(editors.map((node) => node.props.team.id)).toEqual([1, 2])
    expect(own().findByType(TeamMetadata).props.team.id).toBe(1)
    expect(text(renderer!.root)).toContain('Teams & managers')
  })

  it('handles a commissioner who does not play and reports unfinished teams before starting', async () => {
    commissioner()
    await render()
    expect(text(own())).toContain('You are not managing a team')
    expect(own().findAllByType('form')).toHaveLength(0)
    expect(renderer!.root.findAllByType(TeamMetadata)).toHaveLength(2)
    expect(text(renderer!.root)).toContain('1 of 2 teams still need a name or color')
    expect(button('Start draft').props.disabled).toBe(true)
    props.snapshot = {
      ...props.snapshot,
      teams: props.snapshot.teams.map((team) => ({ ...team, metadata_complete: true })),
      actions: [...props.snapshot.actions, { ...props.snapshot.actions[0], kind: 'start' }],
    }
    await render()
    expect(button('Start draft').props.disabled).toBe(false)
    await click(button('Start draft'))
    expect(props.onStartDraft).toHaveBeenCalledOnce()
  })

  it('requires the locally changed draft order to be saved before starting', async () => {
    commissioner()
    props.snapshot = {
      ...props.snapshot,
      teams: props.snapshot.teams.map((team) => ({ ...team, metadata_complete: true })),
      state: { ...props.snapshot.state, can_start: true },
      actions: [...props.snapshot.actions, { ...props.snapshot.actions[0], kind: 'start' }],
    }
    await render()
    expect(button('Start draft').props.disabled).toBe(false)
    await click(renderer!.root.findByProps({ 'aria-label': 'Move Team #1 down' }))
    expect(button('Start draft').props.disabled).toBe(true)
    expect(text(renderer!.root)).toContain('Save this order before starting the draft.')
    await click(button('Start draft'))
    expect(props.onStartDraft).not.toHaveBeenCalled()
    await click(button('Save draft order'))
    expect(api.setDraftOrder).toHaveBeenCalledWith({ p_league: 7, p_revision: 5, p_teams: [2, 1] })
    expect(button('Start draft').props.disabled).toBe(true)
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) => ({
        ...team,
        draft_position: team.id === 1 ? 2 : 1,
      })),
    }
    await render()
    expect(button('Start draft').props.disabled).toBe(false)
    await click(button('Start draft'))
    expect(props.onStartDraft).toHaveBeenCalledOnce()
  })

  it('adds a team without forcing the commissioner to choose its name', async () => {
    commissioner()
    await render()
    const addForm = renderer!.root
      .findAllByType('form')
      .find((form) => text(form).includes('Add a team'))!
    await change(control('Manager', addForm), '99')
    await submit(addForm)
    expect(api.addTeam).toHaveBeenCalledWith(
      expect.objectContaining({ p_name: null, p_manager: 99 }),
    )
  })

  it('preserves a manager’s newly saved name when changing their budget', async () => {
    commissioner()
    await render()
    const adminForm = () =>
      renderer!.root
        .findAllByType('form')
        .find(
          (form) =>
            text(form).includes('Save manager & budget') && text(form).startsWith('Team #1'),
        )!
    await change(control('Point budget', adminForm()), '120')
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) =>
        team.id === 1 ? { ...team, name: 'Night Owls' } : team,
      ),
    }
    await render()
    const updatedForm = renderer!.root
      .findAllByType('form')
      .find(
        (form) =>
          text(form).includes('Save manager & budget') && text(form).startsWith('Night Owls'),
      )!
    await submit(updatedForm)
    expect(api.updateTeam).toHaveBeenCalledWith(
      expect.objectContaining({
        p_name: 'Night Owls',
        p_point_budget: 120,
        p_revision: 6,
      }),
    )
  })

  it('uses the server permission and locks metadata once drafting starts', async () => {
    props.snapshot.teams[0] = {
      ...props.snapshot.teams[0],
      name: 'Night Owls',
      color_rgb: 0,
      metadata_complete: true,
      can_set_metadata: false,
    }
    await render()
    expect(own().findAllByType('form')).toHaveLength(0)
    expect(text(own())).toContain('#000000')
    props.snapshot = {
      ...props.snapshot,
      state: { ...props.snapshot.state, status: 'drafting' },
      teams: props.snapshot.teams.map((team) => ({ ...team, can_set_metadata: true })),
    }
    await render()
    expect(own().findAllByType('form')).toHaveLength(0)
    expect(text(own())).toContain('locked once the draft begins')
  })
})

describe('metadata refresh and conflict handling', () => {
  it('keeps a confirmed save visible if refreshing the league is delayed', async () => {
    await render()
    await change(control('Team name'), 'Night Owls')
    await click(own().findByProps({ 'aria-label': 'Use black' }))
    await submit()
    expect(control('Team name').props.value).toBe('Night Owls')
    expect(text(own())).toContain('Waiting for refreshed league details')
    expect(text(own())).not.toContain('Unsaved preview')
    expect(own().findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledTimes(1)
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) =>
        team.id === 1
          ? { ...team, name: 'Night Owls', color_rgb: 0, metadata_complete: true }
          : team,
      ),
    }
    await render()
    expect(own().findByType('fieldset').props.disabled).toBe(false)
    expect(text(own())).not.toContain('Waiting for refreshed league details')
    expect(control('Team name').props.value).toBe('Night Owls')
  })

  it('shows newer saved values before editing and preserves drafts on clock refreshes', async () => {
    await render()
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) =>
        team.id === 1 ? { ...team, name: 'Saved name', color_rgb: 0 } : team,
      ),
    }
    await render()
    expect(control('Team name').props.value).toBe('Saved name')
    await change(control('Team name'), 'My pending name')
    props.snapshot = { ...props.snapshot, server_now: '2026-10-21T12:01:00Z' }
    await render()
    expect(control('Team name').props.value).toBe('My pending name')
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledWith(
      expect.objectContaining({ p_revision: 6, p_name: 'My pending name', p_color_rgb: 0 }),
    )
  })

  it('keeps the first edited revision until an explicit reload after league changes', async () => {
    await render()
    await change(control('Team name'), 'My pending name')
    await click(own().findByProps({ 'aria-label': 'Use black' }))
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) =>
        team.id === 1 ? { ...team, name: 'New server name', color_rgb: 0x123456 } : team,
      ),
    }
    await render()
    expect(control('Team name').props.value).toBe('My pending name')
    expect(text(own())).toContain('changed while you were editing')
    expect(own().findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.setTeamMetadata).not.toHaveBeenCalled()
    await click(button('Reload saved team', own()))
    expect(control('Team name').props.value).toBe('New server name')
    expect(own().findByProps({ 'aria-label': 'Team color hex code' }).props.value).toBe('#123456')
    await change(control('Team name'), 'Reviewed name')
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledWith(
      expect.objectContaining({ p_revision: 6, p_name: 'Reviewed name', p_color_rgb: 0x123456 }),
    )
  })

  it('does not replay a rejected stale write and waits for a newer snapshot to reload', async () => {
    vi.mocked(api.setTeamMetadata).mockRejectedValue(new ApiError(409, 'stale_revision'))
    await render()
    await change(control('Team name'), 'My pending name')
    await click(own().findByProps({ 'aria-label': 'Use black' }))
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledTimes(1)
    expect(control('Team name').props.value).toBe('My pending name')
    expect(button('Reload saved team', own()).props.disabled).toBe(true)
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledTimes(1)
    props.snapshot = { ...props.snapshot, revision: 6 }
    await render()
    expect(button('Reload saved team', own()).props.disabled).toBe(false)
    await click(button('Reload saved team', own()))
    expect(control('Team name').props.value).toBe('')
    expect(api.setTeamMetadata).toHaveBeenCalledTimes(1)
  })

  it('keeps a failed draft and displays failure without claiming success', async () => {
    vi.mocked(api.setTeamMetadata).mockRejectedValue(new TypeError('Network failed'))
    await render()
    await change(control('Team name'), 'My pending name')
    await click(own().findByProps({ 'aria-label': 'Use black' }))
    await submit()
    expect(control('Team name').props.value).toBe('My pending name')
    expect(text(own())).toContain('Could not reach the server')
    expect(text(own())).not.toContain('Team saved.')
  })

  it('stops editing when the draft starts and discards an editor when the manager changes', async () => {
    await render()
    await change(control('Team name'), 'My pending name')
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      state: { ...props.snapshot.state, status: 'drafting' },
    }
    await render()
    expect(own().findAllByType('form')).toHaveLength(0)
    expect(text(own())).not.toContain('My pending name')
    props.snapshot = {
      ...props.snapshot,
      teams: props.snapshot.teams.map((team) => ({ ...team, manager_user_id: 99 })),
    }
    await render()
    expect(own().findAllByType(TeamMetadata)).toHaveLength(0)
    expect(api.setTeamMetadata).not.toHaveBeenCalled()
  })

  it('preserves another team’s metadata draft when its commissioner budget form refreshes', async () => {
    commissioner()
    await render()
    const metadata = () => renderer!.root.findAllByType(TeamMetadata)[0]
    await change(control('Team name', metadata()), 'My pending name')
    props.snapshot = {
      ...props.snapshot,
      revision: 6,
      teams: props.snapshot.teams.map((team) =>
        team.id === 1 ? { ...team, point_budget: 120 } : team,
      ),
    }
    await render()
    expect(control('Team name', metadata()).props.value).toBe('My pending name')
    expect(text(metadata())).toContain('changed while you were editing')
  })

  it('rejects invalid hex colors and blocks duplicate submissions during a save', async () => {
    let finish: (value: { result: null }) => void = () => {}
    vi.mocked(api.setTeamMetadata).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve
        }),
    )
    await render()
    await change(control('Team name'), 'Night Owls')
    await change(own().findByProps({ 'aria-label': 'Team color hex code' }), '#ZZZZZZ')
    await submit()
    expect(api.setTeamMetadata).not.toHaveBeenCalled()
    await change(own().findByProps({ 'aria-label': 'Team color hex code' }), '#abcdef')
    await submit()
    await submit()
    expect(api.setTeamMetadata).toHaveBeenCalledTimes(1)
    expect(text(own())).toContain('Saving team…')
    await act(async () => finish({ result: null }))
    expect(text(own())).toContain('Team saved.')
  })
})
