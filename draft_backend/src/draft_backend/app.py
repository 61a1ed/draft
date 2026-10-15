import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from .auth import install_auth, require_user, session_valid
from .config import Settings
from .db import Database
from .events import EventHub, event

ID = Annotated[int, Field(strict=True, gt=0, le=2147483647)]
Revision = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Points = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Name = Annotated[str, Field(min_length=1)]
User = Annotated[int, Depends(require_user)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateLeague(Input):
    p_name: Name
    p_roster_size: ID
    p_pick_seconds: ID = 60


class LeagueRevision(Input):
    p_league: ID
    p_revision: Revision


class UpdateLeague(LeagueRevision):
    p_name: Name
    p_roster_size: ID
    p_pick_seconds: ID


class AddTeam(LeagueRevision):
    p_name: Name
    p_manager: ID
    p_point_budget: Points


class TeamRevision(LeagueRevision):
    p_team: ID


class UpdateTeam(TeamRevision):
    p_name: Name
    p_manager: ID
    p_point_budget: Points


class DraftOrder(LeagueRevision):
    p_teams: list[ID]


class DraftAction(LeagueRevision):
    p_kind: Literal["start", "pick", "undo"]
    p_draftable: ID | None = None
    p_charge_points: Annotated[bool, Field(strict=True)] = True


class ApplicationSessionMiddleware(SessionMiddleware):
    """Public image responses must not refresh or distribute session cookies."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/images/"):
            return await self.app({**scope, "session": {}}, receive, send)
        return await super().__call__(scope, receive, send)


class RequestBoundary:
    """Same-origin cookie API with CSRF protection; pure ASGI preserves streaming."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != self.settings.origin:
                return await JSONResponse({"detail": "origin_not_allowed"}, 403)(
                    scope, receive, send
                )
            if request.url.path != "/auth/test":
                if not session_valid(request):
                    return await JSONResponse({"detail": "sign_in_required"}, 401)(
                        scope, receive, send
                    )
                supplied = request.headers.get("x-csrf-token", "")
                if not secrets.compare_digest(
                    supplied.encode(), request.session["csrf_token"].encode()
                ):
                    return await JSONResponse({"detail": "csrf_failed"}, 403)(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, private_send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        db = app.state.db = Database(settings)
        # Successful immutable image reads live until this worker restarts.
        app.state.images = {}
        hub = app.state.events = EventHub(settings.database_url, "dl_changed")
        await db.open()
        try:
            if settings.test_login_enabled and not await db.is_test_database():
                raise RuntimeError("Test login requires a database provisioned with test users")
            await hub.start()
            try:
                yield
            finally:
                await hub.close()
        finally:
            await db.close()

    app = FastAPI(title="Draft database API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(RequestBoundary, settings=settings)
    # Last installed is outermost, so the request boundary can read the session.
    app.add_middleware(
        ApplicationSessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="draft_session",
        max_age=settings.session_ttl_seconds,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    install_auth(app)

    @app.exception_handler(psycopg.Error)
    async def sql_error(request, exc):
        state = exc.sqlstate
        if state in {"PT400", "PT401", "PT404", "PT409"}:
            status, code = int(state[2:]), exc.diag.message_primary
        elif state == "23505":
            status, code = 409, "already_exists_or_claimed"
        elif state == "23503":
            status, code = 400, "invalid_reference"
        elif state in {"23514", "23502", "22003", "22P02"}:
            status, code = 400, "invalid_value"
        elif state in {"40001", "40P01"}:
            status, code = 503, "transaction_retry_exhausted"
        elif isinstance(exc, psycopg.OperationalError):
            status, code = 503, "database_unavailable"
        else:
            status, code = 500, "database_error"
        # Constraint details may contain private records; do not return them.
        return JSONResponse({"detail": code}, status_code=status)

    @app.exception_handler(PoolTimeout)
    async def pool_timeout(request, exc):
        return JSONResponse({"detail": "database_busy"}, status_code=503)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/docs")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "notifications_connected": app.state.events.connected.is_set()}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(1):
                await app.state.db.ready()
            if not app.state.events.connected.is_set():
                raise HTTPException(503, "not_ready")
        except (psycopg.Error, PoolTimeout, TimeoutError):
            raise HTTPException(503, "not_ready") from None
        return {"ready": True}

    @app.get("/api/users", tags=["reads"])
    async def users(user: User):
        return await app.state.db.read(user, "SELECT id, display_name FROM dl.app_user ORDER BY id")

    @app.get("/api/draftables", tags=["reads"])
    async def draftables(user: User):
        return await app.state.db.read(
            user,
            "SELECT id, name, point_cost, dex, species, form, "
            "'/images/' || image_hash AS image_url FROM dl.draftable ORDER BY id",
        )

    @app.api_route("/images/{image_hash}", methods=["GET", "HEAD"], tags=["images"])
    async def image(request: Request, image_hash: str):
        if not re.fullmatch(r"[0-9a-f]{64}", image_hash):
            raise HTTPException(404, "image_not_found")
        cached = app.state.images.get(image_hash)
        if cached is None:
            cached = await app.state.db.image(image_hash)
            if cached is None:
                raise HTTPException(404, "image_not_found")
            app.state.images[image_hash] = cached
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{image_hash}"',
        }
        # If-None-Match uses weak comparison for GET and HEAD.
        candidates = request.headers.get("if-none-match", "").split(",")
        if any(tag.strip().removeprefix("W/") in {"*", headers["ETag"]} for tag in candidates):
            return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(cached["data"]))
        return Response(
            content=cached["data"] if request.method == "GET" else b"",
            media_type=cached["content_type"],
            headers=headers,
        )

    @app.get("/api/leagues", tags=["reads"])
    async def leagues(user: User):
        return await app.state.db.read(user, "SELECT * FROM dl.league ORDER BY id")

    async def snapshot(user, league_id):
        rows = await app.state.db.read(
            user, "SELECT * FROM dl.league_snapshot WHERE league_id = %s", (league_id,)
        )
        if not rows:
            raise HTTPException(404, "league_not_found")
        return rows[0]

    @app.get("/api/leagues/{league_id}", tags=["reads"])
    async def league(league_id: int, user: User):
        return await snapshot(user, league_id)

    async def rpc(user, function, body):
        # Both identifier and argument order come from code, never from input.
        params = tuple(body.model_dump().values())
        placeholders = ", ".join(["%s"] * len(params))
        rows = await app.state.db.write(
            user, f"SELECT dl.{function}({placeholders}) AS result", params
        )
        if function in {"update_league", "update_team", "remove_team", "set_draft_order"}:
            return {"result": None}  # PostgreSQL's void otherwise decodes as an empty string.
        return rows[0]

    @app.post("/api/rpc/create_league", tags=["SQL functions"])
    async def create_league(body: CreateLeague, user: User):
        return await rpc(user, "create_league", body)

    @app.post("/api/rpc/update_league", tags=["SQL functions"])
    async def update_league(body: UpdateLeague, user: User):
        return await rpc(user, "update_league", body)

    @app.post("/api/rpc/add_team", tags=["SQL functions"])
    async def add_team(body: AddTeam, user: User):
        return await rpc(user, "add_team", body)

    @app.post("/api/rpc/update_team", tags=["SQL functions"])
    async def update_team(body: UpdateTeam, user: User):
        return await rpc(user, "update_team", body)

    @app.post("/api/rpc/remove_team", tags=["SQL functions"])
    async def remove_team(body: TeamRevision, user: User):
        return await rpc(user, "remove_team", body)

    @app.post("/api/rpc/set_draft_order", tags=["SQL functions"])
    async def set_draft_order(body: DraftOrder, user: User):
        return await rpc(user, "set_draft_order", body)

    @app.post("/api/rpc/draft_action", tags=["SQL functions"])
    async def draft_action(body: DraftAction, user: User):
        return await rpc(user, "draft_action", body)

    @app.get("/api/leagues/{league_id}/events", tags=["events"])
    async def events(request: Request, league_id: int, user: User):
        hub = app.state.events
        # Subscribe before checking access so changes during setup remain pending.
        topic = str(league_id)
        queue = hub.subscribe(topic)
        try:
            rows = await app.state.db.read(
                user, "SELECT id FROM dl.league WHERE id = %s", (league_id,)
            )
            if not rows:
                raise HTTPException(404, "league_not_found")
        except BaseException:
            hub.unsubscribe(topic, queue)
            raise

        async def stream():
            try:
                yield "retry: 2000\n\n"
                yield event("invalidate")
                while True:
                    try:
                        await asyncio.wait_for(
                            queue.get(), timeout=settings.sse_heartbeat_seconds
                        )
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                    else:
                        yield event("invalidate")
            finally:
                hub.unsubscribe(topic, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app
