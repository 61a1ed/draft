"""Auth HTTP boundary tests; provider cryptography belongs to Authlib."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from authlib.integrations.base_client import OAuthError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from draft_backend import auth


def make_app(*, test_login=True):
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        google_client_id=None,
        google_client_secret=None,
        public_base_url="https://draft.example",
        test_login_enabled=test_login,
        app_env="test",
        session_ttl_seconds=3600,
    )
    app.state.db = SimpleNamespace(
        test_user=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
        user=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
        register_google=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
    )
    app.add_middleware(SessionMiddleware, secret_key="unit-test-secret", max_age=3600)
    auth.install_auth(app)
    return app


def login(client):
    return client.post("/auth/test", json={"username": "alice"}, headers={"X-Test-Login": "1"})


def test_fixture_login_rotates_session_and_me_does_not_extend_expiry(monkeypatch):
    app = make_app()
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    with TestClient(app) as client:
        first = login(client)
        assert first.status_code == 200
        assert first.json()["user"] == {"id": 7, "display_name": "Alice"}
        assert first.json()["expires_at"] == 4600
        monkeypatch.setattr(auth.time, "time", lambda: 1100)
        assert client.get("/auth/me").json() == first.json()
        second = login(client).json()
        assert second["csrf_token"] != first.json()["csrf_token"]
        assert second["expires_at"] == 4700
        monkeypatch.setattr(auth.time, "time", lambda: 4700)
        assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({}, 403),
        ({"X-Test-Login": "1", "Origin": "https://other.example"}, 403),
        ({"X-Test-Login": "1", "Origin": "null"}, 403),
        ({"X-Test-Login": "1", "Origin": "https://draft.example"}, 200),
    ],
)
def test_fixture_login_requires_explicit_header_and_same_origin(headers, expected):
    app = make_app()
    with TestClient(app) as client:
        response = client.post("/auth/test", json={"username": "alice"}, headers=headers)
    assert response.status_code == expected
    if expected != 200:
        app.state.db.test_user.assert_not_awaited()


def test_fixture_route_is_absent_when_disabled_and_only_logs_in_existing_fixtures():
    with TestClient(make_app(test_login=False)) as client:
        assert login(client).status_code == 404
    app = make_app()
    app.state.db.test_user.return_value = None
    with TestClient(app) as client:
        assert login(client).status_code == 404
    app.state.db.register_google.assert_not_awaited()


def test_google_callback_registers_only_verified_identity_and_drops_provider_tokens():
    app = make_app()
    google = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                "userinfo": {"sub": "google-subject", "name": " Alice "},
                "id_token": "provider-id-token",
                "access_token": "provider-access-token",
            }
        )
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: google)
    captured = {}
    original_login = auth._login

    def capture_session(request, user):
        response = original_login(request, user)
        captured.update(request.session)
        return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "_login", capture_session)
        with TestClient(app) as client:
            response = client.get("/auth/google/callback?code=example&state=example")
            assert response.status_code == 200
            assert client.get("/auth/me").status_code == 200
    app.state.db.register_google.assert_awaited_once_with("google-subject", "Alice")
    assert set(captured) == {"user_id", "expires_at", "csrf_token"}


def test_failed_oidc_validation_never_registers_or_logs_in():
    app = make_app()
    google = SimpleNamespace(
        authorize_access_token=AsyncMock(side_effect=OAuthError("mismatching_state"))
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: google)
    with TestClient(app) as client:
        assert client.get("/auth/google/callback").status_code == 400
        assert client.get("/auth/me").status_code == 401
    app.state.db.register_google.assert_not_awaited()


@pytest.mark.parametrize("subject", [None, "", "test:alice"])
def test_invalid_or_fixture_google_subject_is_rejected(subject):
    app = make_app()
    google = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={"id_token": "verified", "userinfo": {"sub": subject}}
        )
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: google)
    with TestClient(app) as client:
        assert client.get("/auth/google/callback").status_code == 400
    app.state.db.register_google.assert_not_awaited()


def test_session_validation_rejects_bad_types():
    for session in (
        {"user_id": True, "expires_at": 9999999999, "csrf_token": "csrf"},
        {"user_id": 1, "expires_at": "9999999999", "csrf_token": "csrf"},
        {"user_id": 1, "expires_at": 9999999999},
    ):
        request = Request({"type": "http", "session": session})
        assert not auth.session_valid(request)
