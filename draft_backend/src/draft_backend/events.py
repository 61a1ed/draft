"""One dedicated PostgreSQL listener per browser stream; notifications are wake-ups."""

import asyncio
import json
import logging
import time
from contextlib import aclosing

import anyio
import psycopg
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from starlette.responses import StreamingResponse

log = logging.getLogger(__name__)


def event(kind, payload=None):
    return f"event: {kind}\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"


class DraftEventResponse(StreamingResponse):
    """Own the connection for the complete response, including failed socket sends.

    Opening inside stream_response also lets Starlette cancel connection setup on
    disconnect. There are no pooled listeners or subscriber mappings.
    """

    def __init__(self, settings, league_id: int, expires_at: int, slots: asyncio.Semaphore):
        super().__init__(
            content=(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
        self.settings = settings
        self.league_id = league_id
        self.expires_at = expires_at
        self.slots = slots

    async def _notifications(self, conn):
        # LISTEN is already committed, so setup races are covered by this refresh.
        yield event("invalidate")
        try:
            while True:
                async with aclosing(
                    conn.notifies(timeout=self.settings.sse_heartbeat_seconds)
                ) as notices:
                    async for _ in notices:
                        yield event("invalidate")
                yield ": keep-alive\n\n"
        except (psycopg.Error, OSError):
            # Closing forces a fresh listener and snapshot after any missed interval.
            # Do not include provider errors, connection strings, or credentials.
            log.warning("Draft notification connection lost; ending stream")

    async def stream_response(self, send):
        # Admission is immediate: reserve query capacity rather than letting idle
        # streams occupy every PostgreSQL connection. The semaphore is per worker.
        if self.slots.locked():
            raise HTTPException(503, "event_stream_capacity", headers={"Retry-After": "2"})
        await self.slots.acquire()
        conn = None
        notifications = None
        started = False
        expired = False
        try:
            try:
                # Absolute session expiry also bounds connection setup and slow sends.
                async with asyncio.timeout(max(0, self.expires_at - time.time())):
                    try:
                        conn = await psycopg.AsyncConnection.connect(
                            self.settings.database_url,
                            autocommit=True,
                            connect_timeout=10,
                            application_name="draft-events",
                            keepalives_idle=15,
                            keepalives_interval=5,
                            keepalives_count=3,
                        )
                        # The endpoint validated this numeric league ID and its visibility.
                        # Quote the derived channel as an identifier, never raw SQL.
                        await conn.execute(
                            psycopg.sql.SQL("LISTEN {}").format(
                                psycopg.sql.Identifier(f"dl_draft_{self.league_id}")
                            )
                        )
                    except (psycopg.Error, OSError):
                        raise HTTPException(503, "database_unavailable") from None
                    notifications = self._notifications(conn)
                    self.body_iterator = notifications
                    started = True
                    await super().stream_response(send)
            except TimeoutError:
                if not started:
                    raise HTTPException(401, "sign_in_required") from None
                expired = True
        finally:
            # Starlette may cancel this task repeatedly after a browser disconnect.
            # Close the iterator (and its notifies generator) before the connection.
            with anyio.CancelScope(shield=True):
                try:
                    if notifications is not None:
                        await notifications.aclose()
                finally:
                    try:
                        if conn is not None:
                            await conn.close()
                    finally:
                        self.slots.release()
        if expired:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
