import { ApiError } from './api'

export type GooglePopupState =
  | { status: 'pending' | 'complete' | 'closed'; message?: never }
  | { status: 'error'; message: string }

const unexpectedResponse = 'Google sign-in returned an unexpected response. Please try again.'

/**
 * Inspect the popup without touching /auth/me: an unauthenticated session read
 * clears the cookie containing the pending OAuth state on this backend.
 */
export function inspectGooglePopup(popup: Window | null, expectedOrigin: string): GooglePopupState {
  if (!popup || popup.closed) return { status: 'closed' }
  try {
    const location = popup.location
    if (location.origin !== expectedOrigin) return { status: 'pending' }
    const path = location.pathname
    if (path !== '/auth/google' && path !== '/auth/google/callback') return { status: 'pending' }

    const document = popup.document
    if (document.readyState !== 'complete') return { status: 'pending' }
    if (document.contentType.split(';', 1)[0].trim() !== 'application/json') {
      return { status: 'error', message: 'The sign-in page could not be loaded. Please try again.' }
    }
    let payload: unknown
    try {
      // Native JSON viewers commonly place the response in a <pre> element.
      payload = JSON.parse(document.querySelector('pre')?.textContent ?? document.body?.textContent ?? '')
    } catch {
      return { status: 'error', message: unexpectedResponse }
    }
    if (typeof payload !== 'object' || payload === null)
      return { status: 'error', message: unexpectedResponse }
    if ('detail' in payload) {
      const message =
        typeof payload.detail === 'string'
          ? new ApiError(400, payload.detail).message
          : unexpectedResponse
      return { status: 'error', message }
    }
    if (
      path === '/auth/google/callback' &&
      'user' in payload &&
      typeof payload.user === 'object' &&
      payload.user !== null &&
      'id' in payload.user &&
      Number.isInteger(payload.user.id) &&
      Number(payload.user.id) > 0 &&
      'display_name' in payload.user &&
      typeof payload.user.display_name === 'string'
    ) {
      return { status: 'complete' }
    }
    return { status: 'error', message: unexpectedResponse }
  } catch (cause) {
    // Google is intentionally cross-origin while the user completes authorization.
    if (cause && typeof cause === 'object' && 'name' in cause && cause.name === 'SecurityError') {
      return { status: 'pending' }
    }
    return {
      status: 'error',
      message: 'The sign-in window could not be read. Close it and try again.',
    }
  }
}
