# Draft backend

A small Python/uv gateway for the supplied PostgreSQL schema. FastAPI handles
HTTP, Authlib handles OpenID Connect, and psycopg calls the SQL functions
directly. PostgreSQL owns draft rules, permissions, revisions, and snapshots.
There is no ORM or second implementation of the draft state machine.

For the full frontend + backend exploration loop, run
`uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'` after installing the
backend and frontend dependencies. Each invocation creates a fresh database in
`.dev-runs`, imports the Pokémon catalog, seeds users and scenarios, and starts
both servers. Ctrl+C stops owned processes and keeps files. Use the printed
`--data-dir` command to resume, or `--database-url-env NAME` for an existing
network database without initialization or seeding.
See [the SQL scenario workflow](scripts/README.md#frontend-exploration-with-sql)
for one-command resets, custom SQL, and isolation choices.

`sql/schema.sql` includes point costs and team budgets. `sql/grants.sql` supplies
the deployment permissions. Authentication inserts/updates users using a separate
role.

## Local development and integration tests

Requirements: uv, Python 3.12+, PostgreSQL 16+ binaries, and (for the frontend)
Node.js. Run `uv sync --locked` here and `npm ci` in `draft_frontend`.

```powershell
uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'
# Database tests only; no Node.js, catalog files, or running servers required:
uv run python -m pytest --integration --pg-bin='C:\postgresql\bin' -m integration -q
```

Both use `scripts/postgres.py` for PostgreSQL and `scripts/processes.py` for
owned process trees and logs. PostgreSQL runs on loopback without an OS service
or Docker. On POSIX, run as an ordinary OS user.

Every test invocation that selects integration tests creates a fresh cluster in
`.test-runs/integration-*/database`, on an automatically chosen port. Its processes
stop at teardown, even after failures. Database files and logs remain for inspection;
the final test summary prints the run directory. Reclaim disk space manually when
finished. There is no scripted database deletion or filesystem ownership marker.

Development uses the Pokémon catalog; integration tests use the small synthetic
catalog in `tests/catalog.sql`. Both explicitly load `sql/test_users.sql` for test
users and the database test-login marker. Database initialization itself seeds neither.
To inspect a retained test database through the app, use `dev.py --data-dir
<run>/database/data --pg-bin <binaries>`; this resumes without reseeding.

The API is at `http://127.0.0.1:8000`; documentation is at `/docs` and
`/openapi.json`. `draft-server` explicitly uses HTTP/1.1 and a selector event
loop, including on Windows. `HOST` and `PORT` override its listening address.
When systemd supplies `RUNTIME_DIRECTORY`, the server instead listens at
`$RUNTIME_DIRECTORY/http.sock`, trusts proxy headers on that restricted socket,
and gives requests five seconds to finish during shutdown. The socket directory
must restrict access to the application and reverse proxy accounts.

## Authentication

Configure one OpenID Connect provider with `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and
`OIDC_CLIENT_SECRET`. For Google, the issuer is `https://accounts.google.com`;
create a Google OAuth **Web application** client and register exactly
`<PUBLIC_BASE_URL><OIDC_CALLBACK_PATH>` as its redirect URI. `OIDC_CALLBACK_PATH`
defaults to `/auth/callback`; set it to `/g` for a registered callback such as
`https://slaprat.click/g`. This changes both the actual callback route and the
redirect URI sent to the provider. Configure the reverse proxy to forward that
exact path to the backend. The path must be a static root-relative path without
query/fragment, encoded characters, or a collision with application endpoints.
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
update the provider's registered redirect URI to
`<PUBLIC_BASE_URL><OIDC_CALLBACK_PATH>` (normally `/auth/callback`).

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
| `GET /ready` | Public readiness check; 200 only with a working DB query and notification connection, otherwise 503 |

`/ready` bounds database acquisition and its `SELECT 1` query to one second and
returns only a generic `not_ready` error on failure. Deployment probes should
allow more than one second per request. `/healthz` remains a process-status
endpoint and returns 200 even when its notification connection is disconnected.

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
does not insert the test catalog; the integration fixture supplies its 30 players.

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
entries contain each pick's recorded `point_cost`.
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

The stream sends only `invalidate` events; their data is ignored. Every event
starts an ordinary snapshot GET. There is no debounce or single-request lock:
reads may overlap, and clients discard responses older than the displayed
revision (or an older `server_now` at the same revision).

The server subscribes before checking initial access and sends an invalidation
when the stream opens, including after browser reconnection. One PostgreSQL
LISTEN connection per worker routes notification payloads as opaque topic keys.
The SQL trigger sends only the league ID as text. Each subscriber has one pending
wake-up slot; a LISTEN reconnection invalidates all active subscriptions.

The frontend retries failed snapshot reads after two seconds for network/server
errors. Stream errors also trigger an HTTP read, because native EventSource does
not expose HTTP authorization failures. Snapshot responses of 401 or 404 close
the subscription and clear the affected state. No automatic writes are retried.

Access and session validity are checked on HTTP requests and when opening a
stream, not periodically on existing streams. Idle keep-alive comments every
15 seconds only maintain the connection; they do not query the database or
trigger client reads. There are no SSE snapshots, event IDs, revision payloads,
or proactive sign-out/access-revoked messages.

The notification payload changed from JSON to a plain league ID. Rebuild an
existing disposable database (or update its `dl.notify_league` function) before
running the new listener against it.

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
uv run python -m pytest -q
uv run ruff check .
```

Integration tests are opt-in:

```sh
uv run python -m pytest --integration --pg-bin=/path/to/postgresql/bin -q
# Select only integration tests, or narrow them further with -k:
uv run python -m pytest --integration --pg-bin=/path/to/postgresql/bin -m integration -q
```

Omit `--pg-bin` when PostgreSQL is on PATH. No connection URL environment variables
are used: pytest owns provisioning, connections, and teardown. Missing prerequisites
fail an explicitly requested integration run instead of silently skipping it.
Ordinary `pytest` skips integration tests. Test collection never starts PostgreSQL.

The existing suite exercises in-process HTTP workflows against real PostgreSQL,
SQL privileges, identity registration, budgets, and catalog import. It is not a
browser end-to-end suite. OIDC provider exchanges are mocked in the unit tests.

Reference documentation: [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
[Authlib](https://docs.authlib.org/en/latest/client/starlette.html),
[psycopg async connections](https://www.psycopg.org/psycopg3/docs/advanced/async.html),
[PostgreSQL LISTEN](https://www.postgresql.org/docs/current/sql-listen.html),
[PostgreSQL SET ROLE](https://www.postgresql.org/docs/current/sql-set-role.html),
and [Uvicorn event loops](https://uvicorn.dev/concepts/event-loop/).
