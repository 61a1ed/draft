import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Session } from './types'

const session: Session = {
  user: { id: 7, display_name: 'Alice' },
  csrf_token: 'session-csrf-token',
  expires_at: 2_000_000_000,
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
  vi.useRealTimers()
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

  it.each(['mutation', 'logout'] as const)(
    'does not dispatch a deferred %s after the account changes during its session lookup',
    async (kind) => {
      let finishOld!: (response: Response) => void
      const old = new Promise<Response>((resolve) => {
        finishOld = resolve
      })
      const newSession = {
        ...session,
        user: { id: 8, display_name: 'Bob' },
        csrf_token: 'bob-token',
      }
      const fetch = vi.fn().mockReturnValueOnce(old).mockResolvedValueOnce(json(newSession))
      vi.stubGlobal('fetch', fetch)
      const { api } = await import('./api')
      const pending =
        kind === 'mutation'
          ? api.createLeague({ p_name: 'Old account league', p_roster_size: 4 })
          : api.logout()
      const rejected = expect(pending).rejects.toMatchObject({ code: 'session_changed' })
      await api.testLogin('bob')
      finishOld(json(session))
      await rejected
      expect(fetch.mock.calls.map((call) => call[0])).toEqual(['/auth/me', '/auth/test'])
    },
  )

  it('retries transient session reads with bounded delays and does not retry authentication failures', async () => {
    vi.useFakeTimers()
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(json({ detail: 'database_unavailable' }, 503))
      .mockResolvedValueOnce(json(session))
      .mockResolvedValueOnce(json({ detail: 'sign_in_required' }, 401))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const pending = api.session()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetch).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(2000)
    expect(await pending).toEqual(session)
    await expect(api.session()).rejects.toMatchObject({ status: 401 })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetch).toHaveBeenCalledTimes(4)
  })
})

describe('account event stream', () => {
  it('refreshes on every invalidation and explicitly reconnects failed opens', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    const { subscribeAccount } = await import('./api')
    const handlers = { onInvalidate: vi.fn(), onStatus: vi.fn() }
    const cleanup = subscribeAccount(handlers)
    const source = FakeEventSource.instances[0]
    expect(source.url).toBe('/api/events')
    source.dispatchEvent(new Event('open'))
    expect(handlers.onStatus).toHaveBeenLastCalledWith('connected')
    source.emit('invalidate', null)
    source.emit('invalidate', null)
    expect(handlers.onInvalidate).toHaveBeenCalledTimes(2)
    source.dispatchEvent(new Event('error'))
    expect(handlers.onStatus).toHaveBeenLastCalledWith('reconnecting')
    expect(handlers.onInvalidate).toHaveBeenCalledTimes(3)
    expect(source.close).toHaveBeenCalledOnce()
    source.dispatchEvent(new Event('open'))
    source.emit('invalidate', null)
    expect(handlers.onInvalidate).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeEventSource.instances).toHaveLength(2)
    const recovered = FakeEventSource.instances[1]
    recovered.emit('invalidate', '')
    expect(handlers.onInvalidate).toHaveBeenCalledTimes(4)
    cleanup()
    recovered.emit('invalidate', null)
    expect(handlers.onInvalidate).toHaveBeenCalledTimes(4)
    expect(recovered.close).toHaveBeenCalledOnce()
  })

  it('backs off repeated failures up to 30 seconds and cancels pending reconnects', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    const { subscribeAccount } = await import('./api')
    const close = subscribeAccount({ onInvalidate: vi.fn() })
    for (const delay of [1000, 2000, 4000, 8000, 16_000, 30_000, 30_000]) {
      const count = FakeEventSource.instances.length
      const latest = FakeEventSource.instances[count - 1]
      latest.readyState = FakeEventSource.CLOSED
      latest.dispatchEvent(new Event('error'))
      await vi.advanceTimersByTimeAsync(delay - 1)
      expect(FakeEventSource.instances).toHaveLength(count)
      await vi.advanceTimersByTimeAsync(1)
      expect(FakeEventSource.instances).toHaveLength(count + 1)
    }
    FakeEventSource.instances.at(-1)!.dispatchEvent(new Event('error'))
    close()
    const count = FakeEventSource.instances.length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeEventSource.instances).toHaveLength(count)
  })

  it('recovers when construction throws and resets backoff only after LISTEN invalidates', async () => {
    vi.useFakeTimers()
    let fail = true
    class OpeningSource extends FakeEventSource {
      constructor(url: string) {
        if (fail) throw new Error('open failed')
        super(url)
      }
    }
    vi.stubGlobal('EventSource', OpeningSource)
    const { subscribeAccount } = await import('./api')
    const onInvalidate = vi.fn()
    const close = subscribeAccount({ onInvalidate })
    expect(onInvalidate).toHaveBeenCalledOnce()
    fail = false
    await vi.advanceTimersByTimeAsync(1000)
    FakeEventSource.instances[0].emit('invalidate', '')
    FakeEventSource.instances[0].dispatchEvent(new Event('error'))
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeEventSource.instances).toHaveLength(2)
    close()
  })

  it('keeps separate browser streams independent when one closes', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const { subscribeAccount } = await import('./api')
    const first = { onInvalidate: vi.fn() }
    const second = { onInvalidate: vi.fn() }
    const closeFirst = subscribeAccount(first)
    const closeSecond = subscribeAccount(second)
    closeFirst()
    FakeEventSource.instances[0].emit('invalidate', '')
    FakeEventSource.instances[1].emit('invalidate', '')
    expect(first.onInvalidate).not.toHaveBeenCalled()
    expect(second.onInvalidate).toHaveBeenCalledOnce()
    expect(FakeEventSource.instances[1].close).not.toHaveBeenCalled()
    closeSecond()
  })
})
