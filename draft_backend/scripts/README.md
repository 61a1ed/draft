Database scripts run from the project root with `uv run python scripts/...`.
PostgreSQL 16 or newer is required; PostgreSQL 18 is a suitable choice.
Install/unzip PostgreSQL binaries separately. These scripts do not install an OS
service and do not require Docker. On POSIX, run them as an ordinary OS user.

## Frontend exploration with SQL

Install dependencies once (`uv sync --locked` in `draft_backend`, `npm ci` in
`draft_frontend`), then start the complete workspace from `draft_backend`:

```powershell
uv run python scripts/dev.py --pg-bin 'C:\postgresql\bin'
```

Replace the binary path with your PostgreSQL installation. First run creates
`.testdb-dev` on loopback port 55440, imports `../pokemon_catalog.csv` and the PNG
files under `../showdown`, and loads six SQL fixtures. No download is needed.
Open **http://127.0.0.1:5173**, click **Sign in**, then **Use test account**.
Alice is commissioner; Bob and Carol are ordinary managers. The league selector
switches scenarios using the existing API and database access rules.

Later runs are simply:

```powershell
uv run python scripts/dev.py
```

The launcher preserves all draft changes and does not reimport or reseed a
prepared database. It sets the frontend proxy, backend browser origin, and local
test-login flags automatically. Backend failures remain visible in the frontend. Ctrl+C stops the two web servers;
PostgreSQL remains running. Logs are `.testdb-dev/api.log` and `frontend.log`.
Use `--prepare-only` to set up/start just PostgreSQL, or `--root`, `--db-port`,
`--api-port`, and `--web-port` for an independent workspace. The database port is
chosen only at initialization; later runs use its saved marker.

Edit a file in `sql/scenarios/`, apply it, and click the toolbar's **Refresh all data**:

Reseed while you are not making changes through the frontend, and run one loader
at a time. The local fixture tools do not coordinate concurrent writers. The
frontend and backend can stay running.

```powershell
uv run python scripts/scenarios.py list
uv run python scripts/scenarios.py --root .testdb-dev apply live
uv run python scripts/scenarios.py --root .testdb-dev apply all
uv run python scripts/scenarios.py --root .testdb-dev apply --file path/to/my-state.sql
```

| Fixture | What to explore |
|---|---|
| `setup` | Three teams, incomplete draft order; save the order in League settings |
| `ready` | Ordered teams; start the draft as Alice |
| `live` | Four active picks, reversed second round; Bob picks next |
| `budget` | Bob has zero points; switch to Alice to waive a pick's price |
| `undo` | Undone history and a replacement; active rosters and spending exclude undo |
| `completed` | Finished two-round draft; Alice can undo to reopen it |

The files contain ordinary SQL inserts/updates, with no Python fixture model and
no HTTP replay. The runner loads credentials from the marked cluster, checks the
database test marker, selects `dl_owner`, creates the scenario registry if needed,
and executes all selected files in one transaction. Fixture files, including
custom `--file` inputs, must not contain
transaction commands such as `BEGIN`, `COMMIT`, or `ROLLBACK`. PL/pgSQL blocks
inside `DO $$ ... $$` are fine. This is a convention for trusted SQL, not something
the runner parses or enforces. Load files through `scenarios.py`; their shared
setup belongs to the runner.

Built-in reloads replace only leagues registered under their slug in
`dl_test.scenario`, even if you renamed those leagues in the UI. Other leagues,
users, catalog rows, assets, and credentials survive. League/team/pick identities
continue increasing: old tabs or in-flight requests cannot accidentally operate on
a new fixture with reused IDs and lower revisions. Refresh selects a remaining
league; select the reloaded scenario again from the list. A subsequent snapshot request for the old league reports access removed. SQL failure rolls the replacement back.

To create a new repeatable fixture, copy a built-in file and change every occurrence
of its registry slug plus its name/data. Apply the copy with `--file`; no application
code change is needed. Direct inserts bypass draft RPC rules, so SQL fixtures must
maintain a sensible contiguous pick order, valid snake rosters, and recorded prices.
The built-ins deliberately reuse the existing SQL snake-position function.

For an in-place edit to a live league, increment its revision in the same
transaction as the data change. Updating the league invokes the existing PostgreSQL
notification trigger, and the frontend refetches its snapshot. Example file for
`scenarios.py apply --file`:

```sql
UPDATE dl.league
SET name = 'My updated draft', revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'live');
```

Catalog edits and new/deleted league lists have no global event: click **Refresh all
data** after those changes. Timers are anchored when fixtures load and keep running;
rerun a fixture to restart its timer. Timer expiry never auto-picks or skips.

### Choosing isolation

| Approach | Fit for this application |
|---|---|
| Several scenario leagues in one disposable DB | Default. Instant switch through the existing league selector; shared catalog/users; no runtime routing |
| Delete/reseed all application rows | Useful for a fully empty database, but disrupts every scenario and login if users are removed; unnecessary for routine fixture resets |
| One schema per scenario | Poor fit: SQL functions, views, gateway queries, grants and startup checks explicitly use `dl`; changing `search_path` alone does not switch them |
| Separate databases or clusters | Use for different catalogs, schema versions, or simultaneous isolated experiments; switch server connections/restart, or run separate server ports |

Current bootstrap creates fixed cluster-wide service roles and refuses existing
ones, so a separate **cluster** is the supported fully isolated option today.
Sharing one cluster across multiple databases would require a provisioning change
to reuse known roles. The backend needs no scenario endpoints or routing state.
Destroy/reinitialize the dedicated cluster when the schema itself changes; routine
SQL fixture edits only need `scenarios.py apply`.

Create, start, and seed a dedicated test cluster:

```sh
uv run python scripts/testdb.py --pg-bin /path/to/postgresql/bin --root .testdb init
uv run --env-file .testdb/.env draft-server
```

The equivalent first command with the Windows binary distribution is:

```powershell
uv run python scripts/testdb.py --pg-bin 'C:\postgresql\bin' --root .testdb init
```

Use `--port 55433` when initializing another test cluster. Each command accepts
`--root`; after initialization the binary location is read from the marker:

```sh
uv run python scripts/testdb.py --root .testdb status
uv run python scripts/testdb.py --root .testdb stop
uv run python scripts/testdb.py --root .testdb start
uv run python scripts/testdb.py --root .testdb destroy
```

`destroy` stops and permanently removes the entire marked directory. `init`
refuses nonempty directories, and management commands refuse unmarked directories
or a marker belonging to a different path. To reset, destroy and initialize again.
The cluster listens only on `127.0.0.1` and requires SCRAM passwords. The root
directory contains generated application credentials in `.env`, administrator
credentials in `admin.env`, and server output in `postgres.log`. Keep these files
private; POSIX directories/files are created with restrictive permissions. On
Windows, place the root in a directory whose ACL limits access to your account.

The fixtures contain three users (`alice`, `bob`, `carol`, with issuer
`urn:draft:test` and subjects `alice`, `bob`, `carol`) and 30 catalog players. No
league is pre-created. `dl_test.marker` is installed only by the test seed SQL.
Test login requires this marker as well as the application test-mode settings.

For a non-test installation, first create a new empty database using your normal
PostgreSQL administrator tools. Set `ADMIN_DATABASE_URL` to that database's
superuser connection string and `DL_GATEWAY_PASSWORD` to a strong application
password, then run:

```sh
uv run python scripts/bootstrap.py
```

This is a one-time bootstrap, not a migration runner. It refuses pre-existing
`dl_owner`, `dl_api`, `dl_auth`, or `dl_gateway` roles because roles are cluster-wide
and silently reusing unknown accounts would be unsafe. Use a dedicated cluster
or explicitly provision equivalent roles/grants yourself when integrating with
an existing managed PostgreSQL installation. The whole installation is one
transaction owned by bootstrap; `sql/schema.sql` contains no transaction commands.
It executes schema DDL as the non-superuser `dl_owner`, installs explicit grants,
and revokes the temporary
database CREATE privilege afterward. It does not install test fixtures or a
test marker.

The backend's `DATABASE_URL` must authenticate as `dl_gateway`, never the
administrator or schema owner. `dl_gateway` has no inherited application table
privileges and can explicitly select only `dl_api` or `dl_auth`. Authentication
uses `dl_auth`; application requests use `dl_api`. Neither can become `dl_owner`.
App users are rows in `dl.app_user`, not separate PostgreSQL login roles.

## Import the Pokémon catalog

Export the approved source once, or supply a CSV with exactly these columns:
`dex,species,form,points,image_path`. Paths are relative to `--image-root`, must
stay beneath that directory, and must refer to local PNG files. Blank form is
valid. URLs, duplicate dex/form keys, missing files, and invalid points are rejected.

```sh
uv run python scripts/export_catalog.py ../bz.db ../pokemon_catalog.csv
uv run python scripts/import_catalog.py ../pokemon_catalog.csv --image-root ../showdown --dry-run
```

To populate a bootstrapped destination, set `ADMIN_DATABASE_URL` to the local or
remote database connection, with permission to `SET ROLE dl_owner`, then omit
`--dry-run`. Use the administrative connection, not the runtime gateway login.
Credentials are read from the environment and not printed in import errors.

```sh
uv run python scripts/import_catalog.py ../pokemon_catalog.csv --image-root ../showdown
```

The importer sends local image bytes to PostgreSQL. The destination needs no
access to your files and no Showdown connection. Reimports upsert by dex/form
without deleting omitted entries, preserving existing IDs. All changes commit
together. `--dry-run` performs file/CSV validation without connecting to a database.

The schema is installed directly on a fresh database; no migration runner is
provided. For disposable development databases, initialize a fresh test cluster
to exercise the new schema. To run import integration tests as well as the normal
suite, supply `TEST_ADMIN_DATABASE_URL` and `TEST_DATABASE_URL` for the same marked
test database. Never point test variables at production.
