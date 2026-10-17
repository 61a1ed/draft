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

The current `sql/schema.sql` and `sql/grants.sql` are the fixed installation
baseline. Subsequent changes belong in `src/draft_backend/sql_migrations/`;
`0001_tournament.sql` adds tournament storage, rules, public projections, and
permissions. Startup applies these files through the separate
`MIGRATION_DATABASE_URL` connection as `dl_owner`, closes it, then opens the
ordinary `DATABASE_URL` pool. A failed migration prevents startup and readiness.
The local launcher supplies both connections. For manual or external launches,
set `MIGRATION_DATABASE_URL` to a connection allowed to `SET ROLE dl_owner` in
the same database. Production host setup supplies this connection automatically.

Migration files are ordered lexicographically by their case-sensitive filenames.
Use zero-padded names such as `0001_tournament.sql`, `0002_description.sql`, etc.
The whole filename is the history key; its numeric prefix is not parsed. Each file
and its record in `dl.schema_migrations` commit together; earlier successful
files remain applied if a later one fails. Do not edit or rename applied files.
Keep the baseline fixed so fresh installations and existing databases apply
the same migrations. The packaged migration directory describes the SQL rules.

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

Schema data is public. Data reads require no session and come directly from PostgreSQL.
Writes and the account notification stream require a session:

| Method/path | Result |
|---|---|
| `GET /api/users` | Array of `{id, display_name}`; no login identities |
| `GET /api/draftables` | Global catalog array |
| `GET /images/{hash}` / `HEAD /images/{hash}` | Public PNG image; no session required |
| `GET /api/leagues` | Array of all leagues |
| `GET /api/leagues/{id}` | One `dl.league_snapshot` row; actions personalized to the signed-in user |
| `GET /api/leagues/{id}/tournament` | Tournament, schedule, current evidence, official results, records, and withdrawals |
| `GET /api/matches/{id}` | Match context and complete report/decision history |
| `GET /api/teams/{id}/availability` | Availability header and timestamp ranges; empty state before the first edit |
| `GET /api/events` | Account SSE stream, including accounts with no leagues |
| `GET /healthz` | Process status; public |
| `GET /ready` | Public readiness check; 200 with a working pooled DB query, otherwise 503 |

`/ready` bounds database acquisition and its `SELECT 1` query to one second and
returns only a generic `not_ready` error on failure. Deployment probes should
allow more than one second per request. `/healthz` remains a process-status
endpoint. There is no worker-wide notification listener to probe; each browser
stream establishes its own listener and reconnects independently on failure.

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

## Tournaments

After a league's draft is complete, its commissioner can publish a round robin
followed by a two-team final. The migration installs this capability for existing
and fresh databases; it does not publish tournaments automatically. The HTTP API
exposes the operations below. The adjacent frontend provides a signed-in
tournament workspace; availability planning and signed-out spectator pages are deferred.
See [the tournament workflow](sql/tournament.md) for qualification, corrections,
withdrawals, and concurrency semantics.

Each operation uses `POST /api/rpc/<function>` and the existing login/CSRF boundary.
All argument names have the SQL `p_` prefix. Optional values below default to null
unless another default is stated. SQL owns authorization and competitive rules.

| Function | JSON arguments (all prefixed `p_`) | `result` |
|---|---|---|
| `publish_tournament` | `league`, `league_revision`, `first_suggested_date`, optional `season_best_of` (3), `final_best_of` (5) | null |
| `set_round_suggested_date` | `round`, `revision`, `suggested_start_date` (nullable) | null |
| `set_team_availability` | `team`, `revision` (nullable for first save), `time_zone`, `display_step_minutes`, `windows` | null |
| `copy_team_availability_week` | `team`, `revision`, `source`, `target` | null |
| `submit_result` | `match`, `expected_previous_submission_id` (nullable for first report), `outcome_kind`, optional `team_a_game_wins`, `team_b_game_wins`, `awarded_winner_side` | Submission ID |
| `record_result` | `match`, `revision`, `reason`, optional `selected_submission`, `outcome_kind`, `team_a_game_wins`, `team_b_game_wins`, `awarded_winner_side` | Decision ID |
| `correct_result` | `match`, `revision`, `reason`, `outcome_kind`, optional `team_a_game_wins`, `team_b_game_wins`, `awarded_winner_side` | Decision ID |
| `set_match_vod` | `match`, `revision`, `vod_url` (nullable) | null |
| `withdraw_team` | `league`, `revision`, `team`, `reason` | null |
| `create_tiebreak_round` | `stage`, `revision`, `teams`, `reason`, optional `suggested_start_date` | Round ID |
| `publish_final` | `league`, `revision`, `qualifiers`, optional `suggested_start_date` | Match ID |

Suggested dates and availability copy dates use `YYYY-MM-DD`. Suggested dates are
advisory week labels; they never open or close reporting. Availability `windows`
is an array of `{starts_at, ends_at}` objects with ISO timestamps including `Z`
or an explicit offset such as `-07:00`. An empty array clears availability.
The gateway binds these ranges as JSONB. Scores always use fixture A/B orientation;
a forfeit supplies `awarded_winner_side` (`a` or `b`) with no game counts.

Publication checks the league revision. Date hints, withdrawals, tiebreaks, and
final publication check the tournament revision. Commissioner results and VOD
edits check the match revision. Participant reports check only that participant's
previous submission ID; availability uses its own revision. Refresh the relevant
read after a write or a 409. Tournament changes reuse the account notification stream.

All tournament reads are public, including reports and decision history. Each
endpoint reads a consistent database snapshot in one statement. A known team
without saved availability has a null header and empty windows. A league without
a published tournament, or a missing league/match/team, returns 404 from its
corresponding tournament endpoint.

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

Every signed-in tab opens one `GET /api/events` stream, even with zero leagues.
Selecting a league does not reopen it. The authenticated account ID determines
the safely quoted PostgreSQL channel (`dl_user_42`, for example); the browser
cannot choose another account's channel. Each stream runs in an async handler
with one dedicated autocommit connection outside the query pool. It completes
`LISTEN` before sending an initial generic `invalidate`, forwards notifications
as the same event, and sends idle keep-alive comments every 15 seconds.

SQL determines recipients. League creation and changes notify the commissioner
and current managers. Team changes also notify former managers, including those
who just lost their last team. Notifications have empty payloads and are delivered
only after commit. The triggers cover direct SQL edits as well as the public SQL
functions used by HTTP. Direct administrative edits must still preserve draft
invariants and increment the league revision in the same transaction.

An invalidation refreshes the accessible league list and selected snapshot.
Refreshes are serialized; an event during a read schedules another pass. The
frontend preserves an accessible selection, otherwise selects an available
league or clears private league state. Newly accessible leagues produce an
in-app notice. Revision/server-time ordering and account/selection guards discard
stale responses. Transient read and stream-opening failures retry with bounded
backoff. Stream errors also prompt an HTTP read to detect expired sessions.
Mutations are never replayed automatically.

Disconnect, cancellation, fixed session expiry, and database failure close the
dedicated connection, which removes its subscription. A database failure ends
that stream; its replacement sends a fresh initial invalidation after `LISTEN`.
Notifications are transient wake-up signals, with no event IDs or durable replay.
Changes during an outage are recovered through these ordinary HTTP reads.

Each open tab consumes **one persistent PostgreSQL connection**, in addition to
normal query connections. `SSE_MAX_CONNECTIONS` limits streams per worker (default
30); excess stream requests receive 503 and retry. `DB_POOL_SIZE` still bounds
ordinary query connections (default 10). Budget all workers and overlapping
releases: `workers * (SSE_MAX_CONNECTIONS + DB_POOL_SIZE)`, plus administrative,
other-service, and PostgreSQL reserved connections. Lower the stream limit when
that total would consume the database's available connection budget. A single
worker's defaults allow 30 streams plus 10 query connections; two fully loaded
releases would need 80 before headroom. Neither provisioning nor startup raises
PostgreSQL connection limits. Use a direct or session-preserving PostgreSQL
connection, not a transaction-pooling endpoint, for `LISTEN`.

This notification refactor predates the migration history and is part of the
fixed bootstrap baseline. For disposable databases older than that baseline, upgrade
local databases by stopping the old development run and starting
`uv run python scripts/dev.py --pg-bin <binaries>` without `--data-dir`; this
provisions the new schema and preserves the old cluster for inspection. Resuming
such a pre-baseline cluster or deploying application code alone does not update
its notification triggers. Databases already on the fixed baseline receive
subsequent changes, including tournaments, through startup migrations.
For an existing deployment, provision a fresh database/cluster using the normal
bootstrap, import the catalog, and point the new release at it. This does not
transfer existing leagues or accounts; retain the old database if they are needed.
Do not run `schema.sql` over a populated schema or delete an existing database
as an implicit upgrade step.

The stream works over HTTP/1.1. Disable buffering for this route in any reverse
proxy; the response includes `X-Accel-Buffering: no`. Idle timeouts should exceed
the heartbeat interval. Cookies authenticate native EventSource automatically.

## Database roles

| Role | Purpose |
|---|---|
| `dl_owner` | NOLOGIN, non-superuser schema/function owner; never used for requests |
| `dl_api` | NOLOGIN, read views/tables plus public SQL functions; no table DML |
| `dl_auth` | NOLOGIN, upsert `app_user`; public schema reads |
| `dl_gateway` | Unprivileged LOGIN, NOINHERIT; can explicitly select API/auth roles |

People are `app_user` rows. All tables, views, and columns in `dl` are publicly
readable, including account identities and tournament history. New owner-created
tables and views inherit public reads. Commissioner/manager write permissions remain
in the schema. `request.jwt.claims` is populated transaction-locally from the verified
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
