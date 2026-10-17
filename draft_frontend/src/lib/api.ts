import type {
  AddTeamInput,
  CreateLeagueInput,
  Draftable,
  DraftActionInput,
  League,
  AccountEventHandlers,
  LeagueSnapshot,
  RpcResult,
  Session,
  SetDraftOrderInput,
  TeamRevision,
  UpdateLeagueInput,
  UpdateTeamInput,
  User,
} from './types'
import type {
  CorrectResultInput,
  CreateTiebreakRoundInput,
  MatchDetail,
  PublishFinalInput,
  PublishTournamentInput,
  RecordResultInput,
  SetMatchVodInput,
  SetRoundSuggestedDateInput,
  SubmitResultInput,
  TournamentSnapshot,
  WithdrawTeamInput,
} from './tournamentTypes'

const errorMessages: Record<string, string> = {
  sign_in_required: 'Your session has ended. Sign in to continue.',
  stale_revision:
    'The league or match changed while you were working. Review the latest state and try again.',
  action_not_permitted: 'This action is not available to you in the current state.',
  point_budget_exceeded: 'This Pokémon costs more than the team’s remaining points.',
  already_exists_or_claimed: 'That Pokémon is already picked, or that manager is already assigned.',
  league_not_found: 'This league is unavailable or you no longer have access.',
  team_not_found: 'That team no longer exists. Refresh the league and try again.',
  order_must_contain_every_team_once:
    'The draft order must include every team exactly once, with at least two teams.',
  csrf_failed: 'Your sign-in details have changed. Refresh the page before trying again.',
  session_changed:
    'Your sign-in details have changed. Review your current account before trying again.',
  origin_not_allowed: 'The frontend address does not match the backend’s PUBLIC_BASE_URL setting.',
  invalid_reference: 'One of the selected items no longer exists. Refresh and try again.',
  invalid_value: 'Check the names, numbers, and required fields before trying again.',
  invalid_draft_action: 'This draft action is invalid. Refresh the league and try again.',
  validation_error: 'Check the required fields and enter valid whole numbers.',
  database_busy: 'The server is busy. Wait a moment and refresh.',
  database_unavailable: 'The database is unavailable. Try refreshing in a moment.',
  database_error: 'The server could not complete this request. Try refreshing.',
  transaction_retry_exhausted: 'The draft is busy. Refresh its latest state before trying again.',
  oidc_login_not_configured: 'Sign-in has not been configured on this server.',
  oidc_unavailable: 'Sign-in is temporarily unavailable. Please try again.',
  oidc_login_failed: 'Sign-in could not be completed. Please try again.',
  oidc_identity_missing: 'The identity provider did not return a valid sign-in identity.',
  oidc_identity_invalid: 'The identity provider did not return a valid sign-in identity.',
  test_user_not_found: 'This test account is unavailable on the connected backend.',
  test_login_header_required: 'The server rejected the test sign-in request.',
  application_json_required: 'The server could not read the sign-in request.',
  invalid_response:
    'The server returned an unexpected response. Check that the backend is running.',
  tournament_not_found: 'This league has no published tournament.',
  tournament_already_published: 'The tournament has already been published. Review its schedule.',
  draft_not_completed: 'Complete the draft before publishing the tournament.',
  match_not_found: 'This match is no longer available. Refresh the tournament.',
  stage_not_found: 'This tournament stage is no longer available. Refresh the tournament.',
  invalid_suggested_date: 'Choose a valid calendar date for the suggested week.',
  invalid_best_of: 'Best-of values must be positive odd whole numbers.',
  invalid_result: 'Enter a completed series score, or choose the winner of a forfeit.',
  invalid_current_submission: 'The participant report changed. Review the latest reports.',
  stale_submission:
    'Your report changed while you were working. Review it before submitting again.',
  match_not_open: 'This match is already official or has been removed from competition.',
  reason_required: 'Enter a reason for this commissioner decision.',
  select_report_or_supply_result: 'Choose a participant report or enter a new result.',
  submission_not_current: 'That report has been replaced. Review the latest participant reports.',
  official_result_required: 'This match needs an official result before it can be corrected.',
  invalid_vod_url: 'Enter a complete HTTP or HTTPS link, or clear the field to remove the VOD.',
  team_already_withdrawn: 'This team has already withdrawn.',
  tiebreak_requires_source_round_robin: 'Tiebreak rounds can only be added to the season stage.',
  qualification_already_finalized: 'The finalists have already been selected.',
  at_least_two_teams_required: 'Select at least two eligible teams for the tiebreak.',
  distinct_teams_required: 'Select each tiebreak team only once.',
  ineligible_tiebreak_team:
    'A selected team is not eligible for this tiebreak. Review the entrants.',
  stage_matches_unresolved: 'Resolve all active season and tiebreak matches before progressing.',
  final_already_published: 'The final has already been published. Review the final fixture.',
  exactly_two_qualifiers_required: 'Select exactly two eligible finalists.',
  distinct_qualifiers_required: 'Select two different teams for the final.',
  ineligible_qualifier: 'A selected finalist is no longer eligible. Review the entrants.',
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
let sessionGeneration = 0

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const generation = sessionGeneration
  const response = await fetch(path, { ...options, credentials: 'same-origin' })
  if (response.status === 204) return undefined as T
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    if (response.status === 401 && generation === sessionGeneration) csrfToken = null
    throw new ApiError(response.status, 'invalid_response')
  }
  if (!response.ok) {
    if (response.status === 401 && generation === sessionGeneration) csrfToken = null
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
  const generation = ++sessionGeneration
  for (let attempt = 0; ; attempt++) {
    try {
      const value = await request<Session>('/auth/me', { signal })
      if (signal?.aborted) throw signal.reason
      // Do not resume an old implicit login lookup into a write using the newer account.
      if (generation !== sessionGeneration) throw new ApiError(409, 'session_changed')
      csrfToken = value.csrf_token
      return value
    } catch (cause) {
      if (
        signal?.aborted ||
        generation !== sessionGeneration ||
        attempt >= 2 ||
        (cause instanceof ApiError && cause.status < 500 && ![408, 429].includes(cause.status))
      )
        throw cause
      await new Promise<void>((resolve, reject) => {
        const cancel = () => {
          clearTimeout(timer)
          reject(signal?.reason)
        }
        const timer = setTimeout(
          () => {
            signal?.removeEventListener('abort', cancel)
            resolve()
          },
          1000 * 2 ** attempt,
        )
        signal?.addEventListener('abort', cancel, { once: true })
      })
    }
  }
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
    const generation = ++sessionGeneration
    const value = await request<Session>('/auth/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Test-Login': '1' },
      body: JSON.stringify({ username }),
    })
    if (generation === sessionGeneration) csrfToken = value.csrf_token
    return value
  },
  async logout(): Promise<void> {
    if (!csrfToken) await session()
    const generation = ++sessionGeneration
    await request<void>('/auth/logout', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken! },
    })
    if (generation === sessionGeneration) csrfToken = null
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
  tournament: (leagueId: number, signal?: AbortSignal) =>
    request<TournamentSnapshot>(`/api/leagues/${leagueId}/tournament`, { signal }),
  match: (matchId: number, signal?: AbortSignal) =>
    request<MatchDetail>(`/api/matches/${matchId}`, { signal }),
  publishTournament: (input: PublishTournamentInput) => rpc<null>('publish_tournament', input),
  setRoundSuggestedDate: (input: SetRoundSuggestedDateInput) =>
    rpc<null>('set_round_suggested_date', input),
  submitResult: (input: SubmitResultInput) => rpc<number>('submit_result', input),
  recordResult: (input: RecordResultInput) => rpc<number>('record_result', input),
  correctResult: (input: CorrectResultInput) => rpc<number>('correct_result', input),
  setMatchVod: (input: SetMatchVodInput) => rpc<null>('set_match_vod', input),
  withdrawTeam: (input: WithdrawTeamInput) => rpc<null>('withdraw_team', input),
  createTiebreakRound: (input: CreateTiebreakRoundInput) =>
    rpc<number>('create_tiebreak_round', input),
  publishFinal: (input: PublishFinalInput) => rpc<number>('publish_final', input),
}

/** One stream per signed-in browser tab. Explicit retries also recover failed HTTP opens. */
export function subscribeAccount(handlers: AccountEventHandlers): () => void {
  let source: EventSource | null = null
  let closed = false
  let retry: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  const close = () => {
    if (closed) return
    closed = true
    clearTimeout(retry)
    source?.close()
    source = null
    handlers.onStatus?.('closed')
  }
  const reconnect = () => {
    if (closed) return
    source?.close()
    source = null
    handlers.onStatus?.('reconnecting')
    // HTTP can explain an authorization failure that EventSource does not expose.
    handlers.onInvalidate()
    if (!closed) {
      retry = setTimeout(open, Math.min(1000 * 2 ** Math.min(failures++, 5), 30_000))
    }
  }
  const open = () => {
    if (closed) return
    handlers.onStatus?.(failures ? 'reconnecting' : 'connecting')
    try {
      const next = new EventSource('/api/events')
      source = next
      const valid = () => !closed && source === next
      next.addEventListener('open', () => {
        if (valid()) handlers.onStatus?.('connected')
      })
      next.addEventListener('invalidate', () => {
        if (!valid()) return
        // An initial invalidation proves LISTEN is established, not just the HTTP headers.
        failures = 0
        handlers.onInvalidate()
      })
      next.addEventListener('error', () => {
        if (valid()) reconnect()
      })
    } catch {
      reconnect()
    }
  }
  open()
  return close
}
