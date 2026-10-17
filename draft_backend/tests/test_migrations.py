"""Migration history and crash behavior, exercised against real PostgreSQL."""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from draft_backend import migrations


@pytest.mark.parametrize("role", [None, "dl_owner", 'owner"with quotes'])
def test_session_role_is_selected_before_creating_objects(tmp_path, monkeypatch, role):
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(migrations.psycopg, "connect", connect)
    write(tmp_path, "001_first.sql", "SELECT 1;")
    write(tmp_path, "002_second.sql", "SELECT 2;")

    assert migrations.migrate("migration credentials", tmp_path, role=role) == [1, 2]

    connect.assert_called_once_with("migration credentials", autocommit=True)
    statements = [
        call.args[0].as_string() if isinstance(call.args[0], sql.Composable) else call.args[0]
        for call in connection.execute.call_args_list
    ]
    role_statements = [statement for statement in statements if statement.startswith("SET ROLE")]
    if role is None:
        assert role_statements == []
    else:
        expected = 'SET ROLE "' + role.replace('"', '""') + '"'
        assert role_statements == [expected]
        assert statements[0] == expected


def test_failed_role_switch_does_not_create_objects(tmp_path, monkeypatch):
    connection = MagicMock()
    connection.execute.side_effect = psycopg.errors.InsufficientPrivilege("cannot set role")
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(migrations.psycopg, "connect", connect)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        migrations.migrate("migration credentials", tmp_path, role="dl_owner")

    connection.execute.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "filenames",
    [
        ["1_first.sql"],
        ["_first.sql"],
        ["a001 bad.sql"],
        ["a001.bad.sql"],
    ],
)
def test_invalid_files_fail_before_connecting(tmp_path, monkeypatch, filenames):
    for filename in filenames:
        (tmp_path / filename).write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(ValueError):
        migrations.migrate("unused", tmp_path)


def test_case_insensitive_duplicates_fail_before_connecting(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # Separate directories let this exercise case collisions on Windows too.
    paths = [
        write(first, "a001-first.sql", "SELECT 1;"),
        write(second, "A001-FIRST.sql", "SELECT 1;"),
    ]
    monkeypatch.setattr(type(tmp_path), "iterdir", lambda self: iter(paths))
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(ValueError, match="Duplicate"):
        migrations.migrate("unused", tmp_path)


def test_nul_byte_fails_before_connecting(tmp_path, monkeypatch):
    write(tmp_path, "a001-first.sql", "SELECT 1;\x00SELECT 2;")
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(ValueError, match="NUL byte"):
        migrations.migrate("unused", tmp_path)


@pytest.mark.parametrize("timeout", [0, -1, 0.5, "1000", True])
def test_invalid_timeout_fails_before_connecting(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(ValueError, match="lock_timeout_ms"):
        migrations.migrate("unused", tmp_path, lock_timeout_ms=timeout)


@pytest.fixture
def migration_database(test_database):
    schema = f"migration_{uuid4().hex}"
    with psycopg.connect(test_database.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield test_database.admin, schema, conn
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def write(directory, filename, source):
    path = directory / filename
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


def history(conn, schema):
    return conn.execute(
        sql.SQL("SELECT version, checksum FROM {} ORDER BY version").format(
            sql.Identifier(schema, "schema_migrations")
        )
    ).fetchall()


@pytest.mark.integration
def test_filename_order_idempotence_and_portable_checksums(tmp_path, migration_database):
    conninfo, schema, conn = migration_database
    write(tmp_path, "B2026-10-16-update.sql", f"UPDATE {schema}.example SET value = value + 1;")
    write(tmp_path, "a010-update.sql", f"UPDATE {schema}.example SET value = value + 1;")
    first = write(
        tmp_path,
        "a002-create.sql",
        (
            f"CREATE TABLE {schema}.example (value integer);\n"
            f"INSERT INTO {schema}.example VALUES (40);\n"
        ),
    )
    write(tmp_path, "README.txt", "This is not a migration.")
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == [
        "a002-create.sql",
        "a010-update.sql",
        "b2026-10-16-update.sql",
    ]
    assert conn.execute(f"SELECT value FROM {schema}.example").fetchone() == (42,)
    assert history(conn, schema)[0] == (
        "a002-create.sql",
        hashlib.sha256(first.read_bytes()).hexdigest(),
    )
    first.write_bytes(first.read_bytes().replace(b"\n", b"\r\n"))
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == []
    assert conn.execute(f"SELECT value FROM {schema}.example").fetchone() == (42,)


@pytest.mark.integration
def test_migration_role_owns_objects_and_applies_its_default_privileges(
    tmp_path, migration_database
):
    conninfo, schema, conn = migration_database
    conn.execute(
        sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO dl_owner").format(sql.Identifier(schema))
    )
    write(
        tmp_path,
        "a001-owner.sql",
        f"CREATE TABLE {schema}.example (id integer);"
        f"CREATE FUNCTION {schema}.example() RETURNS integer LANGUAGE sql AS 'SELECT 1';",
    )
    assert migrations.migrate(conninfo, tmp_path, schema=schema, role="dl_owner") == [
        "a001-owner.sql"
    ]
    owners = conn.execute(
        "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = %s::regnamespace AND relkind = 'r' ORDER BY relname",
        (schema,),
    ).fetchall()
    assert owners == [("example", "dl_owner"), ("schema_migrations", "dl_owner")]
    function = conn.execute(
        "SELECT pg_get_userbyid(proowner), NOT EXISTS ("
        "SELECT FROM aclexplode(coalesce(proacl, acldefault('f', proowner))) "
        "WHERE grantee = 0 AND privilege_type = 'EXECUTE') "
        "FROM pg_proc WHERE oid = %s::regprocedure",
        (f"{schema}.example()",),
    ).fetchone()
    # Bootstrap removes PUBLIC execution from future functions created by dl_owner.
    assert function == ("dl_owner", True)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["sql", "ledger"])
def test_sql_and_ledger_commit_together_and_failed_file_can_retry(
    tmp_path,
    migration_database,
    failure,
):
    conninfo, schema, conn = migration_database
    write(
        tmp_path,
        "a001-initial.sql",
        (f"CREATE TABLE {schema}.example (value integer);INSERT INTO {schema}.example VALUES (1);"),
    )
    if failure == "ledger":
        assert migrations.migrate(conninfo, tmp_path, schema=schema) == ["a001-initial.sql"]
    source = f"CREATE TABLE {schema}.later (id integer); UPDATE {schema}.example SET value = 2;"
    write(tmp_path, "a002-later.sql", source + ("SELECT 1 / 0;" if failure == "sql" else ""))
    if failure == "ledger":
        conn.execute(
            f"ALTER TABLE {schema}.schema_migrations "
            "ADD CONSTRAINT test_record_failure CHECK (version <> 'a002-later.sql')"
        )
    error = psycopg.errors.DivisionByZero if failure == "sql" else psycopg.errors.CheckViolation
    with pytest.raises(error):
        migrations.migrate(conninfo, tmp_path, schema=schema)
    assert conn.execute(f"SELECT value FROM {schema}.example").fetchone() == (1,)
    assert conn.execute("SELECT to_regclass(%s)", (f"{schema}.later",)).fetchone() == (None,)
    assert [version for version, _ in history(conn, schema)] == ["a001-initial.sql"]
    if failure == "ledger":
        conn.execute(f"ALTER TABLE {schema}.schema_migrations DROP CONSTRAINT test_record_failure")
    write(tmp_path, "a002-later.sql", source)
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == ["a002-later.sql"]
    assert conn.execute(f"SELECT value FROM {schema}.example").fetchone() == (2,)


@pytest.mark.integration
def test_history_validation_and_older_checkout(tmp_path, migration_database):
    conninfo, schema, conn = migration_database
    write(tmp_path, "a001-first.sql", "SELECT 1;")
    newest = write(tmp_path, "a010-latest.sql", "SELECT 10;")
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == [
        "a001-first.sql",
        "a010-latest.sql",
    ]
    newest.unlink()
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == []
    # Validate all known history before executing a later pending migration.
    write(tmp_path, "a011-pending.sql", f"CREATE TABLE {schema}.unexpected (id integer);")
    write(tmp_path, "a001-first.sql", "SELECT 2;")
    with pytest.raises(ValueError, match="changed"):
        migrations.migrate(conninfo, tmp_path, schema=schema)
    write(tmp_path, "a001-first.sql", "SELECT 1;")
    backdated = write(tmp_path, "a002-backdated.sql", "SELECT 2;")
    with pytest.raises(ValueError, match="predates"):
        migrations.migrate(conninfo, tmp_path, schema=schema)
    assert conn.execute("SELECT to_regclass(%s)", (f"{schema}.unexpected",)).fetchone() == (None,)
    assert [version for version, _ in history(conn, schema)] == [
        "a001-first.sql",
        "a010-latest.sql",
    ]
    backdated.unlink()
    assert migrations.migrate(conninfo, tmp_path, schema=schema) == ["a011-pending.sql"]


def wait_for_advisory_lock(conn, application_name, future):
    """Synchronize on PostgreSQL's observed wait state, not thread scheduling."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name = %s "
            "AND wait_event_type = 'Lock' AND lower(wait_event) = 'advisory'",
            (application_name,),
        ).fetchone()
        if row:
            return row[0]
        if future.done():
            pytest.fail(f"Runner finished before reaching the lock: {future.result()}")
        time.sleep(0.01)
    pytest.fail(f"Runner never waited for an advisory lock: {application_name}")


@pytest.mark.integration
@pytest.mark.parametrize("outcome", ["complete", "disconnect", "timeout"])
def test_concurrent_startup_and_lock_recovery(tmp_path, migration_database, outcome):
    conninfo, schema, conn = migration_database
    # Test-only gate simulates a blocked statement without touching the runner's lock.
    gate = uuid4().int % 2**31
    write(
        tmp_path,
        "a001-create.sql",
        (
            f"CREATE TABLE {schema}.example (id integer);"
            f"SELECT pg_advisory_xact_lock({gate});"
            f"INSERT INTO {schema}.example VALUES (1);"
        ),
    )
    first_name, second_name = f"first_{uuid4().hex}", f"second_{uuid4().hex}"
    first_info = make_conninfo(conninfo, application_name=first_name)
    second_info = make_conninfo(conninfo, application_name=second_name)
    conn.execute("SELECT pg_advisory_lock(%s)", (gate,))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            migrations.migrate,
            first_info,
            tmp_path,
            schema=schema,
            lock_timeout_ms=30_000,
        )
        try:
            first_pid = wait_for_advisory_lock(conn, first_name, first)
            if outcome == "timeout":
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    migrations.migrate(second_info, tmp_path, schema=schema, lock_timeout_ms=50)
            else:
                second = executor.submit(
                    migrations.migrate,
                    second_info,
                    tmp_path,
                    schema=schema,
                    lock_timeout_ms=30_000,
                )
                wait_for_advisory_lock(conn, second_name, second)
                if outcome == "disconnect":
                    terminated = conn.execute(
                        "SELECT pg_terminate_backend(%s)", (first_pid,)
                    ).fetchone()
                    assert terminated == (True,)
                    with pytest.raises(psycopg.Error):
                        first.result(timeout=10)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (gate,))
        if outcome != "disconnect":
            assert first.result(timeout=10) == ["a001-create.sql"]
        if outcome != "timeout":
            assert second.result(timeout=10) == (
                ["a001-create.sql"] if outcome == "disconnect" else []
            )
    assert migrations.migrate(second_info, tmp_path, schema=schema) == []
    assert conn.execute(f"SELECT id FROM {schema}.example").fetchall() == [(1,)]
    assert [version for version, _ in history(conn, schema)] == ["a001-create.sql"]
