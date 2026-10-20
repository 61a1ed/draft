# Pokémon Draft frontend

A React + TypeScript + Vite frontend for the adjacent `draft_backend` service.

## Run locally

For the complete database-backed workspace, use the adjacent backend launcher:

```powershell
cd E:\bz\draft_backend
uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'
```

Each run creates a fresh database under `.dev-runs`, imports the Pokémon catalog,
and seeds test users and six SQL scenarios. Ctrl+C stops all owned processes but
keeps data and logs. Use the printed `--data-dir` command to resume a prior run.
`--database-url-env NAME` connects to an existing prepared database without setup.
Open **http://127.0.0.1:5173** and sign in; fresh local databases offer test accounts.
The launcher forwards tagged logs and disables Vite colors and screen clearing.
It requires backend dependencies (`uv sync --locked`) and frontend dependencies
(`npm ci`) to be installed first. See the backend scripts README for SQL reset
commands and the tradeoffs between leagues, schemas, and databases.

To run the frontend against an existing backend:

Use Node.js 22.12+ (or another Node version supported by Vite 8).

```powershell
cd E:\bz\draft_frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:5173** and sign in. The frontend restores valid sessions
automatically. Signed-out visitors can enter an optional **Display name** before
selecting **Sign in**. New accounts use that name, or their Google name when left
blank; returning accounts keep their saved display name. A failed session check
shows **Try again**; sign-in errors appear inline on the welcome page.
Catalog images are served by the backend through `/images`, with no copied
Pokémon image library in `public/`. Named favorites lists are saved to your account
for reuse across leagues, including before you join a league. Existing browser
watchlists remain available in a read-only **Browser watchlist** tab. Bookmark
items there to copy them into the selected account list; nothing is transferred
automatically.

## Connect the backend

Vite proxies `/api`, `/auth`, `/images`, and `/healthz` to
`http://127.0.0.1:8000`. Set `API_TARGET` in `.env.local` if the API runs elsewhere.
The API uses same-origin session cookies and CSRF tokens; no browser API keys or
database credentials belong in this frontend.

**Set the backend's `PUBLIC_BASE_URL=http://127.0.0.1:5173` while developing.**
It must match the exact browser origin, including hostname and port. Keep the
backend listening on port 8000. The proxy preserves the browser Origin header
instead of bypassing the backend's origin checks. `localhost` and `127.0.0.1`
are different origins; use the documented address consistently.

To run against an existing backend test cluster, start the backend in a separate
PowerShell terminal with its generated environment and the frontend public URL:

```powershell
cd E:\bz\draft_backend
$env:PUBLIC_BASE_URL = 'http://127.0.0.1:5173'
uv run --env-file .dev-runs/<run>/database/.env draft-server
```

See the backend README for provisioning a fresh test database. This frontend
does not initialize, import into, or modify a database by itself.

For fixture login, copy `.env.example` to `.env.local` and set
`VITE_ENABLE_TEST_LOGIN=true`, then restart Vite. A separate **Use a test account**
button opens the Alice, Bob, and Carol selector **only in development** with that
explicit flag. The backend must independently allow test login against a marked test database.
Production builds never display this control.

Configure the backend with `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET`.
Register the frontend-origin callback `http://127.0.0.1:5173/auth/callback` with
the provider. Sign-in navigates the current tab to that provider. The backend callback establishes the
session and redirects to the app, which loads the session normally. Expected
sign-in failures return to the app with a readable error and a retry option.
Google OAuth itself requires real credentials and was not exercised during
frontend development.

## Features

- League creation; commissioner settings, team manager/budget changes, removal,
  and explicit draft-order saving before the draft starts.
- Setup leagues open in **League settings**, with **Your team** first. Assigned
  managers choose a name and color there, including commissioners who also play.
  Team assignment can leave the name blank; managers finish their details later.
  Commissioners can complete other teams' details, see unfinished teams, and start
  the draft from settings once all prerequisites are met. Names and colors lock at
  draft start; the Draft room displays them without an editor.
- Team colors use saved RGB values with readable avatar text; unnamed teams have
  a stable label. Metadata edits retain their original revision until saved or
  explicitly reloaded after a conflict; live updates do not replace unsaved input.
- Catalog search, point sorting, affordability filter, and league availability.
- **Catalog & favorites** is available without league membership. Create, select,
  rename, and delete account lists; save/remove Pokémon from catalog cards or pick
  details; reorder favorites with keyboard-accessible priority controls. Drafting
  a Pokémon keeps it in the list, and filtering never drops entries from a saved
  order. Favorites use account-scoped TanStack Query caches, independent list
  revisions, conflict handling, read retries, and no automatic write retries.
- **Favorites are private to their owner.** Reading lists requires sign-in, and
  the backend enforces ownership for list details and entries. Lists remain
  available across leagues.
- Pick confirmation showing the target team and resulting budget; optional
  commissioner point waiver; undo with revision protection.
- Team rosters, charged pick prices, and history including undone selections.
- A signed-in Tournament workspace for the existing round robin and two-team
  final: publication with frozen best-of values, season records, round schedules
  and byes, suggested weeks, participant reports and automatic agreement,
  commissioner decisions/corrections, VOD links, and complete match history.
- Commissioner withdrawals with explicit impact previews, repeatable qualification
  tiebreaks with separate records, and selection/publication of the two finalists.
  The champion comes from the official final result. Qualification follows the
  commissioner's announced criteria; displayed records do not automatically seed
  or select finalists. Signed-out spectator pages are deferred.
- Team availability planning with a dated weekly grid, timezone selection, shared
  free time, and week copying that preserves local wall times across daylight
  saving changes. Managers edit their own teams; match details open both teams
  at the round's suggested week, and any dates can be browsed. Availability is
  advisory and does not book a match or restrict result reporting.
- One SSE stream at `/api/leagues/{id}/events` for the selected draft. Picks wake
  the draft query immediately, including an undo that reopens a completed draft.
  Switching leagues replaces the stream; sign-out and unmount close it.
- TanStack Query owns session, catalog, league, draft, favorites, tournament, match,
  and availability reads, plus mutations. Account-scoped keys, abortable reads,
  revision guards, and cache clearing on logout prevent stale or cross-account data.
- Mutations invalidate affected queries immediately. Other tabs' changes and new
  league assignments arrive through 30-second query polling, window focus, and
  network reconnection. Session checks run every minute; catalog refreshes every
  five minutes. Transient reads retry twice; writes are never automatically replayed.
- Responsive navigation, keyboard-accessible native dialogs, labeled forms,
  and reduced-motion support.

The server remains authoritative for live draft rules, access, budget checks,
and revisions. Mutations are not automatically retried. On conflicts or unknown
outcomes, the app refreshes the snapshot so the user can review before retrying.
The countdown uses the snapshot's server time; expiry is informational, matching
the backend (there is no automatic pick or skip).

Each newly established listener sends an initial invalidation. Notifications are
transient wake-up signals: a reconnect invalidates the selected draft query,
including changes missed while offline. Each active draft stream uses a persistent
PostgreSQL connection in addition to the server's normal query connections. Keep
database capacity available for normal API traffic; the frontend does not change
database connection limits. Closing a tab or signing out closes its stream.

Catalog refresh uses HTTP because administrative repricing has no backend
event. Existing roster/history prices always use the amount originally charged.
The toolbar's **Refresh all data** reloads the session, catalog, user and league
lists, favorites lists, and the selected snapshot/event stream. It keeps the selected league if
it still exists, otherwise selects the first league. Use it after applying
scenario SQL. This also clears old revision authority after administrative resets.

## Checks and production build

```powershell
npm test
npm run build
```

`build` checks strict TypeScript and writes the static site to `dist/`.
`npm run preview` serves it locally at port 4173 with the same API proxy; if
testing authenticated writes there, also change the backend public URL to
`http://127.0.0.1:4173`.

In production, serve `dist/` and proxy the four backend paths on **one HTTPS
origin**. Preserve the `/auth` callbacks, forward cookies, disable buffering for
SSE, and configure backend `PUBLIC_BASE_URL` to that origin with secure cookies.
The Vite development/preview servers are not production servers.

Visual assets reuse the supplied local Pokémon catalog. Lucide provides UI
icons. Google Fonts provides DM Sans and Manrope, with system sans-serif
fallbacks if the font service is unavailable.

## Project layout

- `src/App.tsx`: workspace navigation, draft views, account and pick dialogs.
- `src/components/`: catalog, roster setup, and accessible modal components.
- `src/components/Tournament.tsx`, `TournamentMatch.tsx`: tournament overview,
  management, schedule, reporting, and history within the signed-in league shell.
- `src/components/Availability.tsx`: team schedules, shared availability, window
  editing, and copying a saved week. Saving retains windows on every date, and
  copying adds to the destination. Repeated clock times require an explicit
  occurrence when entered; nonexistent local times are rejected.
- `src/lib/availabilityTypes.ts`, `availabilityTime.ts`, `useAvailability.ts`:
  availability contracts, timezone conversion and interval intersection, and
  revision-aware queries. Availability refreshes independently of draft events
  and league/tournament revisions; writes are never replayed.
- `src/lib/api.ts`, `types.ts`: typed backend contract and event client.
- `src/lib/queryClient.ts`: shared query keys, retry policy, and cache defaults.
- `src/lib/useDraft.ts`: session/league queries, mutations, and draft stream lifecycle.
- `src/lib/useResourceQuery.ts`: reusable resource queries and guarded mutations.
- `src/lib/favoritesTypes.ts`, `useFavorites.ts`: account favorites contract,
  account-scoped queries, selection, revision protection, and mutation lifecycle.
- `src/lib/tournamentTypes.ts`, `useTournament.ts`, `tournamentView.ts`: tournament
  contract, cached queries, and presentation rules. Tournament mutations invalidate
  both tournament and match queries, independently of the draft revision.
  Each operation uses its own league/tournament/match/report token;
  stale forms require review, and uncertain writes are refreshed without replay.
- `src/lib/*.test.*`: API, OAuth, and lifecycle regression tests.
- `public/`: app favicon and generic image placeholder.

Live integration was exercised against native PostgreSQL through the Vite proxy,
including local sign-in, SQL scenario selection, catalog images, persisted picks,
undo, SQL-only changes arriving through SSE, and refreshing after scenario replacement.
Google OAuth still requires real credentials and was not exercised.
