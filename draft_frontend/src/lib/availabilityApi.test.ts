import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function session() {
  return json({
    user: { id: 1, display_name: 'Manager' },
    csrf_token: 'token',
    expires_at: 2_000_000_000,
  })
}

beforeEach(() => vi.resetModules())
afterEach(() => vi.unstubAllGlobals())

describe('availability API contract', () => {
  it('reads an existing team with no availability header and forwards cancellation', async () => {
    const state = { team_id: 7, availability: null, windows: [] }
    const fetch = vi.fn().mockResolvedValue(json(state))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const abort = new AbortController()
    expect(await api.teamAvailability(7, abort.signal)).toEqual(state)
    expect(fetch).toHaveBeenCalledWith('/api/teams/7/availability', {
      credentials: 'same-origin',
      signal: abort.signal,
    })
  })

  it('sends first-save, full replacement, clearing, and week-copy bodies unchanged', async () => {
    const fetch = vi.fn().mockImplementation(async () => json({ result: null }))
    fetch.mockResolvedValueOnce(session())
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const initial = {
      p_team: 7,
      p_revision: null,
      p_time_zone: 'Asia/Kathmandu',
      p_display_step_minutes: 60,
      p_windows: [{ starts_at: '2026-10-19T09:00:00+05:45', ends_at: '2026-10-19T12:00:00+05:45' }],
    }
    const replacement = {
      ...initial,
      p_revision: 0,
      p_time_zone: 'America/Los_Angeles',
      p_display_step_minutes: 30,
      p_windows: [
        ...initial.p_windows,
        { starts_at: '2026-10-27T03:15:00Z', ends_at: '2026-10-27T05:15:00Z' },
      ],
    }
    const cleared = { ...replacement, p_revision: 1, p_windows: [] }
    const copy = { p_team: 7, p_revision: 2, p_source: '2026-10-19', p_target: '2026-10-26' }
    for (const body of [initial, replacement, cleared]) {
      await api.setTeamAvailability(body)
      expect(fetch).toHaveBeenLastCalledWith('/api/rpc/set_team_availability', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'token' },
        body: JSON.stringify(body),
      })
    }
    await api.copyTeamAvailabilityWeek(copy)
    expect(fetch).toHaveBeenLastCalledWith('/api/rpc/copy_team_availability_week', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'token' },
      body: JSON.stringify(copy),
    })
    expect(fetch).toHaveBeenCalledTimes(5)
  })

  it.each([
    ['stale_revision', 409, 'changed while you were working'],
    ['invalid_time_zone', 400, 'valid IANA time zone'],
    ['invalid_availability', 400, 'availability windows'],
    ['invalid_availability_window', 400, 'end after it starts'],
    ['invalid_availability_week', 400, 'source and destination dates'],
    ['availability_not_found', 404, 'Save your availability'],
    ['nonexistent_local_boundary', 400, 'daylight saving time gap'],
  ])('explains %s without retrying the write', async (code, status, message) => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(json({ detail: code }, status as number))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    await expect(
      api.copyTeamAvailabilityWeek({
        p_team: 7,
        p_revision: 2,
        p_source: '2026-10-19',
        p_target: '2026-10-26',
      }),
    ).rejects.toMatchObject({ code, status, message: expect.stringContaining(message as string) })
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('never retries an interrupted save that may have committed', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(session())
      .mockRejectedValueOnce(new TypeError('Connection lost after commit'))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    await expect(
      api.setTeamAvailability({
        p_team: 7,
        p_revision: null,
        p_time_zone: 'UTC',
        p_display_step_minutes: 60,
        p_windows: [],
      }),
    ).rejects.toThrow('Connection lost after commit')
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
