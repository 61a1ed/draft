"""SQL fixture safety, rollback, and the states exposed through the real SQL views."""

import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import scenarios, testdb  # noqa: E402


def marked_cluster(tmp_path, *, host="127.0.0.1", port=55432, database="draft_test"):
    root = tmp_path / "cluster"
    root.mkdir()
    (root / "data").mkdir()
    (root / testdb.MARKER).write_text(json.dumps({
        "format": testdb.MAGIC,
        "root": str(root.resolve()),
        "data": str(root.resolve() / "data"),
        "pg_bin": str(tmp_path / "bin"),
        "port": 55432,
        "database": "draft_test",
    }))
    (root / "admin.env").write_text(
        f"ADMIN_DATABASE_URL=postgresql://postgres:private@{host}:{port}/{database}\n"
    )
    return root


def test_prepare_checks_managed_root_and_never_exposes_credentials(tmp_path):
    root = marked_cluster(tmp_path)
    prepared = scenarios.prepare(root)
    assert prepared.root == root.resolve()
    assert "private" not in repr(prepared)
    (root / testdb.MARKER).unlink()
    with pytest.raises(RuntimeError, match="Not a marked test cluster"):
        scenarios.prepare(root)


@pytest.mark.parametrize("overrides", [
    {"host": "example.com"}, {"port": 55433}, {"database": "production"},
])
def test_prepare_rejects_connection_outside_marked_cluster(tmp_path, overrides):
    root = marked_cluster(tmp_path, **overrides)
    with pytest.raises(RuntimeError, match="does not match"):
        scenarios.prepare(root)


def test_outer_transaction_handling_preserves_procedure_blocks():
    body = "DO $$\nBEGIN\n    PERFORM 1;\nEND $$;\n"
    assert body.strip() in scenarios.transaction_body(
        "-- fixture\nBEGIN;\n" + body + "COMMIT;\n"
    )
    assert scenarios.transaction_body(body) == body
    with pytest.raises(ValueError, match="only one outer"):
        scenarios.transaction_body("BEGIN;\nCOMMIT;\nBEGIN;\nCOMMIT;\n")
    with pytest.raises(ValueError, match="only one outer"):
        scenarios.transaction_body("BEGIN;\nSELECT 1;\nCOMMIT;\nSELECT 2;\n")


@pytest.mark.parametrize("source", [
    "SELECT 1; COMMIT; SELECT 1 / 0;",
    "SELECT 1; COMMIT -- inline comment\n; SELECT 1 / 0;",
    "SELECT 1; END; SELECT 1 / 0;",
    "SELECT 1; ABORT; SELECT 1 / 0;",
    "SELECT 1; START TRANSACTION; SELECT 1 / 0;",
    "SELECT 1; COMMIT AND CHAIN; SELECT 1 / 0;",
    "SELECT 1; PREPARE TRANSACTION 'early'; SELECT 1 / 0;",
    "BEGIN; SELECT 1; /* nested /* comment */ */ COMMIT; SELECT 2;",
])
def test_custom_sql_cannot_commit_partway_through_file(source):
    with pytest.raises(ValueError, match="only one outer"):
        scenarios.transaction_body(source)


def test_transaction_scanner_ignores_quoted_bodies_and_comments():
    source = (
        "/* COMMIT; */ BEGIN; SELECT 'COMMIT;', \"END\", E'escaped\\\'COMMIT;', "
        "$$END;$$; DO $body$ BEGIN PERFORM 1; END $body$; -- ABORT;\nCOMMIT;"
    )
    body = scenarios.transaction_body(source)
    assert "DO $body$ BEGIN PERFORM 1; END $body$;" in body
    assert body.startswith("/* COMMIT; */ ")


def test_invalid_selection_never_connects(tmp_path, monkeypatch):
    def refuse_connect(*args, **kwargs):
        pytest.fail("invalid scenario selection must not connect")

    monkeypatch.setattr(scenarios.psycopg, "connect", refuse_connect)
    database = scenarios.ScenarioDatabase(tmp_path, "unused")
    with pytest.raises(ValueError, match="Unknown scenarios"):
        scenarios.apply(database, ["missing"])
    with pytest.raises(ValueError, match="not both"):
        scenarios.apply(database, ["live"], file=tmp_path / "custom.sql")


@pytest.mark.integration
def test_sql_scenarios_snapshots_scoped_reset_and_atomic_rollback(tmp_path, monkeypatch):
    admin = os.environ.get("TEST_ADMIN_DATABASE_URL")
    gateway = os.environ.get("TEST_DATABASE_URL")
    if not admin or not gateway:
        pytest.skip("Set TEST_ADMIN_DATABASE_URL and TEST_DATABASE_URL for scenario integration")
    for dsn in (admin, gateway):
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT EXISTS (SELECT FROM dl_test.marker WHERE singleton)")\
                .fetchone()[0], "Refusing writes to an unmarked database"

    database = scenarios.ScenarioDatabase(tmp_path, admin)
    with psycopg.connect(admin, row_factory=dict_row) as conn:
        before = conn.execute(
            "SELECT (SELECT count(*) FROM dl.app_user) AS users, "
            "(SELECT count(*) FROM dl.draftable) AS catalog, "
            "(SELECT count(*) FROM dl.asset) AS assets"
        ).fetchone()
        alice = conn.execute(
            "SELECT id FROM dl.app_user WHERE google_sub = 'test:alice'"
        ).fetchone()["id"]
        bob = conn.execute(
            "SELECT id FROM dl.app_user WHERE google_sub = 'test:bob'"
        ).fetchone()["id"]
        unrelated = conn.execute(
            "INSERT INTO dl.league (name, commissioner_user_id, roster_size) "
            "VALUES ('User-owned league', %s, 1) RETURNING id", (alice,)
        ).fetchone()["id"]

    loaded = {row["scenario"]: row for row in scenarios.apply(database)}
    assert set(loaded) == set(scenarios.SCENARIOS)
    assert {name: row["status"] for name, row in loaded.items()} == {
        "setup": "setup", "ready": "setup", "live": "drafting",
        "budget": "drafting", "undo": "drafting", "completed": "completed",
    }
    with psycopg.connect(gateway, row_factory=dict_row) as conn:
        conn.execute("SET LOCAL ROLE dl_api")
        conn.execute("SELECT set_config('request.jwt.claims', %s, true)",
                     (json.dumps({"user_id": alice}),))
        snapshots = {
            name: conn.execute("SELECT * FROM dl.league_snapshot WHERE league_id = %s",
                               (row["league_id"],)).fetchone()
            for name, row in loaded.items()
        }
        assert not snapshots["setup"]["state"]["can_start"]
        assert snapshots["ready"]["state"]["can_start"]
        live = snapshots["live"]
        next_team = next(t for t in live["teams"] if t["id"] == live["state"]["next_team_id"])
        assert next_team["manager_user_id"] == bob
        assert live["state"]["next_pick_number"] == 5
        budget = snapshots["budget"]
        next_team = next(t for t in budget["teams"]
                         if t["id"] == budget["state"]["next_team_id"])
        assert next_team["manager_user_id"] == bob and next_team["points_remaining"] == 0
        assert budget["history"][0]["point_cost"] == 0
        assert len(snapshots["undo"]["history"]) == 3
        assert len(snapshots["undo"]["rosters"]) == 2
        assert len(snapshots["completed"]["rosters"]) == 6
        assert snapshots["completed"]["state"]["deadline"] is None

    old_live = loaded["live"]["league_id"]
    with psycopg.connect(admin) as conn:
        conn.execute("UPDATE dl.league SET name = 'Renamed through UI' WHERE id = %s", (old_live,))
    reloaded = {row["scenario"]: row for row in scenarios.apply(database, ["live"])}
    assert reloaded["live"]["league_id"] != old_live
    assert all(reloaded[name]["league_id"] == row["league_id"]
               for name, row in loaded.items() if name != "live")
    with psycopg.connect(admin, row_factory=dict_row) as conn:
        assert not conn.execute("SELECT id FROM dl.league WHERE id = %s", (old_live,)).fetchone()
        assert conn.execute("SELECT id FROM dl.league WHERE id = %s", (unrelated,)).fetchone()
        assert conn.execute(
            "SELECT (SELECT count(*) FROM dl.app_user) AS users, "
            "(SELECT count(*) FROM dl.draftable) AS catalog, "
            "(SELECT count(*) FROM dl.asset) AS assets"
        ).fetchone() == before

    # A later failing fixture rolls back an earlier successful replacement too.
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "setup.sql").write_text(
        (scenarios.SCENARIO_DIR / "setup.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fixture_dir / "ready.sql").write_text("SELECT 1 / 0;\n")
    monkeypatch.setattr(scenarios, "SCENARIO_DIR", fixture_dir)
    with pytest.raises(psycopg.errors.DivisionByZero):
        scenarios.apply(database, ["setup", "ready"])
    with psycopg.connect(admin, row_factory=dict_row) as conn:
        actual = conn.execute("SELECT slug, league_id FROM dl_test.scenario").fetchall()
        assert {r["slug"]: r["league_id"] for r in actual} == {
            name: row["league_id"] for name, row in reloaded.items()
        }

    custom = tmp_path / "custom.sql"
    custom.write_text("UPDATE dl.league SET revision = revision + 1 WHERE id = "
                      f"{unrelated};\nSELECT 1 / 0;\n")
    with pytest.raises(psycopg.errors.DivisionByZero):
        scenarios.apply(database, file=custom)
    with psycopg.connect(admin) as conn:
        assert conn.execute("SELECT revision FROM dl.league WHERE id = %s", (unrelated,))\
            .fetchone()[0] == 0
        conn.execute("DELETE FROM dl.league WHERE id = %s", (unrelated,))
