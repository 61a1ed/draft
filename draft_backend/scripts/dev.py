"""Prepare a local scenario database and run the real API and frontend together."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg

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
READY = ".exploration-ready"


def read_environment(path: Path) -> dict[str, str]:
    """Read only the simple KEY=value files emitted by testdb.py; never print values."""
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def prepare(root: Path, pg_bin: Path | None, port: int) -> None:
    fresh = not (root / testdb.MARKER).exists()
    if fresh:
        if pg_bin is None:
            found = shutil.which("pg_ctl")
            pg_bin = Path(found).parent if found else None
        if pg_bin is None:
            raise RuntimeError("First run requires --pg-bin pointing to PostgreSQL binaries")
        # Validate the catalog before provisioning anything.
        load_catalog(PROJECT.parent / "pokemon_catalog.csv", PROJECT.parent / "showdown")
        testdb.init(root, pg_bin, port)
    marker = testdb.load_marker(root)
    binaries = Path(marker["pg_bin"])
    if not testdb.is_running(binaries, root / "data"):
        testdb.start(binaries, root)
    if not (root / READY).exists():
        # Initialize a root once for exploration. A failed import/seed can be
        # retried; each operation is transactional.
        database = scenarios.prepare(root)
        pokemon, images = load_catalog(
            PROJECT.parent / "pokemon_catalog.csv", PROJECT.parent / "showdown"
        )
        admin_url = read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
        import_catalog(admin_url, pokemon, images)
        with psycopg.connect(admin_url) as conn:
            conn.execute("SET LOCAL ROLE dl_owner")
            # Remove only unused generic test fixtures; preserve every real entry
            # and anything already referenced by a pick.
            conn.execute(
                "DELETE FROM dl.draftable d WHERE d.dex IS NULL "
                "AND d.name ~ '^Player [0-9]{2}$' "
                "AND NOT EXISTS (SELECT FROM dl.pick p WHERE p.draftable_id = d.id)"
            )
        scenarios.apply(database)
        testdb.private_write(root / READY, "Catalog and SQL exploration scenarios installed.\n")
        print(f"Loaded {len(pokemon)} Pokémon and the SQL scenarios.", flush=True)


def child_environments(root: Path, api_port: int, web_port: int) -> tuple[dict, dict]:
    backend = os.environ.copy()
    backend.update(read_environment(root / ".env"))
    backend.update(
        HOST="127.0.0.1",
        PORT=str(api_port),
        PUBLIC_BASE_URL=f"http://127.0.0.1:{web_port}",
        PYTHONUNBUFFERED="1",
    )
    frontend = os.environ.copy()
    # Do not pass database credentials to the frontend process.
    for name in ("DATABASE_URL", "ADMIN_DATABASE_URL", "SESSION_SECRET"):
        frontend.pop(name, None)
    frontend.update(
        API_TARGET=f"http://127.0.0.1:{api_port}",
        VITE_ENABLE_TEST_LOGIN="true",
    )
    return backend, frontend


def available_port(port: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"Port {port} is in use; stop that server or choose another port"
            ) from exc


def wait_ready(process: subprocess.Popen, url: str, timeout: float = 30) -> None:
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


def serve(root: Path, api_port: int, web_port: int) -> None:
    node = shutil.which("node")
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite.is_file():
        raise RuntimeError("Install Node.js and run npm ci in draft_frontend first")
    if api_port == web_port:
        raise RuntimeError("API and frontend ports must differ")
    available_port(api_port)
    available_port(web_port)
    backend_env, frontend_env = child_environments(root, api_port, web_port)
    children = []
    logs = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        for name, command, cwd, env, url in (
            (
                "api",
                [sys.executable, "-m", "draft_backend"],
                PROJECT,
                backend_env,
                f"http://127.0.0.1:{api_port}/healthz",
            ),
            (
                "frontend",
                [node, str(vite), "--host", "127.0.0.1", "--port", str(web_port)],
                FRONTEND,
                frontend_env,
                f"http://127.0.0.1:{web_port}",
            ),
        ):
            log = (root / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            children.append(child)
            wait_ready(child, url)
        print(
            f"\nOpen http://127.0.0.1:{web_port} and sign in as Alice, Bob, or Carol.", flush=True
        )
        print("Choose a scenario in the league selector. After SQL, click Refresh all data.")
        print(f"Server logs: {root / 'api.log'} and {root / 'frontend.log'}")
        print(
            "Ctrl+C stops both servers. The database and your draft changes are kept.", flush=True
        )
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise RuntimeError("A development server stopped; inspect its log")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / ".testdb-dev")
    parser.add_argument(
        "--pg-bin", type=Path, help="Required on first run unless pg_ctl is on PATH"
    )
    parser.add_argument("--db-port", type=int, default=55440)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument(
        "--prepare-only", action="store_true", help="Prepare/start the database only"
    )
    args = parser.parse_args()
    if any(not 1024 <= p <= 65535 for p in (args.db_port, args.api_port, args.web_port)):
        parser.error("Ports must be in 1024..65535")
    root = args.root.resolve()
    try:
        prepare(root, args.pg_bin, args.db_port)
        if not args.prepare_only:
            serve(root, args.api_port, args.web_port)
    except KeyboardInterrupt:
        print("\nDevelopment servers stopped; database kept.")
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
