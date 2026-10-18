import { act, type ComponentProps, type ReactNode } from 'react'
import { create, type ReactTestInstance, type ReactTestRenderer } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { AvailabilityWindowInput, TeamAvailabilitySnapshot } from '../lib/availabilityTypes'
import type { LeagueSnapshot } from '../lib/types'
import { useAvailability } from '../lib/useAvailability'
import { Availability } from './Availability'

vi.mock('./Modal', () => ({
  Modal: ({ title, children }: { title: string; children: ReactNode }) => (
    <section role="dialog" aria-label={title}>
      {children}
    </section>
  ),
}))
vi.mock('../lib/useAvailability', () => ({ useAvailability: vi.fn() }))
vi.mock('../lib/availabilityTime', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/availabilityTime')>()),
  browserTimeZone: () => 'UTC',
}))
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>()
  return {
    ...actual,
    api: { ...actual.api, setTeamAvailability: vi.fn(), copyTeamAvailabilityWeek: vi.fn() },
  }
})

const stamp = '2026-10-21T12:00:00Z'
const visibleWindow = { starts_at: '2026-10-21T18:00:00Z', ends_at: '2026-10-21T20:00:00Z' }
const futureWindow = {
  starts_at: '2026-11-04T18:00:00.123456+00:00',
  ends_at: '2026-11-04T19:00:00.654321+00:00',
}

function league(): LeagueSnapshot {
  return {
    league_id: 7,
    revision: 5,
    server_now: stamp,
    league: {
      id: 7,
      name: 'Test league',
      commissioner_user_id: 99,
      roster_size: 1,
      pick_seconds: 60,
      revision: 5,
      created_at: stamp,
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
      can_start: true,
      next_pick_number: null,
      next_team_id: null,
      last_pick_id: null,
      deadline: null,
    },
    teams: ['Alpha', 'Beta'].map((name, index) => ({
      id: index + 1,
      league_id: 7,
      name,
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

function saved(
  teamId = 1,
  windows: AvailabilityWindowInput[] = [visibleWindow, futureWindow],
  revision = 3,
  zone = 'UTC',
): TeamAvailabilitySnapshot {
  return {
    team_id: teamId,
    availability: { team_id: teamId, time_zone: zone, display_step_minutes: 30, revision },
    windows: windows.map((window, index) => ({ ...window, id: index + 1, team_id: teamId })),
  }
}

let renderer: ReactTestRenderer | null
let props: ComponentProps<typeof Availability>
let states: Map<number, TeamAvailabilitySnapshot>
let readError: string
let loading: boolean
let run: ReturnType<typeof vi.fn<ReturnType<typeof useAvailability>['run']>>
let refresh: ReturnType<typeof vi.fn<ReturnType<typeof useAvailability>['refresh']>>

function text(node: ReactTestInstance | string): string {
  return typeof node === 'string' ? node : node.children.map(text).join('')
}

const dialog = () => renderer!.root.findByProps({ role: 'dialog' })

function button(label: string, scope = renderer!.root) {
  const found = scope.findAllByType('button').find((node) => text(node).trim() === label)
  if (!found) throw new Error(`Button not found: ${label}`)
  return found
}

function control(label: string, scope = renderer!.root) {
  const found = scope.findAllByType('label').find((node) => text(node).trim().startsWith(label))
  if (!found) throw new Error(`Control not found: ${label}`)
  return found.findAll((node) => ['input', 'select'].includes(String(node.type)))[0]
}

async function click(target: string | ReactTestInstance, scope = renderer!.root) {
  await act(async () =>
    (typeof target === 'string' ? button(target, scope) : target).props.onClick(),
  )
}

async function change(
  target: string | ReactTestInstance,
  value: string | boolean,
  scope = renderer!.root,
) {
  const node = typeof target === 'string' ? control(target, scope) : target
  await act(async () =>
    node.props.onChange({
      target: typeof value === 'boolean' ? { checked: value } : { value },
    }),
  )
}

async function submit() {
  await act(async () => dialog().findByType('form').props.onSubmit({ preventDefault: vi.fn() }))
}

async function render() {
  await act(async () => {
    if (renderer) renderer.update(<Availability {...props} />)
    else renderer = create(<Availability {...props} />)
  })
}

async function openNewWindow(start: string, end: string, zone = 'UTC') {
  await click('Edit availability')
  await change('Team time zone', zone, dialog())
  await click('Add window', dialog())
  await change('Starts', start, dialog())
  await change('Ends', end, dialog())
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  renderer = null
  states = new Map([
    [1, saved()],
    [2, saved(2, [visibleWindow])],
  ])
  readError = ''
  loading = false
  run = vi.fn(async (operation) => {
    await operation()
    return true
  })
  refresh = vi.fn(async () => {})
  props = {
    snapshot: league(),
    userId: 11,
    refreshKey: 0,
    busy: false,
    mutate: run,
    initialTeamId: 1,
    initialDate: '2026-10-21',
  }
  vi.mocked(useAvailability).mockImplementation(({ teamId }) => ({
    state: states.get(teamId) ?? null,
    loading,
    error: readError,
    readError,
    writeError: '',
    clearError: vi.fn(),
    refresh,
    run,
  }))
  vi.mocked(api.setTeamAvailability).mockResolvedValue({ result: null })
  vi.mocked(api.copyTeamAvailabilityWeek).mockResolvedValue({ result: null })
})

afterEach(async () => {
  await act(async () => renderer?.unmount())
  vi.unstubAllGlobals()
})

describe('availability editing', () => {
  it('allows the team manager to edit before tournament publication but grants no commissioner override', async () => {
    await render()
    expect(button('Edit availability').props.disabled).toBe(false)
    props = { ...props, userId: 99 }
    await render()
    expect(renderer!.root.findAllByType('button').map(text)).not.toContain('Edit availability')
    expect(renderer!.root.findAllByType('button').map(text)).not.toContain('Copy week')
    expect(text(renderer!.root)).toContain('only its manager can edit')
  })

  it('saves a first window with a null revision and explicit UTC instants', async () => {
    states.set(1, { team_id: 1, availability: null, windows: [] })
    await render()
    expect(button('Copy week').props.disabled).toBe(true)
    await openNewWindow('2026-10-21T18:00', '2026-10-21T19:00')
    await click('Add to schedule', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith({
      p_team: 1,
      p_revision: null,
      p_time_zone: 'UTC',
      p_display_step_minutes: 30,
      p_windows: [{ starts_at: '2026-10-21T18:00:00.000Z', ends_at: '2026-10-21T19:00:00.000Z' }],
    })
    expect(text(renderer!.root)).toContain('Availability saved.')
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('preserves offscreen windows and exact timestamp strings when saving a different team timezone', async () => {
    await render()
    await click('Edit availability')
    await change('Team time zone', 'Asia/Kathmandu', dialog())
    await change('Time increment', '60', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith({
      p_team: 1,
      p_revision: 3,
      p_time_zone: 'Asia/Kathmandu',
      p_display_step_minutes: 60,
      p_windows: [visibleWindow, futureWindow],
    })
  })

  it('keeps microsecond precision when opening and applying an unchanged saved window', async () => {
    await render()
    await click('Edit availability')
    await click(dialog().findByProps({ 'aria-label': 'Edit window 2' }))
    expect(control('Starts', dialog()).props.value).toBe('2026-11-04T18:00:00.123')
    await click('Apply window', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_windows: [visibleWindow, futureWindow],
      }),
    )
  })

  it('keeps a valid saved interval shorter than one millisecond when applying it unchanged', async () => {
    const precise = {
      starts_at: '2026-10-21T18:00:00.123400Z',
      ends_at: '2026-10-21T18:00:00.123500Z',
    }
    states.set(1, saved(1, [precise]))
    await render()
    await click('Edit availability')
    await click(dialog().findByProps({ 'aria-label': 'Edit window 1' }))
    await click('Apply window', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_windows: [precise],
      }),
    )
  })

  it('uses the configured time increment when adding directly from a day', async () => {
    const state = saved()
    state.availability!.display_step_minutes = 60
    states.set(1, state)
    await render()
    await click(renderer!.root.findByProps({ 'aria-label': 'Add time on 2026-10-21' }))
    expect(control('Starts', dialog()).props.value).toBe('2026-10-21T18:00')
    expect(control('Ends', dialog()).props.value).toBe('2026-10-21T19:00')
    await click('Add to schedule', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_display_step_minutes: 60,
        p_windows: [
          visibleWindow,
          futureWindow,
          {
            starts_at: '2026-10-21T18:00:00.000Z',
            ends_at: '2026-10-21T19:00:00.000Z',
          },
        ],
      }),
    )
  })

  it('keeps display timezone changes read-only and preserves the exact suggested week start', async () => {
    await render()
    expect(control('Week starting').props.value).toBe('2026-10-21')
    await change('Display time zone', 'Asia/Kathmandu')
    expect(control('Week starting').props.value).toBe('2026-10-21')
    expect(text(renderer!.root)).toContain('Times shown in Asia/Kathmandu')
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
    expect(api.copyTeamAvailabilityWeek).not.toHaveBeenCalled()
  })

  it('freezes an edited revision until the user reloads the latest saved times', async () => {
    await render()
    await click('Edit availability')
    await change('Team time zone', 'Asia/Tokyo', dialog())
    const replacement = { starts_at: '2026-10-22T12:00:00Z', ends_at: '2026-10-22T14:00:00Z' }
    states.set(1, saved(1, [replacement], 4, 'Europe/London'))
    await render()
    expect(text(dialog())).toContain('changed while you were editing')
    expect(dialog().findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
    await click('Reload saved times', dialog())
    expect(control('Team time zone', dialog()).props.value).toBe('Europe/London')
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_revision: 4,
        p_time_zone: 'Europe/London',
        p_windows: [replacement],
      }),
    )
  })

  it('requires explicit confirmation before clearing all weeks', async () => {
    await render()
    await click('Edit availability')
    await click(dialog().findByProps({ 'aria-label': 'Remove window 2' }))
    await click(dialog().findByProps({ 'aria-label': 'Remove window 1' }))
    expect(button('Save availability', dialog()).props.disabled).toBe(true)
    await submit()
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
    await change('Clear all saved availability', true, dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({ p_revision: 3, p_windows: [] }),
    )
  })

  it.each([
    ['reversed range', '2026-10-21T20:00', '2026-10-21T19:00', 'UTC'],
    [
      'nonexistent spring clock time',
      '2026-03-08T02:30',
      '2026-03-08T04:00',
      'America/Los_Angeles',
    ],
    [
      'unselected repeated autumn clock time',
      '2026-11-01T01:30',
      '2026-11-01T03:00',
      'America/Los_Angeles',
    ],
  ])('does not save an invalid window: %s', async (_name, start, end, zone) => {
    await render()
    await openNewWindow(start, end, zone)
    await click('Add to schedule', dialog())
    expect(dialog().findAllByProps({ role: 'alert' }).length).toBeGreaterThan(0)
    expect(button('Save availability', dialog()).props.disabled).toBe(true)
    await submit()
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
  })

  it('saves the explicitly chosen occurrence of a repeated local time', async () => {
    states.set(1, { team_id: 1, availability: null, windows: [] })
    await render()
    await openNewWindow('2026-11-01T01:30', '2026-11-01T03:00', 'America/Los_Angeles')
    await change('Starts occurrence', '2026-11-01T09:30:00.000Z', dialog())
    await click('Add to schedule', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_windows: [{ starts_at: '2026-11-01T09:30:00.000Z', ends_at: '2026-11-01T11:00:00.000Z' }],
      }),
    )
  })

  it('changes an existing repeated clock time to the other occurrence without changing the clock input', async () => {
    const repeated = {
      starts_at: '2026-11-01T08:30:00Z',
      ends_at: '2026-11-01T11:00:00Z',
    }
    states.set(1, saved(1, [repeated], 3, 'America/Los_Angeles'))
    await render()
    await click('Edit availability')
    await click(dialog().findByProps({ 'aria-label': 'Edit window 1' }))
    expect(control('Starts', dialog()).props.value).toBe('2026-11-01T01:30:00')
    expect(control('Starts occurrence', dialog()).props.value).toBe('2026-11-01T08:30:00.000Z')
    await change('Starts occurrence', '2026-11-01T09:30:00.000Z', dialog())
    await click('Apply window', dialog())
    await submit()
    expect(api.setTeamAvailability).toHaveBeenCalledWith(
      expect.objectContaining({
        p_windows: [{ ...repeated, starts_at: '2026-11-01T09:30:00.000Z' }],
      }),
    )
  })

  it.each(['2026-02-30', '10000-01-01', '9999-12-25', ''])(
    'rejects an invalid viewed week safely: %s',
    async (invalid) => {
      await render()
      await change('Week starting', invalid)
      expect(control('Week starting').props.value).toBe('2026-10-21')
      expect(renderer!.root.findByProps({ 'aria-label': 'Team availability by day' })).toBeDefined()
      expect(api.setTeamAvailability).not.toHaveBeenCalled()
      expect(api.copyTeamAvailabilityWeek).not.toHaveBeenCalled()
    },
  )

  it('blocks saves while the team timezone is invalid or the authoritative read failed', async () => {
    await render()
    await click('Edit availability')
    await change('Team time zone', 'Not/AZone', dialog())
    await submit()
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
    await change('Team time zone', 'UTC', dialog())
    readError = 'Unable to refresh availability'
    await render()
    expect(dialog().findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
  })

  it('discards the open editor when switching team or account', async () => {
    await render()
    await click('Edit availability')
    await change('Team time zone', 'Asia/Tokyo', dialog())
    await change(renderer!.root.findByProps({ 'aria-label': 'Availability team' }), '2')
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
    expect(renderer!.root.findAllByType('button').map(text)).not.toContain('Edit availability')
    await change(renderer!.root.findByProps({ 'aria-label': 'Availability team' }), '1')
    await click('Edit availability')
    expect(control('Team time zone', dialog()).props.value).toBe('UTC')
    props = { ...props, userId: 99 }
    await render()
    expect(renderer!.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
  })
})

describe('availability week copying and comparison', () => {
  it.each(['2026-02-30', '10000-01-01', '9999-12-25', ''])(
    'blocks invalid source and destination dates safely: %s',
    async (invalid) => {
      await render()
      await click('Copy week')
      await change('Source week starts', invalid, dialog())
      expect(button('Copy week', dialog()).props.disabled).toBe(true)
      await submit()
      await change('Source week starts', '2026-10-21', dialog())
      await change('Destination week starts', invalid, dialog())
      expect(button('Copy week', dialog()).props.disabled).toBe(true)
      await submit()
      expect(api.copyTeamAvailabilityWeek).not.toHaveBeenCalled()
    },
  )

  it('copies saved local dates with the saved revision and explains additive behavior', async () => {
    states.set(1, saved(1, [visibleWindow, futureWindow], 7, 'America/Los_Angeles'))
    await render()
    await change('Display time zone', 'Asia/Tokyo')
    await click('Copy week')
    expect(text(dialog())).toContain('local clock times in America/Los_Angeles')
    expect(text(dialog())).toContain('added to the destination; existing times stay saved')
    expect(control('Source week starts', dialog()).props.value).toBe('2026-10-21')
    await change('Destination week starts', '2026-11-04', dialog())
    await submit()
    expect(api.copyTeamAvailabilityWeek).toHaveBeenCalledWith({
      p_team: 1,
      p_revision: 7,
      p_source: '2026-10-21',
      p_target: '2026-11-04',
    })
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
    expect(control('Week starting').props.value).toBe('2026-11-04')
    expect(text(renderer!.root)).toContain('Existing destination times were kept')
  })

  it('does not copy an empty source week or copy a week onto itself', async () => {
    await render()
    await click('Copy week')
    await change('Source week starts', '2026-12-02', dialog())
    expect(text(dialog())).toContain('There are no saved times in this source week')
    expect(button('Copy week', dialog()).props.disabled).toBe(true)
    await submit()
    await change('Source week starts', '2026-10-21', dialog())
    await change('Destination week starts', '2026-10-21', dialog())
    expect(text(dialog())).toContain('Choose a different destination week')
    await submit()
    expect(api.copyTeamAvailabilityWeek).not.toHaveBeenCalled()
  })

  it('requires reviewing a newer revision before copying', async () => {
    await render()
    await click('Copy week')
    states.set(1, saved(1, [visibleWindow], 4, 'Asia/Kathmandu'))
    await render()
    expect(dialog().findByType('fieldset').props.disabled).toBe(true)
    await submit()
    expect(api.copyTeamAvailabilityWeek).not.toHaveBeenCalled()
    await click('Use latest saved times', dialog())
    expect(text(dialog())).toContain('local clock times in Asia/Kathmandu')
    await submit()
    expect(api.copyTeamAvailabilityWeek).toHaveBeenCalledWith(
      expect.objectContaining({ p_revision: 4 }),
    )
  })

  it('shows the intersection of each team’s union without double-counting overlaps', async () => {
    states.set(
      1,
      saved(1, [
        { starts_at: '2026-10-21T18:00:00Z', ends_at: '2026-10-21T20:00:00Z' },
        { starts_at: '2026-10-21T19:00:00Z', ends_at: '2026-10-21T21:00:00Z' },
        { starts_at: '2026-10-21T21:00:00Z', ends_at: '2026-10-21T22:00:00Z' },
      ]),
    )
    states.set(
      2,
      saved(2, [
        { starts_at: '2026-10-21T19:30:00Z', ends_at: '2026-10-21T22:00:00Z' },
        { starts_at: '2026-10-21T20:30:00Z', ends_at: '2026-10-21T23:00:00Z' },
      ]),
    )
    props = { ...props, initialCompareTeamId: 2 }
    await render()
    const comparison = renderer!.root.findByProps({ 'aria-label': 'Shared availability by day' })
    expect(text(comparison)).toContain('2.5 shared hours')
    const day = comparison
      .findAllByType('section')
      .find((node) => node.props.className === 'availability-day has-windows')!
    const shared = day.findAllByType('ul')[0]
    expect(shared.findAllByType('li')).toHaveLength(1)
    expect(shared.findByType('time').props.dateTime).toBe('2026-10-21T19:30:00Z')
    expect(text(comparison)).toContain('does not book a match')
    expect(api.setTeamAvailability).not.toHaveBeenCalled()
  })
})
