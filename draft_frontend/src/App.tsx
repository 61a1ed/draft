import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Bookmark,
  CalendarDays,
  Check,
  ChevronDown,
  Clock3,
  HelpCircle,
  History,
  LayoutGrid,
  LogOut,
  Plus,
  Radio,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Target,
  Trophy,
  Users,
  WifiOff,
  X,
} from 'lucide-react'
import { api, ApiError, errorMessage } from './lib/api'
import { useDraft } from './lib/useDraft'
import { useFavorites } from './lib/useFavorites'
import type { Draftable, LeagueSnapshot, Session, Team } from './lib/types'
import { teamAvatarStyle, teamName } from './lib/teamPresentation'
import { Catalog, PokemonImage } from './components/Catalog'
import { Modal } from './components/Modal'
import { PokemonDetails } from './components/PokemonDetails'
import { LeagueSetup } from './components/LeagueSetup'
import { Tournament } from './components/Tournament'
import { Availability } from './components/Availability'
import { ThemeToggle } from './components/ThemeToggle'

type View = 'draft' | 'catalog' | 'tournament' | 'availability' | 'teams' | 'activity' | 'settings'

type AvailabilityContext = {
  owner: string
  teamId: number
  compareTeamId: number
  date?: string
}
type SelectedPick = { item: Draftable; revision: number; teamId: number | null }
const navItems = [
  { id: 'draft', label: 'Draft room', icon: LayoutGrid },
  { id: 'catalog', label: 'Catalog & favorites', icon: Bookmark },
  { id: 'tournament', label: 'Tournament', icon: Trophy },
  { id: 'availability', label: 'Availability', icon: CalendarDays },
  { id: 'teams', label: 'Teams & rosters', icon: Users },
  { id: 'activity', label: 'Draft activity', icon: History },
  { id: 'settings', label: 'League settings', icon: Settings2 },
] as const

function initials(name: string) {
  return name
    .split(' ')
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
}

function Countdown({ snapshot }: { snapshot: LeagueSnapshot }) {
  const [now, setNow] = useState(Date.now())
  const timing = useMemo(() => {
    const receivedAt = Date.now()
    return { receivedAt, offset: new Date(snapshot.server_now).getTime() - receivedAt }
  }, [snapshot.server_now])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const seconds = snapshot.state.deadline
    ? Math.max(
        0,
        Math.ceil(
          (new Date(snapshot.state.deadline).getTime() -
            Math.max(now, timing.receivedAt) -
            timing.offset) /
            1000,
        ),
      )
    : null
  return (
    <div className="countdown">
      <span>
        <Clock3 size={14} />
        {seconds === 0 ? 'Overtime' : 'Pick timer'}
      </span>
      <strong>
        {seconds === null
          ? '—:—'
          : `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`}
      </strong>
    </div>
  )
}

function TeamRoster({
  team,
  snapshot,
  catalog,
  userId,
  compact = false,
}: {
  team: Team
  snapshot: LeagueSnapshot
  catalog: Draftable[]
  userId: number
  compact?: boolean
}) {
  const picks = snapshot.rosters.filter((pick) => pick.team_id === team.id)
  return (
    <section className={`card roster-panel ${compact ? 'compact' : ''}`}>
      <div className="roster-heading">
        <span className="team-avatar" style={teamAvatarStyle(team.color_rgb)}>
          {initials(teamName(team))}
        </span>
        <div>
          <span className="eyebrow">
            {team.manager_user_id === userId ? 'YOUR TEAM' : `TEAM ${team.draft_position ?? '—'}`}
          </span>
          <h3>{teamName(team)}</h3>
        </div>
      </div>
      <div className="budget-heading">
        <span>Points remaining</span>
        <strong>
          {team.points_remaining}
          <small> / {team.point_budget}</small>
        </strong>
      </div>
      <div className="budget-track">
        <span
          style={{
            width: `${team.point_budget > 0 ? Math.max(0, (team.points_remaining / team.point_budget) * 100) : 0}%`,
          }}
        />
      </div>
      <div className="roster-subheading">
        <h4>{team.manager_user_id === userId ? 'Your roster' : 'Team roster'}</h4>
        <span>
          {picks.length} / {snapshot.league.roster_size}
        </span>
      </div>
      <div className="roster-slots">
        {Array.from(
          { length: Math.min(snapshot.league.roster_size, compact ? 6 : 50) },
          (_, index) => {
            const pick = picks[index]
            const item = pick ? catalog.find((entry) => entry.id === pick.draftable_id) : null
            return pick ? (
              <div className="roster-slot filled" key={pick.pick_id}>
                {item ? <PokemonImage item={item} /> : <span className="slot-placeholder">?</span>}
                <div>
                  <strong>{item?.name ?? `Pick ${pick.pick_number}`}</strong>
                  <small>Pick #{pick.pick_number}</small>
                </div>
                <span className="slot-cost">
                  {pick.point_cost}
                  <small>pts</small>
                </span>
              </div>
            ) : (
              <div className="roster-slot empty" key={`empty-${index}`}>
                <span className="slot-placeholder">
                  <Plus size={17} />
                </span>
                <span>Open slot</span>
                <small>{String(index + 1).padStart(2, '0')}</small>
              </div>
            )
          },
        )}
      </div>
      {snapshot.league.roster_size > (compact ? 6 : 50) && (
        <p className="muted">
          {snapshot.league.roster_size - (compact ? 6 : 50)} additional roster slots
        </p>
      )}
    </section>
  )
}

function TestSignIn({
  onConnect,
  onClose,
}: {
  onConnect: (session: Session) => Promise<void>
  onClose: () => void
}) {
  const [error, setError] = useState('')
  const login = useMutation({ mutationFn: (name: string) => api.testLogin(name) })
  const busy = login.isPending
  const [username, setUsername] = useState('alice')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const testLogin = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      const next = await login.mutateAsync(username)
      if (!mounted.current) return
      onClose()
      await onConnect(next)
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause))
    }
  }
  return (
    <Modal title="Use a test account" onClose={onClose}>
      {error && (
        <div className="inline-error" role="alert">
          {error}
        </div>
      )}
      <form className="test-login" onSubmit={testLogin}>
        <span className="eyebrow">LOCAL TEST ACCOUNTS</span>
        <label className="field">
          Account
          <select value={username} onChange={(event) => setUsername(event.target.value)}>
            <option value="alice">Alice</option>
            <option value="bob">Bob</option>
            <option value="carol">Carol</option>
          </select>
        </label>
        <button className="button secondary full-width" disabled={busy}>
          {busy ? 'Signing in…' : 'Use test account'}
        </button>
      </form>
    </Modal>
  )
}

export default function App() {
  const draft = useDraft()
  const { snapshot, session, catalog, mode } = draft
  const favorites = useFavorites({
    userId: mode === 'live' ? (session?.user.id ?? null) : null,
    mutate: draft.mutate,
  })
  const workspaceBusy = draft.busy || favorites.busy
  const legacyOwner = mode === 'live' ? (session?.user.id ?? null) : null
  const [legacySaved, setLegacySaved] = useState<{ owner: number | null; ids: number[] }>({
    owner: null,
    ids: [],
  })
  useEffect(() => {
    let ids: number[] = []
    if (legacyOwner !== null) {
      try {
        const saved: unknown = JSON.parse(
          localStorage.getItem(`draft-club:watchlist:live:${legacyOwner}`) ?? '[]',
        )
        if (Array.isArray(saved))
          ids = [...new Set(saved.filter((id): id is number => Number.isInteger(id) && id > 0))]
      } catch {
        /* Browser storage may be unavailable. Account favorites remain usable. */
      }
    }
    setLegacySaved({ owner: legacyOwner, ids })
  }, [legacyOwner])
  const legacyWatchlist = legacySaved.owner === legacyOwner ? legacySaved.ids : []
  const [view, setView] = useState<View>('draft')
  const initialViewOwner = useRef('')
  useEffect(() => {
    if (!snapshot || !session) {
      initialViewOwner.current = ''
      return
    }
    if (snapshot.league_id !== draft.leagueId) return
    const owner = `${session.user.id}:${snapshot.league_id}`
    if (initialViewOwner.current === owner) return
    initialViewOwner.current = owner
    setView((current) =>
      current === 'catalog' ? current : snapshot.state.status === 'setup' ? 'settings' : 'draft',
    )
  }, [snapshot, draft.leagueId, session])
  const [availabilityContext, setAvailabilityContext] = useState<AvailabilityContext | null>(null)
  const availabilityOwner = `${session?.user.id ?? 0}:${draft.leagueId ?? 0}`
  const availabilityNavigation =
    availabilityContext?.owner === availabilityOwner ? availabilityContext : null
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'instant' })
  }, [view])
  const [authError, setAuthError] = useState(() => {
    const code = new URLSearchParams(window.location.search).get('auth_error')
    return code ? new ApiError(400, code).message : ''
  })
  useEffect(() => {
    const url = new URL(window.location.href)
    if (url.searchParams.has('auth_error')) {
      url.searchParams.delete('auth_error')
      window.history.replaceState(null, '', url)
    }
  }, [])
  const [testSignIn, setTestSignIn] = useState(false)
  const [create, setCreate] = useState(false)
  const [help, setHelp] = useState(false)
  const [selected, setSelected] = useState<SelectedPick | null>(null)
  const [confirmAction, setConfirmAction] = useState<{
    kind: 'start' | 'undo'
    revision: number
  } | null>(null)
  const closeTestSignIn = useCallback(() => setTestSignIn(false), [])
  const [chargePoints, setChargePoints] = useState(true)
  useEffect(() => {
    setSelected(null)
    setConfirmAction(null)
    setAvailabilityContext(null)
  }, [draft.leagueId, mode, session?.user.id])
  const activeTeam = snapshot?.teams.find((team) => team.id === snapshot.state.next_team_id)
  const myTeam = snapshot?.teams.find((team) => team.manager_user_id === session?.user.id)
  const rosterTeam = myTeam ?? activeTeam ?? snapshot?.teams[0]
  const can = (kind: string) => snapshot?.actions.some((action) => action.kind === kind) ?? false
  const commissioner = snapshot?.league.commissioner_user_id === session?.user.id
  const currentRound =
    snapshot && snapshot.state.team_count
      ? Math.min(
          snapshot.league.roster_size,
          Math.floor(snapshot.state.active_pick_count / snapshot.state.team_count) + 1,
        )
      : 1
  const selectedTaken =
    selected && snapshot?.rosters.some((pick) => pick.draftable_id === selected.item.id)
  const pickChanged =
    selected &&
    (selected.revision !== snapshot?.revision || selected.teamId !== snapshot?.state.next_team_id)
  const cost = selected ? (chargePoints ? selected.item.point_cost : 0) : 0
  const pickAllowed =
    !selectedTaken &&
    !pickChanged &&
    can('pick') &&
    activeTeam &&
    cost <= activeTeam.points_remaining
  const openPick = (item: Draftable) => {
    setSelected({
      item,
      revision: snapshot?.revision ?? -1,
      teamId: snapshot?.state.next_team_id ?? null,
    })
    setChargePoints(true)
  }

  if (mode === 'loading')
    return (
      <div className="loading-page">
        <img src="/favicon.svg" alt="" />
        <h2>Loading…</h2>
        <span className="loading-bar" />
      </div>
    )

  if (mode === 'disconnected')
    return (
      <main className="loading-page empty-state">
        <ThemeToggle className="floating" />
        <img src="/favicon.svg" alt="" />
        <h2>{draft.sessionCheckFailed ? 'We couldn’t load your account' : 'Pokémon Draft'}</h2>
        {!draft.sessionCheckFailed && (
          <p>Sign in to create a league, draft Pokémon, and manage your team.</p>
        )}
        {authError && (
          <p className="inline-error" role="alert">
            {authError}
          </p>
        )}
        {draft.error && (
          <p className="inline-error" role="alert">
            {draft.error}
          </p>
        )}
        {draft.sessionCheckFailed ? (
          <button
            className="button primary"
            disabled={workspaceBusy}
            onClick={() => void draft.connect().catch(() => {})}
          >
            <RefreshCw size={16} />
            Try again
          </button>
        ) : (
          <form className="sign-in-form" action="/auth/login" method="get">
            <label className="field">
              Display name (optional)
              <input
                name="display_name"
                maxLength={80}
                autoComplete="nickname"
                aria-describedby="display-name-hint"
                disabled={workspaceBusy}
              />
            </label>
            <p id="display-name-hint">For new accounts. Leave blank to use your Google name.</p>
            <button className="button primary" disabled={workspaceBusy}>
              Sign in
              <ArrowRight size={16} />
            </button>
          </form>
        )}
        {import.meta.env.DEV && import.meta.env.VITE_ENABLE_TEST_LOGIN === 'true' && (
          <>
            <button
              className="button text-button"
              disabled={workspaceBusy}
              onClick={() => setTestSignIn(true)}
            >
              Use a test account
            </button>
            {testSignIn && <TestSignIn onConnect={draft.connect} onClose={closeTestSignIn} />}
          </>
        )}
      </main>
    )

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault()
            setView('draft')
          }}
        >
          <img src="/favicon.svg" alt="" />
          <span>
            Pokémon
            <br />
            Draft
          </span>
        </a>
        <div className="workspace-label">LEAGUE</div>
        <div className="league-switch">
          <span className="league-icon">
            <Trophy size={18} />
          </span>
          <label>
            <span>Pokémon draft league</span>
            <select
              aria-label="Choose league"
              value={draft.leagueId ?? ''}
              disabled={workspaceBusy}
              onChange={(event) => {
                draft.selectLeague(Number(event.target.value))
                setView('draft')
              }}
            >
              {draft.leagues.length ? (
                draft.leagues.map((league) => (
                  <option key={league.id} value={league.id}>
                    {league.name}
                  </option>
                ))
              ) : (
                <option value="">Choose league</option>
              )}
            </select>
          </label>
          <ChevronDown size={14} />
        </div>
        <nav className="main-nav" aria-label="Main navigation">
          {navItems.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={view === id ? 'active' : ''}
              aria-label={label}
              aria-current={view === id ? 'page' : undefined}
              onClick={() => {
                if (id === 'availability') setAvailabilityContext(null)
                setView(id)
              }}
            >
              <Icon size={18} />
              <span>{label}</span>
              {id === 'draft' && snapshot?.state.status === 'drafting' && (
                <span className="nav-dot" />
              )}
              {id === 'settings' && myTeam && !myTeam.metadata_complete && (
                <span className="nav-dot" aria-label="Team setup needed" />
              )}
            </button>
          ))}
        </nav>
        <button className="new-league" disabled={workspaceBusy} onClick={() => setCreate(true)}>
          <Plus size={16} />
          Create a league
        </button>
        <div className="sidebar-bottom">
          <button className="help-button" onClick={() => setHelp(true)}>
            <HelpCircle size={17} />
            Drafting guide
            <BookOpen size={14} />
          </button>
          <div className="profile">
            <span className="user-avatar">{initials(session?.user.display_name ?? 'Guest')}</span>
            <div>
              <strong>{session?.user.display_name ?? 'Guest'}</strong>
              <small>{commissioner ? 'Commissioner' : 'League member'}</small>
            </div>
            <button
              className="icon-button"
              aria-label="Sign out"
              disabled={workspaceBusy}
              onClick={() => void draft.logout()}
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace
            <ChevronDown className="breadcrumb-arrow" size={12} />
            <strong>{navItems.find((item) => item.id === view)?.label}</strong>
          </div>
          <div className="topbar-actions">
            <span className={`connection ${draft.connection === 'connected' ? 'connected' : ''}`}>
              {draft.connection === 'connected' ? (
                <>
                  <span />
                  Live updates
                </>
              ) : draft.connection === 'closed' ? (
                <>
                  <RefreshCw size={13} /> Automatic refresh
                </>
              ) : (
                <>
                  <WifiOff size={13} />
                  {draft.connection === 'reconnecting' ? 'Reconnecting' : 'Connecting'}
                </>
              )}
            </span>
            <span className="topbar-divider" />
            <button
              className="icon-button"
              disabled={workspaceBusy}
              aria-label="Refresh all data"
              onClick={() => {
                setSelected(null)
                setConfirmAction(null)
                void draft.refreshData()
              }}
              title="Refresh leagues, catalog, favorites, draft, tournament, and availability"
            >
              <RefreshCw size={16} />
            </button>
            <span className="user-avatar small">
              {initials(session?.user.display_name ?? 'Guest')}
            </span>
            <ThemeToggle />
          </div>
        </header>
        <main>
          <div className="mobile-league-toolbar">
            <label>
              League
              <select
                aria-label="Choose league on mobile"
                value={draft.leagueId ?? ''}
                disabled={workspaceBusy}
                onChange={(event) => {
                  draft.selectLeague(Number(event.target.value))
                  setView('draft')
                }}
              >
                {draft.leagues.length ? (
                  draft.leagues.map((league) => (
                    <option value={league.id} key={league.id}>
                      {league.name}
                    </option>
                  ))
                ) : (
                  <option value="">No leagues yet</option>
                )}
              </select>
            </label>
            <button
              className="icon-button"
              aria-label="Create a league on mobile"
              disabled={workspaceBusy}
              onClick={() => setCreate(true)}
            >
              <Plus size={18} />
            </button>
            <button
              className="icon-button"
              aria-label="Sign out on mobile"
              disabled={workspaceBusy}
              onClick={() => void draft.logout()}
            >
              <LogOut size={17} />
            </button>
          </div>
          {draft.notice && (
            <div className="error-banner notice-banner" role="status">
              <span>{draft.notice}</span>
              <button
                className="icon-button"
                aria-label="Dismiss notice"
                onClick={() => draft.setNotice('')}
              >
                <X size={18} />
              </button>
            </div>
          )}
          {(authError || draft.error) && (
            <div className="error-banner" role="alert">
              <span>{authError || draft.error}</span>
              <button
                className="icon-button"
                aria-label="Dismiss error"
                onClick={() => (authError ? setAuthError('') : draft.setError(''))}
              >
                <X size={18} />
              </button>
            </div>
          )}
          <div className="page-heading">
            <div>
              <h1>{navItems.find((item) => item.id === view)?.label}</h1>
            </div>
            {snapshot && (
              <span className="season-badge">
                <Trophy size={15} />
                {snapshot.league.name}
              </span>
            )}
          </div>
          {view === 'catalog' ? (
            <Catalog
              key={session!.user.id}
              catalog={catalog}
              snapshot={snapshot}
              favorites={favorites}
              legacyWatchlist={legacyWatchlist}
              busy={draft.busy}
              onSelect={openPick}
            />
          ) : !snapshot ? (
            <div className="card empty-state">
              <Trophy size={34} />
              <h2>{draft.leagueId !== null ? 'Loading league…' : 'No leagues'}</h2>
              {draft.leagueId === null && (
                <div className="empty-state-actions">
                  <button className="button primary" onClick={() => setCreate(true)}>
                    <Plus size={16} /> Create league
                  </button>
                  <button className="button secondary" onClick={() => setView('catalog')}>
                    <Bookmark size={16} /> Browse catalog & favorites
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              {view === 'draft' && (
                <>
                  <section className="turn-banner">
                    <div className="turn-copy">
                      <span className="turn-label">
                        <span className="pulse-dot" />
                        {snapshot.state.status === 'setup'
                          ? 'SETUP'
                          : snapshot.state.status === 'completed'
                            ? 'COMPLETED'
                            : activeTeam?.manager_user_id === session?.user.id
                              ? 'YOUR TURN'
                              : 'CURRENT TURN'}
                      </span>
                      <h2>
                        {snapshot.state.status === 'setup'
                          ? 'Draft not started'
                          : snapshot.state.status === 'completed'
                            ? 'Draft complete'
                            : activeTeam
                              ? teamName(activeTeam)
                              : 'Next team'}
                      </h2>
                      <p>
                        {snapshot.state.status === 'setup' ? (
                          'Complete team names, colors, and draft order in League settings before starting.'
                        ) : snapshot.state.status === 'completed' ? (
                          `${snapshot.state.active_pick_count} of ${snapshot.state.required_pick_count} picks completed.`
                        ) : (
                          <>
                            Round {currentRound}
                            <span>·</span>Pick {snapshot.state.next_pick_number} of{' '}
                            {snapshot.state.required_pick_count}
                          </>
                        )}
                      </p>
                      {snapshot.state.status === 'completed' && (
                        <button
                          className="button banner-button"
                          onClick={() => setView('tournament')}
                        >
                          Open tournament <ArrowRight size={15} />
                        </button>
                      )}
                      {snapshot.state.status === 'setup' && (
                        <button
                          className="button banner-button"
                          onClick={() =>
                            can('start')
                              ? setConfirmAction({ kind: 'start', revision: snapshot.revision })
                              : setView('settings')
                          }
                        >
                          {can('start') ? 'Start draft' : 'League settings'}
                          <ArrowRight size={15} />
                        </button>
                      )}
                    </div>
                    <div className="banner-art" aria-hidden="true">
                      <div className="pokeball-ring" />
                    </div>
                    {snapshot.state.status === 'drafting' && <Countdown snapshot={snapshot} />}
                    {snapshot.state.status === 'completed' && (
                      <Trophy className="complete-trophy" size={55} />
                    )}
                  </section>
                  <div className="stats-grid">
                    <div className="stat-card">
                      <span className="stat-icon lavender">
                        <Users size={19} />
                      </span>
                      <div>
                        <span>Teams</span>
                        <strong>{String(snapshot.teams.length).padStart(2, '0')}</strong>
                      </div>
                    </div>
                    <div className="stat-card">
                      <span className="stat-icon mint">
                        <Target size={19} />
                      </span>
                      <div>
                        <span>Draft progress</span>
                        <strong>
                          {snapshot.state.active_pick_count}
                          <small>/ {snapshot.state.required_pick_count} picks made</small>
                        </strong>
                      </div>
                    </div>
                    <div className="stat-card">
                      <span className="stat-icon peach">
                        <ShieldCheck size={19} />
                      </span>
                      <div>
                        <span>{myTeam ? 'Your available points' : 'Current team’s points'}</span>
                        <strong>{(myTeam ?? activeTeam)?.points_remaining ?? '—'}</strong>
                      </div>
                    </div>
                  </div>
                  <div className="draft-layout">
                    <Catalog
                      key={session!.user.id}
                      catalog={catalog}
                      snapshot={snapshot}
                      favorites={favorites}
                      legacyWatchlist={legacyWatchlist}
                      busy={draft.busy}
                      onSelect={openPick}
                    />
                    <aside className="draft-aside">
                      {rosterTeam && (
                        <TeamRoster
                          team={rosterTeam}
                          snapshot={snapshot}
                          catalog={catalog}
                          userId={session!.user.id}
                          compact
                        />
                      )}
                      <section className="card order-panel">
                        <div className="panel-title">
                          <h3>Draft order</h3>
                          <span className="mini-pill">Snake</span>
                        </div>
                        {snapshot.teams.map((team, index) => (
                          <div
                            className={`order-row ${team.id === activeTeam?.id ? 'current' : ''}`}
                            key={team.id}
                          >
                            <span className="order-number">
                              {String(team.draft_position ?? index + 1).padStart(2, '0')}
                            </span>
                            <span className="tiny-avatar" style={teamAvatarStyle(team.color_rgb)}>
                              {initials(teamName(team))}
                            </span>
                            <strong>{teamName(team)}</strong>
                            {team.id === activeTeam?.id ? (
                              <Radio size={14} />
                            ) : (
                              team.manager_user_id === session?.user.id && (
                                <span className="you-label">You</span>
                              )
                            )}
                          </div>
                        ))}
                        <p>
                          <ArrowDown size={13} />
                          Order reverses each round
                        </p>
                      </section>
                      {can('undo') && (
                        <button
                          className="undo-button"
                          disabled={workspaceBusy}
                          onClick={() =>
                            setConfirmAction({ kind: 'undo', revision: snapshot.revision })
                          }
                        >
                          <History size={14} />
                          Undo last pick
                        </button>
                      )}
                    </aside>
                  </div>
                </>
              )}
              {view === 'teams' && (
                <div className="teams-grid">
                  {snapshot.teams.length ? (
                    snapshot.teams.map((team) => (
                      <TeamRoster
                        key={team.id}
                        team={team}
                        snapshot={snapshot}
                        catalog={catalog}
                        userId={session!.user.id}
                      />
                    ))
                  ) : (
                    <div className="card empty-state">
                      <Users size={28} />
                      <h3>No teams yet</h3>
                      <p>Add teams in League settings.</p>
                      <button className="button primary" onClick={() => setView('settings')}>
                        Set up teams
                      </button>
                    </div>
                  )}
                </div>
              )}
              {view === 'activity' && (
                <section className="card activity-panel">
                  <div className="panel-title">
                    <h2>Draft history</h2>
                    <span className="count-pill">{snapshot.history.length} selections</span>
                  </div>
                  {snapshot.history.length ? (
                    snapshot.history.map((pick) => {
                      const item = catalog.find((entry) => entry.id === pick.draftable_id)
                      const round = Math.floor((pick.pick_number - 1) / snapshot.teams.length)
                      const slot =
                        round % 2 === 0
                          ? ((pick.pick_number - 1) % snapshot.teams.length) + 1
                          : snapshot.teams.length - ((pick.pick_number - 1) % snapshot.teams.length)
                      const team = snapshot.teams.find((entry) => entry.draft_position === slot)
                      return (
                        <div
                          className={`activity-row ${pick.undone_at ? 'undone' : ''}`}
                          key={pick.id}
                        >
                          <span className="activity-number">
                            {String(pick.pick_number).padStart(2, '0')}
                          </span>
                          {item && <PokemonImage item={item} />}
                          <div className="activity-detail">
                            <strong>{item?.name ?? `Pokémon ${pick.draftable_id}`}</strong>
                            <span>
                              {team ? teamName(team) : 'Team'}
                              <span>·</span>Round {round + 1}
                            </span>
                          </div>
                          {pick.undone_at && <span className="mini-pill">Undone</span>}
                          <strong className="activity-price">
                            {pick.point_cost}
                            <small> pts</small>
                          </strong>
                          <time dateTime={pick.selected_at}>
                            {new Date(pick.selected_at).toLocaleTimeString([], {
                              hour: '2-digit',
                              minute: '2-digit',
                            })}
                          </time>
                        </div>
                      )
                    })
                  ) : (
                    <div className="empty-state">
                      <History size={30} />
                      <h3>No picks yet</h3>
                    </div>
                  )}
                </section>
              )}
              {view === 'tournament' && (
                <Tournament
                  key={`${session!.user.id}:${snapshot.league_id}`}
                  snapshot={snapshot}
                  userId={session!.user.id}
                  users={draft.users}
                  busy={draft.busy}
                  mutate={draft.mutate}
                  onCompareAvailability={(teamId, compareTeamId, date) => {
                    setAvailabilityContext({
                      owner: availabilityOwner,
                      teamId,
                      compareTeamId,
                      date,
                    })
                    setView('availability')
                  }}
                />
              )}
              {view === 'availability' && (
                <Availability
                  key={JSON.stringify([
                    availabilityOwner,
                    availabilityNavigation?.teamId,
                    availabilityNavigation?.compareTeamId,
                    availabilityNavigation?.date,
                  ])}
                  snapshot={snapshot}
                  userId={session!.user.id}
                  busy={draft.busy}
                  mutate={draft.mutate}
                  initialTeamId={availabilityNavigation?.teamId}
                  initialCompareTeamId={availabilityNavigation?.compareTeamId}
                  initialDate={availabilityNavigation?.date}
                />
              )}
              {view === 'settings' && (
                <LeagueSetup
                  key={`${session!.user.id}:${snapshot.league_id}`}
                  snapshot={snapshot}
                  userId={session!.user.id}
                  users={draft.users}
                  busy={draft.busy}
                  onMutation={draft.mutate}
                  onStartDraft={() =>
                    setConfirmAction({ kind: 'start', revision: snapshot.revision })
                  }
                />
              )}
            </>
          )}
        </main>
      </div>
      {help && (
        <Modal title="Draft rules" onClose={() => setHelp(false)}>
          <div className="guide">
            <p>
              <strong>Setup</strong>Create a league, add managers with a point budget, and save a
              draft order. Each manager needs to sign in at least once before you can add them.
            </p>
            <p>
              <strong>Pick order</strong>Teams pick in snake order: first to last, then last to
              first. Choose an available Pokémon you can afford. The commissioner can also pick on
              the current team’s behalf.
            </p>
            <p>
              <strong>Rosters and budgets</strong>Each Pokémon can only be drafted once per league.
              A pick cannot exceed the team’s remaining points unless the commissioner waives its
              cost.
            </p>
            <p>
              <strong>Timer and undo</strong>The timer is informational; it doesn’t make automatic
              picks. Commissioners can undo the last pick, which refunds its original cost. League
              settings are locked once the draft begins.
            </p>
          </div>
          <button className="button primary full-width" onClick={() => setHelp(false)}>
            Close
          </button>
        </Modal>
      )}
      {create && (
        <CreateLeague
          error={draft.error}
          busy={draft.busy}
          onClose={() => setCreate(false)}
          onSubmit={async (name, size, seconds) => {
            if (await draft.createLeague(name, size, seconds)) {
              setCreate(false)
              setView('settings')
            }
          }}
        />
      )}
      {selected && (
        <Modal
          title={snapshot ? 'Pick details' : 'Pokémon details'}
          onClose={() => setSelected(null)}
        >
          <PokemonDetails key={selected.item.id} item={selected.item} />
          {snapshot && (
            <div className="pick-summary">
              <span>
                Drafting for<strong>{activeTeam ? teamName(activeTeam) : 'No active turn'}</strong>
              </span>
              <span>
                Points after pick
                <strong>{activeTeam ? activeTeam.points_remaining - cost : '—'}</strong>
              </span>
            </div>
          )}
          {commissioner && can('pick') && !selectedTaken && (
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={!chargePoints}
                onChange={(event) => setChargePoints(!event.target.checked)}
              />
              Waive points for this pick<span>Commissioner</span>
            </label>
          )}
          {!snapshot ? (
            <p className="selection-notice">Choose a league to draft this Pokémon.</p>
          ) : selectedTaken ? (
            <p className="selection-notice">This Pokémon has already been drafted.</p>
          ) : pickChanged ? (
            <p className="selection-notice">
              The draft has changed. Close this window and select again to review the new turn.
            </p>
          ) : !can('pick') ? (
            <p className="selection-notice">
              {snapshot.state.status === 'setup'
                ? 'Start the draft before making a pick.'
                : snapshot.state.status === 'completed'
                  ? 'This draft is complete.'
                  : 'It is another manager’s turn.'}
            </p>
          ) : activeTeam && cost > activeTeam.points_remaining ? (
            <p className="selection-notice">
              This pick exceeds the current team’s remaining budget.
            </p>
          ) : null}
          {snapshot && (
            <button
              className="button primary full-width"
              disabled={!pickAllowed || draft.busy}
              onClick={async () => {
                await draft.draftAction('pick', selected.item.id, chargePoints)
                setSelected(null)
              }}
            >
              {draft.busy
                ? 'Submitting pick…'
                : `Draft ${selected.item.species || selected.item.name}`}
              <ArrowRight size={17} />
            </button>
          )}
          <button
            className="button text-button full-width"
            disabled={
              draft.busy ||
              favorites.busy ||
              favorites.loading ||
              !!favorites.readError ||
              !favorites.state
            }
            onClick={() => {
              const list = favorites.state
              if (!list) return
              const input = {
                p_list: list.id,
                p_revision: list.revision,
                p_draftable: selected.item.id,
              }
              const saved = list.entries.some((entry) => entry.draftable_id === selected.item.id)
              void favorites.run(() =>
                saved ? api.removeFavoritesEntry(input) : api.addFavoritesEntry(input),
              )
            }}
          >
            {favorites.state?.entries.some((entry) => entry.draftable_id === selected.item.id) ? (
              <>
                <Check size={15} />
                Remove from {favorites.state.name}
              </>
            ) : favorites.state ? (
              `Save to ${favorites.state.name}`
            ) : (
              'Create a favorites list in the catalog to save this Pokémon'
            )}
          </button>
          {favorites.error && (
            <p className="inline-error" role="alert">
              {favorites.error}
            </p>
          )}
        </Modal>
      )}
      {confirmAction && snapshot && (
        <Modal
          title={confirmAction.kind === 'start' ? 'Start draft?' : 'Undo last pick?'}
          onClose={() => setConfirmAction(null)}
        >
          <p className="modal-description">
            {confirmAction.kind === 'start'
              ? `Start ${snapshot.league.name} with ${snapshot.teams.length} teams and ${snapshot.league.roster_size} Pokémon per roster. Only saved team details and draft order will be used. Team names, colors, and league settings will be locked.`
              : 'The most recent active pick will be removed from its team and its original point cost refunded. The selection will stay in the draft history.'}
          </p>
          <p className="muted">
            {confirmAction.revision !== snapshot.revision
              ? 'The draft changed. Close this window and review the latest state before continuing.'
              : ''}
          </p>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setConfirmAction(null)}>
              <ArrowLeft size={15} />
              Cancel
            </button>
            <button
              className="button primary"
              disabled={
                draft.busy ||
                !can(confirmAction.kind) ||
                confirmAction.revision !== snapshot.revision
              }
              onClick={async () => {
                const kind = confirmAction.kind
                const succeeded = await draft.draftAction(kind)
                if (succeeded && kind === 'start') setView('draft')
                setConfirmAction(null)
              }}
            >
              {confirmAction.kind === 'start' ? 'Start draft' : 'Undo pick'}
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}

function CreateLeague({
  busy,
  error,
  onClose,
  onSubmit,
}: {
  busy: boolean
  error: string
  onClose: () => void
  onSubmit: (name: string, size: number, seconds: number) => Promise<void>
}) {
  return (
    <Modal title="Create league" onClose={onClose}>
      <p className="modal-description">Teams and budgets can be configured after creation.</p>
      {error && (
        <div className="inline-error" role="alert">
          {error}
        </div>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault()
          const data = new FormData(event.currentTarget)
          void onSubmit(
            String(data.get('name')).trim(),
            Number(data.get('roster')),
            Number(data.get('seconds')),
          )
        }}
      >
        <label className="field">
          League name
          <input
            name="name"
            placeholder="e.g. Indigo League"
            required
            maxLength={120}
            pattern=".*\S.*"
          />
        </label>
        <div className="form-grid">
          <label className="field">
            Pokémon per team
            <input name="roster" type="number" defaultValue={6} required min={1} max={100} />
          </label>
          <label className="field">
            Seconds per pick
            <input name="seconds" type="number" defaultValue={90} required min={1} max={86400} />
          </label>
        </div>
        <button className="button primary full-width" disabled={busy}>
          {busy ? 'Creating league…' : 'Create league'}
          <ArrowRight size={16} />
        </button>
      </form>
    </Modal>
  )
}
