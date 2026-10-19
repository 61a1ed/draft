"""Upgrade populated pre-metadata databases without rewriting draft history."""

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


@pytest.fixture
def pre_metadata_database(test_database, tmp_path):
    name = "metadata_migration_" + uuid4().hex
    identifier = sql.Identifier(name)
    admin = make_conninfo(test_database.admin, dbname=name)
    gateway = make_conninfo(test_database.gateway, dbname=name)
    previous = tmp_path / "previous"
    previous.mkdir()
    shutil.copyfile(MIGRATION_DIRECTORY / "0001_tournament.sql", previous / "0001_tournament.sql")
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
                owner, manager = [
                    row[0]
                    for row in conn.execute(
                        "INSERT INTO dl.app_user (issuer, subject, display_name) VALUES "
                        "('urn:metadata-upgrade:test', 'owner', 'Owner'), "
                        "('urn:metadata-upgrade:test', 'manager', 'Manager') RETURNING id",
                    ).fetchall()
                ]
                draftables = [
                    row[0]
                    for row in conn.execute(
                        "INSERT INTO dl.draftable (name, point_cost) "
                        "VALUES ('First selection', 7), ('Second selection', 11) RETURNING id",
                    ).fetchall()
                ]
                leagues = []
                for label, picks in (("setup", 0), ("drafting", 1), ("completed", 2)):
                    league = conn.execute(
                        "INSERT INTO dl.league (name, commissioner_user_id, roster_size, revision, "
                        "started_at, turn_started_at) VALUES (%s, %s, 1, 9, "
                        "CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END) RETURNING id",
                        (label, owner, picks > 0, picks > 0),
                    ).fetchone()[0]
                    leagues.append(league)
                    conn.execute(
                        "INSERT INTO dl.team "
                        "(league_id, name, manager_user_id, draft_position, point_budget) VALUES "
                        "(%s, 'Existing first', %s, 1, 100), (%s, 'Existing second', %s, 2, 100)",
                        (league, owner, league, manager),
                    )
                    for index in range(picks):
                        conn.execute(
                            "INSERT INTO dl.pick "
                            "(league_id, pick_number, draftable_id, selected_by, point_cost) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (league, index + 1, draftables[index], owner, (7, 11)[index]),
                        )
            assert migrate(admin, previous, schema="dl", role="dl_owner") == ["0001_tournament.sql"]
            with psycopg.connect(gateway) as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                conn.execute("SET LOCAL ROLE dl_api")
                conn.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"user_id": owner}),),
                )
                conn.execute(
                    "SELECT dl.publish_tournament(%s, 9, '2026-10-19'::date)", (leagues[2],)
                )
            yield SimpleNamespace(
                admin=admin, gateway=gateway, owner=owner, manager=manager, leagues=leagues
            )
        finally:
            control.execute(sql.SQL("DROP DATABASE {}").format(identifier))


def original_rows(conn):
    # Exclude only the new column; compare every previously stored value and row.
    return {
        table: conn.execute(
            sql.SQL(
                "SELECT to_jsonb(t) - 'color_rgb' FROM dl.{} t "
                "ORDER BY (to_jsonb(t) - 'color_rgb')::text",
            ).format(sql.Identifier(table))
        ).fetchall()
        for table in (
            "app_user",
            "draftable",
            "league",
            "team",
            "pick",
            "tournament",
            "tournament_stage",
            "stage_entry",
            "tournament_round",
            "match",
        )
    }


def metadata_source():
    return next(path for path in MIGRATION_DIRECTORY.glob("*.sql") if "team_metadata" in path.name)


def test_metadata_upgrade_preserves_existing_leagues_and_backfills_black(
    pre_metadata_database, tmp_path,
):
    database = pre_metadata_database
    source = metadata_source()
    # Exercise this upgrade independently of later feature migrations.
    metadata_only = tmp_path / "metadata_only"
    metadata_only.mkdir()
    shutil.copyfile(source, metadata_only / source.name)
    with psycopg.connect(database.admin, autocommit=True) as conn:
        before = original_rows(conn)
        draft_states = conn.execute("SELECT * FROM dl.draft_state ORDER BY league_id").fetchall()
        assert migrate(database.admin, metadata_only, schema="dl", role="dl_owner") == [
            source.name,
        ]
        assert original_rows(conn) == before
        assert (
            conn.execute("SELECT * FROM dl.draft_state ORDER BY league_id").fetchall()
            == draft_states
        )
        assert conn.execute("SELECT color_rgb FROM dl.team").fetchall() == [(0,)] * 6
        snapshots = conn.execute(
            "SELECT teams FROM dl.league_snapshot ORDER BY league_id"
        ).fetchall()
        assert all(team["metadata_complete"] for row in snapshots for team in row[0])
        assert all(team["color_rgb"] == 0 for row in snapshots for team in row[0])
        assert all(team["can_set_metadata"] is False for row in snapshots for team in row[0])

        # The backfill is one-time. New rows retain genuinely unchosen metadata.
        new_league = conn.execute(
            "INSERT INTO dl.league (name, commissioner_user_id, roster_size) "
            "VALUES ('After upgrade', %s, 1) RETURNING id",
            (database.owner,),
        ).fetchone()[0]
        team = conn.execute(
            "INSERT INTO dl.team (league_id, manager_user_id, point_budget) "
            "VALUES (%s, %s, 100) RETURNING id",
            (new_league, database.manager),
        ).fetchone()[0]
        assert conn.execute(
            "SELECT name, color_rgb FROM dl.team WHERE id=%s", (team,)
        ).fetchone() == (
            None,
            None,
        )
        assert conn.execute(
            "SELECT metadata_complete FROM dl.team_points WHERE id=%s", (team,)
        ).fetchone() == (False,)
        ledger = conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall()
        assert migrate(database.admin, metadata_only, schema="dl", role="dl_owner") == []
        assert (
            conn.execute("SELECT * FROM dl.schema_migrations ORDER BY version").fetchall() == ledger
        )
        assert conn.execute("SELECT color_rgb FROM dl.team WHERE id=%s", (team,)).fetchone() == (
            None,
        )


def test_failed_metadata_upgrade_rolls_back_columns_views_functions_and_data(
    pre_metadata_database,
    tmp_path,
):
    database = pre_metadata_database
    source = metadata_source()
    directory = tmp_path / "failing"
    directory.mkdir()
    migration = directory / source.name
    original = source.read_text(encoding="utf-8")
    migration.write_text(original + "\nSELECT 1 / 0;\n", encoding="utf-8")
    with psycopg.connect(database.admin, autocommit=True) as conn:
        before = original_rows(conn)
        views = {
            name: conn.execute("SELECT pg_get_viewdef(%s::regclass)", (name,)).fetchone()
            for name in ("dl.team_points", "dl.draft_state", "dl.league_snapshot")
        }
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrate(database.admin, directory, schema="dl", role="dl_owner")
        assert original_rows(conn) == before
        assert conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema='dl' AND table_name='team' AND column_name IN ('name','color_rgb')",
        ).fetchall() == [("name", "NO")]
        assert conn.execute(
            "SELECT to_regprocedure('dl.set_team_metadata(integer,integer,integer,text,integer)')",
        ).fetchone() == (None,)
        assert conn.execute("SELECT version FROM dl.schema_migrations").fetchall() == [
            ("0001_tournament.sql",),
        ]
        assert all(
            conn.execute("SELECT pg_get_viewdef(%s::regclass)", (name,)).fetchone() == view
            for name, view in views.items()
        )
        migration.write_text(original, encoding="utf-8")
        assert migrate(database.admin, directory, schema="dl", role="dl_owner") == [source.name]
        assert original_rows(conn) == before
