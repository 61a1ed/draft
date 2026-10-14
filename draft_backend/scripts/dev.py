"""Run the API and frontend with a fresh, resumed, or external PostgreSQL database."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
from psycopg.conninfo import make_conninfo

if __package__:
    from . import scenarios, testdb
    from .bootstrap import PROJECT
    from .import_catalog import import_catalog, load_catalog
else:
    import scenarios
    import testdb
    from bootstrap import PROJECT
    from import_catalog import import_catalog, load_catalog

FRONTEND = PROJECT.parent / "draft_frontend"


def read_environment(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


class Processes:
    """Own process trees and drain both streams independently, including during startup."""

    def __init__(self, logs: Path):
        self.logs = logs
        logs.mkdir(parents=True, exist_ok=True)
        self.children = []
        self.readers = []
        self.lock = threading.Lock()

    def forward(self, stream, path, tag):
        with stream, path.open("a", encoding="utf-8") as log:
            for line in stream:
                log.write(line)
                log.flush()
                with self.lock:
                    sys.stderr.write(f"[{tag}] {line}")
                    sys.stderr.flush()

    def start(self, name, command, *, cwd=PROJECT, env=None):
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        self.children.append(child)
        for label, stream in (("out", child.stdout), ("err", child.stderr)):
            reader = threading.Thread(
                target=self.forward,
                args=(stream, self.logs / f"{name}.{label}.log", f"{name} {label}"),
                daemon=True,
            )
            reader.start()
            self.readers.append(reader)
        return child

    def close(self):
        failures = []
        for child in reversed(self.children):
            try:
                if os.name == "nt":
                    if child.poll() is None:
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            timeout=15,
                        )
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=15)
            except (OSError, subprocess.SubprocessError):
                failures.append(child.pid)
        for reader in self.readers:
            reader.join(timeout=5)
        if failures:
            raise RuntimeError(f"Could not stop owned processes: {failures}")

    def run_command(self, pg_bin, name, *args):
        env = os.environ.copy()
        env.pop("PGPASSWORD", None)
        child = self.start(name, [str(testdb.binary(pg_bin, name)), *args], env=env)
        result = child.wait(timeout=60)
        self.children.remove(child)
        if result:
            raise RuntimeError(f"{name} failed; inspect its log")


def wait_database(child, dsn, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Postgres exited before becoming ready; inspect its log")
        try:
            with psycopg.connect(dsn, connect_timeout=1):
                return
        except psycopg.OperationalError:
            time.sleep(0.2)
    raise RuntimeError("Postgres did not become ready; inspect its log")


def start_database(processes, pg_bin, root, port):
    # postgres itself refuses an already-running data directory; never adopt its process.
    child = processes.start(
        "postgres",
        [str(testdb.binary(pg_bin, "postgres")), "-D", str(root / "data"), "-p", str(port)],
    )
    admin = read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
    wait_database(child, make_conninfo(admin, dbname="postgres", port=port))


def seed(root, catalog):
    pokemon, images = catalog
    admin = read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
    import_catalog(admin, pokemon, images)
    with psycopg.connect(admin) as conn:
        conn.execute("SET LOCAL ROLE dl_owner")
        conn.execute(
            "DELETE FROM dl.draftable d WHERE d.dex IS NULL "
            "AND d.name ~ '^Player [0-9]{2}$' "
            "AND NOT EXISTS (SELECT FROM dl.pick p WHERE p.draftable_id = d.id)"
        )
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
    parser.add_argument(
        "--pg-bin", type=Path, help="PostgreSQL binaries (otherwise PATH or saved path)"
    )
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
        marker = None
        if args.data_dir:
            data = args.data_dir.resolve()
            root = data.parent
            marker = testdb.load_marker(root)
            if data != root / "data" or not (data / "PG_VERSION").is_file():
                raise RuntimeError("Expected an existing launcher-created data directory")
        found = shutil.which("postgres")
        pg_bin = args.pg_bin or (
            Path(marker["pg_bin"]) if marker else Path(found).parent if found else None
        )
        if pg_bin is None:
            raise RuntimeError("Provide --pg-bin or put PostgreSQL binaries on PATH")
        port = args.db_port if args.db_port is not None else marker["port"] if marker else 55440
        if port in (args.api_port, args.web_port):
            raise RuntimeError("Server ports must differ")
        if marker:
            if testdb.is_running(pg_bin, root / "data"):
                raise RuntimeError("This cluster is already running; use --database-url-env")
            values = read_environment(root / ".env")
            values["DATABASE_URL"] = make_conninfo(values["DATABASE_URL"], port=port)
        else:
            catalog = load_catalog(
                PROJECT.parent / "pokemon_catalog.csv", PROJECT.parent / "showdown"
            )
    runs = PROJECT / ".dev-runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))
    processes = Processes(run_dir / "logs")
    print(f"Logs: {processes.logs}", flush=True)
    try:
        if not args.database_url_env:
            if root is None:
                root = run_dir / "database"
                testdb.init(
                    root,
                    pg_bin,
                    port,
                    start_server=lambda binaries, path: start_database(
                        processes, binaries, path, port
                    ),
                    run_command=processes.run_command,
                )
                seed(root, catalog)
                values = read_environment(root / ".env")
            else:
                start_database(processes, pg_bin, root, port)
            print(
                f"Database: {root / 'data'}\nResume: python scripts/dev.py "
                f'--data-dir "{root / "data"}" --db-port {port}',
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
