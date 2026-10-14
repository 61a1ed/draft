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
    def __init__(self, database_url: str, channel: str):
        self.database_url = database_url
        self.channel = channel
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

    def subscribe(self, topic: int):
        queue = asyncio.Queue(maxsize=1)
        self.subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: int, queue):
        queues = self.subscribers.get(topic)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self.subscribers[topic]

    @staticmethod
    def _offer(queue):
        if not queue.full():
            queue.put_nowait(None)

    def invalidate(self, topic):
        for queue in tuple(self.subscribers.get(topic, ())):
            self._offer(queue)

    def invalidate_all(self):
        for queues in tuple(self.subscribers.values()):
            for queue in tuple(queues):
                self._offer(queue)

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
                    await conn.execute(
                        psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(self.channel))
                    )
                    self.connected.set()
                    self.invalidate_all()  # Any disconnected interval requires a snapshot.
                    async for notice in conn.notifies():
                        self.invalidate(notice.payload)
            except (psycopg.Error, OSError):
                # No DSN, credentials, or raw provider error in logs.
                log.warning("Draft notification connection lost; reconnecting")
            finally:
                self.connected.clear()
            await asyncio.sleep(1)


def event(kind, payload=None):
    return f"event: {kind}\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"
