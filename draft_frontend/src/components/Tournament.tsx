import { useEffect, useState, type FormEvent } from 'react'
import { ArrowRight, CalendarDays, Check, Flag, RefreshCw, Swords, Trophy } from 'lucide-react'
import { api } from '../lib/api'
import type { LeagueSnapshot, Team, User } from '../lib/types'
import type { StageRecord, TournamentSnapshot } from '../lib/tournamentTypes'
import { useTournament } from '../lib/useTournament'
import { matchStatus, progression, roundByeTeamIds, suggestedWeek } from '../lib/tournamentView'
import { Modal } from './Modal'
import { TournamentMatch } from './TournamentMatch'

interface Props {
  snapshot: LeagueSnapshot
  userId: number
  users: User[]
  refreshKey: number
  busy: boolean
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
}
type Run = Props['mutate']
type Editor = {
  kind: 'date' | 'withdraw' | 'tiebreak' | 'final'
  revision: number
  roundId?: number
}
const teamName = (teams: Team[], id: number) =>
  teams.find((team) => team.id === id)?.name ?? `Team ${id}`

function Records({
  records,
  teams,
  qualifiers = [],
}: {
  records: StageRecord[]
  teams: Team[]
  qualifiers?: number[]
}) {
  const ordered = [...records].sort(
    (a, b) =>
      b.series_wins - a.series_wins ||
      teamName(teams, a.team_id).localeCompare(teamName(teams, b.team_id)),
  )
  return (
    <div className="tournament-table-scroll">
      <table className="tournament-table">
        <caption>
          Records ordered by series wins; qualification is selected by the commissioner.
        </caption>
        <thead>
          <tr>
            <th scope="col">Team</th>
            <th scope="col">Series</th>
            <th scope="col">W</th>
            <th scope="col">L</th>
            <th scope="col">Games W–L</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((record) => (
            <tr key={record.team_id}>
              <th scope="row">
                <span>{teamName(teams, record.team_id)}</span>
                {record.withdrawn && <small>Withdrawn</small>}
                {qualifiers.includes(record.team_id) && (
                  <small className="tournament-qualified">Finalist</small>
                )}
              </th>
              <td>{record.series_completed}</td>
              <td>
                <strong>{record.series_wins}</strong>
              </td>
              <td>{record.series_losses}</td>
              <td>
                {record.recorded_game_wins}–{record.recorded_game_losses}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Publication({
  snapshot,
  busy,
  run,
}: {
  snapshot: LeagueSnapshot
  busy: boolean
  run: Run
}) {
  const [revision, setRevision] = useState(snapshot.revision)
  const [date, setDate] = useState('')
  const [seasonBest, setSeasonBest] = useState('3')
  const [finalBest, setFinalBest] = useState('5')
  const [confirmed, setConfirmed] = useState(false)
  const changed = revision !== snapshot.revision
  const count = snapshot.teams.length
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || changed || !confirmed) return
    await run(() =>
      api.publishTournament({
        p_league: snapshot.league_id,
        p_league_revision: revision,
        p_first_suggested_date: date,
        p_season_best_of: Number(seasonBest),
        p_final_best_of: Number(finalBest),
      }),
    )
  }
  return (
    <section className="card tournament-publication">
      <span className="eyebrow">READY FOR THE NEXT CHAPTER</span>
      <h2>From draft room to match day</h2>
      <p>Publish one round robin for {count} teams, followed by a two-team final.</p>
      <form onSubmit={(event) => void submit(event)}>
        <fieldset className="form-fieldset" disabled={busy}>
          <div className="tournament-form-grid">
            <label className="field">
              First suggested week
              <input
                type="date"
                required
                value={date}
                onChange={(event) => setDate(event.target.value)}
              />
            </label>
            <label className="field">
              Season best of
              <input
                type="number"
                min="1"
                max="2147483647"
                step="2"
                required
                value={seasonBest}
                onChange={(event) => setSeasonBest(event.target.value)}
              />
            </label>
            <label className="field">
              Final best of
              <input
                type="number"
                min="1"
                max="2147483647"
                step="2"
                required
                value={finalBest}
                onChange={(event) => setFinalBest(event.target.value)}
              />
            </label>
          </div>
          <div className="tournament-preview">
            <CalendarDays size={20} />
            <div>
              <strong>
                {count % 2 === 0 ? count - 1 : count} season rounds · {(count * (count - 1)) / 2}{' '}
                matches
              </strong>
              <p>Suggested weeks are seven days apart. Teams can play and report early or late.</p>
            </div>
          </div>
          <label className="tournament-check">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>
              I understand publication fixes both best-of values and prevents further draft undo.
            </span>
          </label>
          {changed && (
            <div className="inline-error" role="alert">
              The draft changed. Review the current teams before publishing.{' '}
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  setRevision(snapshot.revision)
                  setConfirmed(false)
                }}
              >
                Review latest draft
              </button>
            </div>
          )}
          <button className="button primary" disabled={!confirmed || changed || count < 2}>
            <Trophy size={16} />
            {busy ? 'Publishing…' : 'Publish tournament'}
          </button>
        </fieldset>
      </form>
    </section>
  )
}

function ManagementForm({
  editor,
  state,
  snapshot,
  busy,
  error,
  run,
  onClose,
}: {
  editor: Editor
  state: TournamentSnapshot
  snapshot: LeagueSnapshot
  busy: boolean
  error: string
  run: Run
  onClose: () => void
}) {
  const current = progression(state)
  const round = state.rounds.find((item) => item.id === editor.roundId)
  const [date, setDate] = useState(round?.suggested_start_date ?? '')
  const [selected, setSelected] = useState<number[]>([])
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const stale = state.revision !== editor.revision
  const eligible = snapshot.teams.filter((team) => current.eligibleTeamIds.includes(team.id))
  const title = {
    date: 'Edit suggested week',
    withdraw: 'Withdraw a team',
    tiebreak: 'Create qualification tiebreak',
    final: 'Publish the final',
  }[editor.kind]
  const ready =
    !busy &&
    !stale &&
    (editor.kind === 'date' ||
      (confirmed &&
        (editor.kind === 'withdraw'
          ? selected.length === 1 && !!reason.trim()
          : current.unresolved.length === 0 &&
            !current.finalPublished &&
            (editor.kind === 'final'
              ? selected.length === 2
              : selected.length >= 2 && !!reason.trim()))))
  const affected = state.matches.filter(
    (match) =>
      (match.team_a_id === selected[0] || match.team_b_id === selected[0]) &&
      state.rounds.some(
        (item) => item.id === match.round_id && item.stage_id === current.source?.id,
      ) &&
      !state.dispositions.some((item) => item.match_id === match.id),
  )
  const toggle = (id: number) => {
    setConfirmed(false)
    setSelected((previous) =>
      previous.includes(id) ? previous.filter((item) => item !== id) : [...previous, id],
    )
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!ready) return
    const revision = editor.revision
    let operation: () => Promise<unknown>
    if (editor.kind === 'date')
      operation = () =>
        api.setRoundSuggestedDate({
          p_round: editor.roundId!,
          p_revision: revision,
          p_suggested_start_date: date || null,
        })
    else if (editor.kind === 'withdraw')
      operation = () =>
        api.withdrawTeam({
          p_league: snapshot.league_id,
          p_revision: revision,
          p_team: selected[0],
          p_reason: reason.trim(),
        })
    else if (editor.kind === 'tiebreak')
      operation = () =>
        api.createTiebreakRound({
          p_stage: current.source!.id,
          p_revision: revision,
          p_teams: selected,
          p_reason: reason.trim(),
          p_suggested_start_date: date || null,
        })
    else
      operation = () =>
        api.publishFinal({
          p_league: snapshot.league_id,
          p_revision: revision,
          p_qualifiers: selected,
          p_suggested_start_date: date || null,
        })
    if (await run(operation)) onClose()
  }
  return (
    <Modal
      title={title}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <form className="tournament-management" onSubmit={(event) => void submit(event)}>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
        {stale && (
          <p className="inline-error" role="alert">
            The tournament changed while this form was open. Close it and review the updated
            tournament before trying again.
          </p>
        )}
        <fieldset className="form-fieldset" disabled={busy || stale}>
          {editor.kind === 'date' ? (
            <p className="muted">
              This is a suggested week, not a reporting deadline. Leave the date empty to clear it.
            </p>
          ) : editor.kind === 'withdraw' ? (
            <label className="field">
              Team
              <select
                required
                value={selected[0] ?? ''}
                onChange={(event) => {
                  setSelected([Number(event.target.value)])
                  setConfirmed(false)
                }}
              >
                <option value="">Choose a team</option>
                {eligible.map((team) => (
                  <option key={team.id} value={team.id}>
                    {team.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <>
              <p className="muted">
                {editor.kind === 'final'
                  ? 'Select exactly two eligible qualifiers under your competition’s announced criteria. Selection order gives no advantage.'
                  : 'Select the teams still tied for qualification under your competition’s announced criteria.'}
              </p>
              <fieldset className="tournament-team-choices">
                <legend>
                  {editor.kind === 'final' ? 'Finalists' : 'Tied teams'} · {selected.length}{' '}
                  selected
                </legend>
                {eligible.map((team) => (
                  <label className="tournament-check" key={team.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(team.id)}
                      onChange={() => toggle(team.id)}
                    />
                    <span>{team.name}</span>
                  </label>
                ))}
              </fieldset>
            </>
          )}
          {editor.kind !== 'withdraw' && (
            <label className="field">
              Suggested week{editor.kind !== 'date' && ' (optional)'}
              <input type="date" value={date} onChange={(event) => setDate(event.target.value)} />
            </label>
          )}
          {(editor.kind === 'withdraw' || editor.kind === 'tiebreak') && (
            <label className="field">
              Reason
              <textarea
                required
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={3}
              />
            </label>
          )}
          {editor.kind === 'withdraw' && (
            <div className="tournament-preview">
              <Flag size={20} />
              <div>
                <strong>Withdrawal is permanent.</strong>
                <p>
                  {current.finalPublished
                    ? 'The final is already published. This records the withdrawal; season results, finalists, and the final match remain in place. Any forfeit must be recorded explicitly.'
                    : `${affected.length} active fixtures will be removed from counted records, including ${affected.filter((match) => match.current_decision_id !== null).length} official results. Existing reports and decisions remain in history.`}
                </p>
              </div>
            </div>
          )}
          {editor.kind === 'tiebreak' && (
            <div className="tournament-preview">
              <Swords size={20} />
              <div>
                <strong>
                  {(selected.length * (selected.length - 1)) / 2} new matches · best of{' '}
                  {current.source?.best_of}
                </strong>
                <p>
                  Each pair plays once. This attempt has its own records and does not change season
                  totals.
                </p>
              </div>
            </div>
          )}
          {editor.kind === 'final' && (
            <div className="tournament-preview">
              <Trophy size={20} />
              <div>
                <strong>
                  {selected.map((id) => teamName(snapshot.teams, id)).join(' vs ') ||
                    'Choose two finalists'}{' '}
                  · best of {current.final?.best_of}
                </strong>
                <p>
                  Publication fixes these two finalists. Later score corrections or withdrawals do
                  not replace them.
                </p>
              </div>
            </div>
          )}
          {editor.kind !== 'date' && (
            <label className="tournament-check">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              <span>
                {editor.kind === 'withdraw'
                  ? 'I have reviewed the withdrawal and its effects.'
                  : editor.kind === 'tiebreak'
                    ? 'I confirm these entrants remain tied for qualification.'
                    : 'I confirm these are the two qualifiers.'}
              </span>
            </label>
          )}
          <div className="modal-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              className={`button ${editor.kind === 'withdraw' ? 'danger' : 'primary'}`}
              disabled={!ready}
            >
              {busy ? 'Saving…' : editor.kind === 'date' ? 'Save suggested week' : title}
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  )
}

export function Tournament({ snapshot, userId, users, refreshKey, busy, mutate }: Props) {
  const { state, loading, error, readError, writeError, clearError, refresh, run } = useTournament({
    leagueId: snapshot.league_id,
    refreshKey,
    mutate,
  })
  const [editor, setEditor] = useState<Editor | null>(null)
  const [matchId, setMatchId] = useState<number | null>(null)
  const [onlyMine, setOnlyMine] = useState(false)
  const [filter, setFilter] = useState('all')
  const [hasRead, setHasRead] = useState(false)
  useEffect(() => {
    if (!loading && !readError) setHasRead(true)
  }, [loading, readError])
  const commissioner = snapshot.league.commissioner_user_id === userId
  const myTeam = snapshot.teams.find((team) => team.manager_user_id === userId)
  if (!state)
    return (
      <div className="tournament">
        {writeError && (
          <p className="inline-error" role="alert">
            {writeError}
          </p>
        )}
        {readError ? (
          <div className="card empty-state">
            <h2>Couldn’t load the tournament</h2>
            <p role="alert">{readError}</p>
            <button className="button secondary" onClick={() => void refresh()}>
              Try again
            </button>
          </div>
        ) : loading && !hasRead ? (
          <div className="card empty-state" role="status">
            Loading tournament…
          </div>
        ) : snapshot.state.status !== 'completed' ? (
          <div className="card empty-state">
            <Trophy size={34} />
            <h2>The season starts after the draft</h2>
            <p>Complete the draft to publish the round robin and prepare the final.</p>
          </div>
        ) : commissioner ? (
          <Publication snapshot={snapshot} busy={busy || loading} run={run} />
        ) : (
          <div className="card empty-state">
            <Trophy size={34} />
            <h2>Waiting for tournament publication</h2>
            <p>Your commissioner can now publish the season schedule.</p>
          </div>
        )}
      </div>
    )
  const advance = progression(state)
  const qualifiers = state.entries
    .filter((entry) => entry.stage_id === advance.final?.id)
    .map((entry) => entry.team_id)
  const finalMatch = state.matches.find((match) =>
    state.rounds.some(
      (round) => round.id === match.round_id && round.stage_id === advance.final?.id,
    ),
  )
  const finalResult = state.official_results.find((result) => result.match_id === finalMatch?.id)
  const champion =
    finalMatch && finalResult
      ? teamName(
          snapshot.teams,
          finalResult.winner_side === 'a' ? finalMatch.team_a_id : finalMatch.team_b_id,
        )
      : null
  const sourceRounds = state.rounds.filter((round) => round.stage_id === advance.source?.id)
  const sourceMatches = state.matches.filter((match) =>
    sourceRounds.some((round) => round.id === match.round_id && round.purpose === 'normal'),
  )
  const activeSource = sourceMatches.filter(
    (match) => !state.dispositions.some((item) => item.match_id === match.id),
  )
  const completed = activeSource.filter((match) => match.current_decision_id !== null).length
  const reviewCount = state.matches.filter(
    (match) => matchStatus(match, state).tone === 'review',
  ).length
  const open = (kind: Editor['kind'], roundId?: number) => {
    clearError()
    setEditor({ kind, revision: state.revision, roundId })
  }
  const visibleRounds = state.rounds
    .map((round) => ({
      round,
      matches: state.matches.filter(
        (match) =>
          match.round_id === round.id &&
          (!onlyMine || match.team_a_id === myTeam?.id || match.team_b_id === myTeam?.id) &&
          (filter === 'all' ||
            (filter === 'unresolved'
              ? matchStatus(match, state).unresolved
              : matchStatus(match, state).tone === 'review')),
      ),
    }))
    .filter(
      ({ matches, round }) =>
        matches.length ||
        (filter === 'all' &&
          (!onlyMine || roundByeTeamIds(round, state).includes(myTeam?.id ?? -1))),
    )
  return (
    <div className="tournament">
      {error && (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button className="button secondary" onClick={() => void refresh()}>
            Retry
          </button>
        </div>
      )}
      <section className={`tournament-hero ${champion ? 'tournament-champion' : ''}`}>
        <div className="tournament-hero-icon">
          <Trophy size={28} />
        </div>
        <div>
          <span className="eyebrow">
            {champion
              ? 'LEAGUE CHAMPION'
              : advance.finalPublished
                ? 'THE FINAL'
                : 'SEASON IN PROGRESS'}
          </span>
          <h2>
            {champion ??
              (advance.finalPublished
                ? qualifiers.map((id) => teamName(snapshot.teams, id)).join(' vs ')
                : 'Every match counts.')}
          </h2>
          <p>
            {champion
              ? 'Decided by the official final result.'
              : advance.finalPublished
                ? `Best of ${advance.final?.best_of} · ${finalResult ? 'Official result recorded' : 'Awaiting an official result'}`
                : `Round robin · season best of ${advance.source?.best_of} · final best of ${advance.final?.best_of}`}
          </p>
        </div>
        {finalMatch && (
          <button className="button secondary" onClick={() => setMatchId(finalMatch.id)}>
            View final
            <ArrowRight size={16} />
          </button>
        )}
      </section>
      <div className="tournament-overview">
        <section className="card tournament-standings">
          <div className="panel-title">
            <h2>Season standings</h2>
            <span className="count-pill">
              {completed} / {activeSource.length} resolved
            </span>
          </div>
          <Records
            records={state.stage_records.filter((record) => record.stage_id === advance.source?.id)}
            teams={snapshot.teams}
            qualifiers={qualifiers}
          />
          <p className="tournament-footnote">
            Official regular-season results only. Forfeits count as series wins/losses, without game
            scores. Tiebreak attempts appear below.
          </p>
        </section>
        <section className="card tournament-progress">
          <span className="eyebrow">ROAD TO THE FINAL</span>
          <h3>
            {advance.finalPublished
              ? 'Finalists confirmed'
              : advance.unresolved.length
                ? `${advance.unresolved.length} matches to resolve`
                : advance.eligibleTeamIds.length < 2
                  ? 'Not enough eligible teams'
                  : 'Ready for qualification'}
          </h3>
          <p>
            {advance.finalPublished
              ? 'Final membership is fixed. Season records continue to reflect any official corrections.'
              : advance.unresolved.length
                ? 'Every active season and tiebreak match needs an official result before qualification.'
                : advance.eligibleTeamIds.length < 2
                  ? 'At least two eligible teams are needed to publish a final. The tournament remains incomplete.'
                  : 'The commissioner can resolve a qualification tie or select two finalists using the announced criteria.'}
          </p>
          {reviewCount > 0 && (
            <button
              className="tournament-review-link"
              onClick={() => {
                setOnlyMine(false)
                setFilter('review')
              }}
            >
              {reviewCount} {reviewCount === 1 ? 'disagreement needs' : 'disagreements need'} review
              <ArrowRight size={14} />
            </button>
          )}
          {commissioner && (
            <div className="tournament-actions">
              {!advance.finalPublished && (
                <>
                  <button
                    className="button primary"
                    disabled={
                      busy || !!advance.unresolved.length || advance.eligibleTeamIds.length < 2
                    }
                    onClick={() => open('final')}
                  >
                    <Trophy size={15} />
                    Publish final
                  </button>
                  <button
                    className="button secondary"
                    disabled={
                      busy || !!advance.unresolved.length || advance.eligibleTeamIds.length < 2
                    }
                    onClick={() => open('tiebreak')}
                  >
                    <Swords size={15} />
                    Create tiebreak
                  </button>
                </>
              )}
              <button
                className="button text-button"
                disabled={busy || !advance.eligibleTeamIds.length}
                onClick={() => open('withdraw')}
              >
                <Flag size={15} />
                Withdraw a team
              </button>
            </div>
          )}
        </section>
      </div>
      <section className="tournament-schedule" aria-label="Tournament schedule">
        <div className="tournament-schedule-heading">
          <div>
            <span className="eyebrow">MATCH DAY</span>
            <h2>Schedule & results</h2>
            <p>Suggested weeks are flexible. Play and report whenever ready.</p>
          </div>
          <div className="tournament-filters">
            {myTeam && (
              <label className="tournament-check">
                <input
                  type="checkbox"
                  checked={onlyMine}
                  onChange={(event) => setOnlyMine(event.target.checked)}
                />
                My matches
              </label>
            )}
            <label className="field">
              <span className="sr-only">Filter matches</span>
              <select value={filter} onChange={(event) => setFilter(event.target.value)}>
                <option value="all">All matches</option>
                <option value="unresolved">Unresolved</option>
                <option value="review">Disagreements</option>
              </select>
            </label>
            <button
              className="icon-button"
              aria-label="Refresh tournament"
              disabled={busy || loading}
              onClick={() => void refresh()}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </div>
        {visibleRounds.length === 0 && (
          <div className="card empty-state">
            <Check size={28} />
            <h3>No matches in this view</h3>
            <button
              className="button secondary"
              onClick={() => {
                setFilter('all')
                setOnlyMine(false)
              }}
            >
              Show all matches
            </button>
          </div>
        )}
        <div className="tournament-rounds">
          {visibleRounds.map(({ round, matches }) => {
            const isFinal = round.stage_id === advance.final?.id
            const byes = roundByeTeamIds(round, state).filter(
              (id) => !onlyMine || id === myTeam?.id,
            )
            const tieAttempt =
              sourceRounds
                .filter((item) => item.purpose === 'tiebreak')
                .findIndex((item) => item.id === round.id) + 1
            return (
              <section className="card tournament-round" key={round.id}>
                <div className="tournament-round-heading">
                  <div>
                    <h3>
                      {isFinal
                        ? 'Final'
                        : round.purpose === 'tiebreak'
                          ? `Qualification tiebreak ${tieAttempt}`
                          : `Round ${round.number}`}
                    </h3>
                    <span>
                      <CalendarDays size={13} />
                      {suggestedWeek(round.suggested_start_date)}
                    </span>
                  </div>
                  {commissioner && (
                    <button
                      className="button text-button"
                      disabled={busy}
                      onClick={() => open('date', round.id)}
                    >
                      Edit week
                    </button>
                  )}
                </div>
                {round.tiebreak_reason && (
                  <p className="tournament-tiebreak-reason">{round.tiebreak_reason}</p>
                )}
                <div className="tournament-fixtures">
                  {matches.map((match) => {
                    const status = matchStatus(match, state)
                    const result = state.official_results.find((item) => item.match_id === match.id)
                    const score = result
                      ? result.outcome_kind === 'forfeit'
                        ? 'Forfeit'
                        : `${result.team_a_game_wins} – ${result.team_b_game_wins}`
                      : 'vs'
                    return (
                      <button
                        className="tournament-fixture"
                        key={match.id}
                        aria-label={
                          result?.outcome_kind === 'forfeit'
                            ? `${teamName(snapshot.teams, match.team_a_id)} vs ${teamName(snapshot.teams, match.team_b_id)}. ${teamName(snapshot.teams, result.winner_side === 'a' ? match.team_a_id : match.team_b_id)} wins by forfeit. ${status.label}`
                            : undefined
                        }
                        onClick={() => setMatchId(match.id)}
                      >
                        <span className={result?.winner_side === 'a' ? 'tournament-winner' : ''}>
                          {teamName(snapshot.teams, match.team_a_id)}
                          {myTeam?.id === match.team_a_id && <small>Your team</small>}
                        </span>
                        <strong className="tournament-score">{score}</strong>
                        <span className={result?.winner_side === 'b' ? 'tournament-winner' : ''}>
                          {teamName(snapshot.teams, match.team_b_id)}
                          {myTeam?.id === match.team_b_id && <small>Your team</small>}
                        </span>
                        <span className={`tournament-status ${status.tone}`}>{status.label}</span>
                        <ArrowRight size={15} />
                      </button>
                    )
                  })}
                </div>
                {!!byes.length && (
                  <p className="tournament-bye">
                    Bye: {byes.map((id) => teamName(snapshot.teams, id)).join(', ')} · no result or
                    win awarded
                  </p>
                )}
                {round.purpose === 'tiebreak' && (
                  <details className="tournament-round-records">
                    <summary>View this attempt’s records</summary>
                    <Records
                      records={state.round_records.filter((record) => record.round_id === round.id)}
                      teams={snapshot.teams}
                    />
                  </details>
                )}
              </section>
            )
          })}
        </div>
      </section>
      {!!state.withdrawals.length && (
        <section className="card tournament-withdrawals">
          <h3>Withdrawals</h3>
          {state.withdrawals.map((withdrawal) => (
            <div key={withdrawal.team_id}>
              <strong>{teamName(snapshot.teams, withdrawal.team_id)}</strong>
              <p>{withdrawal.reason}</p>
              <small>
                {users.find((user) => user.id === withdrawal.decided_by)?.display_name ??
                  `User ${withdrawal.decided_by}`}{' '}
                · {new Date(withdrawal.decided_at).toLocaleString()}
              </small>
            </div>
          ))}
        </section>
      )}
      {editor && (
        <ManagementForm
          key={`${editor.kind}:${editor.roundId ?? ''}:${editor.revision}`}
          editor={editor}
          state={state}
          snapshot={snapshot}
          busy={busy}
          error={error}
          run={run}
          onClose={() => setEditor(null)}
        />
      )}
      {matchId !== null && (
        <TournamentMatch
          key={matchId}
          matchId={matchId}
          tournament={state}
          snapshot={snapshot}
          userId={userId}
          users={users}
          refreshKey={refreshKey}
          busy={busy}
          run={run}
          onClose={() => setMatchId(null)}
        />
      )}
    </div>
  )
}
