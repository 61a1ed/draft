import { useEffect, useMemo, useState } from 'react'
import { Users } from 'lucide-react'
import type { Draftable, LeagueSnapshot, RosterPick } from '../lib/types'
import { teamAvatarStyle, teamColor, teamName } from '../lib/teamPresentation'
import { PokemonImage } from './Catalog'
import { TypePills } from './PokemonParts'
import { getTeamForOverallPick, TurnBanner } from './TurnBanner'

export { getTeamForOverallPick }

function initials(name: string) {
  return name
    .split(' ')
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
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

export function getPickCellMeta(
  roundIndex: number,
  colIndex: number,
  teamCount: number,
  totalRounds: number,
) {
  const isEvenRound = roundIndex % 2 === 1
  const pickInRound = isEvenRound ? teamCount - colIndex : colIndex + 1
  const overallPick = roundIndex * teamCount + pickInRound
  const coord = `${roundIndex + 1}.${String(pickInRound).padStart(2, '0')}`
  const isRoundEnd = (!isEvenRound && colIndex === teamCount - 1) || (isEvenRound && colIndex === 0)
  const flowArrow = isRoundEnd
    ? roundIndex === totalRounds - 1
      ? 'Finish'
      : '↓ Turn'
    : isEvenRound
      ? '←'
      : '→'
  return { overallPick, pickInRound, coord, flowArrow, isEvenRound }
}

export function DraftBoard({
  snapshot,
  catalog,
  userId,
  onSelect,
  onOpenSettings,
  onStartDraft,
  onOpenTournament,
}: {
  snapshot: LeagueSnapshot
  catalog: Draftable[]
  userId: number
  onSelect: (item: Draftable) => void
  onOpenSettings?: () => void
  onStartDraft?: () => void
  onOpenTournament?: () => void
}) {
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

  const currentRound = activeMeta
    ? activeMeta.roundIndex + 1
    : status === 'completed'
      ? totalRounds
      : 1

  if (teamCount === 0) {
    return (
      <div className="card empty-state">
        <Users size={28} />
        <h3>No teams yet</h3>
        <p>Add teams and set the draft order in League settings to view the Draft Board.</p>
        {onOpenSettings && (
          <button className="button primary" onClick={onOpenSettings}>
            Set up teams
          </button>
        )}
      </div>
    )
  }

  const activeTeam = activeMeta?.team

  return (
    <div className="draft-board-view">
      <TurnBanner
        snapshot={snapshot}
        catalog={catalog}
        userId={userId}
        onSelectPick={onSelect}
        onOpenSettings={onOpenSettings}
        onStartDraft={onStartDraft}
        onOpenTournament={onOpenTournament}
      />

      <section className="board-card" aria-label="Snake Draft Board Grid">
        <div className="board-header-bar">
          <div className="board-header-title">
            <h2>Snake Draft Grid</h2>
            <span className="board-pill">
              {snapshot.state.active_pick_count} / {totalPicks} Picks Made · Round {currentRound} of{' '}
              {totalRounds}
            </span>
          </div>
          <div className="board-legend" aria-label="Cell state legend">
            <span className="legend-item">
              <i className="legend-swatch completed" /> Completed Pick
            </span>
            <span className="legend-item">
              <i className="legend-swatch active" /> On The Clock (Current)
            </span>
            <span className="legend-item">
              <i className="legend-swatch ondeck" /> On Deck (Next Pick)
            </span>
            <span className="legend-item">
              <i className="legend-swatch you" /> Your Team Column
            </span>
          </div>
        </div>

        <div className="board-scroll-container">
          <table className="draft-board-table" aria-label="Draft picks by round and team">
            <thead>
              <tr>
                <th scope="col" className="round-col-header">
                  Round
                  <br />
                  Snake
                </th>
                {orderedTeams.map((team, index) => {
                  const isYou = team.manager_user_id === userId
                  const isCurrentTeam = status === 'drafting' && activeTeam?.id === team.id
                  const colorHex = teamColor(team.color_rgb)
                  const budgetPct =
                    team.point_budget > 0
                      ? Math.max(
                          0,
                          Math.min(100, (team.points_remaining / team.point_budget) * 100),
                        )
                      : 0
                  const slotNum = String(team.draft_position ?? index + 1).padStart(2, '0')
                  return (
                    <th
                      scope="col"
                      key={team.id}
                      className={`team-col-header ${isYou ? 'is-user' : ''} ${isCurrentTeam ? 'is-active' : ''}`}
                    >
                      <div
                        className="team-color-bar"
                        style={{ background: colorHex ?? 'var(--purple)' }}
                      />
                      <div className="team-header-top">
                        <span className="team-mini-avatar" style={teamAvatarStyle(team.color_rgb)}>
                          {initials(teamName(team))}
                        </span>
                        <div className="team-header-name">
                          <strong>{teamName(team)}</strong>
                          <small>Slot #{slotNum}</small>
                        </div>
                        {isCurrentTeam ? (
                          <span className="role-tag clock">On Clock</span>
                        ) : isYou ? (
                          <span className="role-tag you">You</span>
                        ) : null}
                      </div>
                      <div className="team-budget-row">
                        <span>Pts Left</span>
                        <strong>
                          {team.points_remaining} / {team.point_budget}
                        </strong>
                      </div>
                      <div className="team-budget-track" aria-hidden="true">
                        <span
                          style={{
                            width: `${budgetPct}%`,
                            background: colorHex ?? 'var(--purple)',
                          }}
                        />
                      </div>
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: totalRounds }, (_, roundIndex) => {
                const isEven = roundIndex % 2 === 1
                return (
                  <tr key={roundIndex + 1}>
                    <th scope="row" className="round-row-header">
                      <span className="round-num">R{roundIndex + 1}</span>
                      <span
                        className="round-dir"
                        title={
                          isEven
                            ? 'Even round: picks go Right to Left'
                            : 'Odd round: picks go Left to Right'
                        }
                      >
                        {isEven ? '← R-L' : '→ L-R'}
                      </span>
                    </th>
                    {orderedTeams.map((team, colIndex) => {
                      const isYou = team.manager_user_id === userId
                      const { overallPick, coord, flowArrow } = getPickCellMeta(
                        roundIndex,
                        colIndex,
                        teamCount,
                        totalRounds,
                      )
                      const rosterPick = pickByNumber.get(overallPick)
                      const colorHex = teamColor(team.color_rgb)

                      if (rosterPick) {
                        const item = catalogById.get(rosterPick.draftable_id)
                        const pokeName = item?.name ?? `Pokémon #${rosterPick.draftable_id}`
                        const hasTypes = Boolean(item?.types?.length)
                        const fallbackSub = item?.form || 'Drafted'
                        return (
                          <td key={team.id} className={`pick-cell ${isYou ? 'is-user-col' : ''}`}>
                            <button
                              type="button"
                              className="pick-slot completed"
                              style={{
                                borderLeftColor: colorHex ?? 'var(--purple)',
                              }}
                              onClick={() => {
                                if (item) onSelect(item)
                              }}
                              aria-label={`Pick ${coord}, #${overallPick} overall, ${teamName(team)} drafted ${pokeName} for ${rosterPick.point_cost} points`}
                            >
                              <div className="pick-slot-top">
                                <span className="pick-coord">
                                  {coord} · #{overallPick}
                                </span>
                                <span className="pick-cost-badge">{rosterPick.point_cost} pts</span>
                              </div>
                              <div className="pick-pokemon-row">
                                <span className="pick-sprite">
                                  {item ? (
                                    <PokemonImage item={item} />
                                  ) : (
                                    <span className="slot-placeholder">?</span>
                                  )}
                                </span>
                                <div className="pick-poke-meta">
                                  <strong>{pokeName}</strong>
                                  {hasTypes ? (
                                    <TypePills types={item!.types!} small />
                                  ) : (
                                    <span className="pick-poke-type">{fallbackSub}</span>
                                  )}
                                </div>
                              </div>
                            </button>
                          </td>
                        )
                      }

                      if (status === 'drafting' && overallPick === activePickNumber) {
                        return (
                          <td key={team.id} className={`pick-cell ${isYou ? 'is-user-col' : ''}`}>
                            <div
                              className="pick-slot active"
                              aria-current="step"
                              aria-label={`Pick ${coord}, #${overallPick} overall, ${teamName(team)} on the clock`}
                            >
                              <div className="pick-slot-top">
                                <span className="pick-coord">
                                  {coord} · #{overallPick}
                                </span>
                                <span className="active-tag">
                                  <span className="clock-pulse-dot" />
                                  On Clock
                                </span>
                              </div>
                              <div className="active-center">
                                <span className="active-team-label">
                                  {isYou ? 'Your Pick!' : teamName(team)}
                                </span>
                                <span className="active-timer-pill">{formattedTime}</span>
                              </div>
                              <div className="active-sub">Snake {flowArrow}</div>
                            </div>
                          </td>
                        )
                      }

                      if (
                        status === 'drafting' &&
                        activePickNumber !== null &&
                        overallPick === activePickNumber + 1
                      ) {
                        return (
                          <td key={team.id} className={`pick-cell ${isYou ? 'is-user-col' : ''}`}>
                            <div
                              className="pick-slot ondeck"
                              aria-label={`Pick ${coord}, #${overallPick} overall, ${teamName(team)} on deck`}
                            >
                              <div className="pick-slot-top">
                                <span className="pick-coord">
                                  {coord} · #{overallPick}
                                </span>
                                <span className="ondeck-tag">On Deck</span>
                              </div>
                              <div className="ondeck-label">
                                {isYou ? '★ You’re Up Next!' : `Next: ${teamName(team)}`}
                              </div>
                              <div className="empty-center">
                                <span className="empty-slot-hint">In 1 pick</span>
                                <span className="snake-flow-arrow">{flowArrow}</span>
                              </div>
                            </div>
                          </td>
                        )
                      }

                      const turnsAway =
                        status === 'drafting' && activePickNumber !== null
                          ? overallPick - activePickNumber
                          : null

                      return (
                        <td key={team.id} className={`pick-cell ${isYou ? 'is-user-col' : ''}`}>
                          <div
                            className="pick-slot empty"
                            aria-label={`Pick ${coord}, #${overallPick} overall, ${teamName(team)}`}
                          >
                            <div className="pick-slot-top">
                              <span className="pick-coord">
                                {coord} · #{overallPick}
                              </span>
                              <span className="snake-flow-arrow">{flowArrow}</span>
                            </div>
                            <div className="empty-center">
                              {isYou ? (
                                <span className="your-turn-pill">
                                  {turnsAway !== null && turnsAway > 0
                                    ? `★ Your pick · in ${turnsAway}`
                                    : '★ Your pick'}
                                </span>
                              ) : (
                                <span className="empty-slot-hint">
                                  {turnsAway !== null && turnsAway > 0
                                    ? `In ${turnsAway} picks`
                                    : `Pick #${overallPick}`}
                                </span>
                              )}
                            </div>
                          </div>
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
