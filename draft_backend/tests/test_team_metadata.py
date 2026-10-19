"""Team identity contracts, permissions, readiness, and concurrent setup writes."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from draft_backend import app as module
from draft_backend.config import Settings


def login(client, username="alice"):
    response = client.post(
        "/auth/test",
        json={"username": username},
        headers={"X-Test-Login": "1"},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]


def rpc(client, name, *, expected=200, **arguments):
    response = client.post("/api/rpc/" + name, json=arguments)
    assert response.status_code == expected, response.text
    return response.json()["result"] if expected == 200 else response.json()


def snapshot(client, league):
    response = client.get(f"/api/leagues/{league}")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def application(monkeypatch):
    db = SimpleNamespace(
        open=AsyncMock(),
        close=AsyncMock(),
        is_test_database=AsyncMock(return_value=True),
        test_user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        write=AsyncMock(return_value=[{"result": ""}]),
    )
    monkeypatch.setattr(module, "Database", lambda _: db)
    monkeypatch.setattr(module, "migrate", lambda *args, **kwargs: [])
    settings = Settings(
        database_url="postgresql://unused",
        migration_database_url="postgresql://unused",
        session_secret="x" * 40,
        public_base_url="http://testserver",
        app_env="test",
        test_login_enabled=True,
        cookie_secure=False,
    )
    return module.create_app(settings), db


def test_metadata_http_contract_and_session_boundary(application):
    app, db = application
    body = {"p_league": 2, "p_revision": 3, "p_team": 4, "p_name": "Team", "p_color_rgb": 0}
    path = "/api/rpc/set_team_metadata"
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        login(client)
        assert client.post(path, json=body, headers={"X-CSRF-Token": "bad"}).status_code == 403
        assert client.post(path, json=body, headers={"Origin": "https://other"}).status_code == 403
        for key in ("actor_user_id", "p_manager", "p_point_budget", "p_draft_position"):
            assert client.post(path, json={**body, key: 99}).status_code == 422
        db.write.assert_not_awaited()
        for color in (0, 0x123ABC, 0xFFFFFF):
            response = client.post(path, json={**body, "p_color_rgb": color})
            assert response.status_code == 200
            assert response.json() == {"result": None}
            db.write.assert_awaited_with(
                1,
                "SELECT dl.set_team_metadata(%s, %s, %s, %s, %s) AS result",
                (2, 3, 4, "Team", color),
            )


@pytest.mark.parametrize(
    "key, invalid",
    [
        ("p_color_rgb", -1),
        ("p_color_rgb", 0x1000000),
        ("p_color_rgb", True),
        ("p_color_rgb", 1.5),
        ("p_color_rgb", "123"),
        ("p_color_rgb", "#abcdef"),
        ("p_color_rgb", None),
        ("p_name", None),
        ("p_name", ""),
    ],
)
def test_metadata_http_rejects_invalid_fields(application, key, invalid):
    app, db = application
    body = {"p_league": 2, "p_revision": 3, "p_team": 4, "p_name": "Team", "p_color_rgb": 0}
    with TestClient(app) as client:
        login(client)
        assert (
            client.post(
                "/api/rpc/set_team_metadata",
                json={**body, key: invalid},
            ).status_code
            == 422
        )
        db.write.assert_not_awaited()


def test_metadata_http_requires_both_fields_and_supports_pending_team_names(application):
    app, db = application
    with TestClient(app) as client:
        login(client)
        metadata = {"p_league": 2, "p_revision": 3, "p_team": 4, "p_name": "Team", "p_color_rgb": 0}
        for missing in ("p_name", "p_color_rgb"):
            body = {key: value for key, value in metadata.items() if key != missing}
            assert client.post("/api/rpc/set_team_metadata", json=body).status_code == 422
        db.write.assert_not_awaited()

        body = {"p_league": 2, "p_revision": 3, "p_manager": 5, "p_point_budget": 100}
        assert client.post("/api/rpc/add_team", json=body).status_code == 200
        db.write.assert_awaited_with(
            1,
            "SELECT dl.add_team(%s, %s, %s, %s, %s) AS result",
            (2, 3, None, 5, 100),
        )
        body.update(p_team=4)
        assert client.post("/api/rpc/update_team", json=body).status_code == 422
        body.update(p_name=None)
        assert client.post("/api/rpc/update_team", json=body).status_code == 200
        db.write.assert_awaited_with(
            1,
            "SELECT dl.update_team(%s, %s, %s, %s, %s, %s) AS result",
            (2, 3, 4, None, 5, 100),
        )


@pytest.mark.integration
def test_managers_choose_metadata_commissioner_corrects_and_start_locks_it(client):
    login(client)
    users = {row["display_name"]: row["id"] for row in client.get("/api/users").json()}
    league = rpc(client, "create_league", p_name="Metadata " + uuid4().hex, p_roster_size=1)
    first = rpc(
        client,
        "add_team",
        p_league=league,
        p_revision=0,
        p_manager=users["Bob"],
        p_point_budget=100,
    )
    second = rpc(
        client,
        "add_team",
        p_league=league,
        p_revision=1,
        p_name="Provisional",
        p_manager=users["Carol"],
        p_point_budget=100,
    )
    rpc(client, "set_draft_order", p_league=league, p_revision=2, p_teams=[first, second])
    initial = snapshot(client, league)
    assert initial["teams"][0]["name"] is None
    assert all(team["color_rgb"] is None for team in initial["teams"])
    assert not any(team["metadata_complete"] for team in initial["teams"])
    assert all(team["can_set_metadata"] for team in initial["teams"])
    assert initial["state"]["can_start"] is False
    assert not any(action["kind"] == "start" for action in initial["actions"])
    rpc(client, "draft_action", expected=409, p_league=league, p_revision=3, p_kind="start")

    login(client, "bob")
    assert [team["can_set_metadata"] for team in snapshot(client, league)["teams"]] == [True, False]
    rpc(
        client,
        "set_team_metadata",
        expected=409,
        p_league=league,
        p_revision=3,
        p_team=second,
        p_name="Wrong team",
        p_color_rgb=1,
    )
    assert (
        rpc(
            client,
            "set_team_metadata",
            p_league=league,
            p_revision=3,
            p_team=first,
            p_name=" \tShared name\n ",
            p_color_rgb=0,
        )
        is None
    )
    assert snapshot(client, league)["teams"][0]["name"] == "Shared name"
    assert snapshot(client, league)["state"]["can_start"] is False

    login(client, "carol")
    # Duplicate names/colors are explicitly accepted; the commissioner resolves them manually.
    rpc(
        client,
        "set_team_metadata",
        p_league=league,
        p_revision=4,
        p_team=second,
        p_name="Shared name",
        p_color_rgb=0,
    )
    assert snapshot(client, league)["state"]["can_start"] is True
    login(client)
    rpc(
        client,
        "set_team_metadata",
        p_league=league,
        p_revision=5,
        p_team=second,
        p_name="Commissioner correction",
        p_color_rgb=0xFFFFFF,
    )
    login(client, "bob")
    rpc(
        client,
        "set_team_metadata",
        p_league=league,
        p_revision=6,
        p_team=first,
        p_name="Shared name",
        p_color_rgb=0,
    )
    assert rpc(
        client,
        "set_team_metadata",
        expected=409,
        p_league=league,
        p_revision=6,
        p_team=first,
        p_name="Stale edit",
        p_color_rgb=1,
    ) == {"detail": "stale_revision"}
    login(client)
    rpc(client, "draft_action", p_league=league, p_revision=7, p_kind="start")
    locked = snapshot(client, league)
    assert locked["revision"] == 8
    assert locked["state"]["status"] == "drafting"
    assert not any(team["can_set_metadata"] for team in locked["teams"])
    for username in ("alice", "bob"):
        login(client, username)
        rpc(
            client,
            "set_team_metadata",
            expected=409,
            p_league=league,
            p_revision=8,
            p_team=first,
            p_name="Too late",
            p_color_rgb=1,
        )
    client.cookies.clear()
    public = snapshot(client, league)
    assert public["revision"] == 8
    assert [team["color_rgb"] for team in public["teams"]] == [0, 0xFFFFFF]
    assert all(team["metadata_complete"] for team in public["teams"])
    assert not any(team["can_set_metadata"] for team in public["teams"])


class MetadataLeague:
    def __init__(self, database):
        self.database = database
        with psycopg.connect(database.admin) as conn:
            self.users = [
                conn.execute(
                    "INSERT INTO dl.app_user (issuer, subject, display_name) "
                    "VALUES ('urn:metadata:test', %s, %s) RETURNING id",
                    (uuid4().hex, name),
                ).fetchone()[0]
                for name in ("Commissioner", "First manager", "Second manager", "Outsider")
            ]
            self.owner, self.first_manager, self.second_manager, self.outsider = self.users
            self.league = conn.execute(
                "INSERT INTO dl.league (name, commissioner_user_id, roster_size) "
                "VALUES ('Metadata SQL', %s, 1) RETURNING id",
                (self.owner,),
            ).fetchone()[0]
            self.teams = [
                conn.execute(
                    "INSERT INTO dl.team "
                    "(league_id, manager_user_id, draft_position, point_budget) "
                    "VALUES (%s, %s, %s, 100) RETURNING id",
                    (self.league, manager, position),
                ).fetchone()[0]
                for position, manager in enumerate(self.users[1:3], 1)
            ]

    def read(self, query, params=()):
        with psycopg.connect(self.database.admin) as conn:
            return conn.execute(query, params or None).fetchall()

    def revision(self):
        return self.read("SELECT revision FROM dl.league WHERE id=%s", (self.league,))[0][0]

    @contextmanager
    def actor(self, user):
        with psycopg.connect(self.database.gateway) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            conn.execute("SET LOCAL ROLE dl_api")
            conn.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"user_id": user}),),
            )
            yield conn

    def call(self, name, params, *, user, barrier=None):
        query = sql.SQL("SELECT dl.{}({})").format(
            sql.Identifier(name),
            sql.SQL(", ").join(sql.Placeholder() for _ in params),
        )
        for attempt in range(5):
            try:
                with self.actor(user) as conn:
                    if barrier is not None and attempt == 0:
                        conn.execute("SELECT revision FROM dl.league WHERE id=%s", (self.league,))
                        barrier.wait(timeout=15)
                    result = conn.execute(query, params).fetchone()[0]
                return result
            except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
                if attempt == 4:
                    raise

    def set_metadata(self, team, name, color, *, user, revision=None, barrier=None):
        return self.call(
            "set_team_metadata",
            (
                self.league,
                self.revision() if revision is None else revision,
                team,
                name,
                color,
            ),
            user=user,
            barrier=barrier,
        )


@pytest.fixture
def metadata_league(test_database):
    return MetadataLeague(test_database)


@contextmanager
def rejected(code, message=None):
    with pytest.raises(psycopg.Error) as caught:
        yield
    assert caught.value.sqlstate == code, str(caught.value)
    if message is not None:
        assert caught.value.diag.message_primary == message


@pytest.mark.integration
def test_metadata_sql_validation_scope_and_legacy_commissioner_controls(metadata_league):
    state = metadata_league
    first, second = state.teams
    for name, color in (
        (None, 0),
        ("", 0),
        ("   ", 0),
        ("\t\n", 0),
        ("Name", None),
        ("Name", -1),
        ("Name", 0x1000000),
    ):
        with rejected("PT400", "invalid_team_metadata"):
            state.set_metadata(first, name, color, user=state.first_manager)
    for user in (state.outsider, state.second_manager):
        with rejected("PT409", "action_not_permitted"):
            state.set_metadata(first, "Unauthorized", 0, user=user)
    with rejected("PT401", "sign_in_required"):
        state.set_metadata(first, "Anonymous", 0, user=None)
    with rejected("PT404", "team_not_found"):
        state.set_metadata(2147483647, "Missing", 0, user=state.owner)
    other = MetadataLeague(state.database)
    with rejected("PT404", "team_not_found"):
        state.set_metadata(other.teams[0], "Wrong league", 0, user=state.owner)
    with rejected("PT404", "league_not_found"):
        state.call("set_team_metadata", (2147483647, 0, first, "Missing", 0), user=state.owner)
    with rejected("PT409", "action_not_permitted"):
        state.call(
            "update_team",
            (state.league, 0, first, "Name", state.outsider, 999),
            user=state.first_manager,
        )
    assert state.revision() == 0

    state.set_metadata(first, "  First  ", 0x123ABC, user=state.first_manager)
    state.set_metadata(second, "Second", 0xFFFFFF, user=state.owner)
    assert state.read(
        "SELECT can_start FROM dl.draft_state WHERE league_id=%s", (state.league,)
    ) == [(True,)]
    # Legacy commissioner edits still work, preserve RGB, and can leave a pending name.
    state.call("update_team", (state.league, 2, first, None, state.outsider, 75), user=state.owner)
    assert state.read(
        "SELECT name, color_rgb, manager_user_id, point_budget FROM dl.team WHERE id=%s", (first,)
    ) == [(None, 0x123ABC, state.outsider, 75)]
    assert state.read(
        "SELECT can_start FROM dl.draft_state WHERE league_id=%s", (state.league,)
    ) == [(False,)]
    with rejected("PT409", "action_not_permitted"):
        state.set_metadata(first, "Previous manager", 1, user=state.first_manager)
    state.set_metadata(first, "  Replacement  ", 0, user=state.outsider)
    assert state.read(
        "SELECT name, color_rgb, point_budget, draft_position FROM dl.team WHERE id=%s", (first,)
    ) == [("Replacement", 0, 75, 1)]
    with state.actor(None) as conn:
        teams = conn.execute(
            "SELECT teams FROM dl.league_snapshot WHERE league_id=%s", (state.league,)
        ).fetchone()[0]
        assert all(team["metadata_complete"] for team in teams)
        assert all(team["can_set_metadata"] is False for team in teams)


@pytest.mark.integration
def test_metadata_sql_boundary_and_column_constraints(metadata_league):
    state = metadata_league
    signature = "dl.set_team_metadata(integer,integer,integer,text,integer)"
    assert state.read(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), "
        "has_function_privilege('dl_api', p.oid, 'EXECUTE'), "
        "EXISTS (SELECT FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) "
        "WHERE grantee = 0 AND privilege_type = 'EXECUTE') "
        "FROM pg_proc p WHERE p.oid=%s::regprocedure",
        (signature,),
    ) == [(True, "dl_owner", True, False)]
    with rejected("42501"):
        with state.actor(state.first_manager) as conn:
            conn.execute("UPDATE dl.team SET color_rgb=1 WHERE id=%s", (state.teams[0],))
    for color in (-1, 0x1000000):
        with rejected("23514"):
            with psycopg.connect(state.database.admin) as conn:
                conn.execute("UPDATE dl.team SET color_rgb=%s WHERE id=%s", (color, state.teams[0]))
    for name in ("   ", "\t\n"):
        with rejected("23514"):
            with psycopg.connect(state.database.admin) as conn:
                conn.execute("UPDATE dl.team SET name=%s WHERE id=%s", (name, state.teams[0]))


@pytest.mark.integration
def test_metadata_notifies_accounts_only_after_commit_and_failed_edits_do_not_wake(metadata_league):
    state = metadata_league
    with psycopg.connect(state.database.gateway, autocommit=True) as listener:
        for account in state.users:
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(f"dl_user_{account}")))

        def wakes():
            messages = list(listener.notifies(timeout=0.1))
            assert all(message.payload == "" for message in messages)
            return sorted(message.channel for message in messages)

        query = "SELECT dl.set_team_metadata(%s, %s, %s, %s, %s)"
        with state.actor(state.first_manager) as conn:
            conn.execute(query, (state.league, 0, state.teams[0], "First", 0))
            assert wakes() == []
        assert wakes() == sorted(f"dl_user_{account}" for account in state.users[:3])
        assert state.revision() == 1
        with state.actor(state.owner) as conn:
            conn.execute(query, (state.league, 1, state.teams[1], "Rollback", 1))
            assert wakes() == []
            conn.rollback()
        assert wakes() == []
        with rejected("PT409", "stale_revision"):
            state.set_metadata(state.teams[0], "Stale", 2, user=state.first_manager, revision=0)
        assert wakes() == []
        assert state.revision() == 1
        assert state.read("SELECT name, color_rgb FROM dl.team WHERE id=%s", (state.teams[1],)) == [
            (None, None)
        ]


@pytest.mark.integration
@pytest.mark.parametrize("start_race", [False, True])
def test_metadata_concurrent_writers_respect_the_original_revision(metadata_league, start_race):
    state = metadata_league
    if start_race:
        for team in state.teams:
            state.set_metadata(team, "Ready", 0, user=state.owner)
    revision = state.revision()
    barrier = Barrier(2)

    def edit_first():
        return state.set_metadata(
            state.teams[0],
            "Concurrent first",
            1,
            user=state.first_manager,
            revision=revision,
            barrier=barrier,
        )

    def second_write():
        if start_race:
            return state.call(
                "draft_action", (state.league, revision, "start"), user=state.owner, barrier=barrier
            )
        return state.set_metadata(
            state.teams[1],
            "Concurrent second",
            2,
            user=state.second_manager,
            revision=revision,
            barrier=barrier,
        )

    def result(operation):
        try:
            operation()
            return "saved"
        except psycopg.Error as exc:
            return exc.sqlstate, exc.diag.message_primary

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(result, operation) for operation in (edit_first, second_write)]
        results = [job.result(timeout=30) for job in jobs]
    assert results.count("saved") == 1
    assert ("PT409", "stale_revision") in results
    assert state.revision() == revision + 1
    if start_race:
        status = state.read(
            "SELECT status FROM dl.draft_state WHERE league_id=%s", (state.league,)
        )[0][0]
        assert status == ("drafting" if results[1] == "saved" else "setup")
    else:
        assert state.read(
            "SELECT count(*) FROM dl.team WHERE league_id=%s AND name IS NOT NULL", (state.league,)
        ) == [(1,)]
