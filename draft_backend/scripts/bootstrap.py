"""Install the application schema and its four service roles.

Run once against an empty, dedicated database as a PostgreSQL superuser. Role
names are cluster-wide: existing reserved roles are deliberately never reused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql

PROJECT = Path(__file__).resolve().parents[1]
ROLES = ("dl_owner", "dl_api", "dl_auth", "dl_gateway")


def schema_body() -> str:
    """Keep the shipped SQL intact, but let bootstrap own its transaction."""
    source = (PROJECT / "sql" / "schema.sql").read_text(encoding="utf-8")
    # Only the two standalone wrapper lines are removed. PL/pgSQL BEGIN/END
    # statements and the source file itself are left untouched.
    if len(re.findall(r"(?m)^BEGIN;\s*$", source)) != 1:
        raise RuntimeError("Expected exactly one outer BEGIN; in sql/schema.sql")
    if len(re.findall(r"(?m)^COMMIT;\s*$", source)) != 1:
        raise RuntimeError("Expected exactly one outer COMMIT; in sql/schema.sql")
    return re.sub(r"(?m)^(?:BEGIN|COMMIT);[ \t]*$", "", source)


def bootstrap(conninfo: str, gateway_password: str) -> None:
    """Atomically provision fresh roles, schema, and grants; never print secrets."""
    if not gateway_password or "\x00" in gateway_password:
        raise ValueError("A nonempty gateway password without NUL is required")
    source = schema_body()
    grants = (PROJECT / "sql" / "grants.sql").read_text(encoding="utf-8")
    with psycopg.connect(conninfo) as conn:
        if conn.info.server_version < 160000:
            raise RuntimeError("PostgreSQL 16 or newer is required")
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        if not row or not row[0]:
            raise RuntimeError("Bootstrap requires an administrator superuser connection")
        existing = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLES),)
        ).fetchall()
        if existing:
            raise RuntimeError(
                "Reserved roles already exist; refusing to repurpose them: "
                + ", ".join(row[0] for row in existing)
            )
        existing_schemas = conn.execute(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname NOT IN ('public', 'information_schema') "
            "AND nspname NOT LIKE 'pg_%'"
        ).fetchall()
        existing_objects = conn.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' LIMIT 1"
        ).fetchone()
        if existing_schemas or existing_objects:
            raise RuntimeError("Bootstrap requires an empty application database")

        for name in ROLES[:-1]:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(name))
            )
        conn.execute("SET LOCAL password_encryption = 'scram-sha-256'")
        conn.execute(
            sql.SQL(
                "CREATE ROLE dl_gateway LOGIN NOINHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Literal(gateway_password))
        )
        conn.execute("GRANT dl_api, dl_auth TO dl_gateway WITH SET TRUE")
        conn.execute("GRANT dl_api, dl_auth TO dl_gateway WITH INHERIT FALSE")
        database = sql.Identifier(conn.info.dbname)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO dl_gateway").format(database))
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO dl_owner").format(database))
        conn.execute("SET LOCAL ROLE dl_owner")
        conn.execute(source, prepare=False)
        conn.execute(grants, prepare=False)
        conn.execute("RESET ROLE")
        conn.execute(sql.SQL("REVOKE CREATE ON DATABASE {} FROM dl_owner").format(database))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-dsn", default=os.environ.get("ADMIN_DATABASE_URL"))
    parser.add_argument("--gateway-password-env", default="DL_GATEWAY_PASSWORD")
    args = parser.parse_args()
    if not args.admin_dsn:
        parser.error("Set ADMIN_DATABASE_URL or supply --admin-dsn")
    password = os.environ.get(args.gateway_password_env)
    if not password:
        parser.error(f"Set {args.gateway_password_env} to the gateway password")
    try:
        bootstrap(args.admin_dsn, password)
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        # psycopg connection exceptions can include conninfo; do not echo them.
        detail = (
            exc
            if not isinstance(exc, psycopg.Error)
            else (f"database operation failed (SQLSTATE {exc.sqlstate or 'connection error'})")
        )
        print(f"Bootstrap failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Installed application schema and service roles.")


if __name__ == "__main__":
    main()
