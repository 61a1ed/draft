"""Shared local PostgreSQL lifecycle. Callers own processes; database files are retained."""

import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

if __package__:
    from .bootstrap import bootstrap
else:
    from bootstrap import bootstrap

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


def find_binaries(path=None):
    found = shutil.which("postgres")
    if path is None and found is None:
        raise RuntimeError("Provide --pg-bin or put PostgreSQL binaries on PATH")
    return Path(path).resolve() if path else Path(found).resolve().parent


def read_environment(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def connection_url(user: str, password: str, port: int, database: str) -> str:
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return f"postgresql://{credentials}@127.0.0.1:{port}/{database}"


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


def start(processes, pg_bin, root, port):
    status = processes.run_command(
        "pg_ctl", [str(binary(pg_bin, "pg_ctl")), "status", "-D", str(root / "data")], check=False
    )
    if status == 0:
        raise RuntimeError("This cluster is already running; use --database-url-env")
    if status != 3:
        raise RuntimeError("Could not determine PostgreSQL status; inspect its log")
    # PostgreSQL also guards against another process starting between status and launch.
    child = processes.start(
        "postgres",
        [str(binary(pg_bin, "postgres")), "-D", str(root / "data"), "-p", str(port)],
    )
    admin = read_environment(root / "admin.env")["ADMIN_DATABASE_URL"]
    wait_database(child, make_conninfo(admin, dbname="postgres", port=port))


def init(root: Path, pg_bin: Path, port: int, processes) -> None:
    def execute(pg_bin, name, *args):
        env = os.environ.copy()
        env.pop("PGPASSWORD", None)
        processes.run_command(name, [str(binary(pg_bin, name)), *args], env=env)

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
    execute(pg_bin, "initdb", "--version")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    data = root / "data"
    admin_password = secrets.token_urlsafe(32)
    gateway_password = secrets.token_urlsafe(32)
    password_file = root / ".initdb-password"
    private_write(password_file, admin_password + "\n")
    try:
        execute(
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
            "\n# Managed by scripts/postgres.py; local development and testing.\n"
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
    start(processes, pg_bin, root, port)
    maintenance_url = connection_url("postgres", admin_password, port, "postgres")
    with psycopg.connect(maintenance_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    bootstrap(admin_url, gateway_password)
