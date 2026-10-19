"""Favorites are ordered account lists with owner-only, revisioned mutations."""

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
    response = client.post("/auth/test", json={"username": username}, headers={"X-Test-Login": "1"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]


def rpc(client, name, *, expected=200, **arguments):
    response = client.post("/api/rpc/" + name, json=arguments)
    assert response.status_code == expected, response.text
    return response.json()["result"] if expected == 200 else response.json()


def snapshot(client, list_id):
    response = client.get(f"/api/favorites-lists/{list_id}")
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
        read=AsyncMock(return_value=[]),
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


WRITES = [
    ("create_favorites_list", {"p_name": "Targets"}, ("Targets",)),
    (
        "rename_favorites_list",
        {"p_list": 2, "p_revision": 3, "p_name": "Targets"},
        (2, 3, "Targets"),
    ),
    ("delete_favorites_list", {"p_list": 2, "p_revision": 3}, (2, 3)),
    (
        "add_favorites_entry",
        {"p_list": 2, "p_revision": 3, "p_draftable": 4},
        (2, 3, 4),
    ),
    (
        "remove_favorites_entry",
        {"p_list": 2, "p_revision": 3, "p_draftable": 4},
        (2, 3, 4),
    ),
    (
        "set_favorites_order",
        {"p_list": 2, "p_revision": 3, "p_draftables": [5, 4]},
        (2, 3, [5, 4]),
    ),
]


@pytest.mark.parametrize("name, body, params", WRITES)
def test_favorites_http_mutations_use_session_identity_and_csrf(application, name, body, params):
    app, db = application
    path = "/api/rpc/" + name
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        login(client)
        assert client.post(path, json=body, headers={"X-CSRF-Token": "bad"}).status_code == 403
        assert client.post(path, json=body, headers={"Origin": "https://other"}).status_code == 403
        for key in ("owner_user_id", "actor_user_id", "p_manager"):
            assert client.post(path, json={**body, key: 99}).status_code == 422
        db.write.assert_not_awaited()
        db.write.return_value = [{"result": 17 if name == "create_favorites_list" else ""}]
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"result": 17 if name == "create_favorites_list" else None}
        placeholders = ", ".join("%s" for _ in params)
        db.write.assert_awaited_once_with(1, f"SELECT dl.{name}({placeholders}) AS result", params)


@pytest.mark.parametrize(
    "name, key, invalid",
    [
        ("rename_favorites_list", "p_list", True),
        ("rename_favorites_list", "p_list", "2"),
        ("rename_favorites_list", "p_list", 0),
        ("rename_favorites_list", "p_list", 2147483648),
        ("rename_favorites_list", "p_revision", True),
        ("rename_favorites_list", "p_revision", -1),
        ("rename_favorites_list", "p_revision", 3.5),
        ("rename_favorites_list", "p_name", ""),
        ("rename_favorites_list", "p_name", None),
        ("create_favorites_list", "p_name", None),
        ("add_favorites_entry", "p_draftable", "4"),
        ("add_favorites_entry", "p_draftable", False),
        ("remove_favorites_entry", "p_draftable", 0),
        ("set_favorites_order", "p_draftables", None),
        ("set_favorites_order", "p_draftables", [1, True]),
        ("set_favorites_order", "p_draftables", ["1"]),
        ("set_favorites_order", "p_draftables", [2147483648]),
    ],
)
def test_favorites_http_rejects_invalid_input_before_writing(application, name, key, invalid):
    app, db = application
    body = next(body for candidate, body, _ in WRITES if candidate == name)
    with TestClient(app) as client:
        login(client)
        assert client.post("/api/rpc/" + name, json={**body, key: invalid}).status_code == 422
        db.write.assert_not_awaited()


def test_favorites_public_read_contract_and_id_validation(application):
    app, db = application
    header = {"id": 2, "owner_user_id": 7, "name": "Targets", "revision": 3}
    detail = {**header, "entries": [{"draftable_id": 5, "ordinal": 1}]}
    with TestClient(app) as client:
        # TODO: These reads deliberately require no login until privacy is implemented.
        db.read.return_value = [header]
        assert client.get("/api/favorites-lists").json() == [header]
        assert db.read.await_args.args[0] is None
        response = client.get("/api/favorites-lists", params={"owner_user_id": "7"})
        assert response.status_code == 200
        assert response.json() == [header]
        assert db.read.await_args.args[2] == (7,)
        db.read.return_value = [detail]
        response = client.get("/api/favorites-lists/2")
        assert response.status_code == 200
        assert response.json() == detail
        db.read.return_value = []
        response = client.get("/api/favorites-lists/2")
        assert response.status_code == 404
        assert response.json() == {"detail": "favorites_list_not_found"}

        db.read.reset_mock()
        for path in (
            "/api/favorites-lists/0",
            "/api/favorites-lists/2147483648",
            "/api/favorites-lists?owner_user_id=0",
            "/api/favorites-lists?owner_user_id=2147483648",
            "/api/favorites-lists?owner_user_id=not-an-id",
        ):
            assert client.get(path).status_code == 422
        db.read.assert_not_awaited()


@pytest.mark.integration
def test_favorites_http_lifecycle_and_intentionally_public_reads(client):
    owner = login(client)["id"]
    draftables = [row["id"] for row in client.get("/api/draftables").json()[:3]]
    first, second, third = draftables
    name = "Favorites " + uuid4().hex
    list_id = rpc(client, "create_favorites_list", p_name=name)
    other_list = rpc(client, "create_favorites_list", p_name=name)
    initial = {"id": list_id, "owner_user_id": owner, "name": name, "revision": 0, "entries": []}
    assert snapshot(client, list_id) == initial
    assert (
        rpc(client, "set_favorites_order", p_list=other_list, p_revision=0, p_draftables=[]) is None
    )
    assert snapshot(client, other_list)["revision"] == 1

    for revision, draftable in enumerate(draftables):
        assert (
            rpc(
                client,
                "add_favorites_entry",
                p_list=list_id,
                p_revision=revision,
                p_draftable=draftable,
            )
            is None
        )
    assert rpc(
        client,
        "add_favorites_entry",
        expected=409,
        p_list=list_id,
        p_revision=3,
        p_draftable=first,
    ) == {"detail": "already_exists_or_claimed"}
    assert (
        rpc(
            client,
            "set_favorites_order",
            p_list=list_id,
            p_revision=3,
            p_draftables=[third, first, second],
        )
        is None
    )
    assert snapshot(client, list_id)["entries"] == [
        {"draftable_id": draftable, "ordinal": ordinal}
        for ordinal, draftable in enumerate([third, first, second], 1)
    ]
    rpc(client, "remove_favorites_entry", p_list=list_id, p_revision=4, p_draftable=first)
    rpc(client, "add_favorites_entry", p_list=list_id, p_revision=5, p_draftable=first)
    assert snapshot(client, list_id)["entries"] == [
        {"draftable_id": third, "ordinal": 1},
        {"draftable_id": second, "ordinal": 3},
        {"draftable_id": first, "ordinal": 4},
    ]
    rpc(client, "rename_favorites_list", p_list=list_id, p_revision=6, p_name="Renamed")
    assert rpc(
        client,
        "rename_favorites_list",
        expected=409,
        p_list=list_id,
        p_revision=6,
        p_name="Stale name",
    ) == {"detail": "stale_revision"}
    assert snapshot(client, other_list)["revision"] == 1

    login(client, "bob")
    assert rpc(
        client,
        "delete_favorites_list",
        expected=409,
        p_list=list_id,
        p_revision=7,
    ) == {"detail": "action_not_permitted"}
    client.cookies.clear()
    # TODO: Replace these public-read expectations when favorites read restrictions land.
    public = snapshot(client, list_id)
    assert public["name"] == "Renamed"
    assert public["revision"] == 7
    headers = client.get("/api/favorites-lists").json()
    assert [row["id"] for row in headers] == sorted(row["id"] for row in headers)
    assert {key: value for key, value in public.items() if key != "entries"} in headers
    filtered = client.get("/api/favorites-lists", params={"owner_user_id": owner}).json()
    assert all(row["owner_user_id"] == owner for row in filtered)
    assert {list_id, other_list} <= {row["id"] for row in filtered}
    missing = client.get("/api/favorites-lists/2147483647")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "favorites_list_not_found"}
    for path in (
        "/api/favorites-lists/0",
        "/api/favorites-lists/2147483648",
        "/api/favorites-lists?owner_user_id=0",
        "/api/favorites-lists?owner_user_id=not-an-id",
    ):
        assert client.get(path).status_code == 422

    login(client)
    rpc(client, "delete_favorites_list", p_list=list_id, p_revision=7)
    assert client.get(f"/api/favorites-lists/{list_id}").status_code == 404
    assert snapshot(client, other_list)["entries"] == []


class Favorites:
    def __init__(self, database):
        self.database = database
        with psycopg.connect(database.admin) as conn:
            self.owner, self.other = [
                conn.execute(
                    "INSERT INTO dl.app_user (issuer, subject, display_name) "
                    "VALUES ('urn:favorites:test', %s, %s) RETURNING id",
                    (uuid4().hex, name),
                ).fetchone()[0]
                for name in ("List owner", "Other account")
            ]
            self.draftables = [
                row[0] for row in conn.execute("SELECT id FROM dl.draftable ORDER BY id LIMIT 3")
            ]
        self.list_id = self.call("create_favorites_list", ("Priorities",), user=self.owner)

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
            sql.Identifier(name), sql.SQL(", ").join(sql.Placeholder() for _ in params)
        )
        for attempt in range(5):
            try:
                with self.actor(user) as conn:
                    if barrier is not None and attempt == 0:
                        conn.execute(
                            "SELECT revision FROM dl.favorites_list WHERE id=%s", (self.list_id,)
                        )
                        barrier.wait(timeout=15)
                    result = conn.execute(query, params).fetchone()[0]
                return result
            except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
                if attempt == 4:
                    raise

    def read(self, query, params=()):
        with psycopg.connect(self.database.admin) as conn:
            return conn.execute(query, params or None).fetchall()

    def snapshot(self):
        return self.read(
            "SELECT to_jsonb(s) FROM dl.favorites_list_snapshot s WHERE id=%s", (self.list_id,)
        )[0][0]


@pytest.fixture
def favorites(test_database):
    return Favorites(test_database)


@contextmanager
def rejected(code, message=None):
    with pytest.raises(psycopg.Error) as caught:
        yield
    assert caught.value.sqlstate == code, str(caught.value)
    if message is not None:
        assert caught.value.diag.message_primary == message


@pytest.mark.integration
def test_favorites_sql_all_mutations_enforce_owner_revision_and_existing_list(favorites):
    state = favorites
    first = state.draftables[0]
    state.call("add_favorites_entry", (state.list_id, 0, first), user=state.owner)
    operations = [
        ("rename_favorites_list", ("Renamed",)),
        ("delete_favorites_list", ()),
        ("add_favorites_entry", (state.draftables[1],)),
        ("remove_favorites_entry", (first,)),
        ("set_favorites_order", ([first],)),
    ]
    initial = state.snapshot()
    with rejected("PT401", "sign_in_required"):
        state.call("create_favorites_list", ("Anonymous",), user=None)
    for name, extra in operations:
        for user, revision, code, message in (
            (None, 1, "PT401", "sign_in_required"),
            (state.other, 1, "PT409", "action_not_permitted"),
            (state.owner, 0, "PT409", "stale_revision"),
        ):
            with rejected(code, message):
                state.call(name, (state.list_id, revision, *extra), user=user)
            assert state.snapshot() == initial
        with rejected("PT404", "favorites_list_not_found"):
            state.call(name, (2147483647, 0, *extra), user=state.owner)
    for revision in (None, -1):
        with rejected("PT409", "stale_revision"):
            state.call("delete_favorites_list", (state.list_id, revision), user=state.owner)
    assert state.snapshot() == initial


@pytest.mark.integration
def test_favorites_sql_validates_names_and_order_and_failed_edits_are_atomic(favorites):
    state = favorites
    for name in (None, "", " ", "\t\r\n"):
        with rejected("PT400"):
            state.call("create_favorites_list", (name,), user=state.owner)
        with rejected("PT400"):
            state.call("rename_favorites_list", (state.list_id, 0, name), user=state.owner)
    assert state.snapshot()["revision"] == 0
    first, second, third = state.draftables
    for revision, draftable in enumerate([first, second]):
        state.call("add_favorites_entry", (state.list_id, revision, draftable), user=state.owner)
    initial = state.snapshot()
    for order in (
        None,
        [],
        [first],
        [first, first],
        [first, third],
        [first, second, third],
        [first, None],
        [[first, second]],
    ):
        with rejected("PT400", "order_must_contain_every_draftable_once"):
            state.call("set_favorites_order", (state.list_id, 2, order), user=state.owner)
        assert state.snapshot() == initial
    with rejected("23505"):
        state.call("add_favorites_entry", (state.list_id, 2, first), user=state.owner)
    with rejected("23503"):
        state.call("add_favorites_entry", (state.list_id, 2, 2147483647), user=state.owner)
    with rejected("PT404", "favorites_entry_not_found"):
        state.call("remove_favorites_entry", (state.list_id, 2, third), user=state.owner)
    assert state.snapshot() == initial
    state.call("set_favorites_order", (state.list_id, 2, [second, first]), user=state.owner)
    assert state.snapshot()["entries"] == [
        {"draftable_id": second, "ordinal": 1},
        {"draftable_id": first, "ordinal": 2},
    ]
    for revision, draftable in enumerate([second, first], 3):
        state.call("remove_favorites_entry", (state.list_id, revision, draftable), user=state.owner)
    state.call("set_favorites_order", (state.list_id, 5, []), user=state.owner)
    assert state.snapshot()["entries"] == []
    assert state.snapshot()["revision"] == 6
    state.call("add_favorites_entry", (state.list_id, 6, first), user=state.owner)
    assert state.snapshot()["entries"] == [{"draftable_id": first, "ordinal": 1}]


@pytest.mark.integration
def test_favorites_relational_constraints_and_cascade(favorites):
    state = favorites
    first, second, _ = state.draftables
    state.call("add_favorites_entry", (state.list_id, 0, first), user=state.owner)
    with psycopg.connect(state.database.admin, autocommit=True) as conn:
        for list_id, draftable, ordinal, code in (
            (state.list_id, first, 2, "23505"),
            (state.list_id, second, 1, "23505"),
            (state.list_id, second, 0, "23514"),
            (state.list_id, second, -1, "23514"),
            (state.list_id, second, None, "23502"),
            (state.list_id, None, 2, "23502"),
            (state.list_id, 2147483647, 2, "23503"),
            (2147483647, second, 2, "23503"),
        ):
            with rejected(code):
                conn.execute(
                    "INSERT INTO dl.favorites_entry "
                    "(favorites_list_id, draftable_id, ordinal) VALUES (%s,%s,%s)",
                    (list_id, draftable, ordinal),
                )
        for name in (None, "", "\t\r\n "):
            with pytest.raises(psycopg.Error):
                conn.execute(
                    "INSERT INTO dl.favorites_list (owner_user_id, name) VALUES (%s,%s)",
                    (state.owner, name),
                )
        with rejected("23503"):
            conn.execute(
                "INSERT INTO dl.favorites_list (owner_user_id, name) VALUES (2147483647,'Invalid')"
            )
    with rejected("42501"):
        with state.actor(state.owner) as conn:
            conn.execute("UPDATE dl.favorites_list SET name='Bypass' WHERE id=%s", (state.list_id,))
    with rejected("42501"):
        with state.actor(state.owner) as conn:
            conn.execute(
                "DELETE FROM dl.favorites_entry WHERE favorites_list_id=%s", (state.list_id,)
            )
    state.call("delete_favorites_list", (state.list_id, 1), user=state.owner)
    assert (
        state.read("SELECT * FROM dl.favorites_entry WHERE favorites_list_id=%s", (state.list_id,))
        == []
    )
    assert state.read("SELECT id FROM dl.draftable WHERE id=%s", (first,)) == [(first,)]


@pytest.mark.integration
@pytest.mark.parametrize(
    "query",
    [
        "SELECT dl.favorites_admit(1, 0)",
        "SELECT dl.favorites_bump(1)",
        "SELECT dl.notify_favorites_list()",
        "SELECT dl.notify_favorites_entry()",
    ],
)
def test_favorites_internal_helpers_cannot_be_called_by_api(favorites, query):
    with rejected("42501"):
        with favorites.actor(favorites.owner) as conn:
            conn.execute(query)


@pytest.mark.integration
def test_favorites_survive_drafting_and_do_not_change_league_revisions(favorites):
    state = favorites
    first = state.draftables[0]
    state.call("add_favorites_entry", (state.list_id, 0, first), user=state.owner)
    original = state.snapshot()
    league = state.call("create_league", ("Favorites independent draft", 1), user=state.owner)
    team = state.call("add_team", (league, 0, "Team", state.owner, 100), user=state.owner)
    other_team = state.call(
        "add_team", (league, 1, "Other team", state.other, 100), user=state.owner
    )
    state.call("set_team_metadata", (league, 2, team, "Team", 0), user=state.owner)
    state.call("set_team_metadata", (league, 3, other_team, "Other team", 1), user=state.owner)
    state.call("set_draft_order", (league, 4, [team, other_team]), user=state.owner)
    state.call("draft_action", (league, 5, "start"), user=state.owner)
    state.call("draft_action", (league, 6, "pick", first), user=state.owner)
    assert state.snapshot() == original
    state.call("remove_favorites_entry", (state.list_id, 1, first), user=state.owner)
    assert state.read("SELECT revision FROM dl.league WHERE id=%s", (league,)) == [(7,)]
    assert state.read("SELECT draftable_id FROM dl.pick WHERE league_id=%s", (league,)) == [
        (first,)
    ]


@pytest.mark.integration
def test_concurrent_favorites_edits_have_one_winner_and_one_stale_revision(favorites):
    state = favorites
    barrier = Barrier(2)

    def add(draftable):
        try:
            state.call(
                "add_favorites_entry",
                (state.list_id, 0, draftable),
                user=state.owner,
                barrier=barrier,
            )
            return "saved"
        except psycopg.Error as exc:
            return exc.sqlstate, exc.diag.message_primary

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(add, draftable) for draftable in state.draftables[:2]]
        outcomes = [job.result(timeout=30) for job in jobs]
    assert outcomes.count("saved") == 1
    assert ("PT409", "stale_revision") in outcomes
    assert state.snapshot()["revision"] == 1
    assert state.snapshot()["entries"] == [
        {"draftable_id": state.draftables[outcomes.index("saved")], "ordinal": 1}
    ]


def assert_wakes(listener, *owners):
    messages = list(listener.notifies(timeout=0.1))
    assert all(message.payload == "" for message in messages)
    assert sorted(message.channel for message in messages) == sorted(
        f"dl_user_{owner}" for owner in owners
    )


@pytest.mark.integration
def test_favorites_notify_only_owner_after_commit_and_never_on_rollback(favorites):
    state = favorites
    first = state.draftables[0]
    with psycopg.connect(state.database.gateway, autocommit=True) as listener:
        for channel in (f"dl_user_{state.owner}", f"dl_user_{state.other}", "dl_changed"):
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        with state.actor(state.owner) as conn:
            extra = conn.execute("SELECT dl.create_favorites_list('Another list')").fetchone()[0]
            assert_wakes(listener)
        assert_wakes(listener, state.owner)
        for name, params in (
            ("rename_favorites_list", (state.list_id, 0, "Revised")),
            ("add_favorites_entry", (state.list_id, 1, first)),
            ("set_favorites_order", (state.list_id, 2, [first])),
            ("remove_favorites_entry", (state.list_id, 3, first)),
            ("delete_favorites_list", (extra, 0)),
        ):
            state.call(name, params, user=state.owner)
            assert_wakes(listener, state.owner)
        with state.actor(state.owner) as conn:
            conn.execute("SELECT dl.add_favorites_entry(%s, 4, %s)", (state.list_id, first))
            assert_wakes(listener)
            raise psycopg.Rollback
        assert_wakes(listener)
        assert state.snapshot()["revision"] == 4
        assert state.snapshot()["entries"] == []
        with rejected("PT409", "stale_revision"):
            state.call("delete_favorites_list", (state.list_id, 3), user=state.owner)
        assert_wakes(listener)
        state.call("add_favorites_entry", (state.list_id, 4, first), user=state.owner)
        assert_wakes(listener, state.owner)
        state.call("delete_favorites_list", (state.list_id, 5), user=state.owner)
        assert_wakes(listener, state.owner)
