import type {
  AddTeamInput,
  CreateLeagueInput,
  Draftable,
  DraftActionInput,
  League,
  LeagueChangedEvent,
  LeagueEventHandlers,
  LeagueSnapshot,
  RpcResult,
  Session,
  SetDraftOrderInput,
  TeamRevision,
  UpdateLeagueInput,
  UpdateTeamInput,
  User,
} from './types'

const errorMessages: Record<string, string> = {
  sign_in_required: 'Your session has ended. Sign in to continue.',
  stale_revision:
    'The draft changed while you were working. Review the latest state and try again.',
  action_not_permitted: 'This action is not available to you for the current turn.',
  point_budget_exceeded: 'This Pokémon costs more than the team’s remaining points.',
  already_exists_or_claimed: 'That Pokémon is already picked, or that manager is already assigned.',
  league_not_found: 'This league is unavailable or you no longer have access.',
  team_not_found: 'That team no longer exists. Refresh the league and try again.',
  order_must_contain_every_team_once:
    'The draft order must include every team exactly once, with at least two teams.',
  csrf_failed: 'Your sign-in details have changed. Refresh the page before trying again.',
  origin_not_allowed: 'The frontend address does not match the backend’s PUBLIC_BASE_URL setting.',
  invalid_reference: 'One of the selected items no longer exists. Refresh and try again.',
  invalid_value: 'Check the names, numbers, and required fields before trying again.',
  invalid_draft_action: 'This draft action is invalid. Refresh the league and try again.',
  validation_error: 'Check the required fields and enter valid whole numbers.',
  database_busy: 'The server is busy. Wait a moment and refresh.',
  database_unavailable: 'The database is unavailable. Try refreshing in a moment.',
  database_error: 'The server could not complete this request. Try refreshing.',
  transaction_retry_exhausted: 'The draft is busy. Refresh its latest state before trying again.',
  google_login_not_configured: 'Google sign-in has not been configured on this server.',
  google_unavailable: 'Google sign-in is temporarily unavailable. Please try again.',
  google_login_failed: 'Google sign-in could not be completed. Please try again.',
  google_identity_missing: 'Google did not return a valid sign-in identity.',
  google_identity_invalid: 'Google did not return a valid sign-in identity.',
  test_user_not_found: 'This test account is unavailable on the connected backend.',
  test_login_header_required: 'The server rejected the test sign-in request.',
  application_json_required: 'The server could not read the sign-in request.',
  invalid_response:
    'The server returned an unexpected response. Check that the backend is running.',
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: unknown

  constructor(status: number, code: string, detail?: unknown) {
    super(errorMessages[code] ?? `The request failed (${status}). Please try again.`)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof TypeError) {
    return 'Could not reach the server. Check your connection and refresh before repeating an action.'
  }
  if (error instanceof Error) return error.message
  return 'Something went wrong. Please try again.'
}

// Keep the token in memory. The signed session itself stays in its HttpOnly cookie.
let csrfToken: string | null = null

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, credentials: 'same-origin' })
  if (response.status === 204) return undefined as T
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    if (response.status === 401) csrfToken = null
    throw new ApiError(response.status, 'invalid_response')
  }
  if (!response.ok) {
    if (response.status === 401) csrfToken = null
    const detail =
      typeof payload === 'object' && payload !== null && 'detail' in payload
        ? payload.detail
        : undefined
    const code =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? 'validation_error'
          : 'request_failed'
    throw new ApiError(response.status, code, detail)
  }
  return payload as T
}

async function session(signal?: AbortSignal): Promise<Session> {
  const value = await request<Session>('/auth/me', { signal })
  csrfToken = value.csrf_token
  return value
}

async function rpc<T>(name: string, body: object): Promise<RpcResult<T>> {
  if (!csrfToken) await session()
  // Never retry mutations automatically: an interrupted response may already have committed.
  return request<RpcResult<T>>(`/api/rpc/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken! },
    body: JSON.stringify(body),
  })
}

export const api = {
  session,
  async testLogin(username: string): Promise<Session> {
    const value = await request<Session>('/auth/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Test-Login': '1' },
      body: JSON.stringify({ username }),
    })
    csrfToken = value.csrf_token
    return value
  },
  async logout(): Promise<void> {
    if (!csrfToken) await session()
    await request<void>('/auth/logout', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken! },
    })
    csrfToken = null
  },
  users: (signal?: AbortSignal) => request<User[]>('/api/users', { signal }),
  draftables: (signal?: AbortSignal) => request<Draftable[]>('/api/draftables', { signal }),
  leagues: (signal?: AbortSignal) => request<League[]>('/api/leagues', { signal }),
  snapshot: (id: number, signal?: AbortSignal) =>
    request<LeagueSnapshot>(`/api/leagues/${id}`, { signal }),
  createLeague: (input: CreateLeagueInput) => rpc<number>('create_league', input),
  updateLeague: (input: UpdateLeagueInput) => rpc<null>('update_league', input),
  addTeam: (input: AddTeamInput) => rpc<number>('add_team', input),
  updateTeam: (input: UpdateTeamInput) => rpc<null>('update_team', input),
  removeTeam: (input: TeamRevision) => rpc<null>('remove_team', input),
  setDraftOrder: (input: SetDraftOrderInput) => rpc<null>('set_draft_order', input),
  draftAction: (input: DraftActionInput) => rpc<number | null>('draft_action', input),
}

/** Close on logout, account change, or league change. Native EventSource reconnects itself. */
export function subscribeLeague(id: number, handlers: LeagueEventHandlers): () => void {
  const source = new EventSource(`/api/leagues/${id}/events`)
  let closed = false
  const close = () => {
    if (closed) return
    closed = true
    source.close()
    handlers.onStatus?.('closed')
  }
  const parse = <T>(event: MessageEvent<string>, handle: (value: T) => void) => {
    if (closed) return
    let value: T
    try {
      value = JSON.parse(event.data) as T
    } catch {
      handlers.onError?.(new ApiError(200, 'invalid_response'))
      return
    }
    handle(value)
  }
  handlers.onStatus?.('connecting')
  source.addEventListener('open', () => {
    if (!closed) handlers.onStatus?.('connected')
  })
  source.addEventListener('snapshot', (event) => parse<LeagueSnapshot>(event, handlers.onSnapshot))
  source.addEventListener('changed', (event) =>
    parse<LeagueChangedEvent>(event, handlers.onChanged),
  )
  source.addEventListener('signed_out', () => {
    csrfToken = null
    close()
    handlers.onSignedOut?.()
  })
  source.addEventListener('access_revoked', () => {
    close()
    handlers.onAccessRevoked?.()
  })
  source.addEventListener('reconnect', () => {
    if (!closed) handlers.onStatus?.('reconnecting')
  })
  source.addEventListener('error', () => {
    if (closed) return
    handlers.onStatus?.(source.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting')
  })
  return close
}
