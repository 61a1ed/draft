"""Favorites install transactionally on existing databases without changing draft data."""

import json
import shutil
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from draft_backend.app import MIGRATION_DIRECTORY
from draft_backend.migrations import migrate
from scripts.bootstrap import PROJECT

pytestmark = pytest.mark.integration
SOURCE = MIGRATION_DIRECTORY / "0003_favorites.sql"


@pytest.fixture
def pre_favorites_database(test_database, tmp_path):
    name = "favorites_migration_" + uuid4().hex
    identifier = sql.Identifier(name)
    admin = make_conninfo(test_database.admin, dbname=name)
    gateway = make_conninfo(test_database.gateway, dbname=name)
    previous = tmp_path / "previous"
    previous.mkdir()
    for filename in ("0001_tournament.sql", "0002_team_metadata.sql"):
        shutil.copyfile(MIGRATION_DIRECTORY / filename, previous / filename)
    with psycopg.connect(test_database.admin, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(identifier))
        try:
            with psycopg.connect(admin) as conn:
                conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(identifier))
                conn.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO dl_gateway").format(identifier)
                )
                conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO dl_owner").format(identifier))
                conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                conn.execute("SET LOCAL ROLE dl_owner")
                for filename in ("schema.sql", "grants.sql"):
                    conn.execute((PROJECT / "sql" / filename).read_text(encoding="utf-8"))
                conn.execute("RESET ROLE")
                conn.execute(
                    sql.SQL("REVOKE CREATE ON DATABASE {} FROM dl_owner").format(identifier)
                )
                owner = conn.execute(
                    "INSERT INTO dl.app_user (issuer, subject, display_name) "
                    "VALUES ('urn:favorites-upgrade:test', 'owner', 'Owner') RETURNING id"
                ).fetchone()[0]
                draftable = conn.execute(
                    "INSERT INTO dl.draftable (name, point_cost) "
                    "VALUES ('Selection', 7) RETURNING id"
                ).fetchone()[0]
                league = conn.execute(
                    "INSERT INTO dl.league (name, commissioner_user_id, roster_size, revision) "
                    "VALUES ('Existing draft', %s, 1, 9) RETURNING id",
                    (owner,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO dl.team "
                    "(league_id, name, manager_user_id, draft_position, point_budget) "
                    "VALUES (%s, 'Existing team', %s, 1, 100)",
                    (league, owner),
                )
            assert migrate(admin, previous, schema="dl", role="dl_owner") == [
                "0001_tournament.sql",
                "0002_team_metadata.sql",
            ]
            yield SimpleNamespace(admin=admin, gateway=gateway, owner=owner, draftable=draftable)
        finally:
            control.execute(sql.SQL("DROP DATABASE {}").format(identifier))


def previous_rows(conn):
    return {
        table: conn.execute(
            sql.SQL("SELECT to_jsonb(t) FROM dl.{} t ORDER BY to_jsonb(t)::text").format(
                sql.Identifier(table)
            )
        ).fetchall()
        for table in ("app_user", "draftable", "league", "team", "pick", "draft_state")
    }


def test_favorites_upgrade_preserves_existing_data_and_is_idempotent(
    pre_favorites_database, tmp_path,
):
    database = pre_favorites_database
    favorites_only = tmp_path / "favorites_only"
    favorites_only.mkdir()
    shutil.copyfile(SOURCE, favorites_only / SOURCE.name)
    with psycopg.connect(database.admin, autocommit=True) as conn:
        original = previous_rows(conn)
        assert migrate(database.admin, favorites_only, schema="dl", role="dl_owner") == [
            SOURCE.name
        ]
        assert previous_rows(conn) == original
        assert conn.execute("SELECT * FROM dl.favorites_list_snapshot").fetchall() == []
        with psycopg.connect(database.gateway) as writer:
            writer.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            writer.execute("SET LOCAL ROLE dl_api")
            writer.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"user_id": database.owner}),),
            )
            list_id = writer.execute("SELECT dl.create_favorites_list('After upgrade')").fetchone()[
                0
            ]
            writer.execute(
                "SELECT dl.add_favorites_entry(%s, 0, %s)", (list_id, database.draftable)
            )
        favorites = conn.execute("SELECT * FROM dl.favorites_list_snapshot").fetchall()
        ledger = conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall()
        assert migrate(database.admin, favorites_only, schema="dl", role="dl_owner") == []
        assert conn.execute("SELECT * FROM dl.favorites_list_snapshot").fetchall() == favorites
        assert (
            conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall() == ledger
        )
        assert previous_rows(conn) == original


def test_failed_favorites_upgrade_rolls_back_tables_functions_and_ledger(
    pre_favorites_database, tmp_path
):
    database = pre_favorites_database
    directory = tmp_path / "failing"
    directory.mkdir()
    migration = directory / SOURCE.name
    original_sql = SOURCE.read_text(encoding="utf-8")
    migration.write_text(original_sql + "\nSELECT 1 / 0;\n", encoding="utf-8")
    with psycopg.connect(database.admin, autocommit=True) as conn:
        original = previous_rows(conn)
        ledger = conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall()
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrate(database.admin, directory, schema="dl", role="dl_owner")
        assert previous_rows(conn) == original
        assert (
            conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall() == ledger
        )
        for name in ("favorites_list", "favorites_entry", "favorites_list_snapshot"):
            assert conn.execute("SELECT to_regclass(%s)", ("dl." + name,)).fetchone() == (None,)
        assert conn.execute(
            "SELECT to_regprocedure('dl.create_favorites_list(text)')"
        ).fetchone() == (None,)
        migration.write_text(original_sql, encoding="utf-8")
        assert migrate(database.admin, directory, schema="dl", role="dl_owner") == [SOURCE.name]
        assert previous_rows(conn) == original
