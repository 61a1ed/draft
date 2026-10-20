"""Upgrade populated baseline databases through the packaged tournament migration."""

import json
from dataclasses import dataclass, field
from datetime import date
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from draft_backend.app import MIGRATION_DIRECTORY
from draft_backend.migrations import migrate
from scripts.bootstrap import PROJECT

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class BaselineDatabase:
    admin: str = field(repr=False)
    gateway: str = field(repr=False)
    commissioner: int
    league: int
    last_pick: int


@pytest.fixture
def populated_baseline(test_database):
    # Roles belong to the runner-owned cluster; schemas and default privileges
    # belong to this separate database. No existing application database changes.
    name = "tournament_migration_" + uuid4().hex
    identifier = sql.Identifier(name)
    admin = make_conninfo(test_database.admin, dbname=name)
    gateway = make_conninfo(test_database.gateway, dbname=name)
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
                commissioner, opponent = [
                    row[0]
                    for row in conn.execute(
                        "INSERT INTO dl.app_user (issuer, subject, display_name) VALUES "
                        "('urn:migration:test', 'commissioner', 'Commissioner'), "
                        "('urn:migration:test', 'opponent', 'Opponent') RETURNING id"
                    ).fetchall()
                ]
                draftables = conn.execute(
                    "INSERT INTO dl.draftable (name, point_cost) "
                    "VALUES ('Existing first pick', 7), ('Existing last pick', 11) RETURNING id"
                ).fetchall()
                league = conn.execute(
                    "INSERT INTO dl.league "
                    "(name, commissioner_user_id, roster_size, revision, "
                    "started_at, turn_started_at) "
                    "VALUES ('Existing completed draft', %s, 1, 9, now(), now()) RETURNING id",
                    (commissioner,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO dl.team "
                    "(league_id, name, manager_user_id, draft_position, point_budget) "
                    "VALUES (%s, 'First team', %s, 1, 100), (%s, 'Second team', %s, 2, 100)",
                    (league, commissioner, league, opponent),
                )
                picks = conn.execute(
                    "INSERT INTO dl.pick "
                    "(league_id, pick_number, draftable_id, selected_by, point_cost) "
                    "VALUES (%s, 1, %s, %s, 7), (%s, 2, %s, %s, 11) RETURNING id",
                    (league, draftables[0][0], commissioner, league, draftables[1][0], opponent),
                ).fetchall()
            yield BaselineDatabase(admin, gateway, commissioner, league, picks[-1][0])
        finally:
            control.execute(sql.SQL("DROP DATABASE {}").format(identifier))


def baseline_rows(conn):
    return {
        table: conn.execute(
            sql.SQL("SELECT * FROM dl.{} ORDER BY id").format(sql.Identifier(table))
        ).fetchall()
        for table in ("app_user", "draftable", "league", "team", "pick")
    }


def call_as_commissioner(database, query, parameters, *, rollback=False):
    with psycopg.connect(database.gateway) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        conn.execute("SET LOCAL ROLE dl_api")
        conn.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"user_id": database.commissioner}),),
        )
        result = conn.execute(query, parameters).fetchone()[0]
        if rollback:
            conn.rollback()
        return result


def test_draft_events_upgrade_preserves_populated_data_and_retires_account_wakeups(
    populated_baseline, tmp_path,
):
    database = populated_baseline
    for source in MIGRATION_DIRECTORY.glob("*.sql"):
        if source.name < "0005_draft_events.sql":
            (tmp_path / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8",
            )
    migrate(database.admin, tmp_path, schema="dl", role="dl_owner")
    favorites = call_as_commissioner(database, "SELECT dl.create_favorites_list(%s)", ("Saved",))
    with psycopg.connect(database.admin, autocommit=True) as conn:
        before = baseline_rows(conn)
        favorite_before = conn.execute("SELECT * FROM dl.favorites_list").fetchall()
        account_channel = f"dl_user_{database.commissioner}"
        draft_channel = f"dl_draft_{database.league}"
        for channel in (account_channel, draft_channel):
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))

        assert migrate(database.admin, MIGRATION_DIRECTORY, schema="dl", role="dl_owner") == [
            "0005_draft_events.sql"
        ]
        assert baseline_rows(conn) == before
        assert conn.execute("SELECT * FROM dl.favorites_list").fetchall() == favorite_before
        assert list(conn.notifies(timeout=0.05)) == []
        assert migrate(database.admin, MIGRATION_DIRECTORY, schema="dl", role="dl_owner") == []

        call_as_commissioner(
            database, "SELECT dl.rename_favorites_list(%s, %s, %s)", (favorites, 0, "Renamed"),
        )
        assert list(conn.notifies(timeout=0.05)) == []
        assert call_as_commissioner(
            database, "SELECT dl.draft_action(%s, %s, 'undo')", (database.league, 9),
        ) == database.last_pick
        messages = list(conn.notifies(timeout=0.1))
        assert [(item.channel, item.payload) for item in messages] == [(draft_channel, "")]


def test_packaged_tournament_upgrade_preserves_existing_draft_and_permissions(
    populated_baseline, tmp_path
):
    database = populated_baseline
    # This historical upgrade test deliberately checks only the tournament
    # migration; later packaged migrations may add columns to baseline tables.
    source = MIGRATION_DIRECTORY / "0001_tournament.sql"
    (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with psycopg.connect(database.admin, autocommit=True) as conn:
        before = baseline_rows(conn)
        assert conn.execute("SELECT to_regclass('dl.tournament')").fetchone() == (None,)
        assert conn.execute("SELECT status FROM dl.draft_state").fetchone() == ("completed",)

        applied = migrate(database.admin, tmp_path, schema="dl", role="dl_owner")
        assert applied == ["0001_tournament.sql"]
        assert baseline_rows(conn) == before
        ledger = conn.execute(
            'SELECT * FROM dl.schema_migrations ORDER BY version COLLATE "C"'
        ).fetchall()
        assert [row[0] for row in ledger] == applied

        # New tables/views retain public reads and deny arbitrary API table writes.
        relations = conn.execute(
            "SELECT pg_get_userbyid(c.relowner), "
            "EXISTS (SELECT FROM aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) "
            "WHERE grantee = 0 AND privilege_type = 'SELECT'), "
            "has_table_privilege('dl_api', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'dl' AND c.relkind IN ('r', 'v')"
        ).fetchall()
        assert relations and all(row == ("dl_owner", True, False) for row in relations)
        functions = conn.execute(
            "SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, "
            "has_function_privilege('dl_api', p.oid, 'EXECUTE'), "
            "EXISTS (SELECT FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) "
            "WHERE grantee = 0 AND privilege_type = 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'dl' AND p.proname = ANY(%s)",
            ([
                "publish_tournament", "set_round_suggested_date", "set_team_availability",
                "copy_team_availability_week", "submit_result", "record_result", "correct_result",
                "set_match_vod", "withdraw_team", "create_tiebreak_round", "publish_final",
            ],),
        ).fetchall()
        assert len(functions) == 11
        assert all(row[1:] == ("dl_owner", True, True, False) for row in functions)
        helpers = conn.execute(
            "SELECT has_function_privilege('dl_api', p.oid, 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'dl' AND starts_with(p.proname, 'tournament_')"
        ).fetchall()
        assert helpers and not any(row[0] for row in helpers)

        # Installing the feature leaves ordinary draft undo available. Rolling
        # back this successful call keeps the original completed draft intact.
        assert call_as_commissioner(
            database,
            "SELECT dl.draft_action(%s, %s, 'undo')",
            (database.league, 9),
            rollback=True,
        ) == database.last_pick
        call_as_commissioner(
            database,
            "SELECT dl.publish_tournament(%s, %s, %s)",
            (database.league, 9, date(2026, 10, 19)),
        )
        assert conn.execute("SELECT count(*) FROM dl.match").fetchone() == (1,)
        with pytest.raises(psycopg.Error) as denied:
            call_as_commissioner(
                database, "SELECT dl.draft_action(%s, %s, 'undo')", (database.league, 9)
            )
        assert denied.value.sqlstate == "PT409"
        assert baseline_rows(conn) == before

        assert migrate(database.admin, tmp_path, schema="dl", role="dl_owner") == []
        assert conn.execute(
            'SELECT * FROM dl.schema_migrations ORDER BY version COLLATE "C"'
        ).fetchall() == ledger
        assert conn.execute("SELECT count(*) FROM dl.match").fetchone() == (1,)
        assert baseline_rows(conn) == before


def test_failed_tournament_upgrade_rolls_back_all_feature_changes(populated_baseline, tmp_path):
    database = populated_baseline
    # Exercise the actual first migration, failing after its final permission
    # grant. The packaged source itself is never edited by this test.
    source = next(path for path in MIGRATION_DIRECTORY.glob("*.sql") if "tournament" in path.name)
    migration = tmp_path / source.name
    original = source.read_text(encoding="utf-8")
    migration.write_text(original + "\nSELECT 1 / 0;\n", encoding="utf-8")
    with psycopg.connect(database.admin, autocommit=True) as conn:
        before = baseline_rows(conn)
        actions = conn.execute("SELECT pg_get_viewdef('dl.permitted_action'::regclass)").fetchone()
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrate(database.admin, tmp_path, schema="dl", role="dl_owner")
        assert conn.execute("SELECT to_regclass('dl.tournament')").fetchone() == (None,)
        assert conn.execute(
            "SELECT to_regprocedure('dl.publish_tournament(integer,integer,date,integer,integer)')"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT pg_get_viewdef('dl.permitted_action'::regclass)"
        ).fetchone() == actions
        assert conn.execute("SELECT count(*) FROM dl.schema_migrations").fetchone() == (0,)
        assert baseline_rows(conn) == before

        migration.write_text(original, encoding="utf-8")
        assert migrate(database.admin, tmp_path, schema="dl", role="dl_owner") == [source.name]
        assert baseline_rows(conn) == before
