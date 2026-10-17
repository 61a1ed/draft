"""Transaction-scoped database identity and the small authentication boundary.

Queries passed to read/write are application-owned SQL, never client-supplied SQL.
PostgreSQL remains responsible for authorization and all state transitions.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self.transaction_retries = settings.transaction_retries
        self.pool = AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=settings.pool_size,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )

    async def open(self) -> None:
        await self.pool.open()
        try:
            await self.pool.wait()
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT session_user AS login, current_user AS active_role,
                           r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb,
                           pg_has_role(session_user, n.nspowner, 'MEMBER') AS owner_member
                    FROM pg_catalog.pg_roles r
                    JOIN pg_catalog.pg_namespace n ON n.nspname = 'dl'
                    WHERE r.rolname = session_user
                    """
                )
                identity = await cursor.fetchone()
                if (
                    identity is None
                    or identity["login"] != "dl_gateway"
                    or identity["active_role"] != "dl_gateway"
                    or any(
                        identity[key]
                        for key in (
                            "rolsuper",
                            "rolbypassrls",
                            "rolcreaterole",
                            "rolcreatedb",
                            "owner_member",
                        )
                    )
                ):
                    raise RuntimeError(
                        "DATABASE_URL must use the unprivileged dl_gateway login "
                        "from the deployment SQL; the dl schema must be installed."
                    )
                # Fail at startup if provisioning omitted either service role.
                async with conn.transaction():
                    await conn.execute("SET LOCAL ROLE dl_api")
                    await conn.execute("SET LOCAL ROLE dl_auth")
        except BaseException:
            await self.pool.close()
            raise

    async def close(self) -> None:
        await self.pool.close()

    async def ready(self) -> None:
        """Exercise a pooled connection; the caller bounds acquisition and query time."""
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")

    async def _execute(
        self, user_id: int | None, query: str, params: Any, *, write: bool
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # SET ROLE does not apply ALTER ROLE configuration defaults.
                await conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
                    if write
                    else "SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY"
                )
                await conn.execute("SET LOCAL ROLE dl_api")
                await conn.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"user_id": user_id}),),
                )
                async with conn.cursor() as cursor:
                    await cursor.execute(query, params)
                    rows = await cursor.fetchall()
            # Commit has completed before returning results to the HTTP layer.
        return rows

    async def read(self, user_id: int | None, query: str, params: Any = ()) -> list[dict[str, Any]]:
        return await self._execute(user_id, query, params, write=False)

    async def tournament(self, user_id: int | None, league_id: int) -> dict[str, Any] | None:
        """Public current state from one statement, so revisions and evidence agree."""
        rows = await self.read(
            user_id,
            """
            WITH stages AS (
                SELECT * FROM dl.tournament_stage WHERE tournament_id = %s
            ), rounds AS (
                SELECT r.* FROM dl.tournament_round r JOIN stages s ON s.id = r.stage_id
            ), matches AS (
                SELECT m.* FROM dl.match m JOIN rounds r ON r.id = m.round_id
            )
            SELECT t.*,
                coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.position, s.id)
                    FROM stages s), '[]'::jsonb) AS stages,
                coalesce((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.stage_id, e.team_id)
                    FROM dl.stage_entry e JOIN stages s ON s.id = e.stage_id),
                    '[]'::jsonb) AS entries,
                coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.stage_id, r.number, r.id)
                    FROM rounds r), '[]'::jsonb) AS rounds,
                coalesce((SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id)
                    FROM matches m), '[]'::jsonb) AS matches,
                coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)
                    FROM dl.result_submission s JOIN matches m ON m.id = s.match_id
                    WHERE s.id IN (m.team_a_submission_id, m.team_b_submission_id)),
                    '[]'::jsonb) AS current_submissions,
                coalesce((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.match_id)
                    FROM dl.match_official_result o JOIN matches m ON m.id = o.match_id),
                    '[]'::jsonb) AS official_results,
                coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.stage_id, r.team_id)
                    FROM dl.stage_record r JOIN stages s ON s.id = r.stage_id),
                    '[]'::jsonb) AS stage_records,
                coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.round_id, r.team_id)
                    FROM dl.round_record r JOIN rounds tr ON tr.id = r.round_id),
                    '[]'::jsonb) AS round_records,
                coalesce((SELECT jsonb_agg(to_jsonb(w) ORDER BY w.team_id)
                    FROM dl.tournament_withdrawal w JOIN dl.team tm ON tm.id = w.team_id
                    WHERE tm.league_id = t.league_id), '[]'::jsonb) AS withdrawals,
                coalesce((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.match_id)
                    FROM dl.match_disposition d JOIN matches m ON m.id = d.match_id),
                    '[]'::jsonb) AS dispositions
            FROM dl.tournament t WHERE t.league_id = %s
            """,
            (league_id, league_id),
        )
        return rows[0] if rows else None

    async def match(self, user_id: int | None, match_id: int) -> dict[str, Any] | None:
        """Public fixture context and complete immutable evidence/decision history."""
        rows = await self.read(
            user_id,
            """
            SELECT m.*, s.tournament_id AS league_id, r.stage_id,
                to_jsonb(o) AS official_result, to_jsonb(d) AS disposition,
                coalesce((SELECT jsonb_agg(to_jsonb(rs) ORDER BY rs.id)
                    FROM dl.result_submission rs WHERE rs.match_id = m.id),
                    '[]'::jsonb) AS submissions,
                coalesce((SELECT jsonb_agg(to_jsonb(md) ORDER BY md.id)
                    FROM dl.match_decision md WHERE md.match_id = m.id),
                    '[]'::jsonb) AS decisions
            FROM dl.match m
            JOIN dl.tournament_round r ON r.id = m.round_id
            JOIN dl.tournament_stage s ON s.id = r.stage_id
            LEFT JOIN dl.match_official_result o ON o.match_id = m.id
            LEFT JOIN dl.match_disposition d ON d.match_id = m.id
            WHERE m.id = %s
            """,
            (match_id,),
        )
        return rows[0] if rows else None

    async def team_availability(self, user_id: int | None, team_id: int) -> dict[str, Any] | None:
        """An existing team has an empty, unrevisioned state before its first edit."""
        rows = await self.read(
            user_id,
            """
            SELECT t.id AS team_id, to_jsonb(a) AS availability,
                coalesce((SELECT jsonb_agg(to_jsonb(w) ORDER BY w.starts_at, w.ends_at, w.id)
                    FROM dl.availability_window w WHERE w.team_id = t.id),
                    '[]'::jsonb) AS windows
            FROM dl.team t LEFT JOIN dl.team_availability a ON a.team_id = t.id
            WHERE t.id = %s
            """,
            (team_id,),
        )
        return rows[0] if rows else None

    async def image(self, image_hash: str) -> dict[str, Any] | None:
        """Public immutable asset lookup; no user identity or league access."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SET LOCAL ROLE dl_api")
                cursor = await conn.execute(
                    "SELECT content_type, data FROM dl.asset WHERE hash = %s", (image_hash,)
                )
                return await cursor.fetchone()

    async def write(self, user_id: int, query: str, params: Any = ()) -> list[dict[str, Any]]:
        """Retry aborted transactions, including failures raised during commit.

        transaction_retries counts retries after the initial attempt. Arguments,
        including the client's revision, remain unchanged. PT409 is not retried.
        """
        for attempt in range(self.transaction_retries + 1):
            try:
                return await self._execute(user_id, query, params, write=True)
            except psycopg.Error as exc:
                if exc.sqlstate not in {"40001", "40P01"} or attempt == self.transaction_retries:
                    raise
                await asyncio.sleep(random.uniform(0, min(0.4, 0.025 * 2**attempt)))
        raise AssertionError("unreachable")

    async def register_identity(
        self, issuer: str, subject: str, display_name: str
    ) -> dict[str, Any]:
        """Call only with the verified issuer and subject from an ID token; registration is open."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
                await conn.execute("SET LOCAL ROLE dl_auth")
                cursor = await conn.execute(
                    """
                    INSERT INTO dl.app_user (issuer, subject, display_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (issuer, subject) DO UPDATE
                    SET display_name = EXCLUDED.display_name
                    RETURNING id, display_name
                    """,
                    (issuer, subject, display_name),
                )
                result = await cursor.fetchone()
                assert result is not None
        return result

    async def test_user(self, username: str) -> dict[str, Any] | None:
        """Look up an existing fixture; this method never creates a test user."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SET LOCAL ROLE dl_auth")
                cursor = await conn.execute(
                    "SELECT id, display_name FROM dl.app_user "
                    "WHERE issuer = 'urn:draft:test' AND subject = %s",
                    (username,),
                )
                return await cursor.fetchone()

    async def user(self, user_id: int) -> dict[str, Any] | None:
        rows = await self.read(
            user_id, "SELECT id, display_name FROM dl.app_user WHERE id = %s", (user_id,)
        )
        return rows[0] if rows else None

    async def is_test_database(self) -> bool:
        """Check the test-tool marker as gateway; permission failures are errors."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                cursor = await conn.execute("SELECT to_regclass('dl_test.marker') AS marker")
                result = await cursor.fetchone()
                if result is None or result["marker"] is None:
                    return False
                cursor = await conn.execute(
                    "SELECT EXISTS (SELECT FROM dl_test.marker WHERE singleton) AS is_test"
                )
                result = await cursor.fetchone()
                return bool(result and result["is_test"])
