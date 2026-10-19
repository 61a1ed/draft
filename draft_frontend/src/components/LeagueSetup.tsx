import { useState, type FormEvent } from 'react'
import { ArrowDown, ArrowUp, Check, Plus, Save, Settings2, Trash2, Users } from 'lucide-react'
import { api } from '../lib/api'
import { teamColor, teamName } from '../lib/teamPresentation'
import type { LeagueSnapshot, Team, User } from '../lib/types'
import { TeamMetadata } from './TeamMetadata'

interface LeagueSetupProps {
  snapshot: LeagueSnapshot
  users: User[]
  userId: number
  busy: boolean
  onMutation: (operation: () => Promise<unknown>) => Promise<unknown>
  onStartDraft?: () => void
}

type MutationHandler = LeagueSetupProps['onMutation']

type SettingsProps = Pick<LeagueSetupProps, 'snapshot' | 'busy' | 'onMutation'>

function LeagueSettings({ snapshot, busy, onMutation }: SettingsProps) {
  const [name, setName] = useState(snapshot.league.name)
  const [rosterSize, setRosterSize] = useState(String(snapshot.league.roster_size))
  const [pickSeconds, setPickSeconds] = useState(String(snapshot.league.pick_seconds))
  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !name.trim()) return
    void onMutation(() =>
      api.updateLeague({
        p_league: snapshot.league_id,
        p_revision: snapshot.revision,
        p_name: name.trim(),
        p_roster_size: Number(rosterSize),
        p_pick_seconds: Number(pickSeconds),
      }),
    )
  }

  return (
    <form onSubmit={save}>
      <fieldset disabled={busy} className="form-fieldset">
        <div className="form-grid">
          <label className="field">
            <span>League name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              pattern=".*\S.*"
            />
          </label>
          <label className="field">
            <span>Pokémon per team</span>
            <input
              type="number"
              min="1"
              max="2147483647"
              step="1"
              value={rosterSize}
              onChange={(event) => setRosterSize(event.target.value)}
              required
            />
          </label>
          <label className="field">
            <span>Seconds per pick</span>
            <input
              type="number"
              min="1"
              max="2147483647"
              step="1"
              value={pickSeconds}
              onChange={(event) => setPickSeconds(event.target.value)}
              required
            />
          </label>
        </div>
        <button className="button secondary" type="submit">
          <Save size={16} aria-hidden="true" />
          Save settings
        </button>
      </fieldset>
    </form>
  )
}

interface TeamEditorProps {
  team?: Team
  snapshot: LeagueSnapshot
  users: User[]
  busy: boolean
  onMutation: MutationHandler
}

function TeamEditor({ team, snapshot, users, busy, onMutation }: TeamEditorProps) {
  const [name, setName] = useState(team?.name ?? '')
  const [manager, setManager] = useState(team ? String(team.manager_user_id) : '')
  const [budget, setBudget] = useState(String(team?.point_budget ?? 100))
  const [confirmRemove, setConfirmRemove] = useState(false)
  const assignedManagers = new Set(
    snapshot.teams.filter((item) => item.id !== team?.id).map((item) => item.manager_user_id),
  )
  const availableUsers = users.filter((user) => !assignedManagers.has(user.id))
  const managerIsKnown = !team || users.some((user) => user.id === team.manager_user_id)

  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !manager) return
    const input = {
      p_league: snapshot.league_id,
      p_revision: snapshot.revision,
      p_name: team ? team.name : name.trim() || null,
      p_manager: Number(manager),
      p_point_budget: Number(budget),
    }
    void onMutation(async () => {
      if (team) return api.updateTeam({ ...input, p_team: team.id })
      const result = await api.addTeam(input)
      setName('')
      setManager('')
      return result
    })
  }

  const remove = () => {
    if (!team || busy) return
    void onMutation(() =>
      api.removeTeam({
        p_league: snapshot.league_id,
        p_revision: snapshot.revision,
        p_team: team.id,
      }),
    )
  }

  return (
    <div className="team-editor">
      <form onSubmit={save}>
        <fieldset disabled={busy} className="form-fieldset">
          <legend className="team-editor-title">{team ? teamName(team) : 'Add a team'}</legend>
          <div className="form-grid team-form-grid">
            {!team && (
              <label className="field initial-team-name">
                <span>Team name (optional)</span>
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Their manager can choose this later"
                />
              </label>
            )}
            <label className="field">
              <span>Manager</span>
              <select value={manager} onChange={(event) => setManager(event.target.value)} required>
                <option value="" disabled>
                  Select a manager
                </option>
                {!managerIsKnown && (
                  <option value={team.manager_user_id}>Manager #{team.manager_user_id}</option>
                )}
                {users.map((user) => (
                  <option key={user.id} value={user.id} disabled={assignedManagers.has(user.id)}>
                    {user.display_name}
                    {assignedManagers.has(user.id) ? ' · assigned' : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Point budget</span>
              <input
                type="number"
                min="0"
                max="2147483647"
                step="1"
                value={budget}
                onChange={(event) => setBudget(event.target.value)}
                required
              />
            </label>
          </div>
          <div className="form-actions">
            <button
              type="submit"
              className={`button ${team ? 'secondary' : 'primary'}`}
              disabled={!manager || (!team && availableUsers.length === 0)}
            >
              {team ? <Save size={16} aria-hidden="true" /> : <Plus size={16} aria-hidden="true" />}
              {team ? 'Save manager & budget' : 'Add team'}
            </button>
            {team && !confirmRemove && (
              <button
                type="button"
                className="button danger"
                onClick={() => setConfirmRemove(true)}
              >
                <Trash2 size={16} aria-hidden="true" />
                Remove
              </button>
            )}
          </div>
          {!team && availableUsers.length === 0 && (
            <p className="muted">
              All available managers have a team. Another manager must sign in before a team can be
              added.
            </p>
          )}
        </fieldset>
      </form>
      {team && confirmRemove && (
        <div className="confirm-removal" role="alert">
          <p>
            Remove <strong>{teamName(team)}</strong> from this league? You can add the team again
            during setup.
          </p>
          <div className="form-actions">
            <button type="button" className="button danger" disabled={busy} onClick={remove}>
              Remove team
            </button>
            <button
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={() => setConfirmRemove(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function DraftOrder({
  snapshot,
  busy,
  onMutation,
  onStartDraft,
}: SettingsProps & Pick<LeagueSetupProps, 'onStartDraft'>) {
  const [order, setOrder] = useState(() =>
    [...snapshot.teams]
      .sort(
        (a, b) => (a.draft_position ?? Infinity) - (b.draft_position ?? Infinity) || a.id - b.id,
      )
      .map((team) => team.id),
  )
  const teamsById = new Map(snapshot.teams.map((team) => [team.id, team]))
  const isSaved = order.every((id, index) => teamsById.get(id)?.draft_position === index + 1)
  const unfinishedTeams = snapshot.teams.filter((team) => !team.metadata_complete)
  const canStart = snapshot.actions.some((action) => action.kind === 'start')

  const move = (index: number, offset: -1 | 1) => {
    setOrder((previous) => {
      const next = [...previous]
      const target = index + offset
      if (target < 0 || target >= next.length) return previous
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  return (
    <section className="card setup-order" aria-labelledby="draft-order-heading">
      <div className="section-title">
        <h2 id="draft-order-heading">Draft order</h2>
        {isSaved && order.length >= 2 && (
          <span className="muted">
            <Check size={15} aria-hidden="true" /> Saved
          </span>
        )}
      </div>
      <p className="muted">Set the first round’s order. The order reverses each round.</p>
      {order.length ? (
        <ol className="order-list">
          {order.map((id, index) => {
            const team = teamsById.get(id)!
            return (
              <li key={id}>
                <span className="order-number">{index + 1}</span>
                <span className="order-team-name">{teamName(team)}</span>
                <div className="order-controls">
                  <button
                    type="button"
                    className="button icon-button secondary"
                    aria-label={`Move ${teamName(team)} up`}
                    disabled={busy || index === 0}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp size={16} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="button icon-button secondary"
                    aria-label={`Move ${teamName(team)} down`}
                    disabled={busy || index === order.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown size={16} aria-hidden="true" />
                  </button>
                </div>
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="empty-inline">No teams added</p>
      )}
      <button
        className="button primary"
        type="button"
        disabled={busy || order.length < 2 || isSaved}
        onClick={() =>
          void onMutation(() =>
            api.setDraftOrder({
              p_league: snapshot.league_id,
              p_revision: snapshot.revision,
              p_teams: order,
            }),
          )
        }
      >
        <Save size={16} aria-hidden="true" />
        Save draft order
      </button>
      {order.length < 2 && <p className="muted">Add at least two teams to set the draft order.</p>}
      {!isSaved && order.length >= 2 && (
        <p className="muted">Save this order before starting the draft.</p>
      )}
      <div className="setup-readiness">
        <h3>Team setup</h3>
        {unfinishedTeams.length ? (
          <>
            <p className="muted">
              {unfinishedTeams.length} of {snapshot.teams.length} teams still need a name or color.
              Each manager can finish their team in League settings.
            </p>
            <ul>
              {unfinishedTeams.map((team) => (
                <li key={team.id}>{teamName(team)}</li>
              ))}
            </ul>
          </>
        ) : snapshot.teams.length > 0 ? (
          <p className="muted">All teams have a saved name and color.</p>
        ) : null}
        {onStartDraft && (
          <button
            type="button"
            className="button primary"
            disabled={busy || !canStart || !isSaved}
            onClick={() => {
              if (!busy && canStart && isSaved) onStartDraft()
            }}
          >
            Start draft
          </button>
        )}
      </div>
    </section>
  )
}

export function LeagueSetup({
  snapshot,
  users,
  userId,
  busy,
  onMutation,
  onStartDraft,
}: LeagueSetupProps) {
  const canConfigure = snapshot.actions.some((action) => action.kind === 'configure')
  const league = snapshot.league
  const orderKey = snapshot.teams.map((team) => `${team.id}:${team.draft_position}`).join(',')
  const userNames = new Map(users.map((user) => [user.id, user.display_name]))
  const ownTeam = snapshot.teams.find((team) => team.manager_user_id === userId)
  const yourTeam = (
    <section className="card your-team-section" aria-labelledby="your-team-heading">
      <div className="section-title">
        <h2 id="your-team-heading">
          <Users size={18} aria-hidden="true" />
          Your team
        </h2>
      </div>
      {ownTeam ? (
        <>
          {snapshot.state.status === 'setup' && (
            <p className="muted">Choose your team name and color before the draft begins.</p>
          )}
          <TeamMetadata
            key={`${league.id}:${userId}:${ownTeam.id}`}
            team={ownTeam}
            snapshot={snapshot}
            busy={busy}
            onMutation={onMutation}
          />
        </>
      ) : (
        <p className="muted">You are not managing a team in this league.</p>
      )}
    </section>
  )

  if (!canConfigure) {
    return (
      <div className="setup-layout">
        {yourTeam}
        <section className="card" aria-labelledby="league-settings-heading">
          <div className="section-title">
            <h2 id="league-settings-heading">
              <Settings2 size={18} aria-hidden="true" />
              League settings
            </h2>
          </div>
          <p className="muted">
            {snapshot.state.status === 'setup'
              ? 'Only the commissioner can change these settings.'
              : 'Settings are locked once the draft begins.'}
          </p>
          <dl className="settings-summary">
            <div>
              <dt>League</dt>
              <dd>{league.name}</dd>
            </div>
            <div>
              <dt>Pokémon per team</dt>
              <dd>{league.roster_size}</dd>
            </div>
            <div>
              <dt>Pick clock</dt>
              <dd>{league.pick_seconds} seconds</dd>
            </div>
          </dl>
        </section>
        <section className="card" aria-labelledby="league-teams-heading">
          <div className="section-title">
            <h2 id="league-teams-heading">
              <Users size={18} aria-hidden="true" />
              Teams
            </h2>
            <span className="muted">{snapshot.teams.length} teams</span>
          </div>
          <ol className="order-list">
            {[...snapshot.teams]
              .sort(
                (a, b) =>
                  (a.draft_position ?? Infinity) - (b.draft_position ?? Infinity) || a.id - b.id,
              )
              .map((team) => (
                <li key={team.id}>
                  <span className="order-number">{team.draft_position ?? '—'}</span>
                  <div className="order-team-name">
                    <strong>
                      {teamColor(team.color_rgb) && (
                        <span
                          className="team-color-dot"
                          style={{ backgroundColor: teamColor(team.color_rgb)! }}
                          aria-hidden="true"
                        />
                      )}
                      {teamName(team)}
                    </strong>
                    <p className="muted">
                      {userNames.get(team.manager_user_id) ?? `Manager #${team.manager_user_id}`}
                    </p>
                    {snapshot.state.status === 'setup' && (
                      <p className="muted">
                        {team.metadata_complete ? 'Ready for draft' : 'Needs a name and color'}
                      </p>
                    )}
                  </div>
                  <span className="muted">{team.point_budget} pts</span>
                </li>
              ))}
          </ol>
          {snapshot.teams.length === 0 && <p className="empty-inline">No teams added</p>}
        </section>
      </div>
    )
  }

  return (
    <div className="setup-layout">
      {yourTeam}
      <div className="setup-main">
        <section className="card" aria-labelledby="league-settings-heading">
          <div className="section-title">
            <h2 id="league-settings-heading">
              <Settings2 size={18} aria-hidden="true" />
              League settings
            </h2>
            <span className="muted">Commissioner</span>
          </div>
          <p className="muted">Settings can be changed until the draft begins.</p>
          <LeagueSettings
            key={`${league.id}:${league.name}:${league.roster_size}:${league.pick_seconds}`}
            snapshot={snapshot}
            busy={busy}
            onMutation={onMutation}
          />
        </section>
        <section className="card" aria-labelledby="league-teams-heading">
          <div className="section-title">
            <h2 id="league-teams-heading">
              <Users size={18} aria-hidden="true" />
              Teams & managers
            </h2>
            <span className="muted">{snapshot.teams.length} teams</span>
          </div>
          <p className="muted">
            Add each manager now; they can choose their team name and color later. Each manager can
            have one team.
          </p>
          <div className="team-editors">
            {snapshot.teams.map((team) => (
              <div key={team.id} className="team-management-entry">
                <TeamEditor
                  key={`${team.id}:${team.manager_user_id}:${team.point_budget}`}
                  team={team}
                  snapshot={snapshot}
                  users={users}
                  busy={busy}
                  onMutation={onMutation}
                />
                {team.id !== ownTeam?.id && (
                  <TeamMetadata
                    team={team}
                    snapshot={snapshot}
                    busy={busy}
                    onMutation={onMutation}
                  />
                )}
              </div>
            ))}
            <TeamEditor
              key={`new:${league.id}`}
              snapshot={snapshot}
              users={users}
              busy={busy}
              onMutation={onMutation}
            />
          </div>
        </section>
      </div>
      <DraftOrder
        key={`${league.id}:${orderKey}`}
        snapshot={snapshot}
        busy={busy}
        onMutation={onMutation}
        onStartDraft={onStartDraft}
      />
    </div>
  )
}

export default LeagueSetup
