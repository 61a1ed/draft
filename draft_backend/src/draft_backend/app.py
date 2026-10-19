import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from .auth import install_auth, optional_user, require_user, session_valid
from .config import Settings
from .db import Database
from .events import AccountEventResponse
from .migrations import migrate

MIGRATION_DIRECTORY = Path(__file__).with_name("sql_migrations")

ID = Annotated[int, Field(strict=True, gt=0, le=2147483647)]
Revision = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Points = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
ColorRGB = Annotated[int, Field(strict=True, ge=0, le=16777215)]
Name = Annotated[str, Field(min_length=1)]
User = Annotated[int, Depends(require_user)]
Reader = Annotated[int | None, Depends(optional_user)]


def calendar_date(value):
    # Pydantic otherwise also accepts epoch numbers and midnight timestamps.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use a calendar date in YYYY-MM-DD format")
    return date.fromisoformat(value)


def offset_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}"
        r"(?::[0-9]{2}(?:\.[0-9]+)?)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value,
    ):
        raise ValueError("Use a timestamp with an explicit Z or +/-HH:MM offset")
    return value


CalendarDate = Annotated[date, BeforeValidator(calendar_date)]
OffsetTimestamp = Annotated[AwareDatetime, BeforeValidator(offset_timestamp)]
OutcomeKind = Literal["played", "forfeit"]
Side = Literal["a", "b"]


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
    p_name: Name | None = None
    p_manager: ID
    p_point_budget: Points


class TeamRevision(LeagueRevision):
    p_team: ID


class UpdateTeam(TeamRevision):
    p_name: Name | None
    p_manager: ID
    p_point_budget: Points


class SetTeamMetadata(TeamRevision):
    p_name: Name
    p_color_rgb: ColorRGB


class DraftOrder(LeagueRevision):
    p_teams: list[ID]


class DraftAction(LeagueRevision):
    p_kind: Literal["start", "pick", "undo"]
    p_draftable: ID | None = None
    p_charge_points: Annotated[bool, Field(strict=True)] = True


class CreateFavoritesList(Input):
    p_name: Name


class FavoritesListRevision(Input):
    p_list: ID
    p_revision: Revision


class RenameFavoritesList(FavoritesListRevision):
    p_name: Name


class FavoritesEntry(FavoritesListRevision):
    p_draftable: ID


class FavoritesOrder(FavoritesListRevision):
    p_draftables: list[ID]


class PublishTournament(Input):
    p_league: ID
    p_league_revision: Revision
    p_first_suggested_date: CalendarDate
    p_season_best_of: ID = 3
    p_final_best_of: ID = 5


class SetRoundSuggestedDate(Input):
    p_round: ID
    p_revision: Revision
    p_suggested_start_date: CalendarDate | None


class AvailabilityWindow(Input):
    starts_at: OffsetTimestamp
    ends_at: OffsetTimestamp


class SetTeamAvailability(Input):
    p_team: ID
    p_revision: Revision | None
    p_time_zone: Name
    p_display_step_minutes: ID
    p_windows: list[AvailabilityWindow]


class CopyTeamAvailabilityWeek(Input):
    p_team: ID
    p_revision: Revision
    p_source: CalendarDate
    p_target: CalendarDate


class SubmitResult(Input):
    p_match: ID
    p_expected_previous_submission_id: ID | None
    p_outcome_kind: OutcomeKind
    p_team_a_game_wins: Points | None = None
    p_team_b_game_wins: Points | None = None
    p_awarded_winner_side: Side | None = None


class RecordResult(Input):
    p_match: ID
    p_revision: Revision
    p_reason: Name
    p_selected_submission: ID | None = None
    p_outcome_kind: OutcomeKind | None = None
    p_team_a_game_wins: Points | None = None
    p_team_b_game_wins: Points | None = None
    p_awarded_winner_side: Side | None = None


class CorrectResult(Input):
    p_match: ID
    p_revision: Revision
    p_reason: Name
    p_outcome_kind: OutcomeKind
    p_team_a_game_wins: Points | None = None
    p_team_b_game_wins: Points | None = None
    p_awarded_winner_side: Side | None = None


class SetMatchVod(Input):
    p_match: ID
    p_revision: Revision
    p_vod_url: str | None


class WithdrawTeam(LeagueRevision):
    p_team: ID
    p_reason: Name


class CreateTiebreakRound(Input):
    p_stage: ID
    p_revision: Revision
    p_teams: list[ID]
    p_reason: Name
    p_suggested_start_date: CalendarDate | None = None


class PublishFinal(LeagueRevision):
    p_qualifiers: list[ID]
    p_suggested_start_date: CalendarDate | None = None


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
        migrate(settings.migration_database_url, MIGRATION_DIRECTORY, schema="dl", role="dl_owner")
        db = app.state.db = Database(settings)
        # Successful immutable image reads live until this worker restarts.
        app.state.images = {}
        app.state.event_slots = asyncio.Semaphore(settings.sse_max_connections)
        await db.open()
        try:
            if settings.test_login_enabled and not await db.is_test_database():
                raise RuntimeError("Test login requires a database provisioned with test users")
            yield
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
        # Return stable application errors rather than database implementation details.
        return JSONResponse({"detail": code}, status_code=status)

    @app.exception_handler(PoolTimeout)
    async def pool_timeout(request, exc):
        return JSONResponse({"detail": "database_busy"}, status_code=503)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/docs")

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(1):
                await app.state.db.ready()
        except (psycopg.Error, PoolTimeout, TimeoutError):
            raise HTTPException(503, "not_ready") from None
        return {"ready": True}

    @app.get("/api/users", tags=["reads"])
    async def users(user: Reader):
        return await app.state.db.read(user, "SELECT id, display_name FROM dl.app_user ORDER BY id")

    @app.get("/api/draftables", tags=["reads"])
    async def draftables(user: Reader):
        return await app.state.db.read(
            user,
            "SELECT id, name, point_cost, dex, species, form, "
            "'/images/' || image_hash AS image_url FROM dl.draftable ORDER BY id",
        )

    # TODO FAVORITES READ PRIVACY: Both endpoints deliberately allow anonymous
    # and other-account reads for now. owner_user_id is only a browsing filter,
    # NOT authorization. Add owner-only read checks together with database
    # restrictions on BOTH favorites tables and the snapshot view; see 0003.
    @app.get("/api/favorites-lists", tags=["reads"])
    async def favorites_lists(
        user: Reader,
        owner_user_id: Annotated[int | None, Field(gt=0, le=2147483647)] = None,
    ):
        query = "SELECT id, owner_user_id, name, revision FROM dl.favorites_list"
        params = ()
        if owner_user_id is not None:
            query += " WHERE owner_user_id = %s"
            params = (owner_user_id,)
        return await app.state.db.read(user, query + " ORDER BY id", params)

    @app.get("/api/favorites-lists/{list_id}", tags=["reads"])
    async def favorites_list(
        list_id: Annotated[int, Field(gt=0, le=2147483647)], user: Reader,
    ):
        rows = await app.state.db.read(
            user, "SELECT * FROM dl.favorites_list_snapshot WHERE id = %s", (list_id,),
        )
        if not rows:
            raise HTTPException(404, "favorites_list_not_found")
        return rows[0]

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
    async def leagues(user: Reader):
        return await app.state.db.read(user, "SELECT * FROM dl.league ORDER BY id")

    async def snapshot(user, league_id):
        rows = await app.state.db.read(
            user, "SELECT * FROM dl.league_snapshot WHERE league_id = %s", (league_id,)
        )
        if not rows:
            raise HTTPException(404, "league_not_found")
        return rows[0]

    @app.get("/api/leagues/{league_id}", tags=["reads"])
    async def league(league_id: int, user: Reader):
        return await snapshot(user, league_id)

    @app.get("/api/leagues/{league_id}/tournament", tags=["reads"])
    async def tournament(league_id: int, user: Reader):
        result = await app.state.db.tournament(user, league_id)
        if result is None:
            raise HTTPException(404, "tournament_not_found")
        return result

    @app.get("/api/matches/{match_id}", tags=["reads"])
    async def match(match_id: int, user: Reader):
        result = await app.state.db.match(user, match_id)
        if result is None:
            raise HTTPException(404, "match_not_found")
        return result

    @app.get("/api/teams/{team_id}/availability", tags=["reads"])
    async def team_availability(team_id: int, user: Reader):
        result = await app.state.db.team_availability(user, team_id)
        if result is None:
            raise HTTPException(404, "team_not_found")
        return result

    async def rpc(user, function, body):
        # Both identifier and argument order come from code, never from input.
        arguments = body.model_dump()
        if isinstance(body, SetTeamAvailability):
            # JSONB needs JSON primitives; date/timestamp arguments outside this
            # payload remain native values for psycopg's typed adapters.
            arguments["p_windows"] = Jsonb(body.model_dump(mode="json")["p_windows"])
        params = tuple(arguments.values())
        placeholders = ", ".join(["%s"] * len(params))
        rows = await app.state.db.write(
            user, f"SELECT dl.{function}({placeholders}) AS result", params
        )
        if function in {
            "update_league", "update_team", "remove_team", "set_draft_order",
            "set_team_metadata",
            "rename_favorites_list", "delete_favorites_list", "add_favorites_entry",
            "remove_favorites_entry", "set_favorites_order",
            "publish_tournament", "set_round_suggested_date", "set_team_availability",
            "copy_team_availability_week", "set_match_vod", "withdraw_team",
        }:
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

    @app.post("/api/rpc/set_team_metadata", tags=["SQL functions"])
    async def set_team_metadata(body: SetTeamMetadata, user: User):
        return await rpc(user, "set_team_metadata", body)

    @app.post("/api/rpc/remove_team", tags=["SQL functions"])
    async def remove_team(body: TeamRevision, user: User):
        return await rpc(user, "remove_team", body)

    @app.post("/api/rpc/set_draft_order", tags=["SQL functions"])
    async def set_draft_order(body: DraftOrder, user: User):
        return await rpc(user, "set_draft_order", body)

    @app.post("/api/rpc/draft_action", tags=["SQL functions"])
    async def draft_action(body: DraftAction, user: User):
        return await rpc(user, "draft_action", body)

    @app.post("/api/rpc/create_favorites_list", tags=["SQL functions"])
    async def create_favorites_list(body: CreateFavoritesList, user: User):
        return await rpc(user, "create_favorites_list", body)

    @app.post("/api/rpc/rename_favorites_list", tags=["SQL functions"])
    async def rename_favorites_list(body: RenameFavoritesList, user: User):
        return await rpc(user, "rename_favorites_list", body)

    @app.post("/api/rpc/delete_favorites_list", tags=["SQL functions"])
    async def delete_favorites_list(body: FavoritesListRevision, user: User):
        return await rpc(user, "delete_favorites_list", body)

    @app.post("/api/rpc/add_favorites_entry", tags=["SQL functions"])
    async def add_favorites_entry(body: FavoritesEntry, user: User):
        return await rpc(user, "add_favorites_entry", body)

    @app.post("/api/rpc/remove_favorites_entry", tags=["SQL functions"])
    async def remove_favorites_entry(body: FavoritesEntry, user: User):
        return await rpc(user, "remove_favorites_entry", body)

    @app.post("/api/rpc/set_favorites_order", tags=["SQL functions"])
    async def set_favorites_order(body: FavoritesOrder, user: User):
        return await rpc(user, "set_favorites_order", body)

    @app.post("/api/rpc/publish_tournament", tags=["SQL functions"])
    async def publish_tournament(body: PublishTournament, user: User):
        return await rpc(user, "publish_tournament", body)

    @app.post("/api/rpc/set_round_suggested_date", tags=["SQL functions"])
    async def set_round_suggested_date(body: SetRoundSuggestedDate, user: User):
        return await rpc(user, "set_round_suggested_date", body)

    @app.post("/api/rpc/set_team_availability", tags=["SQL functions"])
    async def set_team_availability(body: SetTeamAvailability, user: User):
        return await rpc(user, "set_team_availability", body)

    @app.post("/api/rpc/copy_team_availability_week", tags=["SQL functions"])
    async def copy_team_availability_week(body: CopyTeamAvailabilityWeek, user: User):
        return await rpc(user, "copy_team_availability_week", body)

    @app.post("/api/rpc/submit_result", tags=["SQL functions"])
    async def submit_result(body: SubmitResult, user: User):
        return await rpc(user, "submit_result", body)

    @app.post("/api/rpc/record_result", tags=["SQL functions"])
    async def record_result(body: RecordResult, user: User):
        return await rpc(user, "record_result", body)

    @app.post("/api/rpc/correct_result", tags=["SQL functions"])
    async def correct_result(body: CorrectResult, user: User):
        return await rpc(user, "correct_result", body)

    @app.post("/api/rpc/set_match_vod", tags=["SQL functions"])
    async def set_match_vod(body: SetMatchVod, user: User):
        return await rpc(user, "set_match_vod", body)

    @app.post("/api/rpc/withdraw_team", tags=["SQL functions"])
    async def withdraw_team(body: WithdrawTeam, user: User):
        return await rpc(user, "withdraw_team", body)

    @app.post("/api/rpc/create_tiebreak_round", tags=["SQL functions"])
    async def create_tiebreak_round(body: CreateTiebreakRound, user: User):
        return await rpc(user, "create_tiebreak_round", body)

    @app.post("/api/rpc/publish_final", tags=["SQL functions"])
    async def publish_final(body: PublishFinal, user: User):
        return await rpc(user, "publish_final", body)

    @app.get("/api/events", tags=["events"])
    async def events(request: Request, user: User):
        return AccountEventResponse(
            settings, user, request.session["expires_at"], app.state.event_slots,
        )

    return app
