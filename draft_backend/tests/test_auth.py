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
from draft_backend.config import Settings


def make_app(*, test_login=True, callback_path="/auth/callback"):
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        oidc_issuer="https://id.example",
        oidc_client_id=None,
        oidc_client_secret=None,
        oidc_callback_path=callback_path,
        public_base_url="https://draft.example",
        test_login_enabled=test_login,
        app_env="test",
        session_ttl_seconds=3600,
    )
    app.state.db = SimpleNamespace(
        test_user=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
        user=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
        register_identity=AsyncMock(return_value={"id": 7, "display_name": "Alice"}),
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


def test_fixture_route_is_absent_when_disabled_and_only_logs_in_existing_fixtures():
    with TestClient(make_app(test_login=False)) as client:
        assert login(client).status_code == 404
    app = make_app()
    app.state.db.test_user.return_value = None
    with TestClient(app) as client:
        assert login(client).status_code == 404
    app.state.db.register_identity.assert_not_awaited()


def test_oidc_callback_registers_only_verified_identity_and_drops_provider_tokens():
    app = make_app()
    provider = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                "userinfo": {"iss": "https://id.example", "sub": "alice", "name": " Alice "},
                "id_token": "provider-id-token",
                "access_token": "provider-access-token",
            }
        )
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: provider)
    captured = {}
    original_login = auth._login

    def capture_session(request, user):
        response = original_login(request, user)
        captured.update(request.session)
        return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "_login", capture_session)
        with TestClient(app) as client:
            response = client.get(
                "/auth/callback?code=example&state=example&next=https://other.example",
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"] == "https://draft.example/"
            assert response.headers["cache-control"] == "no-store"
            assert client.get("/auth/me").status_code == 200
    app.state.db.register_identity.assert_awaited_once_with("https://id.example", "alice", "Alice")
    assert set(captured) == {"user_id", "expires_at", "csrf_token"}


def test_failed_oidc_validation_never_registers_or_logs_in():
    app = make_app()
    provider = SimpleNamespace(
        authorize_access_token=AsyncMock(side_effect=OAuthError("mismatching_state"))
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: provider)
    with TestClient(app) as client:
        response = client.get("/auth/callback", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "https://draft.example/?auth_error=oidc_login_failed"
        )
        assert client.get("/auth/me").status_code == 401
    app.state.db.register_identity.assert_not_awaited()


@pytest.mark.parametrize(
    "issuer, subject",
    [
        ("https://id.example", None),
        ("https://id.example", ""),
        ("https://other.example", "alice"),
        ("urn:draft:test", "alice"),
        (None, "alice"),
    ],
)
def test_invalid_oidc_identity_is_rejected(issuer, subject):
    app = make_app()
    provider = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={"id_token": "verified", "userinfo": {"iss": issuer, "sub": subject}}
        )
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: provider)
    with TestClient(app) as client:
        response = client.get("/auth/callback", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "https://draft.example/?auth_error=oidc_identity_invalid"
        )
        assert client.get("/auth/me").status_code == 401
    app.state.db.register_identity.assert_not_awaited()


def test_session_validation_rejects_bad_types():
    for session in (
        {"user_id": True, "expires_at": 9999999999, "csrf_token": "csrf"},
        {"user_id": 1, "expires_at": "9999999999", "csrf_token": "csrf"},
        {"user_id": 1, "expires_at": 9999999999},
    ):
        request = Request({"type": "http", "session": session})
        assert not auth.session_valid(request)


@pytest.mark.parametrize("path", ["/auth/login", "/auth/callback"])
def test_unconfigured_oidc_returns_to_app_with_error(path):
    with TestClient(make_app()) as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "https://draft.example/?auth_error=oidc_login_not_configured"
        )
        assert response.headers["cache-control"] == "no-store"


def test_oidc_login_redirects_to_provider_with_configured_callback():
    from starlette.responses import RedirectResponse

    app = make_app()
    provider = SimpleNamespace(
        authorize_redirect=AsyncMock(return_value=RedirectResponse("https://id.example/"))
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: provider)
    with TestClient(app) as client:
        response = client.get("/auth/login", follow_redirects=False)
        assert response.headers["location"] == "https://id.example/"
        assert response.headers["cache-control"] == "no-store"
    assert provider.authorize_redirect.await_args.args[1] == ("https://draft.example/auth/callback")


def test_custom_oidc_callback_is_both_redirect_uri_and_callback_route():
    from starlette.responses import RedirectResponse

    app = make_app(callback_path="/g")
    provider = SimpleNamespace(
        authorize_redirect=AsyncMock(return_value=RedirectResponse("https://id.example/")),
        authorize_access_token=AsyncMock(return_value={
            "id_token": "verified",
            "userinfo": {"iss": "https://id.example", "sub": "alice", "name": "Alice"},
        }),
    )
    app.state.oauth = SimpleNamespace(create_client=lambda _: provider)
    with TestClient(app) as client:
        assert client.get("/auth/login", follow_redirects=False).status_code == 307
        assert provider.authorize_redirect.await_args.args[1] == "https://draft.example/g"
        assert client.get("/auth/callback", follow_redirects=False).status_code == 404
        response = client.get("/g?code=example&state=example", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "https://draft.example/"
        assert client.get("/auth/me").json()["user"] == {"id": 7, "display_name": "Alice"}
    app.state.db.register_identity.assert_awaited_once_with("https://id.example", "alice", "Alice")


@pytest.mark.parametrize("path", ["/auth/callback", "/g", "/oauth/callback-v2"])
def test_oidc_callback_path_accepts_static_root_relative_paths(path):
    settings = Settings(
        database_url="unused", session_secret="x" * 32,
        public_base_url="https://draft.example", app_env="test", test_login_enabled=True,
        oidc_callback_path=path,
    )
    assert settings.oidc_callback_path == path


@pytest.mark.parametrize("path", [
    "", "/", "g", "//evil.example/g", "https://evil.example/g", "/g?x=1", "/g#fragment",
    "/g/../auth/login", "/g/./callback", "/g/%2fcallback", "/g/{code}", "/g\\callback",
    "/g\n", "/g/", "/auth", "/auth/login", "/auth/logout", "/auth/me", "/auth/test",
    "/api", "/api/rpc/create_league", "/images/a", "/assets/app.js", "/ready", "/healthz",
    "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
])
def test_oidc_callback_path_rejects_ambiguous_or_conflicting_routes(path):
    with pytest.raises(ValueError, match="OIDC_CALLBACK_PATH"):
        Settings(
            database_url="unused", session_secret="x" * 32,
            public_base_url="https://draft.example", app_env="test", test_login_enabled=True,
            oidc_callback_path=path,
        )


def test_oidc_callback_path_can_be_configured_from_environment(monkeypatch):
    for name, value in {
        "DATABASE_URL": "unused", "SESSION_SECRET": "x" * 32, "APP_ENV": "test",
        "TEST_LOGIN_ENABLED": "true", "OIDC_ISSUER": "", "OIDC_CLIENT_ID": "",
        "OIDC_CLIENT_SECRET": "", "OIDC_CALLBACK_PATH": "/g",
    }.items():
        monkeypatch.setenv(name, value)
    assert Settings.from_env().oidc_callback_path == "/g"


@pytest.mark.parametrize(
    "issuer", ["urn:draft:test", "http://id.example", "https://id.example?x=1"]
)
def test_oidc_configuration_rejects_non_https_or_invalid_issuers(issuer):
    with pytest.raises(ValueError, match="OIDC_ISSUER"):
        Settings(
            database_url="unused",
            session_secret="x" * 32,
            public_base_url="https://draft.example",
            oidc_issuer=issuer,
            oidc_client_id="client",
            oidc_client_secret="secret",
        )


def test_oidc_configuration_requires_complete_provider_settings():
    with pytest.raises(ValueError, match="together"):
        Settings(
            database_url="unused",
            session_secret="x" * 32,
            public_base_url="https://draft.example",
            oidc_issuer="https://id.example",
        )


def test_oidc_discovery_uses_the_configured_issuer():
    app = FastAPI()
    app.state.settings = Settings(
        database_url="unused",
        session_secret="x" * 32,
        public_base_url="https://draft.example",
        oidc_issuer="https://id.example/tenant",
        oidc_client_id="client",
        oidc_client_secret="secret",
    )
    auth.install_auth(app)
    client = app.state.oauth.create_client("oidc")
    assert client._server_metadata_url == (
        "https://id.example/tenant/.well-known/openid-configuration"
    )


def test_settings_owns_test_login_and_retry_validation():
    settings = dict(
        database_url="unused", session_secret="x" * 32,
        public_base_url="https://draft.example", app_env="test", test_login_enabled=True,
    )
    assert Settings(**settings, transaction_retries=0).transaction_retries == 0
    with pytest.raises(ValueError, match="nonnegative"):
        Settings(**settings, transaction_retries=-1)
    with pytest.raises(ValueError, match="APP_ENV=test"):
        Settings(**{**settings, "app_env": "development"})
