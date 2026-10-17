"""An isolated, forward-only PostgreSQL migration runner.

Call ``migrate(conninfo, directory, schema="public")`` with a dedicated migration
identity and an existing schema for the ledger. Application startup calls this
with ``role="dl_owner"`` to create objects under their intended owner.
Files are UTF-8 ``.sql`` scripts, identified and ordered by their case-sensitive
filenames. Zero-padded numbers such as ``0001_tournament.sql`` keep ordering clear.
Applied files are immutable; checksums normalize line endings for Windows/POSIX.

SQL is trusted application code: it must not control transactions, change session
settings/identity, manipulate advisory locks, or modify the migration ledger.
Use transaction-compatible SQL and schema-qualified object names. The caller
provisions the schema and migration identity; this function owns its connection.
"""

import hashlib
from pathlib import Path

import psycopg
from psycopg import sql

_LOCK_ID = 0x425A4D4947524154  # Stable across releases; one runner per database.


def migrate(
    conninfo: str,
    directory: str | Path,
    *,
    schema: str = "public",
    role: str | None = None,
    lock_timeout_ms: int = 10_000,
) -> list[str]:
    """Apply pending files and return their versions; propagate failures.

    Each file and its history entry commit together. Earlier successful files
    remain committed if a later file fails. The session lock spans the entire run
    and releases on disconnect, including failure. Use a direct connection or a
    session-pooling proxy, never a transaction-pooling proxy.

    The timeout bounds waiting for migration/DDL locks, not total execution time.
    An older release may omit newer applied versions without failing startup.
    ``schema`` selects the ledger only; it does not change the SQL search path.
    ``role`` switches the session identity before creating the ledger or applying
    files, so objects inherit that owner's default privileges. If omitted, the
    connection identity is retained. The login must be allowed to SET ROLE.
    """
    if type(lock_timeout_ms) is not int or lock_timeout_ms <= 0:
        raise ValueError("lock_timeout_ms must be a positive integer")
    migrations = {}
    for path in Path(directory).iterdir():
        if path.suffix != ".sql":
            continue
        source = path.read_text(encoding="utf-8")
        if "\x00" in source:
            raise ValueError(f"NUL byte in migration: {path.name}")
        checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
        migrations[path.name] = (source, checksum)

    ledger = sql.Identifier(schema, "schema_migrations")
    completed = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        if role is not None:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        conn.execute("SELECT set_config('lock_timeout', %s, false)", (f"{lock_timeout_ms}ms",))
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "version text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
            ).format(ledger)
        )
        applied = dict(
            conn.execute(sql.SQL("SELECT version, checksum FROM {}").format(ledger)).fetchall()
        )
        latest = max(applied, default="")
        # Validate the whole history before executing any pending migration.
        for name, (_, checksum) in sorted(migrations.items()):
            if name in applied:
                if applied[name] != checksum:
                    raise ValueError(f"Applied migration has changed: {name}")
            elif name <= latest:
                raise ValueError(f"Migration predates applied history: {name}")

        for name, (source, checksum) in sorted(migrations.items()):
            if name in applied:
                continue
            try:
                with conn.transaction():
                    conn.execute(source, prepare=False)
                    conn.execute(
                        sql.SQL("INSERT INTO {} (version, checksum) VALUES (%s, %s)").format(
                            ledger
                        ),
                        (name, checksum),
                    )
            except psycopg.Error as exc:
                exc.add_note(f"While applying migration {name}")
                raise
            completed.append(name)
    return completed
