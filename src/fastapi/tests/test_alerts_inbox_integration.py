"""Live-stack integration tests for the alerts inbox + acknowledge flow.

Inserts synthetic *.alert rows into audit.audit_ledger, hits the running
FastAPI endpoint, asserts the listing + filter + pagination + ack flow
works end-to-end, then cleans up.

Requires the Docker stack:
    docker compose up -d fastapi postgresql pgbouncer
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

FASTAPI_URL = os.environ.get("FASTAPI_URL", "http://localhost:8000")
SERVICE_KEY = os.environ.get("FASTAPI_SERVICE_KEY", "georag-service-key-dev")
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def _insert_alert(
    conn: asyncpg.Connection,
    action_type: str,
    target_id: str,
    severity: str = "high",
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO audit.audit_ledger
            (workspace_id, actor_id, actor_kind, action_type,
             target_schema, target_table, target_id, payload)
        VALUES
            (NULL, 1, 'system', $1, 'audit', 'audit_ledger', $2, $3::jsonb)
        RETURNING id::text AS id
        """,
        action_type, target_id,
        f'{{"severity":"{severity}","test":"alerts-inbox-it"}}',
    )
    return row["id"]


async def _cleanup(conn: asyncpg.Connection, target_id_prefix: str) -> None:
    await conn.execute(
        "DELETE FROM audit.audit_ledger WHERE target_id LIKE $1",
        f"{target_id_prefix}%",
    )


def _headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_KEY, "Accept": "application/json"}


@pytest.mark.asyncio
async def test_alerts_inbox_lists_inserted_rows(pg_conn: asyncpg.Connection) -> None:
    tag = f"it-{uuid.uuid4().hex[:8]}"
    try:
        await _insert_alert(pg_conn, "cost.burn.alert", f"{tag}-1", "high")
        await _insert_alert(pg_conn, "vllm_security.alert", f"{tag}-2", "critical")

        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            r = await client.get(
                "/api/v1/admin/alerts-inbox",
                headers=_headers(),
                params={"action_type_prefix": "cost.", "limit": 50},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 1
        ids = [i["target_id"] for i in body["items"]]
        assert f"{tag}-1" in ids
        # cost. prefix filter must NOT return vllm row
        assert f"{tag}-2" not in ids
    finally:
        await _cleanup(pg_conn, tag)


@pytest.mark.asyncio
async def test_alerts_inbox_pagination_offset(pg_conn: asyncpg.Connection) -> None:
    tag = f"itp-{uuid.uuid4().hex[:8]}"
    try:
        for i in range(5):
            await _insert_alert(pg_conn, "ingestion.breach.alert", f"{tag}-{i}", "medium")

        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            r1 = await client.get(
                "/api/v1/admin/alerts-inbox",
                headers=_headers(),
                params={"action_type_prefix": "ingestion.", "limit": 2, "offset": 0},
            )
            r2 = await client.get(
                "/api/v1/admin/alerts-inbox",
                headers=_headers(),
                params={"action_type_prefix": "ingestion.", "limit": 2, "offset": 2},
            )
        body1 = r1.json()
        body2 = r2.json()
        assert body1["total"] >= 5
        assert body2["total"] >= 5
        ids1 = {i["target_id"] for i in body1["items"]}
        ids2 = {i["target_id"] for i in body2["items"]}
        # No overlap between pages
        assert ids1.isdisjoint(ids2)
    finally:
        await _cleanup(pg_conn, tag)


@pytest.mark.asyncio
async def test_alerts_inbox_acknowledge_roundtrip(pg_conn: asyncpg.Connection) -> None:
    tag = f"ita-{uuid.uuid4().hex[:8]}"
    try:
        audit_id = await _insert_alert(pg_conn, "cost.burn.alert", f"{tag}-1", "low")

        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            # Acknowledge it
            ack = await client.post(
                "/api/v1/admin/alerts-inbox/acknowledge",
                headers=_headers(),
                json={"audit_id": audit_id, "actor_id": 1},
            )
            assert ack.status_code == 201, ack.text
            assert ack.json()["acknowledged_action"] == "cost.burn.alert.acknowledged"

            # Default listing must hide the acknowledged row
            r_default = await client.get(
                "/api/v1/admin/alerts-inbox",
                headers=_headers(),
                params={"action_type_prefix": "cost."},
            )
            ids_default = {i["target_id"] for i in r_default.json()["items"]}
            assert f"{tag}-1" not in ids_default

            # include_acknowledged=true must show it AND have ack timestamp populated
            r_all = await client.get(
                "/api/v1/admin/alerts-inbox",
                headers=_headers(),
                params={"action_type_prefix": "cost.", "include_acknowledged": "true"},
            )
            matches = [i for i in r_all.json()["items"] if i["target_id"] == f"{tag}-1"]
            assert len(matches) == 1
            assert matches[0]["acknowledged_at"] is not None
            assert matches[0]["acknowledged_by_user_id"] == 1
    finally:
        await _cleanup(pg_conn, tag)


@pytest.mark.asyncio
async def test_acknowledge_rejects_malformed_uuid() -> None:
    """Pydantic UUID coercion must produce 422 for a non-UUID audit_id —
    proves we don't fall through to a 500 from the SQL layer."""
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.post(
            "/api/v1/admin/alerts-inbox/acknowledge",
            headers=_headers(),
            json={"audit_id": "not-a-uuid", "actor_id": 1},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_acknowledge_rejects_non_alert_row(pg_conn: asyncpg.Connection) -> None:
    """If audit_id points at a row whose action_type doesn't end in .alert,
    the endpoint must 400 — protects against ack-bombing arbitrary rows."""
    tag = f"itn-{uuid.uuid4().hex[:8]}"
    try:
        # Insert a non-alert audit row directly
        row = await pg_conn.fetchrow(
            """
            INSERT INTO audit.audit_ledger
                (workspace_id, actor_id, actor_kind, action_type,
                 target_schema, target_table, target_id, payload)
            VALUES
                (NULL, 1, 'system', 'report.build.planned', 'audit', 'audit_ledger', $1, '{}'::jsonb)
            RETURNING id::text AS id
            """,
            f"{tag}-not-alert",
        )

        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            r = await client.post(
                "/api/v1/admin/alerts-inbox/acknowledge",
                headers=_headers(),
                json={"audit_id": row["id"], "actor_id": 1},
            )
        assert r.status_code == 400
    finally:
        await _cleanup(pg_conn, tag)
