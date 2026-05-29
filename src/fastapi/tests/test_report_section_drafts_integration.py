"""Live-stack integration tests for the §7 per-section editor.

Verifies:
  - Build planning creates an audit anchor
  - PUT section draft writes a report.build.section.drafted audit row
  - GET build envelope surfaces the latest draft per section
  - Re-PUT updates the draft in place (DISTINCT ON returns latest)
  - 404 when build_id is unknown
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


@pytest.fixture
async def workspace_id(pg_conn: asyncpg.Connection) -> str:
    row = await pg_conn.fetchrow(
        "SELECT workspace_id::text AS workspace_id FROM silver.workspaces LIMIT 1",
    )
    if row is None:
        pytest.skip("no rows in silver.workspaces — seed required for §7 IT")
    return row["workspace_id"]


def _headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_KEY, "Content-Type": "application/json"}


async def _plan_build(workspace_id: str) -> tuple[str, str]:
    """Plan a build; return (build_id, first_section_id)."""
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.post(
            "/api/v1/admin/reports/build",
            headers=_headers(),
            json={
                "report_type": "weekly_project_digest",
                "workspace_id": workspace_id,
                "project_id": "22222222-2222-2222-2222-222222222222",
                "requested_by_user_id": 1,
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sections_planned"] >= 1
    return body["build_id"], body["sections"][0]["section_id"]


async def _cleanup_build(conn: asyncpg.Connection, build_id: str) -> None:
    await conn.execute(
        "DELETE FROM audit.audit_ledger WHERE target_id = $1 "
        "AND action_type IN ('report.build.planned', 'report.build.section.drafted')",
        build_id,
    )


@pytest.mark.asyncio
async def test_put_section_draft_round_trip(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    build_id, section_id = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            put = await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}",
                headers=_headers(),
                json={"body_markdown": "# Hello v1", "updated_by_user_id": 7},
            )
            assert put.status_code == 200, put.text
            body = put.json()
            assert body["section_id"] == section_id
            assert body["body_markdown"] == "# Hello v1"
            assert body["updated_by_user_id"] == 7

            # GET build envelope should expose the draft
            get_r = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}",
                headers=_headers(),
            )
            assert get_r.status_code == 200
            drafts = get_r.json()["drafts"]
            assert section_id in drafts
            assert drafts[section_id]["body_markdown"] == "# Hello v1"
    finally:
        await _cleanup_build(pg_conn, build_id)


@pytest.mark.asyncio
async def test_put_section_draft_overwrite_returns_latest(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """Two PUTs against the same section_id must result in the GET
    returning the most recent draft (DISTINCT ON ... ORDER BY created_at DESC)."""
    build_id, section_id = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}",
                headers=_headers(),
                json={"body_markdown": "v1", "updated_by_user_id": 1},
            )
            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}",
                headers=_headers(),
                json={"body_markdown": "v2 latest", "updated_by_user_id": 2},
            )
            r = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}",
                headers=_headers(),
            )
        drafts = r.json()["drafts"]
        assert drafts[section_id]["body_markdown"] == "v2 latest"
        assert drafts[section_id]["updated_by_user_id"] == 2

        # Both audit rows must persist (history preserved by audit ledger)
        count = await pg_conn.fetchval(
            "SELECT count(*) FROM audit.audit_ledger "
            "WHERE action_type='report.build.section.drafted' AND target_id=$1",
            build_id,
        )
        assert count == 2
    finally:
        await _cleanup_build(pg_conn, build_id)


@pytest.mark.asyncio
async def test_put_section_draft_unknown_build_returns_404(workspace_id: str) -> None:
    fake_id = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.put(
            f"/api/v1/admin/reports/builds/{fake_id}/sections/intro",
            headers=_headers(),
            json={"body_markdown": "x", "updated_by_user_id": 1},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_section_draft_oversized_body_rejected(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """Pydantic max_length on body_markdown must reject 200_001-char body."""
    build_id, section_id = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            r = await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}",
                headers=_headers(),
                json={"body_markdown": "x" * 200_001, "updated_by_user_id": 1},
            )
        assert r.status_code == 422
    finally:
        await _cleanup_build(pg_conn, build_id)


@pytest.mark.asyncio
async def test_section_draft_history_returns_revisions_newest_first(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """GET /history must return all PUT revisions for the section
    in newest-first order, with body lengths recorded."""
    build_id, section_id = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            for i in range(3):
                await client.put(
                    f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}",
                    headers=_headers(),
                    json={"body_markdown": f"revision {i}", "updated_by_user_id": i + 1},
                )
            r = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{section_id}/history",
                headers=_headers(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["section_id"] == section_id
        assert body["total"] == 3
        entries = body["entries"]
        # Newest first
        bodies = [e["body_markdown"] for e in entries]
        assert bodies == ["revision 2", "revision 1", "revision 0"]
        # body_length populated correctly
        assert all(e["body_length"] == len(e["body_markdown"]) for e in entries)
        # updated_by_user_id round-trips
        assert [e["updated_by_user_id"] for e in entries] == [3, 2, 1]
    finally:
        await _cleanup_build(pg_conn, build_id)


@pytest.mark.asyncio
async def test_section_draft_history_unknown_build_returns_404() -> None:
    fake_id = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
        r = await client.get(
            f"/api/v1/admin/reports/builds/{fake_id}/sections/intro/history",
            headers=_headers(),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_section_draft_history_other_section_isolated(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """History for section A must not include revisions of section B."""
    build_id, _ = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            built = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}",
                headers=_headers(),
            )
            sections = built.json()["sections"]
            if len(sections) < 2:
                pytest.skip("report type has <2 sections")
            s1 = sections[0]["section_id"]
            s2 = sections[1]["section_id"]

            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s1}",
                headers=_headers(),
                json={"body_markdown": f"s1-only", "updated_by_user_id": 1},
            )
            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s2}",
                headers=_headers(),
                json={"body_markdown": f"s2-only", "updated_by_user_id": 2},
            )
            r1 = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s1}/history",
                headers=_headers(),
            )
            r2 = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s2}/history",
                headers=_headers(),
            )
        assert all(e["body_markdown"] == "s1-only" for e in r1.json()["entries"])
        assert all(e["body_markdown"] == "s2-only" for e in r2.json()["entries"])
    finally:
        await _cleanup_build(pg_conn, build_id)


@pytest.mark.asyncio
async def test_multiple_sections_independent_drafts(
    pg_conn: asyncpg.Connection, workspace_id: str,
) -> None:
    """Drafts on different section_ids must not interfere with each other."""
    build_id, _ = await _plan_build(workspace_id)
    try:
        async with httpx.AsyncClient(base_url=FASTAPI_URL) as client:
            # Read the build to discover all section_ids
            built = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}",
                headers=_headers(),
            )
            sections = built.json()["sections"]
            if len(sections) < 2:
                pytest.skip("report type has <2 sections — can't test independence")
            s1 = sections[0]["section_id"]
            s2 = sections[1]["section_id"]

            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s1}",
                headers=_headers(),
                json={"body_markdown": f"section-1 body for {s1}", "updated_by_user_id": 1},
            )
            await client.put(
                f"/api/v1/admin/reports/builds/{build_id}/sections/{s2}",
                headers=_headers(),
                json={"body_markdown": f"section-2 body for {s2}", "updated_by_user_id": 1},
            )

            r = await client.get(
                f"/api/v1/admin/reports/builds/{build_id}",
                headers=_headers(),
            )
        drafts = r.json()["drafts"]
        assert s1 in drafts and s2 in drafts
        assert s1 in drafts[s1]["body_markdown"]
        assert s2 in drafts[s2]["body_markdown"]
    finally:
        await _cleanup_build(pg_conn, build_id)
