"""Account discovery and stream lifetime through ASGI and real PostgreSQL."""

import asyncio
from contextlib import AsyncExitStack, suppress
from uuid import uuid4

import httpx
import psycopg
import pytest

from draft_backend.app import create_app
from draft_backend.config import Settings

pytestmark = pytest.mark.integration


class BrowserStream:
    """Drive streaming ASGI directly: httpx's ASGI transport buffers responses."""

    def __init__(self, app, client):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        request = client.build_request("GET", "/api/events")
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/events", "raw_path": b"/api/events", "query_string": b"",
            "root_path": "",
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.outgoing.put))

    async def opened(self):
        message = await asyncio.wait_for(self.outgoing.get(), 5)
        assert message["type"] == "http.response.start"
        assert message["status"] == 200, message
        await self.invalidated()
        return self

    async def invalidated(self):
        async with asyncio.timeout(5):
            while True:
                message = await self.outgoing.get()
                body = message.get("body", b"")
                if b"event: invalidate\n" in body:
                    assert b"data: null\n" in body
                    return
                assert message.get("more_body", False), message

    async def close(self):
        self.incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 5)


def settings(test_database, **overrides):
    return Settings(
        database_url=test_database.gateway, migration_database_url=test_database.admin,
        session_secret="s" * 40,
        public_base_url="http://testserver", app_env="test", test_login_enabled=True,
        cookie_secure=False, sse_heartbeat_seconds=0.05, **overrides,
    )


async def signed_in(stack, app, username):
    client = await stack.enter_async_context(httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ))
    response = await client.post(
        "/auth/test", json={"username": username}, headers={"X-Test-Login": "1"},
    )
    assert response.status_code == 200, response.text
    session = response.json()
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    return client, session["user"]


async def rpc(client, function, **body):
    response = await client.post("/api/rpc/" + function, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def snapshot(client, league):
    response = await client.get(f"/api/leagues/{league}")
    assert response.status_code == 200, response.text
    return response.json()


def create_accounts(admin, count):
    names = ["events_" + uuid4().hex for _ in range(count)]
    with psycopg.connect(admin) as conn:
        for name in names:
            conn.execute(
                "INSERT INTO dl.app_user (issuer, subject, display_name) "
                "VALUES ('urn:draft:test', %s, %s)", (name, name),
            )
    return names


async def listener_pids(admin):
    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
        rows = await (await conn.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name = 'draft-events'"
        )).fetchall()
        return {row[0] for row in rows}


async def expect_listeners(admin, count):
    async with asyncio.timeout(5):
        while True:
            pids = await listener_pids(admin)
            if len(pids) == count:
                return pids
            await asyncio.sleep(0.02)


def test_account_discovery_reassignment_turns_tabs_and_reconnect(test_database):
    names = create_accounts(test_database.admin, 3)

    async def exercise():
        app = create_app(settings(test_database))
        async with app.router.lifespan_context(app), AsyncExitStack() as stack:
            commissioner, owner = await signed_in(stack, app, names[0])
            manager, first = await signed_in(stack, app, names[1])
            replacement, second = await signed_in(stack, app, names[2])
            tab1 = await BrowserStream(app, manager).opened()
            stack.push_async_callback(tab1.close)
            tab2 = await BrowserStream(app, manager).opened()
            stack.push_async_callback(tab2.close)
            other = await BrowserStream(app, replacement).opened()
            stack.push_async_callback(other.close)
            await expect_listeners(test_database.admin, 3)

            league = await rpc(commissioner, "create_league", p_name="Discovered", p_roster_size=2)
            team = await rpc(
                commissioner, "add_team", p_league=league, p_revision=0,
                p_name="Manager", p_manager=first["id"], p_point_budget=100,
            )
            await tab1.invalidated()
            await tab2.invalidated()
            assert league in [row["id"] for row in (await manager.get("/api/leagues")).json()]
            assert (await snapshot(manager, league))["revision"] == 1

            # Both former-manager tabs wake up after their management role changes.
            await rpc(
                commissioner, "update_team", p_league=league, p_revision=1,
                p_team=team, p_name="Reassigned", p_manager=second["id"], p_point_budget=100,
            )
            await tab1.invalidated()
            await tab2.invalidated()
            await other.invalidated()
            assert (await snapshot(manager, league))["revision"] == 2
            assert (await snapshot(manager, league))["actions"] == []
            assert (await snapshot(replacement, league))["revision"] == 2
            await rpc(commissioner, "remove_team", p_league=league, p_revision=2, p_team=team)
            await other.invalidated()
            assert (await snapshot(replacement, league))["revision"] == 3

            team = await rpc(
                commissioner, "add_team", p_league=league, p_revision=3,
                p_name="Returned", p_manager=first["id"], p_point_budget=100,
            )
            await tab1.invalidated()
            await tab2.invalidated()
            owner_team = await rpc(
                commissioner, "add_team", p_league=league, p_revision=4,
                p_name="Commissioner", p_manager=owner["id"], p_point_budget=100,
            )
            await tab1.invalidated()
            await tab2.invalidated()
            await rpc(
                commissioner, "set_draft_order", p_league=league, p_revision=5,
                p_teams=[owner_team, team],
            )
            await tab1.invalidated()
            await tab2.invalidated()
            await rpc(commissioner, "draft_action", p_league=league, p_revision=6, p_kind="start")
            await tab1.invalidated()
            await tab2.invalidated()
            state = await snapshot(manager, league)
            assert state["state"]["next_team_id"] == owner_team
            assert state["state"]["deadline"] is not None
            player = (await commissioner.get("/api/draftables")).json()[0]["id"]
            await rpc(
                commissioner, "draft_action", p_league=league, p_revision=7,
                p_kind="pick", p_draftable=player,
            )
            await tab1.invalidated()
            await tab2.invalidated()
            assert (await snapshot(manager, league))["state"]["next_team_id"] == team

            # Disconnect one browser while its sibling remains subscribed.
            before = await listener_pids(test_database.admin)
            await tab1.close()
            surviving = await expect_listeners(test_database.admin, 2)
            assert len(before - surviving) == 1
            await rpc(commissioner, "draft_action", p_league=league, p_revision=8, p_kind="undo")
            await tab2.invalidated()
            tab1 = await BrowserStream(app, manager).opened()
            stack.push_async_callback(tab1.close)
            assert (await snapshot(manager, league))["revision"] == 9

            # A failed dedicated DB connection ends only that browser's stream.
            new_pid, = (await expect_listeners(test_database.admin, 3)) - surviving
            async with await psycopg.AsyncConnection.connect(
                test_database.admin, autocommit=True,
            ) as conn:
                await conn.execute("SELECT pg_terminate_backend(%s)", (new_pid,))
            await asyncio.wait_for(tab1.task, 5)
            await expect_listeners(test_database.admin, 2)
            await rpc(
                commissioner, "draft_action", p_league=league, p_revision=9,
                p_kind="pick", p_draftable=player,
            )
            await tab2.invalidated()
            recovered = await BrowserStream(app, manager).opened()
            stack.push_async_callback(recovered.close)
            state = await snapshot(manager, league)
            assert state["revision"] == 10 and state["state"]["next_team_id"] == team
        await expect_listeners(test_database.admin, 0)

    asyncio.run(exercise(), loop_factory=asyncio.SelectorEventLoop)


def test_stream_expiry_and_cancellation_close_database_connections(test_database):
    name, = create_accounts(test_database.admin, 1)

    async def exercise():
        app = create_app(settings(test_database, session_ttl_seconds=2))
        async with app.router.lifespan_context(app), AsyncExitStack() as stack:
            manager, _ = await signed_in(stack, app, name)
            stream = await BrowserStream(app, manager).opened()
            stack.push_async_callback(stream.close)
            await expect_listeners(test_database.admin, 1)
            await asyncio.wait_for(stream.task, 4)
            await expect_listeners(test_database.admin, 0)
            assert (await manager.get("/api/leagues")).status_code == 200
            assert (await manager.get("/auth/me")).status_code == 401

            manager, _ = await signed_in(stack, app, name)
            cancelled = await BrowserStream(app, manager).opened()
            await expect_listeners(test_database.admin, 1)
            cancelled.task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(cancelled.task, 5)
            await expect_listeners(test_database.admin, 0)

    asyncio.run(exercise(), loop_factory=asyncio.SelectorEventLoop)
