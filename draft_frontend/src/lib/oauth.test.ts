import { describe, expect, it } from 'vitest'
import { inspectGooglePopup } from './oauth'

const origin = 'http://127.0.0.1:5173'

function popup(
  options: {
    path?: string
    origin?: string
    closed?: boolean
    readyState?: DocumentReadyState
    contentType?: string
    body?: string
  } = {},
): Window {
  return {
    closed: options.closed ?? false,
    location: {
      origin: options.origin ?? origin,
      pathname: options.path ?? '/auth/google/callback',
    },
    document: {
      readyState: options.readyState ?? 'complete',
      contentType: options.contentType ?? 'application/json',
      querySelector: () => null,
      body: {
        textContent: options.body ?? JSON.stringify({ user: { id: 42, display_name: 'Alice' } }),
      },
    },
  } as unknown as Window
}

describe('Google popup observation without session polling', () => {
  it('waits while the popup is on a different origin', () => {
    expect(inspectGooglePopup(popup({ origin: 'https://accounts.google.com' }), origin)).toEqual({
      status: 'pending',
    })
  })

  it('treats cross-origin SecurityError as pending', () => {
    const crossOrigin = {
      closed: false,
      get location() {
        throw new DOMException('Blocked a frame with origin', 'SecurityError')
      },
    } as unknown as Window
    expect(inspectGooglePopup(crossOrigin, origin)).toEqual({ status: 'pending' })
  })

  it('does not inspect documents outside the two backend auth paths', () => {
    const unrelated = popup({ path: '/another-page' })
    Object.defineProperty(unrelated, 'document', {
      get: () => {
        throw new Error('must not read unrelated document')
      },
    })
    expect(inspectGooglePopup(unrelated, origin)).toEqual({ status: 'pending' })
  })

  it('waits for loading documents and reports completed proxy errors', () => {
    expect(inspectGooglePopup(popup({ readyState: 'loading' }), origin)).toEqual({
      status: 'pending',
    })
    expect(
      inspectGooglePopup(
        popup({ path: '/auth/google', contentType: 'text/plain', body: 'Proxy unavailable' }),
        origin,
      ),
    ).toEqual({
      status: 'error',
      message: 'The sign-in page could not be loaded. Please try again.',
    })
  })

  it('recognizes a callback user without returning cookie or CSRF data', () => {
    const successful = popup({
      body: JSON.stringify({
        user: { id: 42, display_name: 'Alice' },
        csrf_token: 'never-return-this',
        expires_at: 2_000_000_000,
      }),
    })
    expect(inspectGooglePopup(successful, origin)).toEqual({ status: 'complete' })
  })

  it('reports readable errors on the initial auth path and the callback', () => {
    const initial = popup({
      path: '/auth/google',
      body: '{"detail":"google_login_not_configured"}',
    })
    expect(inspectGooglePopup(initial, origin)).toEqual({
      status: 'error',
      message: 'Google sign-in has not been configured on this server.',
    })
    const callback = popup({ body: '{"detail":"google_login_failed"}' })
    expect(inspectGooglePopup(callback, origin)).toEqual({
      status: 'error',
      message: 'Google sign-in could not be completed. Please try again.',
    })
  })

  it('does not treat an invalid JSON document or invalid user as a successful login', () => {
    expect(inspectGooglePopup(popup({ body: 'not json' }), origin).status).toBe('error')
    expect(
      inspectGooglePopup(popup({ body: '{"user":{"id":0,"display_name":"Alice"}}' }), origin)
        .status,
    ).toBe('error')
    expect(inspectGooglePopup(popup({ path: '/auth/google' }), origin).status).toBe('error')
  })

  it('reports a closed or missing popup without reading its location', () => {
    const closed = {
      closed: true,
      get location() {
        throw new Error('closed window')
      },
    } as unknown as Window
    expect(inspectGooglePopup(closed, origin)).toEqual({ status: 'closed' })
    expect(inspectGooglePopup(null, origin)).toEqual({ status: 'closed' })
  })
})
