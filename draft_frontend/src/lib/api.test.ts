import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LeagueSnapshot, Session } from './types'

const session: Session = {
  user: { id: 7, display_name: 'Alice' },
  csrf_token: 'session-csrf-token',
  expires_at: 2_000_000_000,
}

const snapshot: LeagueSnapshot = {
  league_id: 3,
  revision: 4,
  server_now: '2026-10-13T12:00:00Z',
  league: {
    id: 3,
    name: 'Autumn League',
    commissioner_user_id: 7,
    roster_size: 3,
    pick_seconds: 60,
    revision: 4,
    created_at: '2026-10-13T12:00:00Z',
    started_at: null,
    turn_started_at: null,
  },
  state: {
    league_id: 3,
    revision: 4,
    status: 'setup',
    roster_size: 3,
    team_count: 0,
    active_pick_count: 0,
    required_pick_count: 0,
    can_start: false,
    next_pick_number: null,
    next_team_id: null,
    last_pick_id: null,
    deadline: null,
  },
  teams: [],
  rosters: [],
  history: [],
  actions: [],
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

class FakeEventSource extends EventTarget {
  static CLOSED = 2
  static instances: FakeEventSource[] = []
  readonly url: string
  readyState = 0
  close = vi.fn(() => {
    this.readyState = FakeEventSource.CLOSED
  })

  constructor(url: string) {
    super()
    this.url = url
    FakeEventSource.instances.push(this)
  }

  emit(type: string, data: unknown) {
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) }))
  }
}

beforeEach(() => {
  vi.resetModules()
  FakeEventSource.instances = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('backend API contract', () => {
  it('obtains a session token and sends the exact revision and free-pick flag once', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(json({ result: 25 }))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const input = {
      p_league: 3,
      p_revision: 4,
      p_kind: 'pick' as const,
      p_draftable: 42,
      p_charge_points: false,
    }

    expect(await api.draftAction(input)).toEqual({ result: 25 })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[0]).toEqual([
      '/auth/me',
      {
        credentials: 'same-origin',
        signal: undefined,
      },
    ])
    expect(fetch.mock.calls[1]).toEqual([
      '/api/rpc/draft_action',
      {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token },
        body: JSON.stringify(input),
      },
    ])
  })

  it('uses the restricted test-login protocol then reuses its CSRF token for logout', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    expect(await api.testLogin('alice')).toEqual(session)
    await api.logout()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[0]).toEqual([
      '/auth/test',
      {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Test-Login': '1' },
        body: '{"username":"alice"}',
      },
    ])
    expect(fetch.mock.calls[1][1].headers).toEqual({ 'X-CSRF-Token': session.csrf_token })
  })

  it('preserves stale-revision conflicts and never retries or substitutes the revision', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(json({ detail: 'stale_revision' }, 409))
    vi.stubGlobal('fetch', fetch)
    const { api, ApiError } = await import('./api')
    await api.session()

    const write = api.setDraftOrder({ p_league: 3, p_revision: 4, p_teams: [6, 2] })
    await expect(write).rejects.toBeInstanceOf(ApiError)
    await expect(write).rejects.toMatchObject({ status: 409, code: 'stale_revision' })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      p_league: 3,
      p_revision: 4,
      p_teams: [6, 2],
    })
  })

  it('does not repeat a mutation after a network failure with an unknown outcome', async () => {
    const failed = new TypeError('fetch failed')
    const fetch = vi.fn().mockResolvedValueOnce(json(session)).mockRejectedValueOnce(failed)
    vi.stubGlobal('fetch', fetch)
    const { api, errorMessage } = await import('./api')

    await expect(api.createLeague({ p_name: 'Autumn', p_roster_size: 4 })).rejects.toBe(failed)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(errorMessage(failed)).toContain('refresh before repeating an action')
  })

  it('clears an expired session token before any future write', async () => {
    const fresh = { ...session, csrf_token: 'fresh-token' }
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(json({ detail: 'sign_in_required' }, 401))
      .mockResolvedValueOnce(json(fresh))
      .mockResolvedValueOnce(json({ result: 8 }))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    await api.session()
    await expect(api.leagues()).rejects.toMatchObject({ status: 401 })
    await api.createLeague({ p_name: 'Autumn', p_roster_size: 4 })

    expect(fetch.mock.calls[2][0]).toBe('/auth/me')
    expect(fetch.mock.calls[3][1].headers['X-CSRF-Token']).toBe('fresh-token')
  })

  it('exposes FastAPI field validation as a readable error with details preserved', async () => {
    const detail = [{ loc: ['body', 'p_roster_size'], msg: 'Input should be greater than 0' }]
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(json({ detail }, 422))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    await expect(api.createLeague({ p_name: 'Autumn', p_roster_size: 0 })).rejects.toMatchObject({
      status: 422,
      code: 'validation_error',
      detail,
    })
  })
})

describe('league event stream', () => {
  it('parses snapshots and invalidations, reports reconnecting, and closes on access revocation', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const { subscribeLeague } = await import('./api')
    const handlers = {
      onSnapshot: vi.fn(),
      onChanged: vi.fn(),
      onStatus: vi.fn(),
      onAccessRevoked: vi.fn(),
    }
    const cleanup = subscribeLeague(3, handlers)
    const source = FakeEventSource.instances[0]

    expect(source.url).toBe('/api/leagues/3/events')
    expect(handlers.onStatus).toHaveBeenCalledWith('connecting')
    source.dispatchEvent(new Event('open'))
    source.emit('snapshot', snapshot)
    source.emit('changed', { league_id: 3, revision: 5 })
    source.emit('reconnect', {})
    expect(handlers.onSnapshot).toHaveBeenCalledWith(snapshot)
    expect(handlers.onChanged).toHaveBeenCalledWith({ league_id: 3, revision: 5 })
    expect(handlers.onStatus).toHaveBeenLastCalledWith('reconnecting')

    source.emit('access_revoked', {})
    expect(handlers.onAccessRevoked).toHaveBeenCalledOnce()
    expect(source.close).toHaveBeenCalledOnce()
    expect(handlers.onStatus).toHaveBeenLastCalledWith('closed')
    source.emit('snapshot', snapshot)
    expect(handlers.onSnapshot).toHaveBeenCalledOnce()
    cleanup()
    expect(source.close).toHaveBeenCalledOnce()
  })

  it('closes on session expiry and reports malformed messages without applying them', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const { subscribeLeague } = await import('./api')
    const handlers = {
      onSnapshot: vi.fn(),
      onChanged: vi.fn(),
      onError: vi.fn(),
      onSignedOut: vi.fn(),
    }
    subscribeLeague(3, handlers)
    const source = FakeEventSource.instances[0]

    source.dispatchEvent(new MessageEvent('snapshot', { data: '{broken' }))
    expect(handlers.onError).toHaveBeenCalledOnce()
    expect(handlers.onSnapshot).not.toHaveBeenCalled()
    source.emit('signed_out', {})
    expect(source.close).toHaveBeenCalledOnce()
    expect(handlers.onSignedOut).toHaveBeenCalledOnce()
  })
})
