import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Clock3,
  Trophy,
  Zap,
} from 'lucide-react'
import type { Draftable, LeagueSnapshot, RosterPick, Team } from '../lib/types'
import { teamAvatarStyle, teamName } from '../lib/teamPresentation'
import { PokemonImage } from './Catalog'

function initials(name: string) {
  return name
    .split(' ')
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
}

export function getTeamForOverallPick(overallPick: number, orderedTeams: Team[]) {
  const teamCount = orderedTeams.length
  const roundIndex = Math.floor((overallPick - 1) / teamCount)
  const offset = (overallPick - 1) % teamCount
  const colIndex = roundIndex % 2 === 0 ? offset : teamCount - 1 - offset
  const pickInRound = offset + 1
  const coord = `${roundIndex + 1}.${String(pickInRound).padStart(2, '0')}`
  return {
    team: orderedTeams[colIndex],
    roundIndex,
    colIndex,
    pickInRound,
    coord,
  }
}

function useTurnSeconds(snapshot: LeagueSnapshot) {
  const [now, setNow] = useState(Date.now())
  const timing = useMemo(() => {
    const receivedAt = Date.now()
    return { receivedAt, offset: new Date(snapshot.server_now).getTime() - receivedAt }
  }, [snapshot.server_now])

  useEffect(() => {
    if (snapshot.state.status !== 'drafting' || !snapshot.state.deadline) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [snapshot.state.status, snapshot.state.deadline])

  if (!snapshot.state.deadline) return null
  return Math.max(
    0,
    Math.ceil(
      (new Date(snapshot.state.deadline).getTime() -
        Math.max(now, timing.receivedAt) -
        timing.offset) /
        1000,
    ),
  )
}

function formatTimer(seconds: number | null) {
  if (seconds === null) return '—:—'
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function useTurnBannerState(snapshot: LeagueSnapshot, catalog: Draftable[], userId: number) {
  const seconds = useTurnSeconds(snapshot)
  const formattedTime = formatTimer(seconds)

  const orderedTeams = useMemo(
    () =>
      [...snapshot.teams].sort(
        (a, b) =>
          (a.draft_position ?? Number.MAX_SAFE_INTEGER) -
            (b.draft_position ?? Number.MAX_SAFE_INTEGER) || a.id - b.id,
      ),
    [snapshot.teams],
  )

  const catalogById = useMemo(() => new Map(catalog.map((entry) => [entry.id, entry])), [catalog])

  const pickByNumber = useMemo(() => {
    const map = new Map<number, RosterPick>()
    for (const pick of snapshot.rosters) {
      map.set(pick.pick_number, pick)
    }
    return map
  }, [snapshot.rosters])

  const teamCount = orderedTeams.length
  const totalRounds = Math.max(1, snapshot.league.roster_size)
  const totalPicks = snapshot.state.required_pick_count || teamCount * totalRounds
  const status = snapshot.state.status
  const activePickNumber = status === 'drafting' ? snapshot.state.next_pick_number : null

  const activeMeta = useMemo(() => {
    if (
      activePickNumber === null ||
      teamCount === 0 ||
      activePickNumber < 1 ||
      activePickNumber > totalPicks
    ) {
      return null
    }
    return getTeamForOverallPick(activePickNumber, orderedTeams)
  }, [activePickNumber, orderedTeams, teamCount, totalPicks])

  const activeTeam =
    activeMeta?.team ?? snapshot.teams.find((team) => team.id === snapshot.state.next_team_id)
  const activeIsYou = status === 'drafting' && activeTeam?.manager_user_id === userId
  const isCommissionerProxyTurn = Boolean(
    status === 'drafting' &&
      snapshot.league.commissioner_user_id === userId &&
      activeTeam &&
      activeTeam.manager_user_id !== userId,
  )
  const canStart = snapshot.actions.some((action) => action.kind === 'start')

  const currentRound = activeMeta
    ? activeMeta.roundIndex + 1
    : status === 'completed'
      ? totalRounds
      : snapshot.state.team_count
        ? Math.min(
            snapshot.league.roster_size,
            Math.floor(snapshot.state.active_pick_count / snapshot.state.team_count) + 1,
          )
        : 1

  const prevPickInfo = useMemo(() => {
    if (teamCount === 0) return null
    const targetPick =
      status === 'drafting' && activePickNumber !== null && activePickNumber > 1
        ? activePickNumber - 1
        : status === 'completed' && totalPicks > 1
          ? totalPicks - 1
          : null
    if (targetPick === null || targetPick < 1) return null
    const meta = getTeamForOverallPick(targetPick, orderedTeams)
    const rosterPick = pickByNumber.get(targetPick)
    const item = rosterPick ? catalogById.get(rosterPick.draftable_id) : undefined
    return {
      overallPick: targetPick,
      coord: meta.coord,
      team: meta.team,
      rosterPick,
      item,
    }
  }, [activePickNumber, catalogById, orderedTeams, pickByNumber, status, teamCount, totalPicks])

  const focalPickInfo = useMemo(() => {
    if (teamCount === 0) return null
    const targetPick =
      status === 'drafting' && activePickNumber !== null
        ? activePickNumber
        : status === 'setup'
          ? 1
          : status === 'completed' && totalPicks >= 1
            ? totalPicks
            : null
    if (targetPick === null || targetPick < 1 || targetPick > totalPicks) return null
    const meta = getTeamForOverallPick(targetPick, orderedTeams)
    const rosterPick = pickByNumber.get(targetPick)
    const item = rosterPick ? catalogById.get(rosterPick.draftable_id) : undefined
    return {
      overallPick: targetPick,
      coord: meta.coord,
      team: meta.team,
      rosterPick,
      item,
    }
  }, [activePickNumber, catalogById, orderedTeams, pickByNumber, status, teamCount, totalPicks])

  const nextPickInfo = useMemo(() => {
    if (teamCount === 0) return null
    const targetPick =
      status === 'drafting' && activePickNumber !== null
        ? activePickNumber + 1
        : status === 'setup' && totalPicks >= 2
          ? 2
          : null
    if (targetPick === null || targetPick < 1 || targetPick > totalPicks) return null
    const meta = getTeamForOverallPick(targetPick, orderedTeams)
    return {
      overallPick: targetPick,
      coord: meta.coord,
      team: meta.team,
      isYou: meta.team?.manager_user_id === userId,
    }
  }, [activePickNumber, orderedTeams, status, teamCount, totalPicks, userId])

  const userSubsequentPick = useMemo(() => {
    if (teamCount === 0 || status === 'completed') return null
    const hasUserTeam = orderedTeams.some((team) => team.manager_user_id === userId)
    if (!hasUserTeam) return null

    const searchFrom =
      status === 'drafting' && activePickNumber !== null
        ? nextPickInfo?.isYou
          ? activePickNumber + 2
          : activePickNumber + 1
        : status === 'setup'
          ? 1
          : totalPicks + 1

    for (let p = searchFrom; p <= totalPicks; p++) {
      const info = getTeamForOverallPick(p, orderedTeams)
      if (info.team?.manager_user_id === userId) {
        const delta =
          status === 'drafting' && activePickNumber !== null ? p - activePickNumber : null
        return {
          overallPick: p,
          coord: info.coord,
          delta,
          isSubsequent: activeIsYou || Boolean(nextPickInfo?.isYou),
        }
      }
    }
    return null
  }, [
    activeIsYou,
    activePickNumber,
    nextPickInfo,
    orderedTeams,
    status,
    teamCount,
    totalPicks,
    userId,
  ])

  return {
    seconds,
    formattedTime,
    teamCount,
    totalRounds,
    totalPicks,
    status,
    activePickNumber,
    activeMeta,
    activeTeam,
    activeIsYou,
    isCommissionerProxyTurn,
    canStart,
    currentRound,
    prevPickInfo,
    focalPickInfo,
    nextPickInfo,
    userSubsequentPick,
  }
}

export function TurnBanner({
  snapshot,
  catalog,
  userId,
  onSelectPick,
  onStartDraft,
  onOpenSettings,
  onOpenTournament,
}: {
  snapshot: LeagueSnapshot
  catalog: Draftable[]
  userId: number
  onSelectPick?: (item: Draftable) => void
  onStartDraft?: () => void
  onOpenSettings?: () => void
  onOpenTournament?: () => void
}) {
  const {
    seconds,
    formattedTime,
    teamCount,
    totalRounds,
    totalPicks,
    status,
    activePickNumber,
    activeMeta,
    activeTeam,
    activeIsYou,
    isCommissionerProxyTurn,
    canStart,
    currentRound,
    prevPickInfo,
    focalPickInfo,
    nextPickInfo,
    userSubsequentPick,
  } = useTurnBannerState(snapshot, catalog, userId)

  return (
    <section
      className={`turn-banner ${activeIsYou ? 'is-your-turn' : ''} ${isCommissionerProxyTurn ? 'is-proxy-turn' : ''} status-${status}`}
      aria-label="On the clock status banner"
    >
      <div className="turn-copy">
        <span
          className={`turn-label ${activeIsYou ? 'your-turn-pill-badge' : ''} ${isCommissionerProxyTurn ? 'proxy-turn-pill-badge' : ''}`}
        >
          {activeIsYou ? (
            <Zap size={13} />
          ) : isCommissionerProxyTurn ? (
            <AlertTriangle size={13} />
          ) : (
            <span className="pulse-dot" />
          )}
          {status === 'setup'
            ? 'SETUP — DRAFT NOT STARTED'
            : status === 'completed'
              ? 'DRAFT COMPLETED'
              : activeIsYou
                ? 'YOUR TURN — ON THE CLOCK!'
                : isCommissionerProxyTurn
                  ? 'COMMISSIONER OVERRIDE — NOT YOUR TURN (ON THE CLOCK)'
                  : 'ON THE CLOCK'}
        </span>
        <div className="turn-headline-row">
          {status === 'drafting' && activeTeam && (
            <span className="turn-team-avatar" style={teamAvatarStyle(activeTeam.color_rgb)}>
              {initials(teamName(activeTeam))}
            </span>
          )}
          <div>
            <h2>
              {status === 'setup'
                ? 'Draft not started'
                : status === 'completed'
                  ? 'Draft complete'
                  : activeTeam
                    ? `${teamName(activeTeam)}${activeIsYou ? ' (You)' : ''}`
                    : 'Next team'}
            </h2>
            <p>
              {status === 'setup' ? (
                teamCount > 0 ? (
                  `Complete team names, colors, and draft order in League settings before starting (${teamCount} teams · ${totalRounds} rounds · ${totalPicks} total picks).`
                ) : (
                  'Complete team names, colors, and draft order in League settings before starting.'
                )
              ) : status === 'completed' ? (
                `${snapshot.state.active_pick_count} of ${totalPicks} picks completed.`
              ) : activeMeta ? (
                <>
                  Round {activeMeta.roundIndex + 1} · Pick {activeMeta.coord} (#{activePickNumber} of{' '}
                  {totalPicks})
                  {(activeIsYou || isCommissionerProxyTurn) && activeTeam && (
                    <>
                      <span>·</span>
                      <strong>{activeTeam.points_remaining} pts left</strong>
                    </>
                  )}
                </>
              ) : (
                <>
                  Round {currentRound}
                  <span>·</span>Pick {snapshot.state.next_pick_number} of{' '}
                  {snapshot.state.required_pick_count}
                </>
              )}
            </p>
          </div>
        </div>
        {isCommissionerProxyTurn && activeTeam && (
          <div className="turn-proxy-warning" role="status">
            <AlertTriangle size={14} />
            <span>
              Commissioner controls active: selecting a Pokémon now will draft on behalf of{' '}
              <strong>{teamName(activeTeam)}</strong>, not your team.
            </span>
          </div>
        )}
        {status === 'completed' && onOpenTournament && (
          <button className="button banner-button" onClick={onOpenTournament}>
            Open tournament <ArrowRight size={15} />
          </button>
        )}
        {status === 'setup' && (onStartDraft || onOpenSettings) && (
          <button
            className="button banner-button"
            onClick={() => (canStart && onStartDraft ? onStartDraft() : onOpenSettings?.())}
          >
            {canStart ? 'Start draft' : 'League settings'}
            <ArrowRight size={15} />
          </button>
        )}
      </div>

      {teamCount > 0 && (
        <div className="turn-flow-strip" aria-label="Previous, current, and next picks">
          {/* Card 1: Previous Pick */}
          <div className={`turn-pick-card previous ${prevPickInfo ? 'filled' : 'empty'}`}>
            <div className="turn-card-top">
              <span className="turn-card-tag">
                {status === 'completed' ? '◄ PENULTIMATE' : '◄ PREVIOUS PICK'}
              </span>
              {prevPickInfo && (
                <span className="turn-card-coord">
                  {prevPickInfo.coord} · #{prevPickInfo.overallPick}
                </span>
              )}
            </div>
            {prevPickInfo ? (
              <button
                type="button"
                className="turn-card-poke-btn"
                disabled={!prevPickInfo.item || !onSelectPick}
                onClick={() => {
                  if (prevPickInfo.item && onSelectPick) onSelectPick(prevPickInfo.item)
                }}
              >
                <span className="turn-card-sprite">
                  {prevPickInfo.item ? (
                    <PokemonImage item={prevPickInfo.item} />
                  ) : (
                    <span className="slot-placeholder">?</span>
                  )}
                </span>
                <div className="turn-card-meta">
                  <strong>
                    {prevPickInfo.item?.name ??
                      `Pokémon #${prevPickInfo.rosterPick?.draftable_id ?? prevPickInfo.overallPick}`}
                  </strong>
                  <small>
                    {teamName(prevPickInfo.team)}
                    {prevPickInfo.rosterPick ? ` · ${prevPickInfo.rosterPick.point_cost} pts` : ''}
                  </small>
                </div>
              </button>
            ) : (
              <div className="turn-card-placeholder">
                <strong>—</strong>
                <small>{status === 'setup' ? 'Waiting for start' : 'Opening pick of draft'}</small>
              </div>
            )}
          </div>

          {/* Card 2: Current / Focal Pick */}
          <div
            className={`turn-pick-card current ${activeIsYou ? 'is-you-clock' : ''}`}
            aria-current={status === 'drafting' ? 'step' : undefined}
          >
            <div className="turn-card-top">
              <span className="turn-card-tag active">
                {status === 'setup'
                  ? 'OPENING PICK'
                  : status === 'completed'
                    ? '✓ FINAL PICK'
                    : activeIsYou
                      ? '⚡ YOUR PICK NOW'
                      : '● CURRENT PICK'}
              </span>
              {focalPickInfo && (
                <span className="turn-card-coord">
                  {focalPickInfo.coord} · #{focalPickInfo.overallPick}
                </span>
              )}
            </div>
            {status === 'completed' && focalPickInfo?.rosterPick ? (
              <button
                type="button"
                className="turn-card-poke-btn"
                disabled={!focalPickInfo.item || !onSelectPick}
                onClick={() => {
                  if (focalPickInfo.item && onSelectPick) onSelectPick(focalPickInfo.item)
                }}
              >
                <span className="turn-card-sprite">
                  {focalPickInfo.item ? (
                    <PokemonImage item={focalPickInfo.item} />
                  ) : (
                    <span className="slot-placeholder">?</span>
                  )}
                </span>
                <div className="turn-card-meta">
                  <strong>
                    {focalPickInfo.item?.name ??
                      `Pokémon #${focalPickInfo.rosterPick.draftable_id}`}
                  </strong>
                  <small>
                    {teamName(focalPickInfo.team)} · {focalPickInfo.rosterPick.point_cost} pts
                  </small>
                </div>
              </button>
            ) : focalPickInfo ? (
              <div className="turn-card-team-row">
                <span
                  className="turn-mini-avatar"
                  style={teamAvatarStyle(focalPickInfo.team.color_rgb)}
                >
                  {initials(teamName(focalPickInfo.team))}
                </span>
                <div className="turn-card-meta">
                  <strong>
                    {teamName(focalPickInfo.team)}
                    {focalPickInfo.team.manager_user_id === userId ? ' (You)' : ''}
                  </strong>
                  <small>
                    {status === 'setup'
                      ? 'Slot #01 · First overall'
                      : activeIsYou
                        ? 'Select an available Pokémon below'
                        : 'Currently on the clock'}
                  </small>
                </div>
              </div>
            ) : (
              <div className="turn-card-placeholder">
                <strong>—</strong>
                <small>No active pick</small>
              </div>
            )}
          </div>

          {/* Card 3: Next Pick + User's Subsequent Pick */}
          <div
            className={`turn-pick-card next ${nextPickInfo?.isYou ? 'is-you-ondeck' : ''} ${!nextPickInfo ? 'empty' : ''}`}
          >
            <div className="turn-card-top">
              <span className="turn-card-tag">
                {status === 'setup' ? 'SECOND PICK ►' : 'NEXT PICK ►'}
              </span>
              {nextPickInfo && (
                <span className="turn-card-coord">
                  {nextPickInfo.coord} · #{nextPickInfo.overallPick}
                </span>
              )}
            </div>
            {nextPickInfo ? (
              <div className="turn-card-team-row">
                <span
                  className="turn-mini-avatar"
                  style={teamAvatarStyle(nextPickInfo.team.color_rgb)}
                >
                  {initials(teamName(nextPickInfo.team))}
                </span>
                <div className="turn-card-meta">
                  <strong>
                    {teamName(nextPickInfo.team)}
                    {nextPickInfo.isYou ? ' (You)' : ''}
                  </strong>
                  <small>
                    {status === 'setup'
                      ? 'Slot #02 · Up second'
                      : nextPickInfo.isYou
                        ? '★ You’re On Deck!'
                        : `On Deck · #${nextPickInfo.overallPick}`}
                  </small>
                </div>
              </div>
            ) : (
              <div className="turn-card-placeholder">
                <strong>—</strong>
                <small>
                  {status === 'completed'
                    ? 'Every round is complete.'
                    : 'Final pick of the draft is on the clock.'}
                </small>
              </div>
            )}
            {userSubsequentPick && (
              <div className="turn-subsequent-pill">
                <span>
                  {userSubsequentPick.isSubsequent ? 'Your subsequent pick:' : 'Your next pick:'}
                </span>
                <strong>
                  {userSubsequentPick.coord} (#{userSubsequentPick.overallPick}
                  {userSubsequentPick.delta !== null ? ` · in ${userSubsequentPick.delta}` : ''})
                </strong>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="banner-art" aria-hidden="true">
        <div className="pokeball-ring" />
      </div>

      {status === 'drafting' && (
        <div className={`countdown ${seconds === 0 ? 'overtime' : ''}`}>
          <span>
            <Clock3 size={14} />
            {seconds === 0 ? 'Overtime' : activeIsYou ? 'Your clock' : 'Pick timer'}
          </span>
          <strong>{formattedTime}</strong>
        </div>
      )}
      {status === 'completed' && <Trophy className="complete-trophy" size={55} />}
    </section>
  )
}

export function FloatingTurnTracker({
  snapshot,
  catalog,
  userId,
  onSelectPick,
  onGoToDraft,
}: {
  snapshot: LeagueSnapshot
  catalog: Draftable[]
  userId: number
  onSelectPick?: (item: Draftable) => void
  onGoToDraft: () => void
}) {
  const [minimized, setMinimized] = useState(false)
  const {
    seconds,
    formattedTime,
    status,
    activePickNumber,
    activeMeta,
    activeTeam,
    activeIsYou,
    isCommissionerProxyTurn,
    prevPickInfo,
    nextPickInfo,
    userSubsequentPick,
  } = useTurnBannerState(snapshot, catalog, userId)

  if (status !== 'drafting') return null

  return (
    <aside
      className={`floating-turn-tracker ${activeIsYou ? 'is-your-turn' : ''} ${isCommissionerProxyTurn ? 'is-proxy-turn' : ''} ${minimized ? 'is-minimized' : ''}`}
      aria-label="Floating live draft tracker"
    >
      <div className="floating-tracker-head">
        <div className="floating-tracker-current">
          {activeIsYou ? (
            <span className="floating-your-turn-badge">
              <Zap size={11} />
              YOUR TURN
            </span>
          ) : isCommissionerProxyTurn ? (
            <span className="floating-proxy-turn-badge">
              <AlertTriangle size={11} />
              OTHER TEAM
            </span>
          ) : (
            <span className="pulse-dot" />
          )}
          {activeMeta && (
            <span className="turn-card-coord">
              {activeMeta.coord} · #{activePickNumber}
            </span>
          )}
          {activeTeam && !minimized && (
            <span
              className="floating-tracker-avatar"
              style={teamAvatarStyle(activeTeam.color_rgb)}
            >
              {initials(teamName(activeTeam))}
            </span>
          )}
          <strong className="floating-tracker-team">
            {activeTeam
              ? `${teamName(activeTeam)}${activeIsYou ? ' (You)' : ''}`
              : 'Next team'}
          </strong>
        </div>

        <div className="floating-tracker-controls">
          <span
            className={`floating-tracker-timer ${seconds === 0 ? 'overtime' : ''}`}
            title={seconds === 0 ? 'Overtime' : 'Pick timer'}
          >
            <Clock3 size={12} />
            {formattedTime}
          </span>
          <button
            type="button"
            className="floating-tracker-toggle"
            aria-expanded={!minimized}
            aria-label={minimized ? 'Expand live draft tracker' : 'Minimize live draft tracker'}
            title={minimized ? 'Expand tracker' : 'Minimize tracker'}
            onClick={() => setMinimized((current) => !current)}
          >
            {minimized ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
        </div>
      </div>

      {!minimized && (
        <div className="floating-tracker-body">
          <div className="floating-tracker-grid">
            <div className="floating-tracker-box">
              <span className="floating-box-label">
                ◄ Prev{prevPickInfo ? ` (${prevPickInfo.coord})` : ''}
              </span>
              {prevPickInfo ? (
                <button
                  type="button"
                  className="floating-box-poke-btn"
                  disabled={!prevPickInfo.item || !onSelectPick}
                  onClick={() => {
                    if (prevPickInfo.item && onSelectPick) onSelectPick(prevPickInfo.item)
                  }}
                >
                  {prevPickInfo.item && (
                    <span className="floating-box-sprite">
                      <PokemonImage item={prevPickInfo.item} />
                    </span>
                  )}
                  <div className="floating-box-meta">
                    <strong>
                      {prevPickInfo.item?.name ??
                        `Pokémon #${prevPickInfo.rosterPick?.draftable_id ?? prevPickInfo.overallPick}`}
                    </strong>
                    <small>
                      {teamName(prevPickInfo.team)}
                      {prevPickInfo.rosterPick ? ` · ${prevPickInfo.rosterPick.point_cost}p` : ''}
                    </small>
                  </div>
                </button>
              ) : (
                <strong className="floating-box-empty">Opening pick</strong>
              )}
            </div>

            <div
              className={`floating-tracker-box ${nextPickInfo?.isYou ? 'is-you-ondeck' : ''}`}
            >
              <span className="floating-box-label">
                Next{nextPickInfo ? ` (${nextPickInfo.coord})` : ''} ►
              </span>
              {nextPickInfo ? (
                <div className="floating-box-meta">
                  <strong>
                    {nextPickInfo.isYou ? '★ You (On Deck)' : teamName(nextPickInfo.team)}
                  </strong>
                  <small>Pick #{nextPickInfo.overallPick}</small>
                </div>
              ) : (
                <strong className="floating-box-empty">Final pick</strong>
              )}
            </div>
          </div>

          <div className="floating-tracker-footer">
            {userSubsequentPick ? (
              <span className="floating-subsequent">
                {userSubsequentPick.isSubsequent ? 'Your subseq:' : 'Your next:'}{' '}
                <strong>
                  {userSubsequentPick.coord} (#{userSubsequentPick.overallPick}
                  {userSubsequentPick.delta !== null ? ` · in ${userSubsequentPick.delta}` : ''})
                </strong>
              </span>
            ) : activeIsYou && activeTeam ? (
              <span className="floating-subsequent">
                Budget: <strong>{activeTeam.points_remaining} pts left</strong>
              </span>
            ) : (
              <span className="floating-subsequent">
                Round {activeMeta ? activeMeta.roundIndex + 1 : 1}
              </span>
            )}

            <button
              type="button"
              className="floating-tracker-cta"
              onClick={onGoToDraft}
            >
              {activeIsYou ? 'Pick Now' : 'Draft Room'}
              <ArrowRight size={13} />
            </button>
          </div>
        </div>
      )}
    </aside>
  )
}
