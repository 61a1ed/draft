"""Dedicated listener ownership, invalidation ordering, and termination paths."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from draft_backend.events import DraftEventResponse


class Connection:
    def __init__(self):
        self.execute = AsyncMock()
        self.close = AsyncMock(side_effect=self._close)
        self.pending = asyncio.Queue()
        self.listening = False
        self.closed = False

    async def _close(self):
        # Real cleanup must remain usable under Starlette's cancellation scope.
        await asyncio.sleep(0)
        self.closed = True

    async def notifies(self, *, timeout):
        self.listening = True
        try:
            async with asyncio.timeout(timeout):
                while True:
                    value = await self.pending.get()
                    if isinstance(value, BaseException):
                        raise value
                    yield value
        except TimeoutError:
            return
        finally:
            self.listening = False


def response(*, slots=None, expires_at=None, heartbeat=0.02):
    return DraftEventResponse(
        SimpleNamespace(database_url="unused", sse_heartbeat_seconds=heartbeat),
        42,
        expires_at if expires_at is not None else time.time() + 60,
        slots if slots is not None else asyncio.Semaphore(2),
    )


async def next_message(messages):
    return await asyncio.wait_for(messages.get(), timeout=1)


async def stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_listen_precedes_headers_and_initial_invalidation_then_forwards_every_notice(
    monkeypatch,
):
    conn = Connection()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    listened = asyncio.Event()

    async def listen(query):
        assert query.as_string() == 'LISTEN "dl_draft_42"'
        await listened.wait()

    conn.execute.side_effect = listen
    value = response()
    messages = asyncio.Queue()
    task = asyncio.create_task(value.stream_response(messages.put))
    await asyncio.sleep(0)
    assert messages.empty()
    listened.set()
    assert (await next_message(messages))["type"] == "http.response.start"
    assert (await next_message(messages))["body"] == b"event: invalidate\ndata: null\n\n"
    for payload in ["", "ignored-private-payload"]:
        conn.pending.put_nowait(SimpleNamespace(payload=payload))
    for _ in range(2):
        assert (await next_message(messages))["body"] == b"event: invalidate\ndata: null\n\n"
    assert (await next_message(messages))["body"] == b": keep-alive\n\n"
    assert connect.await_args.kwargs["autocommit"] is True
    assert connect.await_args.kwargs["application_name"] == "draft-events"
    assert value.headers["x-accel-buffering"] == "no"
    await stop(task)
    conn.close.assert_awaited_once()
    assert conn.closed and not conn.listening
    assert value.slots._value == 2


async def test_two_tabs_have_independent_connections_and_closing_one_keeps_the_other(monkeypatch):
    first, second = Connection(), Connection()
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    slots = asyncio.Semaphore(2)
    streams = [response(slots=slots), response(slots=slots)]
    queues = [asyncio.Queue(), asyncio.Queue()]
    tasks = [asyncio.create_task(s.stream_response(q.put)) for s, q in zip(streams, queues)]
    try:
        for queue in queues:
            await next_message(queue)
            assert b"invalidate" in (await next_message(queue))["body"]
        assert slots.locked()
        for conn in [first, second]:
            assert conn.execute.await_args.args[0].as_string() == 'LISTEN "dl_draft_42"'
        await stop(tasks[0])
        assert first.closed and not second.closed
        assert slots._value == 1
        second.pending.put_nowait(object())
        assert b"invalidate" in (await next_message(queues[1]))["body"]
    finally:
        if not tasks[1].done():
            await stop(tasks[1])
    assert second.closed and slots._value == 2


async def test_database_failure_ends_stream_and_reconnect_starts_with_an_invalidation(monkeypatch):
    first, second = Connection(), Connection()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(side_effect=[first, second]))
    slots = asyncio.Semaphore(1)
    messages = asyncio.Queue()
    task = asyncio.create_task(response(slots=slots).stream_response(messages.put))
    await next_message(messages)
    await next_message(messages)
    first.pending.put_nowait(psycopg.OperationalError("private database error"))
    assert (await next_message(messages))["more_body"] is False
    await task
    assert first.closed and slots._value == 1
    task = asyncio.create_task(response(slots=slots).stream_response(messages.put))
    await next_message(messages)
    assert b"invalidate" in (await next_message(messages))["body"]
    await stop(task)
    assert second.closed and slots._value == 1


async def test_session_expiry_closes_connection_before_ending_response(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    value = response(expires_at=time.time() + 0.04, heartbeat=10)
    messages = []

    async def send(message):
        messages.append(message)
        if message.get("more_body") is False:
            assert conn.closed

    await value.stream_response(send)
    assert len(messages) == 3
    assert messages[-1]["more_body"] is False
    assert conn.closed and not conn.listening and value.slots._value == 2


@pytest.mark.parametrize("failure", [psycopg.OperationalError("private"), OSError("private")])
async def test_listen_failure_closes_connection_without_starting_response(monkeypatch, failure):
    conn = Connection()
    conn.execute.side_effect = failure
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    value = response()
    send = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await value.stream_response(send)
    assert error.value.status_code == 503
    assert error.value.detail == "database_unavailable"
    send.assert_not_awaited()
    assert conn.closed and value.slots._value == 2


async def test_session_expiry_during_listen_closes_without_starting_response(monkeypatch):
    conn = Connection()

    async def wait_forever(_):
        await asyncio.Future()

    conn.execute.side_effect = wait_forever
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    value = response(expires_at=time.time() + 0.02)
    send = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await value.stream_response(send)
    assert error.value.status_code == 401
    send.assert_not_awaited()
    assert conn.closed and value.slots._value == 2


async def test_browser_disconnect_cancels_wait_and_shields_connection_cleanup(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    value = response(heartbeat=10)
    messages, incoming = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(value({"type": "http"}, incoming.get, messages.put))
    await next_message(messages)
    await next_message(messages)
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 1)
    assert conn.closed and not conn.listening and value.slots._value == 2


async def test_socket_send_failure_closes_suspended_notification_generator(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    value = response()
    conn.pending.put_nowait(object())
    count = 0

    async def send(message):
        nonlocal count
        count += 1
        if count == 3:
            assert conn.listening
            raise OSError("socket closed")

    with pytest.raises(ClientDisconnect):
        await value({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
    assert conn.closed and not conn.listening and value.slots._value == 2


async def test_capacity_rejects_stream_before_opening_a_connection(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(HTTPException) as error:
        await response(slots=asyncio.Semaphore(0)).stream_response(AsyncMock())
    assert error.value.status_code == 503
    connect.assert_not_awaited()
