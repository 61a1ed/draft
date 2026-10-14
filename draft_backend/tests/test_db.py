"""The retry boundary must include COMMIT, not just the SQL statement."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from draft_backend.db import Database


@pytest.mark.parametrize(
    "failure", [psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected]
)
async def test_commit_failure_repeats_whole_transaction_with_same_identity_and_arguments(
    monkeypatch, failure
):
    monkeypatch.setattr("draft_backend.db.asyncio.sleep", AsyncMock())
    attempts = []
    calls = []
    params = (17, 42, "pick", 5)

    @asynccontextmanager
    async def transaction():
        attempts.append([])
        yield
        if len(attempts) == 1:
            raise failure("concurrent transaction won at commit")

    async def execute(query, parameters=()):
        attempts[-1].append((query, parameters))

    async def execute_rpc(query, parameters):
        calls.append((query, parameters))

    async def fetchall():
        # A result obtained before failed COMMIT must never reach the caller.
        return [{"result": len(attempts)}]

    @asynccontextmanager
    async def cursor():
        yield SimpleNamespace(execute=execute_rpc, fetchall=fetchall)

    conn = SimpleNamespace(transaction=transaction, execute=execute, cursor=cursor)

    @asynccontextmanager
    async def connection():
        yield conn

    db = Database.__new__(Database)
    db.pool = SimpleNamespace(connection=connection)
    db.transaction_retries = 2
    query = "SELECT dl.draft_action(%s, %s, %s, %s) AS result"

    assert await db.write(8, query, params) == [{"result": 2}]
    assert len(attempts) == 2
    assert calls == [(query, params), (query, params)]
    assert all(arguments is params for _, arguments in calls)
    for attempt in attempts:
        assert "SERIALIZABLE" in attempt[0][0]
        assert ("SET LOCAL ROLE dl_api", ()) in attempt
        claims = [args for sql, args in attempt if "request.jwt.claims" in sql]
        assert len(claims) == 1
        assert json.loads(claims[0][0]) == {"user_id": 8}


async def test_stale_revision_is_not_retried():
    class StaleRevision(psycopg.DatabaseError):
        sqlstate = "PT409"

    db = Database.__new__(Database)
    db.transaction_retries = 4
    db._execute = AsyncMock(side_effect=StaleRevision("stale_revision"))
    with pytest.raises(StaleRevision):
        await db.write(8, "SELECT dl.draft_action(%s, %s, %s, %s)", (17, 42, "pick", 5))
    db._execute.assert_awaited_once()


async def test_retry_budget_is_bounded(monkeypatch):
    monkeypatch.setattr("draft_backend.db.asyncio.sleep", AsyncMock())
    db = Database.__new__(Database)
    db.transaction_retries = 2
    db._execute = AsyncMock(side_effect=psycopg.errors.SerializationFailure("retry"))
    with pytest.raises(psycopg.errors.SerializationFailure):
        await db.write(8, "SELECT 1")
    assert db._execute.await_count == 3
