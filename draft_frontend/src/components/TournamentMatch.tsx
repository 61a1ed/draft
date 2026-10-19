import { useState, type FormEvent } from 'react'
import { CalendarDays, ExternalLink, RefreshCw } from 'lucide-react'
import { api, errorMessage } from '../lib/api'
import { teamName } from '../lib/teamPresentation'
import type { LeagueSnapshot, User } from '../lib/types'
import type {
  MatchDetail,
  MatchSide,
  OfficialResult,
  OutcomeKind,
  ResultOutcomeInput,
  ResultSubmission,
  TournamentSnapshot,
} from '../lib/tournamentTypes'
import { useTournamentMatch } from '../lib/useTournament'
import { Modal } from './Modal'

interface TournamentMatchProps {
  matchId: number
  tournament: TournamentSnapshot
  snapshot: LeagueSnapshot
  userId: number
  users: User[]
  refreshKey: number
  busy: boolean
  run: (operation: () => Promise<unknown>) => Promise<boolean>
  onClose: () => void
  onCompareAvailability?: (teamId: number, compareTeamId: number, date?: string) => void
}

type Names = { a: string; b: string }
type Outcome = Pick<
  ResultSubmission,
  'outcome_kind' | 'team_a_game_wins' | 'team_b_game_wins' | 'winner_side'
>
type OutcomeDraft = { kind: OutcomeKind; a: string; b: string; winner: MatchSide }

function outcomeText(outcome: Outcome | OfficialResult, names: Names) {
  const winner = names[outcome.winner_side]
  return outcome.outcome_kind === 'forfeit'
    ? `${winner} wins by forfeit`
    : `${names.a} ${outcome.team_a_game_wins} – ${outcome.team_b_game_wins} ${names.b}`
}

function outcomeDraft(outcome?: Outcome | null): OutcomeDraft {
  return {
    kind: outcome?.outcome_kind ?? 'played',
    a: outcome?.team_a_game_wins == null ? '' : String(outcome.team_a_game_wins),
    b: outcome?.team_b_game_wins == null ? '' : String(outcome.team_b_game_wins),
    winner: outcome?.winner_side ?? 'a',
  }
}

function outcomeInput(draft: OutcomeDraft, winsRequired: number): ResultOutcomeInput | null {
  if (draft.kind === 'forfeit')
    return { p_outcome_kind: 'forfeit', p_awarded_winner_side: draft.winner }
  if (!draft.a.trim() || !draft.b.trim()) return null
  const a = Number(draft.a)
  const b = Number(draft.b)
  if (
    !Number.isInteger(a) ||
    !Number.isInteger(b) ||
    a < 0 ||
    b < 0 ||
    !((a === winsRequired && b < winsRequired) || (b === winsRequired && a < winsRequired))
  )
    return null
  return { p_outcome_kind: 'played', p_team_a_game_wins: a, p_team_b_game_wins: b }
}

function safeVodUrl(value: string | null): string | null {
  if (!value || value.length > 4096 || /[\s\\\u0000-\u001f\u007f]/.test(value)) return null
  try {
    const url = new URL(value)
    return /^https?:\/\//i.test(value) &&
      ['http:', 'https:'].includes(url.protocol) &&
      !!url.hostname &&
      !url.username &&
      !url.password
      ? value
      : null
  } catch {
    return null
  }
}

function timestamp(value: string) {
  return new Date(value).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function OutcomeFields({
  draft,
  setDraft,
  names,
  winsRequired,
}: {
  draft: OutcomeDraft
  setDraft: (value: OutcomeDraft) => void
  names: Names
  winsRequired: number
}) {
  return (
    <>
      <label className="field">
        <span>Result type</span>
        <select
          value={draft.kind}
          onChange={(event) => setDraft({ ...draft, kind: event.target.value as OutcomeKind })}
        >
          <option value="played">Played series</option>
          <option value="forfeit">Forfeit</option>
        </select>
      </label>
      {draft.kind === 'played' ? (
        <>
          <div className="form-grid">
            <label className="field">
              <span>{names.a} (A) game wins</span>
              <input
                type="number"
                min="0"
                max={winsRequired}
                step="1"
                required
                value={draft.a}
                onChange={(event) => setDraft({ ...draft, a: event.target.value })}
              />
            </label>
            <label className="field">
              <span>{names.b} (B) game wins</span>
              <input
                type="number"
                min="0"
                max={winsRequired}
                step="1"
                required
                value={draft.b}
                onChange={(event) => setDraft({ ...draft, b: event.target.value })}
              />
            </label>
          </div>
          <p className="muted">
            The winner must have {winsRequired} game {winsRequired === 1 ? 'win' : 'wins'}; the
            other team must have fewer. Scores always follow the named A/B sides above.
          </p>
        </>
      ) : (
        <>
          <label className="field">
            <span>Team awarded the win</span>
            <select
              value={draft.winner}
              onChange={(event) => setDraft({ ...draft, winner: event.target.value as MatchSide })}
            >
              <option value="a">{names.a} (A)</option>
              <option value="b">{names.b} (B)</option>
            </select>
          </label>
          <p className="muted">A forfeit records a series win and loss without game scores.</p>
        </>
      )}
    </>
  )
}

function StaleEditor({ onCancel }: { onCancel: () => void }) {
  return (
    <div className="notice-banner tournament-stale" role="status">
      <p>
        This match changed while you were editing. Review the latest details before starting again.
      </p>
      <button type="button" className="button secondary" onClick={onCancel}>
        Discard edit and review
      </button>
    </div>
  )
}

interface EditorProps {
  match: MatchDetail
  names: Names
  busy: boolean
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
  onCancel: () => void
}

function ParticipantEditor({
  match,
  names,
  busy,
  mutate,
  onCancel,
  side,
  winsRequired,
}: EditorProps & { side: MatchSide; winsRequired: number }) {
  const currentId = side === 'a' ? match.team_a_submission_id : match.team_b_submission_id
  const [original] = useState({ matchId: match.id, side, submissionId: currentId })
  const [draft, setDraft] = useState(() =>
    outcomeDraft(match.submissions.find((submission) => submission.id === currentId)),
  )
  const [validation, setValidation] = useState('')
  const stale =
    match.id !== original.matchId ||
    side !== original.side ||
    currentId !== original.submissionId ||
    match.current_decision_id !== null ||
    match.disposition !== null
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy || stale) return
    const input = outcomeInput(draft, winsRequired)
    if (!input) {
      setValidation(`Enter a completed series: one team needs ${winsRequired} game wins.`)
      return
    }
    setValidation('')
    void mutate(() =>
      api.submitResult({
        p_match: original.matchId,
        p_expected_previous_submission_id: original.submissionId,
        ...input,
      }),
    )
  }
  return (
    <form className="tournament-editor" onSubmit={submit}>
      <h3>{original.submissionId === null ? 'Report result' : 'Replace your report'}</h3>
      <p className="muted">
        Reporting for {names[side]}. Matching reports from both teams make the result official
        automatically. Previous reports remain in the history.
      </p>
      {stale && !busy && <StaleEditor onCancel={onCancel} />}
      {validation && (
        <p className="inline-error" role="alert">
          {validation}
        </p>
      )}
      <fieldset className="form-fieldset" disabled={busy || stale}>
        <OutcomeFields
          draft={draft}
          setDraft={setDraft}
          names={names}
          winsRequired={winsRequired}
        />
        <div className="form-actions">
          <button type="submit" className="button primary">
            Submit your report
          </button>
          <button type="button" className="button secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </fieldset>
    </form>
  )
}

function CommissionerEditor({
  match,
  names,
  busy,
  mutate,
  onCancel,
  winsRequired,
}: EditorProps & { winsRequired: number }) {
  const [original] = useState(match)
  const correction = original.current_decision_id !== null
  const [source, setSource] = useState('manual')
  const [draft, setDraft] = useState(() => outcomeDraft(original.official_result))
  const [reason, setReason] = useState('')
  const [validation, setValidation] = useState('')
  const reports = original.submissions.filter(
    (submission) =>
      submission.id === original.team_a_submission_id ||
      submission.id === original.team_b_submission_id,
  )
  const stale = match.id !== original.id || match.revision !== original.revision
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy || stale) return
    if (!reason.trim()) {
      setValidation('Enter the reason for this decision.')
      return
    }
    const base = { p_match: original.id, p_revision: original.revision, p_reason: reason.trim() }
    if (!correction && source !== 'manual') {
      const selectedId = Number(source)
      if (!reports.some((report) => report.id === selectedId)) return
      setValidation('')
      void mutate(() => api.recordResult({ ...base, p_selected_submission: selectedId }))
      return
    }
    const input = outcomeInput(draft, winsRequired)
    if (!input) {
      setValidation(`Enter a completed series: one team needs ${winsRequired} game wins.`)
      return
    }
    setValidation('')
    void mutate(() =>
      correction
        ? api.correctResult({ ...base, ...input })
        : api.recordResult({ ...base, ...input }),
    )
  }
  return (
    <form className="tournament-editor" onSubmit={submit}>
      <h3>{correction ? 'Correct official result' : 'Commissioner decision'}</h3>
      <p className="muted">
        {correction
          ? 'A changed outcome and its reason stay in the history. An unchanged outcome keeps the existing decision. Published finalists remain unchanged.'
          : 'Select a current participant report or enter the official outcome. A reason is required.'}
        {correction && match.disposition && ' This result will remain excluded from records.'}
      </p>
      {stale && !busy && <StaleEditor onCancel={onCancel} />}
      {validation && (
        <p className="inline-error" role="alert">
          {validation}
        </p>
      )}
      <fieldset className="form-fieldset" disabled={busy || stale}>
        {!correction && (
          <label className="field">
            <span>Decision source</span>
            <select value={source} onChange={(event) => setSource(event.target.value)}>
              <option value="manual">Enter an outcome</option>
              {reports.map((report) => (
                <option key={report.id} value={report.id}>
                  Use {names[report.reporting_side!]}’s report: {outcomeText(report, names)}
                </option>
              ))}
            </select>
          </label>
        )}
        {(correction || source === 'manual') && (
          <OutcomeFields
            draft={draft}
            setDraft={setDraft}
            names={names}
            winsRequired={winsRequired}
          />
        )}
        <label className="field">
          <span>Reason for {correction ? 'correction' : 'decision'}</span>
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={3}
            required
          />
        </label>
        <div className="form-actions">
          <button type="submit" className="button primary">
            {correction ? 'Save correction' : 'Record official result'}
          </button>
          <button type="button" className="button secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </fieldset>
    </form>
  )
}

function VodEditor({ match, busy, mutate, onCancel }: EditorProps) {
  const [original] = useState({ id: match.id, revision: match.revision })
  const [url, setUrl] = useState(match.vod_url ?? '')
  const [validation, setValidation] = useState('')
  const stale = match.id !== original.id || match.revision !== original.revision
  const save = (event: FormEvent) => {
    event.preventDefault()
    if (busy || stale) return
    const value = url.trim()
    if (value && !safeVodUrl(value)) {
      setValidation('Enter a full http:// or https:// link without spaces or sign-in credentials.')
      return
    }
    setValidation('')
    void mutate(() =>
      api.setMatchVod({
        p_match: original.id,
        p_revision: original.revision,
        p_vod_url: value || null,
      }),
    )
  }
  return (
    <form className="tournament-editor" onSubmit={save}>
      <h3>Match video</h3>
      {stale && !busy && <StaleEditor onCancel={onCancel} />}
      {validation && (
        <p className="inline-error" role="alert">
          {validation}
        </p>
      )}
      <fieldset className="form-fieldset" disabled={busy || stale}>
        <label className="field">
          <span>Video link (VOD)</span>
          <input
            type="url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            maxLength={4096}
            placeholder="https://…"
          />
        </label>
        <p className="muted">Leave blank to remove the saved link.</p>
        <div className="form-actions">
          <button type="submit" className="button primary">
            Save video link
          </button>
          <button type="button" className="button secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </fieldset>
    </form>
  )
}

export function TournamentMatch(props: TournamentMatchProps) {
  return <TournamentMatchContent key={`${props.userId}:${props.matchId}`} {...props} />
}

function TournamentMatchContent({
  matchId,
  tournament,
  snapshot,
  userId,
  users,
  refreshKey,
  busy,
  run,
  onClose,
  onCompareAvailability,
}: TournamentMatchProps) {
  const { match, loading, error, refresh } = useTournamentMatch({
    matchId,
    refreshKey,
    identityKey: userId,
  })
  const [editor, setEditor] = useState<'participant' | 'commissioner' | 'vod' | null>(null)
  const [pending, setPending] = useState(false)
  const [actionError, setActionError] = useState('')
  const [success, setSuccess] = useState('')
  const name = (id: number) => {
    const team = snapshot.teams.find((item) => item.id === id)
    return team ? teamName(team) : `Team #${id}`
  }
  const author = (id: number | null) =>
    users.find((user) => user.id === id)?.display_name ?? `User ${id}`
  const mutate = async (operation: () => Promise<unknown>) => {
    if (pending || busy || loading) return false
    setPending(true)
    setActionError('')
    setSuccess('')
    try {
      const ok = await run(async () => {
        try {
          return await operation()
        } catch (cause) {
          setActionError(errorMessage(cause))
          throw cause
        }
      })
      if (ok) {
        setEditor(null)
        setSuccess('Saved.')
      } else {
        setActionError(
          (message) =>
            message || 'The action could not complete. Review the refreshed match details.',
        )
      }
      return ok
    } catch (cause) {
      setActionError(errorMessage(cause))
      return false
    } finally {
      // The server may have committed even when the response was interrupted.
      // Reload authoritative details after every attempt; never replay the operation.
      await refresh()
      setPending(false)
    }
  }
  const openEditor = (next: typeof editor) => {
    setActionError('')
    setSuccess('')
    setEditor(next)
  }
  const names: Names = match
    ? { a: name(match.team_a_id), b: name(match.team_b_id) }
    : { a: '', b: '' }
  const round = match ? tournament.rounds.find((item) => item.id === match.round_id) : undefined
  const stage = match ? tournament.stages.find((item) => item.id === match.stage_id) : undefined
  const participantSide: MatchSide | null = !match
    ? null
    : snapshot.teams.find((team) => team.id === match.team_a_id)?.manager_user_id === userId
      ? 'a'
      : snapshot.teams.find((team) => team.id === match.team_b_id)?.manager_user_id === userId
        ? 'b'
        : null
  const commissioner = snapshot.league.commissioner_user_id === userId
  const overviewMatch = tournament.matches.find((item) => item.id === matchId)
  const disabled =
    busy ||
    pending ||
    loading ||
    !!error ||
    !!(match && overviewMatch && overviewMatch.revision > match.revision)
  const reportA = match?.submissions.find((item) => item.id === match.team_a_submission_id)
  const reportB = match?.submissions.find((item) => item.id === match.team_b_submission_id)
  const official = match?.official_result
  const disposition = match?.disposition
  const withdrawal = disposition
    ? tournament.withdrawals.find((item) => item.team_id === disposition.withdrawal_team_id)
    : undefined
  const withdrawals = match
    ? tournament.withdrawals.filter((item) =>
        [match.team_a_id, match.team_b_id].includes(item.team_id),
      )
    : []
  const open = !!match && !match.current_decision_id && !disposition
  const vod = safeVodUrl(match?.vod_url ?? null)
  const editorProps = match
    ? { match, names, busy: disabled, mutate, onCancel: () => setEditor(null) }
    : null

  return (
    <Modal title={match ? `${names.a} vs ${names.b}` : 'Match details'} onClose={onClose}>
      <div className="tournament-match-detail">
        <div className="tournament-detail-toolbar">
          <p className="muted">
            {stage
              ? `${stage.name} · ${round?.purpose === 'tiebreak' ? 'Tiebreak' : 'Round'} ${round?.number ?? ''} · Best of ${stage.best_of}`
              : 'Loading match…'}
          </p>
          <button
            type="button"
            className="icon-button"
            aria-label="Refresh match details"
            disabled={pending || loading}
            onClick={() => void refresh()}
          >
            <RefreshCw size={16} />
          </button>
        </div>
        {loading && (
          <p className="muted" role="status">
            Refreshing match details…
          </p>
        )}
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        {actionError && (
          <p className="inline-error" role="alert">
            {actionError}
          </p>
        )}
        {success && (
          <p className="notice-banner" role="status">
            {success}
          </p>
        )}
        {match && (
          <>
            {onCompareAvailability && (
              <section className="tournament-match-section">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy || pending}
                  onClick={() =>
                    onCompareAvailability(
                      match.team_a_id,
                      match.team_b_id,
                      round?.suggested_start_date ?? undefined,
                    )
                  }
                >
                  <CalendarDays size={16} />
                  Compare availability
                </button>
                <p className="muted">
                  Find shared free time for these teams. Suggested weeks are flexible; browse any
                  dates and arrange the match together.
                </p>
              </section>
            )}
            <section className="tournament-match-section">
              <h3>
                {disposition
                  ? official
                    ? 'Excluded result'
                    : reportA || reportB
                      ? 'Excluded evidence'
                      : 'Cancelled fixture'
                  : official
                    ? 'Official result'
                    : reportA && reportB
                      ? 'Reports disagree'
                      : 'Awaiting reports'}
              </h3>
              {official ? (
                <>
                  <p className="tournament-official-score">{outcomeText(official, names)}</p>
                  <p className="muted">
                    {official.authority === 'agreement'
                      ? 'Confirmed automatically by matching participant reports'
                      : `Recorded by ${author(official.decided_by)}`}{' '}
                    · {timestamp(official.decided_at)}
                  </p>
                  {official.reason && <p className="tournament-reason">{official.reason}</p>}
                  <p className="muted">
                    {disposition
                      ? 'Excluded from all counted records.'
                      : round?.purpose === 'tiebreak'
                        ? 'Counted in this tiebreak attempt only; season totals are unchanged.'
                        : 'Counted in this stage’s records.'}{' '}
                    Participant reports are now frozen.
                  </p>
                </>
              ) : !disposition ? (
                <p className="muted">
                  {reportA && reportB
                    ? 'Either participant may replace their report, or the commissioner may resolve the difference.'
                    : 'Both participants report the outcome. Matching reports become official automatically.'}
                </p>
              ) : null}
              {disposition && (
                <div className="notice-banner tournament-withdrawal-note">
                  <p>
                    Removed because {name(disposition.withdrawal_team_id)} withdrew. This fixture
                    does not count in records.
                  </p>
                  {withdrawal && (
                    <>
                      <p>{withdrawal.reason}</p>
                      <p className="muted">
                        By {author(withdrawal.decided_by)} · {timestamp(withdrawal.decided_at)}
                      </p>
                    </>
                  )}
                  {!official && (
                    <p className="muted">
                      Further reports and an initial official result are unavailable.
                    </p>
                  )}
                </div>
              )}
              {!disposition && withdrawals.length > 0 && (
                <div className="notice-banner tournament-withdrawal-note">
                  {withdrawals.map((item) => (
                    <p key={item.team_id}>
                      {name(item.team_id)} withdrew: {item.reason}{' '}
                      <span className="muted">
                        By {author(item.decided_by)} · {timestamp(item.decided_at)}
                      </span>
                    </p>
                  ))}
                  <p className="muted">
                    This fixture remains in place. Withdrawal does not award a forfeit or change an
                    existing result.
                  </p>
                </div>
              )}
            </section>

            <section className="tournament-match-section">
              <h3>Participant reports</h3>
              <div className="tournament-reports">
                {(['a', 'b'] as const).map((side) => {
                  const report = side === 'a' ? reportA : reportB
                  return (
                    <article key={side} className="tournament-report">
                      <h4>
                        {side.toUpperCase()} · {names[side]}
                        {participantSide === side ? ' · Your team' : ''}
                      </h4>
                      {report ? (
                        <>
                          <p>{outcomeText(report, names)}</p>
                          <p className="muted">
                            {author(report.submitted_by)} · {timestamp(report.submitted_at)}
                          </p>
                        </>
                      ) : (
                        <p className="muted">No report submitted</p>
                      )}
                    </article>
                  )
                })}
              </div>
              {!editor && (
                <div className="form-actions">
                  {open && participantSide && stage && (
                    <button
                      type="button"
                      className="button primary"
                      disabled={disabled}
                      onClick={() => openEditor('participant')}
                    >
                      {(participantSide === 'a' ? reportA : reportB)
                        ? 'Replace your report'
                        : 'Report result'}
                    </button>
                  )}
                  {commissioner && stage && (open || official) && (
                    <button
                      type="button"
                      className="button secondary"
                      disabled={disabled}
                      onClick={() => openEditor('commissioner')}
                    >
                      {official ? 'Correct official result' : 'Record official result'}
                    </button>
                  )}
                </div>
              )}
            </section>

            {editorProps && stage && editor === 'participant' && participantSide && (
              <ParticipantEditor
                {...editorProps}
                side={participantSide}
                winsRequired={stage.wins_required}
              />
            )}
            {editorProps && stage && editor === 'commissioner' && commissioner && (
              <CommissionerEditor {...editorProps} winsRequired={stage.wins_required} />
            )}

            <section className="tournament-match-section">
              <h3>Match video</h3>
              {vod ? (
                <a
                  className="tournament-vod-link"
                  href={vod}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Watch match video <ExternalLink size={15} aria-hidden="true" />
                </a>
              ) : (
                <p className="muted">
                  {match.vod_url ? 'The saved video link is invalid.' : 'No video link added.'}
                </p>
              )}
              {!editor && (participantSide || commissioner) && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={disabled}
                  onClick={() => openEditor('vod')}
                >
                  {match.vod_url ? 'Edit or remove video link' : 'Add video link'}
                </button>
              )}
              {editorProps && editor === 'vod' && <VodEditor {...editorProps} />}
            </section>

            <details className="tournament-match-history">
              <summary>
                Report and decision history ({match.submissions.length + match.decisions.length})
              </summary>
              <p className="muted">
                Every report and official decision is retained, including replaced and excluded
                evidence.
              </p>
              <h4>All reports</h4>
              {!match.submissions.length && <p className="muted">No reports yet.</p>}
              <ol className="tournament-history">
                {match.submissions.map((report) => (
                  <li key={report.id} id={`tournament-submission-${report.id}`}>
                    <strong>
                      Report #{report.id} ·{' '}
                      {report.reporting_side
                        ? `${names[report.reporting_side]} (${report.reporting_side.toUpperCase()})`
                        : 'Commissioner outcome'}
                    </strong>
                    {(report.id === match.team_a_submission_id ||
                      report.id === match.team_b_submission_id) && (
                      <span className="tournament-badge">Current participant report</span>
                    )}
                    <p>{outcomeText(report, names)}</p>
                    <p className="muted">
                      By {author(report.submitted_by)} · {timestamp(report.submitted_at)}
                    </p>
                  </li>
                ))}
              </ol>
              <h4>All official decisions</h4>
              {!match.decisions.length && <p className="muted">No official decisions yet.</p>}
              <ol className="tournament-history">
                {match.decisions.map((decision) => {
                  const selected = match.submissions.find(
                    (report) => report.id === decision.selected_submission_id,
                  )
                  return (
                    <li key={decision.id}>
                      <strong>
                        Decision #{decision.id} ·{' '}
                        {decision.authority === 'agreement'
                          ? 'Participant agreement'
                          : 'Commissioner decision'}
                      </strong>
                      {decision.id === match.current_decision_id && (
                        <span className="tournament-badge">Current official decision</span>
                      )}
                      {selected && <p>{outcomeText(selected, names)}</p>}
                      <p className="muted">
                        {decision.authority === 'agreement'
                          ? 'Automatically confirmed'
                          : `By ${author(decision.decided_by)}`}{' '}
                        · {timestamp(decision.decided_at)}
                      </p>
                      {decision.reason && <p className="tournament-reason">{decision.reason}</p>}
                      <p className="muted">
                        Selected{' '}
                        <a href={`#tournament-submission-${decision.selected_submission_id}`}>
                          report #{decision.selected_submission_id}
                        </a>
                        {decision.supporting_submission_id !== null && (
                          <>
                            ; supported by{' '}
                            <a href={`#tournament-submission-${decision.supporting_submission_id}`}>
                              report #{decision.supporting_submission_id}
                            </a>
                          </>
                        )}
                        .
                      </p>
                    </li>
                  )
                })}
              </ol>
            </details>
          </>
        )}
      </div>
    </Modal>
  )
}
