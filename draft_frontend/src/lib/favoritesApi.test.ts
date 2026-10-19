import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FavoritesList, FavoritesListSnapshot } from './favoritesTypes'

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function session() {
  return json({
    user: { id: 3, display_name: 'Manager' },
    csrf_token: 'token',
    expires_at: 2_000_000_000,
  })
}

beforeEach(() => vi.resetModules())
afterEach(() => vi.unstubAllGlobals())

describe('favorites API contract', () => {
  it('filters lists by account and reads ordered entries with cancellation', async () => {
    const list: FavoritesList = { id: 7, owner_user_id: 3, name: 'Rain team', revision: 4 }
    const snapshot: FavoritesListSnapshot = {
      ...list,
      entries: [
        { draftable_id: 42, ordinal: 1 },
        { draftable_id: 19, ordinal: 3 },
      ],
    }
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json([list]))
      .mockResolvedValueOnce(json(snapshot))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')
    const abort = new AbortController()

    expect(await api.favoritesLists(3, abort.signal)).toEqual([list])
    expect(await api.favoritesList(7, abort.signal)).toEqual(snapshot)
    expect(fetch.mock.calls).toEqual([
      [
        '/api/favorites-lists?owner_user_id=3',
        { credentials: 'same-origin', signal: abort.signal },
      ],
      ['/api/favorites-lists/7', { credentials: 'same-origin', signal: abort.signal }],
    ])
  })

  it('sends every favorites operation with its own list revision and CSRF token', async () => {
    const fetch = vi.fn().mockImplementation(async () => json({ result: null }))
    fetch.mockResolvedValueOnce(session()).mockResolvedValueOnce(json({ result: 7 }))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    expect(await api.createFavoritesList({ p_name: 'Rain team' })).toEqual({ result: 7 })
    expect(fetch).toHaveBeenLastCalledWith('/api/rpc/create_favorites_list', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'token' },
      body: JSON.stringify({ p_name: 'Rain team' }),
    })

    const operations = [
      [
        'rename_favorites_list',
        () => api.renameFavoritesList({ p_list: 7, p_revision: 0, p_name: 'Rain shortlist' }),
        { p_list: 7, p_revision: 0, p_name: 'Rain shortlist' },
      ],
      [
        'add_favorites_entry',
        () => api.addFavoritesEntry({ p_list: 7, p_revision: 1, p_draftable: 42 }),
        { p_list: 7, p_revision: 1, p_draftable: 42 },
      ],
      [
        'set_favorites_order',
        () => api.setFavoritesOrder({ p_list: 7, p_revision: 2, p_draftables: [42] }),
        { p_list: 7, p_revision: 2, p_draftables: [42] },
      ],
      [
        'remove_favorites_entry',
        () => api.removeFavoritesEntry({ p_list: 7, p_revision: 3, p_draftable: 42 }),
        { p_list: 7, p_revision: 3, p_draftable: 42 },
      ],
      [
        'set_favorites_order',
        () => api.setFavoritesOrder({ p_list: 7, p_revision: 4, p_draftables: [] }),
        { p_list: 7, p_revision: 4, p_draftables: [] },
      ],
      [
        'delete_favorites_list',
        () => api.deleteFavoritesList({ p_list: 7, p_revision: 5 }),
        { p_list: 7, p_revision: 5 },
      ],
    ] as const

    for (const [name, call, body] of operations) {
      expect(await call()).toEqual({ result: null })
      expect(fetch).toHaveBeenLastCalledWith(`/api/rpc/${name}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'token' },
        body: JSON.stringify(body),
      })
    }
    expect(fetch).toHaveBeenCalledTimes(operations.length + 2)
  })

  it.each([
    ['stale_revision', 409, 'changed while you were working'],
    ['action_not_permitted', 409, 'not available to you'],
    ['favorites_list_not_found', 404, 'favorites list no longer exists'],
    ['favorites_entry_not_found', 404, 'no longer in this list'],
    ['invalid_favorites_name', 400, 'Enter a name'],
    ['order_must_contain_every_draftable_once', 400, 'exactly once'],
  ])('explains %s without retrying the edit', async (code, status, message) => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(json({ detail: code }, status as number))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    await expect(
      api.setFavoritesOrder({ p_list: 7, p_revision: 2, p_draftables: [42, 19] }),
    ).rejects.toMatchObject({ code, status, message: expect.stringContaining(message as string) })
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('never repeats a mutation after an interrupted response that may have committed', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(session())
      .mockRejectedValueOnce(new TypeError('Connection lost after commit'))
    vi.stubGlobal('fetch', fetch)
    const { api } = await import('./api')

    await expect(
      api.addFavoritesEntry({ p_list: 7, p_revision: 0, p_draftable: 42 }),
    ).rejects.toThrow('Connection lost after commit')
    expect(fetch).toHaveBeenCalledTimes(2)
  })
})
