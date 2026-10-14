"""Manage a disposable loopback-only PostgreSQL cluster, without an OS service."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

if __package__:
    from .bootstrap import PROJECT, bootstrap
else:
    from bootstrap import PROJECT, bootstrap

MARKER = ".draft-backend-testdb.json"
MAGIC = "draft-backend-test-cluster-v1"
DATABASE = "draft_test"


def private_write(path: Path, text: str) -> None:
    """Exclusive creation, and owner-only permissions on POSIX."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def binary(pg_bin: Path, name: str) -> Path:
    path = pg_bin / (name + (".exe" if os.name == "nt" else ""))
    if not path.is_file():
        raise RuntimeError(f"PostgreSQL binary not found: {path}")
    return path


def run(pg_bin: Path, name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Argument lists avoid shell quoting problems, including Windows paths with spaces.
    # Credentials are supplied by files/connections, never command-line arguments.
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    return subprocess.run(
        [str(binary(pg_bin, name)), *args],
        check=check,
        env=env,
        timeout=60,
    )


def load_marker(root: Path) -> dict:
    if root.is_symlink():
        raise RuntimeError("Refusing a symbolic-link cluster root")
    resolved = root.resolve()
    marker_path = resolved / MARKER
    if marker_path.is_symlink():
        raise RuntimeError("Refusing a symbolic-link cluster marker")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Not a marked test cluster: {resolved}") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("format") != MAGIC
        or marker.get("root") != str(resolved)
        or marker.get("data") != str(resolved / "data")
        or marker.get("database") != DATABASE
        or not isinstance(marker.get("port"), int)
        or not isinstance(marker.get("pg_bin"), str)
        or (resolved / "data").is_symlink()
    ):
        raise RuntimeError("Test cluster marker does not match this root")
    return marker


def is_running(pg_bin: Path, data: Path) -> bool:
    if not (data / "PG_VERSION").exists():
        return False
    result = run(pg_bin, "pg_ctl", "status", "-D", str(data), check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 3:
        return False
    raise RuntimeError(f"pg_ctl status failed with exit status {result.returncode}")


def start(pg_bin: Path, root: Path) -> None:
    run(
        pg_bin,
        "pg_ctl",
        "start",
        "-D",
        str(root / "data"),
        "-l",
        str(root / "postgres.log"),
        "-w",
        "-t",
        "30",
    )


def stop(pg_bin: Path, root: Path) -> None:
    run(pg_bin, "pg_ctl", "stop", "-D", str(root / "data"), "-m", "fast", "-w", "-t", "30")


def connection_url(user: str, password: str, port: int, database: str) -> str:
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return f"postgresql://{credentials}@127.0.0.1:{port}/{database}"


def init(root: Path, pg_bin: Path, port: int) -> None:
    if root.is_symlink():
        raise RuntimeError("Refusing a symbolic-link cluster root")
    root = root.resolve()
    pg_bin = pg_bin.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise RuntimeError("init requires a nonexistent or empty root directory")
    if os.name != "nt" and os.geteuid() == 0:
        raise RuntimeError(
            "PostgreSQL cannot run as root; invoke this script as an ordinary OS user"
        )
    for name in ("initdb", "pg_ctl", "postgres"):
        binary(pg_bin, name)
    run(pg_bin, "initdb", "--version")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    data = root / "data"
    private_write(
        root / MARKER,
        json.dumps(
            {
                "format": MAGIC,
                "root": str(root),
                "data": str(data),
                "pg_bin": str(pg_bin),
                "port": port,
                "database": DATABASE,
            },
            indent=2,
        )
        + "\n",
    )
    admin_password = secrets.token_urlsafe(32)
    gateway_password = secrets.token_urlsafe(32)
    password_file = root / ".initdb-password"
    private_write(password_file, admin_password + "\n")
    try:
        run(
            pg_bin,
            "initdb",
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth-local=scram-sha-256",
            "--auth-host=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
            "--pwfile",
            str(password_file),
        )
    finally:
        password_file.unlink(missing_ok=True)
    with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write(
            "\n# Managed by scripts/testdb.py; local disposable testing only.\n"
            "listen_addresses = '127.0.0.1'\n"
            f"port = {port}\n"
            "unix_socket_directories = ''\n"
            "password_encryption = 'scram-sha-256'\n"
        )
    # Replace initdb defaults, so a machine's address/hostname settings cannot
    # broaden connectivity. Other hosts and replication connections are rejected.
    (data / "pg_hba.conf").write_text("host all all 127.0.0.1/32 scram-sha-256\n", encoding="utf-8")
    admin_url = connection_url("postgres", admin_password, port, DATABASE)
    private_write(root / "admin.env", f"ADMIN_DATABASE_URL={admin_url}\n")
    gateway_url = connection_url("dl_gateway", gateway_password, port, DATABASE)
    private_write(
        root / ".env",
        "\n".join(
            [
                f"DATABASE_URL={gateway_url}",
                "APP_ENV=test",
                "TEST_LOGIN_ENABLED=true",
                f"SESSION_SECRET={secrets.token_urlsafe(48)}",
                "PUBLIC_BASE_URL=http://127.0.0.1:8000",
                "COOKIE_SECURE=false",
                "",
            ]
        ),
    )
    started = False
    try:
        start(pg_bin, root)
        started = True
        maintenance_url = connection_url("postgres", admin_password, port, "postgres")
        with psycopg.connect(maintenance_url, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
        bootstrap(admin_url, gateway_password)
        with psycopg.connect(admin_url, connect_timeout=10) as conn:
            conn.execute(
                (PROJECT / "sql" / "test_seed.sql").read_text(encoding="utf-8"), prepare=False
            )
    except BaseException:
        if started:
            try:
                stop(pg_bin, root)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                print(
                    "Could not stop cluster after initialization failure; inspect its log.",
                    file=sys.stderr,
                )
        raise
    print(f"Test database is running on 127.0.0.1:{port}.")
    print(f"Application environment: {root / '.env'}")
    print("Fixture users: alice, bob, carol. Catalog: 30 players.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Dedicated disposable cluster directory"
    )
    parser.add_argument(
        "--pg-bin", type=Path, help="Directory containing initdb and pg_ctl; required for init"
    )
    parser.add_argument(
        "--port", type=int, default=55432, help="Loopback port for init (default: 55432)"
    )
    parser.add_argument("command", choices=("init", "start", "stop", "status", "destroy"))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be in 1024..65535")
    if args.command == "init" and not args.pg_bin:
        parser.error("init requires --pg-bin")
    try:
        if args.command == "init":
            init(args.root, args.pg_bin, args.port)
            return
        marker = load_marker(args.root)
        root = args.root.resolve()
        pg_bin = (args.pg_bin or Path(marker["pg_bin"])).resolve()
        running = is_running(pg_bin, root / "data")
        if args.command == "status":
            print("running" if running else "stopped")
        elif args.command == "start":
            if running:
                print("Already running.")
            else:
                start(pg_bin, root)
        elif args.command == "stop":
            if running:
                stop(pg_bin, root)
            else:
                print("Already stopped.")
        elif args.command == "destroy":
            if running:
                stop(pg_bin, root)
            shutil.rmtree(root)
            print("Removed marked test cluster and its generated credentials.")
    except (psycopg.Error, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Never echo connection strings or SQL that may contain generated secrets.
        detail = (
            exc
            if not isinstance(exc, psycopg.Error)
            else (f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})")
        )
        print(f"Test database command failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
