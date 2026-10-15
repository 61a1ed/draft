"""Run the API and frontend with a fresh, resumed, or external PostgreSQL database."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

if __package__:
    from . import postgres, scenarios
    from .bootstrap import PROJECT
    from .import_catalog import import_catalog, load_catalog
    from .postgres import read_environment
    from .processes import Processes
else:
    import postgres
    import scenarios
    from bootstrap import PROJECT
    from import_catalog import import_catalog, load_catalog
    from postgres import read_environment
    from processes import Processes

FRONTEND = PROJECT.parent / "draft_frontend"


def seed(root, catalog):
    pokemon, images = catalog
    admin = read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
    with psycopg.connect(admin) as conn:
        conn.execute((PROJECT / "sql/test_users.sql").read_text(encoding="utf-8"))
    import_catalog(admin, pokemon, images)
    scenarios.apply(scenarios.prepare(root))
    print(f"Loaded {len(pokemon)} Pokémon and the SQL scenarios.", flush=True)


def child_environments(values, api_port, web_port, database_env=None):
    backend = os.environ.copy()
    backend.update(values)
    backend.update(
        HOST="127.0.0.1",
        PORT=str(api_port),
        PUBLIC_BASE_URL=f"http://127.0.0.1:{web_port}",
        PYTHONUNBUFFERED="1",
    )
    frontend = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "SESSION_SECRET",
        "OIDC_CLIENT_SECRET",
        database_env,
    ):
        if name:
            frontend.pop(name, None)
    frontend.update(
        API_TARGET=f"http://127.0.0.1:{api_port}",
        VITE_ENABLE_TEST_LOGIN=str(
            backend.get("TEST_LOGIN_ENABLED", "false").lower() in {"true", "1"}
        ).lower(),
    )
    for env in (backend, frontend):
        env.pop("FORCE_COLOR", None)
        env.update(NO_COLOR="1", TERM="dumb")
    return backend, frontend


def wait_ready(process, url, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("A development server exited before becoming ready; inspect its log")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Development server did not become ready at {url}; inspect its log")


def serve(processes, values, args):
    backend, frontend = child_environments(
        values, args.api_port, args.web_port, args.database_url_env
    )
    child = processes.start("backend", [sys.executable, "-m", "draft_backend"], env=backend)
    wait_ready(child, f"http://127.0.0.1:{args.api_port}/healthz")
    child = processes.start(
        "frontend",
        [
            shutil.which("node"),
            str(FRONTEND / "node_modules/vite/bin/vite.js"),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.web_port),
            "--strictPort",
            "--clearScreen",
            "false",
        ],
        cwd=FRONTEND,
        env=frontend,
    )
    wait_ready(child, f"http://127.0.0.1:{args.web_port}")
    print(
        f"Open http://127.0.0.1:{args.web_port}. Ctrl+C stops owned processes; files are kept.",
        flush=True,
    )
    while all(child.poll() is None for child in processes.children):
        time.sleep(0.5)
    raise RuntimeError("A development server stopped; inspect its log")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--data-dir", type=Path, help="Resume a launcher-created data directory")
    mode.add_argument(
        "--database-url-env", help="Environment variable with an existing database URL"
    )
    parser.add_argument("--pg-bin", type=Path, help="PostgreSQL binaries (otherwise PATH)")
    parser.add_argument(
        "--db-port", type=int, help="Local Postgres port (fresh: 55440; resume: saved)"
    )
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    args = parser.parse_args(argv)
    if args.database_url_env and (args.db_port is not None or args.pg_bin is not None):
        parser.error("--db-port and --pg-bin are only for local databases")
    ports = [args.api_port, args.web_port]
    if args.db_port is not None:
        ports.append(args.db_port)
    if any(not 1024 <= port <= 65535 for port in ports):
        parser.error("Ports must be in 1024..65535")
    if len(set(ports)) != len(ports):
        parser.error("Server ports must differ")
    return args


def run(args):
    if not shutil.which("node") or not (FRONTEND / "node_modules/vite/bin/vite.js").is_file():
        raise RuntimeError("Install Node.js and run npm ci in draft_frontend first")
    root = None
    catalog = None
    if args.database_url_env:
        dsn = os.environ.get(args.database_url_env)
        if not dsn:
            raise RuntimeError("The selected database URL environment variable is empty or missing")
        values = {"DATABASE_URL": dsn}
    else:
        pg_bin = postgres.find_binaries(args.pg_bin)
        if args.data_dir:
            data = args.data_dir.resolve()
            root = data.parent
            if data != root / "data" or not (data / "PG_VERSION").is_file():
                raise RuntimeError("Expected an existing launcher-created data directory")
            values = read_environment(root / ".env")
            saved_port = int(conninfo_to_dict(values["DATABASE_URL"])["port"])
            port = args.db_port if args.db_port is not None else saved_port
            values["DATABASE_URL"] = make_conninfo(values["DATABASE_URL"], port=port)
        else:
            port = args.db_port if args.db_port is not None else 55440
            catalog = load_catalog(
                PROJECT.parent / "pokemon_catalog.csv", PROJECT.parent / "showdown"
            )
        if port in (args.api_port, args.web_port):
            raise RuntimeError("Server ports must differ")
    runs = PROJECT / ".dev-runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))
    processes = Processes(run_dir / "logs")
    print(f"Logs: {processes.logs}", flush=True)
    try:
        if not args.database_url_env:
            if root is None:
                root = run_dir / "database"
                postgres.init(root, pg_bin, port, processes)
                seed(root, catalog)
                values = read_environment(root / ".env")
            else:
                postgres.start(processes, pg_bin, root, port)
            print(
                f"Database: {root / 'data'}\nResume: python scripts/dev.py "
                f'--data-dir "{root / "data"}" --pg-bin "{pg_bin}" --db-port {port}',
                flush=True,
            )
        serve(processes, values, args)
    finally:
        processes.close()


def main():
    args = parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nDevelopment processes stopped; database files and logs kept.")
    except (psycopg.Error, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = (
            f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})"
            if isinstance(exc, psycopg.Error)
            else str(exc)
        )
        print(f"Local development failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
