"""OpenID Connect and deliberately restricted fixture login.

The verified ``iss`` and ``sub`` identify accounts. The signed session contains only
the local user ID, a fixed expiry, and a CSRF token after login; provider access and
ID tokens never go into the cookie. Authlib temporarily stores OIDC state/nonce
in that cookie while a login is in progress.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping

import httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from joserfc.errors import JoseError
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, RedirectResponse


class TestLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


def session_valid(request: Request) -> bool:
    """Use an absolute expiry even though SessionMiddleware refreshes cookies."""
    session = request.session
    return (
        type(session.get("user_id")) is int
        and session["user_id"] > 0
        and type(session.get("expires_at")) is int
        and session["expires_at"] > time.time()
        and isinstance(session.get("csrf_token"), str)
        and bool(session["csrf_token"])
    )


def require_user(request: Request) -> int:
    if not session_valid(request):
        request.session.clear()
        raise HTTPException(401, "sign_in_required", headers={"Cache-Control": "no-store"})
    return request.session["user_id"]


def _session_response(request: Request, user: Mapping) -> JSONResponse:
    return JSONResponse(
        {
            "user": {"id": user["id"], "display_name": user["display_name"]},
            "csrf_token": request.session["csrf_token"],
            "expires_at": request.session["expires_at"],
        },
        headers={"Cache-Control": "no-store"},
    )


def _login(request: Request, user: Mapping) -> None:
    request.session.clear()
    request.session.update(
        user_id=user["id"],
        expires_at=int(time.time()) + request.app.state.settings.session_ttl_seconds,
        csrf_token=secrets.token_urlsafe(32),
    )


def _oidc_redirect(settings, error: str | None = None) -> RedirectResponse:
    destination = settings.public_base_url.rstrip("/") + "/"
    if error:
        destination += "?auth_error=" + error
    return RedirectResponse(destination, status_code=303, headers={"Cache-Control": "no-store"})


def install_auth(app: FastAPI) -> None:
    """Install once, after ``app.state.settings`` has been configured.

    The app's lifespan must separately verify the test database marker before
    accepting requests when test login is enabled. The app's CSRF middleware
    protects authenticated mutation routes, including ``POST /auth/logout``.
    """
    settings = app.state.settings
    oauth = OAuth()
    if settings.oidc_client_id and settings.oidc_client_secret:
        oauth.register(
            "oidc",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=settings.oidc_issuer.rstrip("/")
            + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile", "timeout": 15.0},
        )
    app.state.oauth = oauth
    router = APIRouter(prefix="/auth", tags=["authentication"])

    @router.get("/login")
    async def oidc_login(request: Request):
        client = request.app.state.oauth.create_client("oidc")
        if client is None:
            return _oidc_redirect(settings, "oidc_login_not_configured")
        # Bound cookie size if someone abandons and restarts sign-in repeatedly.
        for key in list(request.session):
            if key.startswith("_state_oidc_"):
                del request.session[key]
        callback = settings.public_base_url.rstrip("/") + "/auth/callback"
        try:
            response = await client.authorize_redirect(request, callback)
        except (httpx.RequestError, httpx.HTTPStatusError):
            return _oidc_redirect(settings, "oidc_unavailable")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/callback")
    async def oidc_callback(request: Request):
        client = request.app.state.oauth.create_client("oidc")
        if client is None:
            return _oidc_redirect(settings, "oidc_login_not_configured")
        try:
            # Authlib checks state and validates the ID token's signature,
            # issuer, audience, expiry and nonce using the configured provider's discovery/JWKS.
            token = await client.authorize_access_token(request)
        except (OAuthError, JoseError):
            return _oidc_redirect(settings, "oidc_login_failed")
        except (httpx.RequestError, httpx.HTTPStatusError):
            return _oidc_redirect(settings, "oidc_unavailable")

        claims = token.get("userinfo")
        if not token.get("id_token") or not isinstance(claims, Mapping):
            return _oidc_redirect(settings, "oidc_identity_missing")
        issuer = claims.get("iss")
        subject = claims.get("sub")
        if (
            issuer != settings.oidc_issuer
            or not isinstance(subject, str)
            or not subject
            or len(subject) > 255
        ):
            return _oidc_redirect(settings, "oidc_identity_invalid")
        name = claims.get("name")
        display_name = name.strip() if isinstance(name, str) and name.strip() else "User"
        user = await request.app.state.db.register_identity(issuer, subject, display_name)
        _login(request, user)
        return _oidc_redirect(settings)

    @router.get("/me")
    async def me(request: Request, user_id: int = Depends(require_user)):
        user = await request.app.state.db.user(user_id)
        if user is None:
            request.session.clear()
            raise HTTPException(401, "sign_in_required")
        return _session_response(request, user)

    @router.post("/logout", status_code=204)
    async def logout(request: Request, _: int = Depends(require_user)):
        request.session.clear()
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    if settings.test_login_enabled:
        @router.post("/test")
        async def test_login(request: Request, body: TestLogin):
            # Require a non-simple browser request to prevent login CSRF even
            # before a session exists. Do not enable permissive CORS here.
            if request.headers.get("x-test-login") != "1":
                raise HTTPException(403, "test_login_header_required")
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise HTTPException(415, "application_json_required")
            user = await request.app.state.db.test_user(body.username)
            if user is None:
                raise HTTPException(404, "test_user_not_found")
            _login(request, user)
            return _session_response(request, user)

    app.include_router(router)
