"""Existing favorites become private without changing lists or draft history."""

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
SOURCE = MIGRATION_DIRECTORY / "0004_favorites_privacy.sql"
FAVORITES = ("favorites_list", "favorites_entry", "favorites_list_snapshot")


@pytest.fixture
def public_favorites_database(test_database, tmp_path):
    name = "favorites_privacy_" + uuid4().hex
    identifier = sql.Identifier(name)
    admin = make_conninfo(test_database.admin, dbname=name)
    gateway = make_conninfo(test_database.gateway, dbname=name)
    previous = tmp_path / "previous"
    previous.mkdir()
    versions = ["0001_tournament.sql", "0002_team_metadata.sql", "0003_favorites.sql"]
    for filename in versions:
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
            assert migrate(admin, previous, schema="dl", role="dl_owner") == versions
            with psycopg.connect(admin) as conn:
                owners = [
                    row[0]
                    for row in conn.execute(
                        "INSERT INTO dl.app_user (issuer, subject, display_name) VALUES "
                        "('urn:favorites-privacy:test', 'first', 'First owner'), "
                        "('urn:favorites-privacy:test', 'second', 'Second owner') RETURNING id"
                    ).fetchall()
                ]
                draftables = [
                    row[0]
                    for row in conn.execute(
                        "INSERT INTO dl.draftable (name, point_cost) VALUES "
                        "('First selection', 7), ('Second selection', 11) RETURNING id"
                    ).fetchall()
                ]
                league = conn.execute(
                    "INSERT INTO dl.league "
                    "(name, commissioner_user_id, roster_size, revision, "
                    "started_at, turn_started_at) "
                    "VALUES ('Existing draft', %s, 1, 9, now(), now()) RETURNING id",
                    (owners[0],),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO dl.team "
                    "(league_id, name, manager_user_id, draft_position, point_budget, color_rgb) "
                    "VALUES (%s, 'First team', %s, 1, 100, 0), "
                    "(%s, 'Second team', %s, 2, 100, 255)",
                    (league, owners[0], league, owners[1]),
                )
                conn.execute(
                    "INSERT INTO dl.pick "
                    "(league_id, pick_number, draftable_id, selected_by, point_cost) "
                    "VALUES (%s, 1, %s, %s, 7)",
                    (league, draftables[0], owners[0]),
                )
                for owner in owners:
                    list_id = conn.execute(
                        "INSERT INTO dl.favorites_list (owner_user_id, name, revision) "
                        "VALUES (%s, 'Existing priorities', 7) RETURNING id",
                        (owner,),
                    ).fetchone()[0]
                    # Preserve nontrivial priority order, gaps, and revisions.
                    conn.execute(
                        "INSERT INTO dl.favorites_entry "
                        "(favorites_list_id, draftable_id, ordinal) "
                        "VALUES (%s, %s, 2), (%s, %s, 9)",
                        (list_id, draftables[1], list_id, draftables[0]),
                    )
                    conn.execute(
                        "INSERT INTO dl.favorites_list (owner_user_id, name, revision) "
                        "VALUES (%s, 'Empty backup', 3)",
                        (owner,),
                    )
            yield SimpleNamespace(admin=admin, gateway=gateway, owners=owners)
        finally:
            control.execute(sql.SQL("DROP DATABASE {}").format(identifier))


def relation_rows(conn, relations):
    return {
        name: conn.execute(
            sql.SQL("SELECT to_jsonb(t) FROM dl.{} t ORDER BY to_jsonb(t)::text").format(
                sql.Identifier(name)
            )
        ).fetchall()
        for name in relations
    }


def existing_rows(conn):
    return relation_rows(conn, (
        "app_user", "draftable", "league", "team", "pick", "draft_state",
        *FAVORITES,
    ))


def favorites_as(database, owner):
    with psycopg.connect(database.gateway) as conn:
        conn.execute("SET LOCAL ROLE dl_api")
        conn.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"user_id": owner} if owner is not None else {}),),
        )
        return relation_rows(conn, FAVORITES)


def owned_rows(rows, owner):
    lists = [row for row in rows["favorites_list"] if row[0]["owner_user_id"] == owner]
    ids = {row[0]["id"] for row in lists}
    return {
        "favorites_list": lists,
        "favorites_entry": [
            row for row in rows["favorites_entry"] if row[0]["favorites_list_id"] in ids
        ],
        "favorites_list_snapshot": [
            row for row in rows["favorites_list_snapshot"] if row[0]["id"] in ids
        ],
    }


def access_state(conn):
    return (
        conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, relacl::text, reloptions "
            "FROM pg_class WHERE oid = ANY(%s::regclass[]) ORDER BY relname",
            (["dl." + name for name in FAVORITES],),
        ).fetchall(),
        conn.execute(
            "SELECT * FROM pg_policies WHERE schemaname = 'dl' "
            "AND tablename = ANY(%s) ORDER BY tablename, policyname",
            (list(FAVORITES),),
        ).fetchall(),
    )


def test_privacy_upgrade_preserves_existing_lists_and_drafts_and_is_idempotent(
    public_favorites_database, tmp_path,
):
    database = public_favorites_database
    directory = tmp_path / "privacy_only"
    directory.mkdir()
    shutil.copyfile(SOURCE, directory / SOURCE.name)
    with psycopg.connect(database.admin, autocommit=True) as conn:
        original = existing_rows(conn)
        public_rows = {name: original[name] for name in FAVORITES}
        for owner in [None, *database.owners]:
            assert favorites_as(database, owner) == public_rows

        assert migrate(database.admin, directory, schema="dl", role="dl_owner") == [SOURCE.name]
        assert existing_rows(conn) == original
        assert favorites_as(database, None) == {name: [] for name in FAVORITES}
        for owner in database.owners:
            assert favorites_as(database, owner) == owned_rows(original, owner)

        # Reads stay available to the API only, and never authorize table writes.
        privileges = conn.execute(
            "SELECT relname, relrowsecurity, "
            "EXISTS (SELECT FROM aclexplode(coalesce(relacl, acldefault('r', relowner))) "
            "WHERE grantee = 0 AND privilege_type = 'SELECT'), "
            "has_table_privilege('dl_api', oid, 'SELECT'), "
            "has_table_privilege('dl_api', oid, 'INSERT,UPDATE,DELETE,TRUNCATE') "
            "FROM pg_class WHERE oid = ANY(%s::regclass[]) ORDER BY relname",
            (["dl." + name for name in FAVORITES],),
        ).fetchall()
        assert privileges == [
            ("favorites_entry", True, False, True, False),
            ("favorites_list", True, False, True, False),
            ("favorites_list_snapshot", False, False, True, False),
        ]
        state = access_state(conn)
        ledger = conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall()
        assert migrate(database.admin, directory, schema="dl", role="dl_owner") == []
        assert existing_rows(conn) == original
        assert access_state(conn) == state
        assert (
            conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall() == ledger
        )


def test_failed_privacy_upgrade_rolls_back_permissions_policies_and_ledger(
    public_favorites_database, tmp_path,
):
    database = public_favorites_database
    directory = tmp_path / "failing"
    directory.mkdir()
    migration = directory / SOURCE.name
    original_sql = SOURCE.read_text(encoding="utf-8")
    migration.write_text(original_sql + "\nSELECT 1 / 0;\n", encoding="utf-8")
    with psycopg.connect(database.admin, autocommit=True) as conn:
        original = existing_rows(conn)
        state = access_state(conn)
        ledger = conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall()
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrate(database.admin, directory, schema="dl", role="dl_owner")
        assert existing_rows(conn) == original
        assert access_state(conn) == state
        assert (
            conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall() == ledger
        )
        assert favorites_as(database, None) == {name: original[name] for name in FAVORITES}

        migration.write_text(original_sql, encoding="utf-8")
        assert migrate(database.admin, directory, schema="dl", role="dl_owner") == [SOURCE.name]
        assert existing_rows(conn) == original
        assert favorites_as(database, None) == {name: [] for name in FAVORITES}
        for owner in database.owners:
            assert favorites_as(database, owner) == owned_rows(original, owner)
