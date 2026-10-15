"""Exercise the HTTP/session boundary and SSE resynchronization without a server."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from draft_backend import app as module
from draft_backend.config import Settings
from draft_backend.events import EventHub


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

    class LocalHub(EventHub):
        async def start(self):
            self.connected.set()

        async def close(self):
            pass

    monkeypatch.setattr(module, "Database", lambda _: db)
    monkeypatch.setattr(module, "EventHub", LocalHub)
    settings = Settings(
        database_url="postgresql://unused",
        session_secret="x" * 40,
        app_env="test",
        test_login_enabled=True,
        cookie_secure=False,
        public_base_url="http://testserver",
        sse_heartbeat_seconds=0.01,
    )
    return module.create_app(settings), db


def test_readiness_requires_database_and_notifications_without_authentication(application):
    app, db = application
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"ready": True}
        assert response.headers["cache-control"] == "no-store"
        db.ready.assert_awaited_once()
        app.state.events.connected.clear()
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "not_ready"}
        assert client.get("/healthz").json() == {
            "status": "ok", "notifications_connected": False,
        }
        app.state.events.connected.set()
        assert client.get("/ready").status_code == 200


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
        assert client.get("/api/draftables").status_code == 401
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
        assert client.get("/api/leagues").status_code == 401


def test_startup_refuses_test_login_on_an_unmarked_database(application):
    app, db = application
    db.is_test_database.return_value = False
    with pytest.raises(RuntimeError, match="Test login requires"):
        with TestClient(app):
            pass
    db.close.assert_awaited_once()


async def test_sse_invalidations_keepalive_and_cleanup(application):
    app, db = application
    hub = app.state.events = EventHub("unused", "test_channel")
    app.state.db = db
    db.read.return_value = [{"id": 8}]
    request = Request({"type": "http", "app": app, "session": {"expires_at": 0}})
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", None) == "/api/leagues/{league_id}/events"
    )
    response = await endpoint(request, 8, 1)
    chunks = response.body_iterator
    assert await anext(chunks) == "retry: 2000\n\n"
    assert await anext(chunks) == "event: invalidate\ndata: null\n\n"
    assert response.headers["x-accel-buffering"] == "no"
    hub.invalidate("8")
    assert "event: invalidate" in await anext(chunks)
    hub.invalidate_all()
    assert "event: invalidate" in await anext(chunks)
    db.read.return_value = []
    assert await anext(chunks) == ": keep-alive\n\n"
    db.read.assert_awaited_once()
    await chunks.aclose()
    assert not hub.subscribers
    with pytest.raises(HTTPException) as error:
        await endpoint(request, 8, 1)
    assert error.value.status_code == 404
    assert not hub.subscribers


async def test_hub_routes_and_bounds_pending_invalidations():
    hub = EventHub("unused", "test_channel")
    first, other = hub.subscribe("a"), hub.subscribe("b")
    hub.invalidate("a")
    hub.invalidate("a")
    assert first.qsize() == 1
    assert other.empty()
    await first.get()
    hub.invalidate_all()
    assert first.qsize() == other.qsize() == 1


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


