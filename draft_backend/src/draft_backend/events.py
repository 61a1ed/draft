"""One LISTEN connection per worker; coalesced invalidations, no event log."""

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import suppress

import psycopg
from fastapi.encoders import jsonable_encoder

log = logging.getLogger(__name__)


class EventHub:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.subscribers = defaultdict(set)
        self.connected = asyncio.Event()
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self._listen(), name="postgres-listen")
        try:
            await asyncio.wait_for(self.connected.wait(), timeout=20)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def subscribe(self, league_id: int):
        queue = asyncio.Queue(maxsize=1)
        self.subscribers[league_id].add(queue)
        return queue

    def unsubscribe(self, league_id: int, queue):
        queues = self.subscribers.get(league_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self.subscribers[league_id]

    @staticmethod
    def _offer(queue, value):
        # Revisions describe current state; intermediate invalidations can merge.
        # A pending reset must survive until the subscriber has resynchronized.
        if queue.full():
            previous = queue.get_nowait()
            if previous is None:
                value = None
        queue.put_nowait(value)

    def publish(self, payload):
        for queue in tuple(self.subscribers.get(payload["league_id"], ())):
            self._offer(queue, payload)

    def reset(self):
        for queues in tuple(self.subscribers.values()):
            for queue in tuple(queues):
                self._offer(queue, None)

    async def _listen(self):
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.database_url,
                    autocommit=True,
                    connect_timeout=10,
                    keepalives_idle=15,
                    keepalives_interval=5,
                    keepalives_count=3,
                ) as conn:
                    await conn.execute("LISTEN dl_changed")
                    self.connected.set()
                    self.reset()  # Any disconnected interval requires a snapshot.
                    async for notice in conn.notifies():
                        try:
                            payload = json.loads(notice.payload)
                            if (
                                not isinstance(payload, dict)
                                or type(payload.get("league_id")) is not int
                                or type(payload.get("revision")) is not int
                            ):
                                continue
                        except (ValueError, TypeError):
                            continue
                        self.publish(payload)
            except (psycopg.Error, OSError):
                # No DSN, credentials, or raw provider error in logs.
                log.warning("Draft notification connection lost; reconnecting")
            finally:
                self.connected.clear()
                self.reset()
            await asyncio.sleep(1)


def event(kind, payload, revision=None):
    prefix = f"id: {revision}\n" if revision is not None else ""
    return f"{prefix}event: {kind}\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"
