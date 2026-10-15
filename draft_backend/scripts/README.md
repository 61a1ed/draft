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

Each invocation creates a fresh database under `.dev-runs/<unique-run>/database`,
installs the schema, imports the Pokémon catalog/images, and seeds Alice, Bob,
Carol and six SQL scenarios. Put PostgreSQL binaries on PATH or pass `--pg-bin`.
Open **http://127.0.0.1:5173**, sign in with a test account, and select a scenario.

```powershell
# Resume the data directory printed by an earlier run (no schema or seed changes)
uv run python scripts/dev.py --data-dir '.dev-runs/<run>/database/data'
# Connect to an already-prepared database using a URL stored in an environment variable
uv run python scripts/dev.py --database-url-env DEV_DATABASE_URL
```

Network mode never initializes, seeds, migrates, starts, or stops Postgres. Supply
normal backend authentication settings in the environment (SESSION_SECRET and
OIDC settings; for local HTTP use APP_ENV=development and COOKIE_SECURE=false).
It does not automatically enable test login. Normal application actions can still
write to the connected database; this is not a read-only application mode.

Resume uses the saved connection settings beside `data/`. PostgreSQL refuses a
data directory that is already running; the launcher never adopts that process.
Supply `--pg-bin` on resume too, unless PostgreSQL is on PATH.
`--db-port` defaults to 55440 for fresh databases and the saved port for resumed
ones; an override affects this invocation only. `--api-port` and `--web-port`
default to 8000 and 5173. Network mode rejects `--db-port` and `--pg-bin`.
There is no `--root`, `--prepare-only`, seed flag, or migration support.

Ctrl+C force-stops owned process trees, including foreground Postgres. All data
and logs remain available; Postgres performs crash recovery when resumed. The
launcher prints a resume command and creates missing `.dev-runs` parents, so old
runs can be removed manually while stopped. Each invocation has separate logs,
including when resuming. Both output streams are forwarded live to stderr with
`[postgres err]`, `[backend out]`, etc., and retained in `<run>/logs/<name>.<stream>.log`.
Vite color and screen clearing are disabled.

Edit a file in `sql/scenarios/`, apply it, and click the toolbar's **Refresh all data**:

Reseed while you are not making changes through the frontend, and run one loader
at a time. The local fixture tools do not coordinate concurrent writers. The
frontend and backend can stay running.

```powershell
uv run python scripts/scenarios.py list
uv run python scripts/scenarios.py --root .dev-runs/<run>/database apply live
uv run python scripts/scenarios.py --root .dev-runs/<run>/database apply all
uv run python scripts/scenarios.py --root .dev-runs/<run>/database apply --file path/to/my-state.sql
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
no HTTP replay. The runner loads credentials from the selected cluster, checks the
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
Create a fresh cluster when the schema itself changes; routine
SQL fixture edits only need `scenarios.py apply`.

## Automated integration runs

```powershell
uv run python -m pytest --integration --pg-bin='C:\postgresql\bin' -m integration -q
```

Pytest creates and owns a fresh cluster on an available loopback port. Both
connection URLs come from that cluster; no existing database URL is accepted.
`--integration` without `-m integration` runs the ordinary tests as well.
Omit `--pg-bin` if PostgreSQL is on PATH. No frontend dependencies are needed.

The cluster, credentials, and logs stay under `.test-runs/integration-*`, including
on failure. Teardown stops owned processes but never deletes the directory. The
summary prints its location. Resume with `dev.py --data-dir <run>/database/data
--pg-bin <binaries>` to inspect it through the app, or use PostgreSQL tools.
Delete old directories manually when they are no longer useful.

`scripts/postgres.py` is the single PostgreSQL lifecycle implementation used by
pytest and `dev.py`; `scripts/processes.py` owns foreground processes and output.
There is no `testdb.py` management CLI and no JSON filesystem ownership marker.
Connection settings remain in `.env` and `admin.env` beside `data/`.

Initialization installs schema and roles without sample data. Development then
loads test users, Pokémon, and SQL scenarios. Tests load test users and the small
synthetic catalog in `tests/catalog.sql`; they do not apply or test SQL scenarios.
`sql/test_users.sql` installs `dl_test.marker`, which enables test login alongside
the application test-mode settings. Production bootstrap never installs it.

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
to exercise the new schema. Run `pytest --integration` to include the import integration test; its connections
come from the same runner-owned cluster as the other integration tests.
