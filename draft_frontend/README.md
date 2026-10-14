# Pokémon Draft frontend

A React + TypeScript + Vite frontend for the adjacent `draft_backend` service.

## Run locally

For the complete database-backed workspace, use the adjacent backend launcher:

```powershell
cd E:\bz\draft_backend
uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'
```

The first run prepares `.testdb-dev`, imports the 263 Pokémon and their images,
and loads six editable SQL scenarios. Later runs only need `uv run python scripts/dev.py`
and preserve draft changes. Open **http://127.0.0.1:5173**, sign in with a local
test account, and choose a scenario from the league selector. The launcher sets
the matching browser origin, API proxy, and test login.
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

Open **http://127.0.0.1:5173** and sign in. The frontend requires the backend;
missing authentication shows Sign in, and connection failures show Retry.
Catalog images are served by the backend through `/images`, with no copied
Pok�mon image library in `public/`. Watchlists are stored locally per user.

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
uv run --env-file .testdb/.env draft-server
```

See the backend README for provisioning a fresh test database. This frontend
does not initialize, import into, or modify a database by itself.

For fixture login, copy `.env.example` to `.env.local` and set
`VITE_ENABLE_TEST_LOGIN=true`, then restart Vite. The sign-in dialog exposes
Alice, Bob, and Carol **only in development** with that explicit flag. The
backend must independently allow test login against a marked test database.
Production builds never display this control.

For Google login, configure the backend OAuth credentials and register the
frontend-origin callback `http://127.0.0.1:5173/auth/google/callback` with Google.
Sign-in opens a popup because the existing backend returns JSON at its callback.
The app watches for the popup to return to its same-origin callback, then checks
the session and reconnects. It does not poll the session endpoint while Google
is still using the OAuth state cookie.
Google OAuth itself requires real credentials and was not exercised during
frontend development.

## Features

- League creation; commissioner settings, team manager/budget changes, removal,
  and explicit draft-order saving before the draft starts.
- Catalog search, point sorting, affordability filter, availability, and watchlists.
- Pick confirmation showing the target team and resulting budget; optional
  commissioner point waiver; undo with revision protection.
- Team rosters, charged pick prices, and history including undone selections.
- SSE snapshot/invalidation updates, automatic reconnect, monotonic revisions,
  session expiry/access revocation handling, and stream cleanup on account changes.
- Responsive navigation, keyboard-accessible native dialogs, labeled forms,
  and reduced-motion support.

The server remains authoritative for live draft rules, access, budget checks,
and revisions. Mutations are not automatically retried. On conflicts or unknown
outcomes, the app refreshes the snapshot so the user can review before retrying.
The countdown uses the snapshot's server time; expiry is informational, matching
the backend (there is no automatic pick or skip).

Catalog refresh is explicit because administrative repricing has no backend
event. Existing roster/history prices always use the amount originally charged.
The toolbar's **Refresh all data** reloads the session, catalog, user and league
lists, and the selected snapshot/event stream. It keeps the selected league if
it still exists, otherwise selects the first visible league. Use it after applying
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
- `src/lib/api.ts`, `types.ts`: typed backend contract and event client.
- `src/lib/useDraft.ts`: session, league, request, and stream lifecycle.
- `src/lib/*.test.*`: API, OAuth, and lifecycle regression tests.
- `public/`: app favicon and generic image placeholder.

Live integration was exercised against native PostgreSQL through the Vite proxy,
including local sign-in, SQL scenario selection, catalog images, persisted picks,
undo, SQL-only changes arriving through SSE, and refreshing after scenario replacement.
Google OAuth still requires real credentials and was not exercised.
