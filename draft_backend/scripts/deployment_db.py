"""Initialize and import a deployed database over client-certificate TLS.

The deployment controller supplies the six PG connection environment variables.
Certificates use verify-ca: the CA is checked without matching a server hostname.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import psycopg

if __package__:
    from .bootstrap import ROLES, initialize_connection
    from .import_catalog import import_catalog_connection, load_catalog, lock_catalog
else:
    from bootstrap import ROLES, initialize_connection
    from import_catalog import import_catalog_connection, load_catalog, lock_catalog

DATABASE = "draft"
INIT_LOCK = 0x444C494E49540001
ENVIRONMENT = ("PGHOST", "PGPORT", "PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY")
MARKER_VERSION = 1


def connection_options(environment: dict[str, str]) -> dict[str, str]:
    """Use only explicit transport settings, never service files or password files."""
    if any(not environment.get(key) for key in ENVIRONMENT):
        raise ValueError("Set " + ", ".join(ENVIRONMENT) + " explicitly")
    if environment["PGSSLMODE"] != "verify-ca":
        raise ValueError("PGSSLMODE must be verify-ca")
    host = environment["PGHOST"]
    if any(character in host for character in "/\\,\x00\r\n") or host.startswith("@"):
        raise ValueError("PGHOST must be one TCP host or IP address")
    try:
        port = int(environment["PGPORT"])
    except ValueError:
        raise ValueError("PGPORT must be a valid TCP port") from None
    if not 1 <= port <= 65535:
        raise ValueError("PGPORT must be a valid TCP port")
    for key in ("PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"):
        if not Path(environment[key]).is_file():
            raise ValueError(f"{key} must name an existing file")
    return {
        "host": host,
        "port": str(port),
        "sslmode": "verify-ca",
        "sslrootcert": str(Path(environment["PGSSLROOTCERT"]).resolve()),
        "sslcert": str(Path(environment["PGSSLCERT"]).resolve()),
        "sslkey": str(Path(environment["PGSSLKEY"]).resolve()),
        "sslcertmode": "require",
        "gssencmode": "disable",
        "password": "",
        "passfile": os.devnull,
        "connect_timeout": "10",
        "application_name": "draft-deployment",
        "options": "-c search_path=pg_catalog -c statement_timeout=60000",
    }


def validate_identity(name: str, revision: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise ValueError("Deployment name must contain lowercase letters, digits, or hyphens")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
        raise ValueError("Revision must be a full lowercase Git commit ID")


def check_connection(conn: psycopg.Connection, user: str) -> None:
    if conn.info.server_version < 160000:
        raise RuntimeError("PostgreSQL 16 or newer is required")
    row = conn.execute(
        "SELECT current_user, ssl, client_dn FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
    ).fetchone()
    if not row or row[0] != user or not row[1] or not row[2]:
        raise RuntimeError("Expected an encrypted connection with the requested client identity")


def check_marker(conn: psycopg.Connection, name: str, revision: str) -> None:
    marker = conn.execute(
        "SELECT c.oid, pg_get_userbyid(c.relowner), pg_get_userbyid(n.nspowner) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'dl_deployment' AND c.relname = 'completion' AND c.relkind = 'r'"
    ).fetchone()
    if not marker or marker[1:] != ("postgres", "postgres"):
        raise RuntimeError("Database is unmarked or partially initialized; refusing to modify it")
    rows = conn.execute(
        "SELECT name, revision, marker_version FROM dl_deployment.completion"
    ).fetchall()
    if (len(rows) != 1 or rows[0][0] != name or rows[0][2] != MARKER_VERSION
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", rows[0][1])):
        raise RuntimeError("Database completion marker does not match this deployment")


def initialize(options: dict[str, str], name: str, revision: str, password: str) -> bool:
    """Return true for a new installation; failed installations remain visibly incomplete."""
    validate_identity(name, revision)
    if not password or "\x00" in password:
        raise ValueError("A nonempty DL_GATEWAY_PASSWORD without NUL is required")
    # CREATE DATABASE cannot run in a transaction. A maintenance-database session
    # lock serializes that step and remains held until the marker is committed.
    with psycopg.connect(**options, dbname="postgres", user="postgres", autocommit=True) as admin:
        check_connection(admin, "postgres")
        if not admin.execute("SELECT pg_try_advisory_lock(%s)", (INIT_LOCK,)).fetchone()[0]:
            raise RuntimeError("Another database initialization is running")
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
        ).fetchone()
        if not exists:
            reserved = admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(ROLES) + ["dl_importer"],),
            ).fetchall()
            if reserved:
                raise RuntimeError(
                    "Reserved application roles already exist; refusing to reuse them"
                )
            admin.execute("CREATE DATABASE draft TEMPLATE template0 ENCODING 'UTF8'")
        with psycopg.connect(**options, dbname=DATABASE, user="postgres") as conn:
            check_connection(conn, "postgres")
            lock_catalog(conn)
            if exists:
                check_marker(conn, name, revision)
                return False
            initialize_connection(conn, password)
            conn.execute(
                "CREATE ROLE dl_importer LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL"
            )
            conn.execute("GRANT CONNECT ON DATABASE draft TO dl_importer")
            conn.execute("GRANT dl_owner TO dl_importer WITH SET TRUE")
            conn.execute("GRANT dl_owner TO dl_importer WITH INHERIT FALSE")
            # Metadata is administrator-owned so SET ROLE dl_owner cannot forge
            # successful setup, change revision provenance, or authorize reuse.
            conn.execute("CREATE SCHEMA dl_deployment AUTHORIZATION postgres")
            conn.execute("REVOKE ALL ON SCHEMA dl_deployment FROM PUBLIC")
            conn.execute(
                "CREATE TABLE dl_deployment.completion ("
                "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                "name text NOT NULL, revision text NOT NULL, marker_version integer NOT NULL, "
                "completed_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute(
                "INSERT INTO dl_deployment.completion (name, revision, marker_version) "
                "VALUES (%s, %s, %s)", (name, revision, MARKER_VERSION),
            )
            conn.execute("GRANT USAGE ON SCHEMA dl_deployment TO dl_importer")
            conn.execute("GRANT SELECT ON dl_deployment.completion TO dl_importer")
    return True


def import_data(
    options: dict[str, str], name: str, revision: str, csv_path: Path, image_root: Path
) -> tuple[int, int]:
    validate_identity(name, revision)
    pokemon, images = load_catalog(csv_path, image_root)
    with psycopg.connect(**options, dbname=DATABASE, user="dl_importer") as conn:
        check_connection(conn, "dl_importer")
        lock_catalog(conn)
        check_marker(conn, name, revision)
        import_catalog_connection(conn, pokemon, images)
    return len(pokemon), len(images)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "import"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--image-root", type=Path)
    args = parser.parse_args(argv)
    if args.action == "import" and (args.csv is None or args.image_root is None):
        parser.error("import requires --csv and --image-root")
    try:
        environment = {key: os.environ.get(key, "") for key in ENVIRONMENT}
        # Reject ambient PGUSER/PGSERVICE/PGHOSTADDR/etc, including unknown future
        # libpq settings. The explicit options above carry the approved transport.
        for key in list(os.environ):
            if key.upper().startswith("PG"):
                del os.environ[key]
        options = connection_options(environment)
        if args.action == "init":
            created = initialize(
                options, args.name, args.revision, os.environ.get("DL_GATEWAY_PASSWORD", "")
            )
            print("Initialized deployment database."
                  if created else "Database is already initialized.")
        elif args.action == "import":
            count, images = import_data(
                options, args.name, args.revision, args.csv, args.image_root
            )
            print(f"Imported {count} Pokémon and {images} unique PNG images.")
    except (psycopg.Error, OSError, ValueError, RuntimeError, csv.Error) as exc:
        if isinstance(exc, psycopg.Error):
            detail = f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})"
        elif isinstance(exc, OSError):
            detail = "could not read the required local input files"
        else:
            detail = str(exc)
        print(f"Deployment database operation failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
