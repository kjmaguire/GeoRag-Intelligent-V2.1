"""§10.12 — cross-workspace access audit emission tests.

Verifies:
  - First call emits a ``security.cross_workspace_access.alert`` row
  - Repeat call within the idempotency window does NOT emit a new row
  - After window expiry (we use a short window in-test), a second row
    is emitted
  - Fail-open: Redis down still produces an audit row
"""
from __future__ import annotations

import os
import uuid
from uuid import UUID

import asyncpg
import pytest

from app.services.cross_workspace_audit import emit_cross_workspace_alert

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)


def _redis_url() -> str:
    """Build a Redis URL honoring REDIS_URL or the {HOST,PORT,PASSWORD} trio."""
    explicit = os.environ.get("REDIS_URL")
    if explicit:
        return explicit
    host = os.environ.get("REDIS_HOST", "localhost")
    port = os.environ.get("REDIS_PORT", "6379")
    password = os.environ.get("REDIS_PASSWORD")
    if password:
        return f"redis://:{password}@{host}:{port}/0"
    return f"redis://{host}:{port}/0"


REDIS_URL = _redis_url()

# JWT-derived workspace anchor — must be a real row in workspaces (FK).
JWT_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def redis_client():
    import redis.asyncio as redis_asyncio
    client = redis_asyncio.from_url(REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


async def _count_alerts(
    conn: asyncpg.Connection,
    target_workspace_id: UUID,
    actor_id: int,
) -> int:
    return int(await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'security.cross_workspace_access.alert'
           AND target_id = $1
           AND actor_id = $2
        """,
        str(target_workspace_id), actor_id,
    ))


async def _cleanup(
    conn: asyncpg.Connection,
    target_workspace_id: UUID,
    actor_id: int,
) -> None:
    await conn.execute(
        """
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'security.cross_workspace_access.alert'
           AND target_id = $1
           AND actor_id = $2
        """,
        str(target_workspace_id), actor_id,
    )


@pytest.mark.asyncio
async def test_first_call_emits_alert(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
    redis_client,
):
    target_ws = uuid.uuid4()
    actor = 999001

    # Clear any prior dedupe key
    await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")

    try:
        emitted = await emit_cross_workspace_alert(
            pg_pool,
            actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/api/v1/test",
            redis_client=redis_client,
            window_s=60,
        )
        assert emitted is True
        assert await _count_alerts(pg_conn, target_ws, actor) == 1
    finally:
        await _cleanup(pg_conn, target_ws, actor)
        await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")


@pytest.mark.asyncio
async def test_repeat_within_window_dedupes(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
    redis_client,
):
    target_ws = uuid.uuid4()
    actor = 999002

    await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")

    try:
        first = await emit_cross_workspace_alert(
            pg_pool,
            actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/api/v1/test",
            redis_client=redis_client,
            window_s=60,
        )
        second = await emit_cross_workspace_alert(
            pg_pool,
            actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/api/v1/test",
            redis_client=redis_client,
            window_s=60,
        )
        assert first is True
        assert second is False, "repeat within window must dedupe"
        assert await _count_alerts(pg_conn, target_ws, actor) == 1
    finally:
        await _cleanup(pg_conn, target_ws, actor)
        await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")


@pytest.mark.asyncio
async def test_window_expiry_allows_second_emission(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
    redis_client,
):
    """Short 1-second window — sleep past expiry and re-emit."""
    import asyncio
    target_ws = uuid.uuid4()
    actor = 999003

    await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")

    try:
        first = await emit_cross_workspace_alert(
            pg_pool, actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/x", redis_client=redis_client, window_s=1,
        )
        assert first is True
        await asyncio.sleep(1.5)
        second = await emit_cross_workspace_alert(
            pg_pool, actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/x", redis_client=redis_client, window_s=1,
        )
        assert second is True
        assert await _count_alerts(pg_conn, target_ws, actor) == 2
    finally:
        await _cleanup(pg_conn, target_ws, actor)
        await redis_client.delete(f"georag:xworkspace_audit:{actor}:{target_ws}")


@pytest.mark.asyncio
async def test_redis_down_still_emits_alert(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
):
    """Pass redis_client=None — fail-open behaviour."""
    target_ws = uuid.uuid4()
    actor = 999004
    try:
        emitted = await emit_cross_workspace_alert(
            pg_pool,
            actor_user_id=actor,
            jwt_workspace_id=JWT_WORKSPACE_ID,
            target_workspace_id=target_ws,
            request_path="/y",
            redis_client=None,
        )
        assert emitted is True
        assert await _count_alerts(pg_conn, target_ws, actor) == 1
    finally:
        await _cleanup(pg_conn, target_ws, actor)
