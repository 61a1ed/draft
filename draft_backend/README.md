# Draft backend

A small Python/uv gateway for the supplied PostgreSQL schema. FastAPI handles
HTTP, Authlib handles OpenID Connect, and psycopg calls the SQL functions
directly. PostgreSQL owns draft rules, permissions, revisions, and snapshots.
There is no ORM or second implementation of the draft state machine.

For the full frontend + backend exploration loop, run
`uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'` after installing the
backend and frontend dependencies. This prepares a dedicated `.testdb-dev`
database, imports the Pokémon catalog, loads editable SQL scenario leagues, and
starts both servers. Subsequent runs need no binary path and preserve draft changes.
See [the SQL scenario workflow](scripts/README.md#frontend-exploration-with-sql)
for one-command resets, custom SQL, and isolation choices.

`sql/schema.sql` includes point costs and team budgets. `sql/grants.sql` supplies
the deployment permissions. Authentication inserts/updates users using a separate
role.

## Start a local test instance

Requirements: uv, Python 3.12+, and PostgreSQL 16+ binaries. The project pins
Python 3.12 for development and includes `uv.lock`. PostgreSQL 18 is suitable.
Run these commands from the extracted project directory:

```powershell
uv sync --locked
uv run python scripts/testdb.py --pg-bin 'C:\postgresql\bin' --root .testdb init
uv run --env-file .testdb/.env draft-server
```

Replace the binary path with the directory containing `initdb.exe`, `pg_ctl.exe`,
and `postgres.exe`. On Linux/macOS, supply the corresponding binary directory
and run as an ordinary OS user. No OS service or Docker is involved.

`init` starts a separate, loopback-only cluster on port 55432, installs the
schema/roles, creates fixture users `alice`, `bob`, and `carol`, and seeds 30
draftable players. It generates credentials and application settings in
`.testdb/.env`. It does not modify another PostgreSQL installation.

The API is at `http://127.0.0.1:8000`; documentation is at `/docs` and
`/openapi.json`. `draft-server` explicitly uses HTTP/1.1 and a selector event
loop, including on Windows. Use this entry point instead of relying on
Uvicorn's default Windows loop. `HOST` and `PORT` override the listening address.

Manage the test cluster independently of the HTTP server:

```sh
uv run python scripts/testdb.py --root .testdb status
uv run python scripts/testdb.py --root .testdb stop
uv run python scripts/testdb.py --root .testdb start
uv run python scripts/testdb.py --root .testdb destroy
```

`destroy` permanently deletes the marked test directory, including its data and
generated credentials. It refuses unrecognized directories. To reset, destroy
and initialize again. More provisioning details are in `scripts/README.md`.

## Authentication

Configure one OpenID Connect provider with `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and
`OIDC_CLIENT_SECRET`. For Google, the issuer is `https://accounts.google.com`;
create a Google OAuth **Web application** client and register exactly `<PUBLIC_BASE_URL>/auth/callback` as its redirect URI.
Copy `.env.example` to a private `.env`, supply the client credentials,
`DATABASE_URL`, and a random session secret, then run:

```sh
uv run --env-file .env draft-server
```

Visit `/auth/login`. The server performs the authorization-code flow and
validates state, nonce, signature, issuer, audience, and expiry through Authlib.
Any valid account from the configured issuer can register. The first login creates
an `app_user` identified by the verified `(issuer, subject)` pair; subsequent
logins update its display name. Different issuers never share identities, even
when their subjects match. Only `openid profile` is requested; provider tokens
are not stored in the session. Local fixtures use issuer `urn:draft:test` and
subjects `alice`, `bob`, and `carol`.

This schema replaces `google_sub` with `issuer` and `subject`. Reinitialize
existing disposable test clusters after updating. Existing non-disposable databases
need a data migration preserving user IDs before running this version.
Replace `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` with the `OIDC_` variables and
update the provider's registered redirect URI to `<PUBLIC_BASE_URL>/auth/callback`.

The callback sets an HttpOnly, SameSite=Lax signed session cookie and redirects
to `PUBLIC_BASE_URL/`. Expected sign-in failures redirect there with an
`auth_error` code for the frontend to display. `GET /auth/me` returns `user`,
`csrf_token`, and `expires_at`; `POST /auth/logout` clears the cookie. Sessions have a fixed
expiry (24 hours by default, configurable with `SESSION_TTL_SECONDS`). They are
stateless: logout clears this browser's cookie, with no server-side session
revocation table. Close existing event streams on logout or account switching.

For local test login:

```python
import httpx

with httpx.Client(base_url="http://127.0.0.1:8000") as api:
    response = api.post("/auth/test", json={"username": "alice"},
                        headers={"X-Test-Login": "1"})
    response.raise_for_status()
    api.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    response = api.post("/api/rpc/create_league", json={
        "p_name": "Example league", "p_roster_size": 2,
    })
    response.raise_for_status()
    league_id = response.json()["result"]
    print(api.get(f"/api/leagues/{league_id}").json())
```

Run a saved version with `uv run python example.py`. Test login accepts only
existing fixture users with synthetic `test:` subjects. It requires all of:
`APP_ENV=test`, `TEST_LOGIN_ENABLED=true`, and the database marker installed by
the test tools. Otherwise the route is absent or startup refuses the database.
It cannot accept arbitrary user IDs or register arbitrary test accounts.

All authenticated POST requests require `X-CSRF-Token` from `/auth/me` or login.
If an `Origin` header is present, it must match `PUBLIC_BASE_URL`. The pre-login
test endpoint instead requires JSON and `X-Test-Login: 1`. Use a same-origin
frontend or a development proxy; cross-origin CORS is not enabled.

For production set `APP_ENV=production`, an HTTPS `PUBLIC_BASE_URL`, and
`COOKIE_SECURE=true`. Terminate TLS at your reverse proxy. OIDC callback
addresses come from configuration rather than forwarded request headers.

## HTTP contract

All `/api` routes require a session. Read results come directly from PostgreSQL:

| Method/path | Result |
|---|---|
| `GET /api/users` | Array of `{id, display_name}`; no login identities |
| `GET /api/draftables` | Global catalog array |
| `GET /images/{hash}` / `HEAD /images/{hash}` | Public PNG image; no session required |
| `GET /api/leagues` | Array of leagues visible to the caller |
| `GET /api/leagues/{id}` | One `dl.league_snapshot` row, including actions |
| `GET /api/leagues/{id}/events` | Authorized SSE stream |
| `GET /healthz` | Process status and notification-connection status; public |

Each write is `POST /api/rpc/<function>` with a JSON object using the actual SQL
argument names. Unknown fields are rejected. There is no arbitrary-SQL endpoint.

| Function | JSON arguments | `result` |
|---|---|---|
| `create_league` | `p_name`, `p_roster_size`, `p_pick_seconds` (default 60) | League ID |
| `update_league` | `p_league`, `p_revision`, `p_name`, `p_roster_size`, `p_pick_seconds` | null |
| `add_team` | `p_league`, `p_revision`, `p_name`, `p_manager`, `p_point_budget` | Team ID |
| `update_team` | `p_league`, `p_revision`, `p_team`, `p_name`, `p_manager`, `p_point_budget` | null |
| `remove_team` | `p_league`, `p_revision`, `p_team` | null |
| `set_draft_order` | `p_league`, `p_revision`, `p_teams` (array of team IDs) | null |
| `draft_action` | `p_league`, `p_revision`, `p_kind`, `p_draftable` (default null), `p_charge_points` (default true) | Pick ID or null |

Successful writes return `{"result": ...}` **after commit**. `p_kind` is `start`,
`pick`, or `undo`; only `pick` takes a non-null `p_draftable`. Only a commissioner
making a pick may set `p_charge_points` to false. Setup updates are
complete replacements of the supplied fields, not PATCH operations. Fetch the
snapshot after success; every mutation except creation advances revision by one.

Errors use `{"detail": "code"}` (FastAPI validation errors use its standard
detail array): 400 for SQL input/reference errors, 401 for missing/expired login,
403 for CSRF/origin failures, 404 for invisible/missing resources, 409 for stale
revisions, disallowed actions or uniqueness conflicts, and 422 for request-shape
errors. Exhausted transaction retries and database unavailability return 503.
Private PostgreSQL error details are not returned.

The server retries serialization/deadlock failures as complete transactions with
unchanged arguments, up to four retries. It never changes a supplied revision or
automatically retries a `stale_revision` error. After 409, the frontend should
fetch the current snapshot and decide what to do. Lost HTTP responses have no
idempotency-key mechanism; refresh before repeating a write whose outcome is
unknown, especially `create_league`.

## Pokémon catalog and images

Import a UTF-8 CSV with columns `dex,species,form,points,image_path`. Blank forms
represent the base form. Images are local PNG files, with paths relative to an
explicit image-root directory. The importer runs wherever those files live and
connects to the destination PostgreSQL database, which can be remote. PostgreSQL
does not need filesystem access to the source images. No URLs are fetched.

From the backend directory, export the approved SQLite table and validate it:

```sh
uv run python scripts/export_catalog.py ../bz.db ../pokemon_catalog.csv
uv run python scripts/import_catalog.py ../pokemon_catalog.csv --image-root ../showdown --dry-run
```

After bootstrapping a fresh database, set `ADMIN_DATABASE_URL` to an administrative
connection allowed to `SET ROLE dl_owner`, then import:

```sh
uv run python scripts/import_catalog.py ../pokemon_catalog.csv --image-root ../showdown
```

The import validates all rows and reads all files before opening its database
connection, then commits assets and catalog rows in one transaction. Repeating it
updates entries by `(dex, form)`, preserving draftable IDs and recorded pick prices.
Omitted entries and old assets are retained. Changing a dex/form creates a new entry.
There is no delete/reconcile mode or admin HTTP interface. Production bootstrap
does not insert the test catalog; the test initializer still supplies its 30 players.

The CSV carries species, form, points, and image paths; no Showdown IDs are imported.
Only large images are exported from `min_pokemon`. The supplied source contains
263 Pokémon and 6,102,933 bytes of images. Copy the CSV and image directory together
to run the same import elsewhere, preserving the relative image paths.

Catalog responses include `id`, `name`, `point_cost`, `dex`, `species`, `form`, and
`image_url`. Display names include nonempty forms in parentheses. Generic test
players have null Pokémon metadata and image URLs.

`dl.asset` stores the PNG bytes and content type under their SHA-256 hash. The
database checks the hash against the bytes; identical images share one asset.
The public image endpoint supports GET, HEAD, ETag/If-None-Match, and one-year
immutable caching. New bytes get a new URL. Successful image reads also remain in
a simple per-worker dictionary until restart, without an expiry or size limit.
Missing images return uncached 404 responses. The current large-image set uses
about 5.82 MiB of payload memory if entirely cached; each worker has its own cache.

## Point costs and budgets

Each team is a drafter. Costs and budgets are required nonnegative PostgreSQL
integers (0 through 2,147,483,647); zero-cost draftables and zero budgets are valid.
There are no implicit prices or unlimited/default budgets.

| Field | Meaning |
|---|---|
| `dl.draftable.point_cost` | Global catalog price, supplied by admin SQL |
| `dl.team.point_budget` | This team's spending limit, set during setup |
| `dl.pick.point_cost` | Price actually charged when this pick was made |
| `dl.team_points.points_spent` | Sum of this team's active pick prices (`bigint`) |
| `dl.team_points.points_remaining` | Budget minus active spending (`bigint`) |

`add_team` and `update_team` now require `p_point_budget`, including updates that
only change the name or manager. Like other team configuration, the budget freezes
when drafting starts. Each league can assign different budgets to its teams.

A pick is allowed when its catalog price is at most the current team's remaining
points. An over-budget pick returns HTTP 409 with
`{"detail":"point_budget_exceeded"}` and changes neither picks, revision nor timer.
The snake-order team's budget is charged by default, including commissioner picks.
Existing SERIALIZABLE transactions and revision checks govern concurrent writes.
No points are reserved for future turns, and starting a draft does not test whether
any team can afford to complete its roster. `permitted_action` continues to describe
turn permissions; it does not enumerate affordable draftables.

A commissioner may set `p_charge_points: false` to acquire a draftable for free,
including for their own team. The pick records `point_cost = 0`; no separate waiver
field is stored. Your catalog prices can remain strictly positive so zero charges
identify these exceptions. Turn permissions, revisions, draftable existence and
uniqueness are still enforced. An ordinary manager attempting this receives HTTP
409 `action_not_permitted`. `false` on start/undo and SQL NULL for this parameter
are rejected as `invalid_draft_action` (HTTP 400); HTTP input requires a JSON boolean.
Omitting the parameter or supplying true charges the catalog price as before.

```json
{"p_league": 1, "p_revision": 8, "p_kind": "pick", "p_draftable": 42,
 "p_charge_points": false}
```

Spending is derived from active picks, not maintained as a mutable balance. Undo
therefore refunds the original charge automatically. Catalog repricing affects
future picks only; a re-pick after undo uses the new price. The history retains
the old charge. Undoing a free pick refunds zero; a later pick of that draftable
charges the catalog price unless the commissioner explicitly waives it again.
The price lookup uses the catalog primary key; the spending sum
reads the team's active roster, without expanding the catalog into possible picks.

`GET /api/draftables` includes `point_cost`. Snapshot `teams` entries contain
`point_budget`, `points_spent`, and `points_remaining`; `rosters` and `history`
entries contain each pick's recorded `point_cost`. SSE snapshots use the same shape.
A frontend can compare catalog prices with the next team's remaining points.

Catalog writes remain administrator SQL, for example:

```sql
INSERT INTO dl.draftable (name, point_cost) VALUES ('Example', 12);
UPDATE dl.draftable SET point_cost = 15 WHERE id = 42;
```

There is no catalog-write HTTP endpoint or catalog-change notification. Populate
prices before drafting; clients must refetch the catalog after administrative
repricing. The seed catalog uses costs 0 through 29.

## Server-sent events

```javascript
const events = new EventSource(`/api/leagues/${leagueId}/events`);
events.addEventListener("snapshot", e => render(JSON.parse(e.data)));
events.addEventListener("changed", async () => {
  const r = await fetch(`/api/leagues/${leagueId}`);
  if (r.ok) render(await r.json());
});
events.addEventListener("access_revoked", () => events.close());
events.addEventListener("signed_out", () => events.close());
// Call events.close() when logging out or switching users.
```

The initial `snapshot` contains the same object as the snapshot GET. `changed`
contains `{league_id, revision}`; its SSE ID is the league revision. The server
subscribes before reading the initial snapshot, eliminating the initial gap.
One PostgreSQL LISTEN connection per server worker feeds bounded subscriber
queues. Intermediate invalidations may coalesce. Access is checked through RLS
on every wakeup and heartbeat; revocation or session expiry closes the stream.

These are revision invalidations, not a durable record of each pick/start/undo.
`Last-Event-ID` does not request replay: reconnecting always gets a fresh
snapshot. LISTEN reconnection also triggers snapshots. Heartbeats every 15
seconds recheck the revision, covering missed notifications. A transient query
failure emits `reconnect` and closes; native EventSource reconnects automatically.
Consumers should ignore snapshots older than the revision already rendered when
concurrent HTTP fetches finish out of order.

The stream works over HTTP/1.1. Disable buffering for this route in any reverse
proxy; the response includes `X-Accel-Buffering: no`. Idle timeouts should exceed
the heartbeat interval. Cookies authenticate native EventSource automatically.

## Database roles

| Role | Purpose |
|---|---|
| `dl_owner` | NOLOGIN, non-superuser schema/function owner; never used for requests |
| `dl_api` | NOLOGIN, read views/tables plus public SQL functions; no table DML |
| `dl_auth` | NOLOGIN, read/upsert `app_user` only |
| `dl_gateway` | Unprivileged LOGIN, NOINHERIT; can explicitly select API/auth roles |

People are `app_user` rows. Commissioner/manager permissions remain in the
schema. `request.jwt.claims` is populated transaction-locally from the verified
session; despite its name, no browser JWT is needed. Runtime credentials belong
only to the trusted Python server. Startup rejects an owner/superuser connection.

One subtlety in the supplied deployment comments: `SET ROLE` does not apply a
role's `ALTER ROLE ... SET default_transaction_isolation`. The backend explicitly
sets SERIALIZABLE before every draft write. Reads use READ COMMITTED; the single
snapshot SELECT is internally consistent.

For production bootstrap, use `scripts/bootstrap.py` as described in
`scripts/README.md`. It requires an empty database and unused reserved role names.
Catalog population is administrative SQL; no catalog-write HTTP API is exposed.
Test fixtures and the test marker are never installed by production bootstrap.

## Checks

```sh
uv run pytest -q
uv run ruff check .
```

Native integration tests are opt-in and refuse databases lacking the test marker.
With your local test cluster running, this cross-platform command loads its
generated URL without displaying credentials:

```sh
uv run --env-file .testdb/.env python -c "import os,pytest; os.environ['TEST_DATABASE_URL']=os.environ['DATABASE_URL']; raise SystemExit(pytest.main(['-q']))"
```

The integration suite covers all RPCs, RLS and privilege denials, stale revisions,
duplicate picks, a complete snake draft, undo/re-pick, and registration upsert.
It adds uniquely named test data and can be repeated on the same fixture database.

Delivery verification for this revision: 78 backend tests passed against a separate
native PostgreSQL 18.6 test cluster; Ruff passed. Scenario tests cover SQL-derived
snapshots, scoped replacement with fresh IDs, preserved unrelated data, and atomic
rollback. The frontend's 41 tests and production build passed. Live browser checks
through the Vite proxy covered fixture sign-in, catalog images, a persisted pick,
undo, SQL-only updates arriving through SSE, and scenario replacement followed by
Refresh all data. The exploration database contains the approved 263-row catalog.
OIDC login tests use a mocked provider; actual login requires your OAuth credentials.
Existing Authlib/Starlette httpx deprecation warnings remain.

Reference documentation: [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
[Authlib](https://docs.authlib.org/en/latest/client/starlette.html),
[psycopg async connections](https://www.psycopg.org/psycopg3/docs/advanced/async.html),
[PostgreSQL LISTEN](https://www.postgresql.org/docs/current/sql-listen.html),
[PostgreSQL SET ROLE](https://www.postgresql.org/docs/current/sql-set-role.html),
and [Uvicorn event loops](https://uvicorn.dev/concepts/event-loop/).
