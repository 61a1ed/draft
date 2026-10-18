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
automatically. Signed-out visitors see a welcome page with **Sign in**, which
starts authentication directly. A failed session check shows **Try again**;
sign-in errors appear inline on the welcome page.
Catalog images are served by the backend through `/images`, with no copied
Pokémon image library in `public/`. Watchlists are stored locally per user.

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
- Catalog search, point sorting, affordability filter, availability, and watchlists.
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
- One account SSE stream at `/api/events` per signed-in tab, including accounts
  with no leagues. Assignments appear automatically with a notice; selection
  changes keep the stream open, and removed access selects another league or
  clears the private league state.
- Serialized, coalesced league-list and selected-snapshot refreshes, monotonic
  revisions, explicit reconnect and read retries with backoff capped at 30 seconds,
  session expiry handling, and stream cleanup on account changes.
- Responsive navigation, keyboard-accessible native dialogs, labeled forms,
  and reduced-motion support.

The server remains authoritative for live draft rules, access, budget checks,
and revisions. Mutations are not automatically retried. On conflicts or unknown
outcomes, the app refreshes the snapshot so the user can review before retrying.
The countdown uses the snapshot's server time; expiry is informational, matching
the backend (there is no automatic pick or skip).

Each newly established listener sends an initial invalidation. Notifications are
transient wake-up signals: a reconnect reloads the current league list and snapshot,
including changes missed while offline. Every open tab uses its own persistent
PostgreSQL connection in addition to the server's normal query connections. Keep
database capacity available for normal API traffic; the frontend does not change
database connection limits. Closing a tab or signing out closes its stream.

Catalog refresh is explicit because administrative repricing has no backend
event. Existing roster/history prices always use the amount originally charged.
The toolbar's **Refresh all data** reloads the session, catalog, user and league
lists, and the selected snapshot/event stream. It keeps the selected league if
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
  revision-aware reads. Availability refreshes from the existing account stream
  independently of league/tournament revisions; writes are never replayed.
- `src/lib/api.ts`, `types.ts`: typed backend contract and event client.
- `src/lib/useDraft.ts`: session, league, request, and stream lifecycle.
- `src/lib/tournamentTypes.ts`, `useTournament.ts`, `tournamentView.ts`: tournament
  contract, serialized reads/retries, and presentation rules. The existing account
  stream invalidates tournament and match reads even when the draft revision is
  unchanged. Each operation uses its own league/tournament/match/report token;
  stale forms require review, and uncertain writes are refreshed without replay.
- `src/lib/*.test.*`: API, OAuth, and lifecycle regression tests.
- `public/`: app favicon and generic image placeholder.

Live integration was exercised against native PostgreSQL through the Vite proxy,
including local sign-in, SQL scenario selection, catalog images, persisted picks,
undo, SQL-only changes arriving through SSE, and refreshing after scenario replacement.
Google OAuth still requires real credentials and was not exercised.
