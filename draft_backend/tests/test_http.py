"""Exercise the HTTP/session boundary and SSE resynchronization without a server."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from draft_backend import app as module
from draft_backend.config import Settings


@pytest.fixture
def application(monkeypatch):
    db = SimpleNamespace(
        open=AsyncMock(),
        close=AsyncMock(),
        ready=AsyncMock(),
        is_test_database=AsyncMock(return_value=True),
        test_user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        write=AsyncMock(return_value=[{"result": ""}]),
        read=AsyncMock(return_value=[]),
        image=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(module, "Database", lambda _: db)
    monkeypatch.setattr(module, "migrate", lambda *args, **kwargs: [])
    settings = Settings(
        database_url="postgresql://unused",
        migration_database_url="postgresql://migration-unused",
        session_secret="x" * 40,
        app_env="test",
        test_login_enabled=True,
        cookie_secure=False,
        public_base_url="http://testserver",
        sse_heartbeat_seconds=0.01,
    )
    return module.create_app(settings), db


def test_readiness_requires_database_without_a_shared_listener_or_authentication(application):
    app, db = application
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"ready": True}
        assert response.headers["cache-control"] == "no-store"
        db.ready.assert_awaited_once()
        assert client.get("/healthz").json() == {"status": "ok"}
        assert not hasattr(app.state, "events")


def test_startup_migrates_before_opening_the_runtime_pool(application, monkeypatch):
    app, db = application
    calls = []

    def migrate(conninfo, directory, **kwargs):
        assert conninfo == app.state.settings.migration_database_url
        assert directory == module.MIGRATION_DIRECTORY
        assert kwargs == {"schema": "dl", "role": "dl_owner"}
        calls.append("migrate")

    async def open_database():
        calls.append("open")

    monkeypatch.setattr(module, "migrate", migrate)
    db.open.side_effect = open_database
    with TestClient(app) as client:
        assert calls == ["migrate", "open"]
        assert client.get("/ready").status_code == 200


def test_failed_migration_prevents_startup_and_pool_creation(application, monkeypatch):
    app, db = application

    def fail(*args, **kwargs):
        raise psycopg.errors.DivisionByZero("migration failed")

    monkeypatch.setattr(module, "migrate", fail)
    with pytest.raises(psycopg.errors.DivisionByZero):
        with TestClient(app):
            pytest.fail("The application must not become ready")
    db.open.assert_not_awaited()
    assert not hasattr(app.state, "db")


@pytest.mark.integration
def test_startup_failure_retry_and_migration_connection_cleanup(
    test_database, tmp_path, monkeypatch
):
    suffix = uuid4().hex
    name = f"startup_{suffix}"
    table = f"dl.{name}"
    monkeypatch.setattr(module, "MIGRATION_DIRECTORY", tmp_path)
    settings = Settings(
        database_url=test_database.gateway,
        migration_database_url=make_conninfo(test_database.admin, application_name=name),
        session_secret="x" * 40,
        public_base_url="http://testserver",
        app_env="test", test_login_enabled=True, cookie_secure=False,
    )
    options = {"loop_factory": asyncio.SelectorEventLoop}
    with psycopg.connect(test_database.admin, autocommit=True) as conn:
        existing_history = conn.execute(
            "SELECT version FROM dl.schema_migrations ORDER BY version"
        ).fetchall()
        prefix = max((version for version, in existing_history), default="")
        first = f"{prefix}_test_{suffix}_1.sql"
        second = f"{prefix}_test_{suffix}_2.sql"
        (tmp_path / first).write_text(
            f"CREATE TABLE {table} (id integer);", encoding="utf-8"
        )
        later = tmp_path / second
        later.write_text(
            f"ALTER TABLE {table} ADD COLUMN value integer; SELECT 1 / 0;", encoding="utf-8"
        )
        try:
            app = module.create_app(settings)
            with pytest.raises(psycopg.errors.DivisionByZero):
                with TestClient(app, backend_options=options):
                    pytest.fail("The application must not become ready")
            assert not hasattr(app.state, "db")
            assert conn.execute(
                "SELECT version FROM dl.schema_migrations ORDER BY version"
            ).fetchall() == existing_history + [(first,)]
            assert conn.execute(
                "SELECT count(*) FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attname = 'value'", (table,)
            ).fetchone() == (0,)

            later.write_text(f"ALTER TABLE {table} ADD COLUMN value integer;", encoding="utf-8")
            with TestClient(module.create_app(settings), backend_options=options) as client:
                assert client.get("/ready").status_code == 200
                assert conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (name,)
                ).fetchone() == (0,)
                assert conn.execute(
                    "SELECT version FROM dl.schema_migrations ORDER BY version"
                ).fetchall() == existing_history + [(first,), (second,)]
                assert conn.execute(f"SELECT value FROM {table}").fetchall() == []
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(
                "DELETE FROM dl.schema_migrations WHERE version IN (%s, %s)", (first, second)
            )


def test_readiness_hides_database_error_details_and_recovers(application):
    app, db = application
    db.ready.side_effect = psycopg.OperationalError("private credentials and host details")
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "not_ready"}
        db.ready.side_effect = None
        assert client.get("/ready").status_code == 200


def test_readiness_cancels_an_unresponsive_database_check(application):
    app, db = application
    cancelled = []

    async def wait_forever():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    db.ready.side_effect = wait_forever
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "not_ready"}
    assert cancelled == [True]


def test_public_images_cache_bytes_and_support_conditional_requests(application):
    app, db = application
    digest = "a" * 64
    db.image.return_value = {"content_type": "image/png", "data": b"png bytes"}
    with TestClient(app) as client:
        assert client.get("/api/draftables").status_code == 200
        response = client.get(f"/images/{digest}")
        assert response.status_code == 200
        assert response.content == b"png bytes"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert response.headers["etag"] == f'"{digest}"'
        assert response.headers["x-content-type-options"] == "nosniff"
        for condition in [f'"{digest}"', f'"other", W/"{digest}"', "*"]:
            response = client.get(f"/images/{digest}", headers={"If-None-Match": condition})
            assert response.status_code == 304
            assert response.content == b""
        assert client.get(f"/images/{digest}", headers={"If-None-Match": '"other"'}).content
        response = client.head(f"/images/{digest}")
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-length"] == "9"
        db.image.assert_awaited_once_with(digest)
        client.post("/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"})
        assert "draft_session" in client.cookies
        assert "set-cookie" not in client.get(f"/images/{digest}").headers
        assert "set-cookie" not in client.get(
            f"/images/{digest}", headers={"If-None-Match": "*"}
        ).headers


def test_missing_and_invalid_images_are_not_cached(application):
    app, db = application
    with TestClient(app) as client:
        assert client.get("/images/bad-hash").status_code == 404
        db.image.assert_not_awaited()
        for _ in range(2):
            response = client.get("/images/" + "b" * 64)
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
        assert db.image.await_count == 2
        assert app.state.images == {}


@pytest.mark.parametrize(
    "path", ["/api/users", "/api/draftables", "/api/leagues", "/api/leagues/1"]
)
def test_public_reads_use_optional_session_identity(application, path):
    app, db = application
    db.read.return_value = [{"league_id": 1}]
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        assert db.read.await_args.args[0] is None
        login = client.post(
            "/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"}
        ).json()
        assert client.get(path).status_code == 200
        assert db.read.await_args.args[0] == 1
        client.post("/auth/logout", headers={"X-CSRF-Token": login["csrf_token"]})
        assert client.get(path).status_code == 200
        assert db.read.await_args.args[0] is None


def test_csrf_and_origin_protect_writes_and_logout(application):
    app, db = application
    with TestClient(app) as client:
        path = "/api/rpc/remove_team"
        data = {"p_league": 2, "p_revision": 3, "p_team": 4}
        assert client.post(path, json=data).status_code == 401
        login = client.post(
            "/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"}
        ).json()
        assert client.post(path, json=data).status_code == 403
        headers = {"X-CSRF-Token": login["csrf_token"]}
        assert (
            client.post(
                path, json=data, headers={**headers, "Origin": "http://elsewhere"}
            ).status_code
            == 403
        )
        db.write.assert_not_awaited()
        response = client.post(path, json=data, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"result": None}
        assert response.headers["cache-control"] == "no-store"
        db.write.assert_awaited_once_with(
            1, "SELECT dl.remove_team(%s, %s, %s) AS result", (2, 3, 4)
        )
        assert client.post("/auth/logout").status_code == 403
        assert client.post("/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/leagues").status_code == 200


def test_startup_refuses_test_login_on_an_unmarked_database(application):
    app, db = application
    db.is_test_database.return_value = False
    with pytest.raises(RuntimeError, match="Test login requires"):
        with TestClient(app):
            pass
    db.close.assert_awaited_once()


def test_draft_events_require_a_session_and_an_existing_league(application, monkeypatch):
    app, db = application
    connect = AsyncMock(side_effect=psycopg.OperationalError("private database credentials"))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with TestClient(app) as client:
        assert client.get("/api/leagues/8/events").status_code == 401
        connect.assert_not_awaited()
        client.post("/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"})
        assert client.get("/api/leagues").json() == []
        db.read.reset_mock()
        assert client.get("/api/leagues/8/events").status_code == 404
        connect.assert_not_awaited()
        db.read.return_value = [{"id": 8}]
        response = client.get("/api/leagues/8/events?user_id=999&channel=dl_user_999")
        assert response.status_code == 503
        assert response.json() == {"detail": "database_unavailable"}
        connect.assert_awaited_once()
        db.read.assert_awaited_with(1, "SELECT id FROM dl.league WHERE id = %s", (8,))
        assert client.get("/api/events").status_code == 404
        for invalid in ("0", "-1", "2147483648", "not-a-number"):
            assert client.get(f"/api/leagues/{invalid}/events").status_code == 422


def test_stream_capacity_returns_retryable_failure_after_league_validation(application):
    app, db = application
    with TestClient(app) as client:
        client.post("/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"})
        app.state.event_slots = asyncio.Semaphore(0)
        db.read.return_value = [{"id": 8}]
        response = client.get("/api/leagues/8/events")
        assert response.status_code == 503
        assert response.json() == {"detail": "event_stream_capacity"}
        assert response.headers["retry-after"] == "2"
        assert client.get("/ready").status_code == 200
        db.read.assert_awaited_once_with(1, "SELECT id FROM dl.league WHERE id = %s", (8,))


def test_event_channel_comes_only_from_validated_league(application, monkeypatch):
    app, db = application
    conn = SimpleNamespace(execute=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))

    class FiniteResponse(module.DraftEventResponse):
        async def _notifications(self, conn):
            yield "event: invalidate\ndata: null\n\n"

    monkeypatch.setattr(module, "DraftEventResponse", FiniteResponse)
    with TestClient(app) as client:
        client.post("/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"})
        db.read.return_value = [{"id": 8}]
        response = client.get("/api/leagues/8/events?user_id=999&channel=dl_user_999&league_id=9")
        assert response.status_code == 200
        assert response.text == "event: invalidate\ndata: null\n\n"
        assert conn.execute.await_args.args[0].as_string() == 'LISTEN "dl_draft_8"'
        conn.close.assert_awaited_once()
        db.read.assert_awaited_once_with(1, "SELECT id FROM dl.league WHERE id = %s", (8,))


@pytest.mark.parametrize("function", ["add_team", "update_team"])
def test_team_budget_is_required_and_forwarded(application, function):
    app, db = application
    with TestClient(app) as client:
        session = client.post(
            "/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"}
        ).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        data = {"p_league": 2, "p_revision": 3}
        if function == "update_team":
            data["p_team"] = 4
        data.update(p_name="Team", p_manager=5)
        path = "/api/rpc/" + function
        assert client.post(path, json=data).status_code == 422
        for invalid in [-1, 2.5, True, "100", None, 2147483648]:
            assert client.post(path, json={**data, "p_point_budget": invalid}).status_code == 422
        db.write.assert_not_awaited()
        for budget in [0, 100, 2147483647]:
            response = client.post(path, json={**data, "p_point_budget": budget})
            assert response.status_code == 200
            params = (*data.values(), budget)
            placeholders = ", ".join(["%s"] * len(params))
            db.write.assert_awaited_with(
                1, f"SELECT dl.{function}({placeholders}) AS result", params
            )


def test_pick_charge_parameter_defaults_and_validation(application):
    app, db = application
    with TestClient(app) as client:
        session = client.post(
            "/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"}
        ).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        data = {"p_league": 2, "p_revision": 3, "p_kind": "pick", "p_draftable": 4}
        path = "/api/rpc/draft_action"
        for invalid in [None, "false", 0, 1]:
            assert client.post(path, json={**data, "p_charge_points": invalid}).status_code == 422
        db.write.assert_not_awaited()
        for supplied, expected in [({}, True), ({"p_charge_points": True}, True),
                                   ({"p_charge_points": False}, False)]:
            assert client.post(path, json={**data, **supplied}).status_code == 200
            db.write.assert_awaited_with(
                1, "SELECT dl.draft_action(%s, %s, %s, %s, %s) AS result",
                (2, 3, "pick", 4, expected),
            )


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({}, 403),
        ({"X-Test-Login": "1", "Origin": "https://other.example"}, 403),
        ({"X-Test-Login": "1", "Origin": "null"}, 403),
        ({"X-Test-Login": "1", "Origin": "http://testserver"}, 200),
    ],
)
def test_fixture_login_requires_explicit_header_and_same_origin(application, headers, expected):
    app, db = application
    with TestClient(app) as client:
        response = client.post("/auth/test", json={"username": "alice"}, headers=headers)
    assert response.status_code == expected
    if expected != 200:
        db.test_user.assert_not_awaited()


