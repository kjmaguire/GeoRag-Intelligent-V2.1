"""§04p render endpoint behaviour tests (master-plan §3 Step 8b, doc-phase 59).

End-to-end test:
1. Seed a silver.reports row + silver.parser_run_artifacts row with
   raw_output_uri pointing at a real bronze object (uploaded to
   SeaweedFS by the test).
2. Hit GET /internal/v1/ocr/render?report_id=...&page=0
3. Assert: 200 + Content-Type image/png + non-trivial body length

Also covers the failure modes:
- Missing X-Service-Key → 401
- Bogus report_id → 404 (no bronze key tracked)
- Page out of range → 404
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def db_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_USER")
        and os.environ.get("POSTGRES_PASSWORD")
    )


@pytest.fixture(scope="module")
def native_pdf_bytes() -> bytes:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024.read_bytes()


@pytest.fixture(scope="module")
def service_key() -> str:
    key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not key:
        pytest.skip("FASTAPI_SERVICE_KEY not set in environment")
    return key


@pytest.fixture(scope="module")
def app_client() -> TestClient:
    from app.main import app
    return TestClient(app)


async def _make_pool():
    import asyncpg

    from app.ocr._persist import _dsn

    return await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )


async def _seed_report_and_bronze(pool, bronze_key: str) -> str:
    """Create a silver.reports row + a parser_run_artifacts preflight row
    pointing at the given bronze_key. Returns report_id.
    """
    report_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        # Block-1 RLS (2026-05-15): silver.reports now requires workspace_id
        # under strict workspace_id policies.
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            "INSERT INTO silver.reports (report_id, title, workspace_id) "
            "VALUES ($1::uuid, $2, $3::uuid)",
            report_id, f"phase59-render-{report_id[:8]}", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            """
            INSERT INTO silver.parser_run_artifacts (
                report_id, workspace_id, parser_used, parser_version,
                raw_output_uri, errors, warnings, started_at, finished_at
            )
            VALUES (
                $1::uuid, $2::uuid, 'preflight', 'qpdf+pikepdf',
                $3, '[]'::jsonb, '[]'::jsonb, NOW(), NOW()
            )
            """,
            report_id, TEST_WORKSPACE_ID, bronze_key,
        )
    return report_id


async def _delete_report(pool, report_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = $1::uuid",
            report_id,
        )


async def _upload_to_s3(bronze_key: str, body: bytes) -> None:
    """Upload PDF bytes to the bronze bucket using the existing
    aioboto3 helper from the render router (which mirrors the
    ingest_pdf module's S3 conventions).
    """
    import aioboto3

    from app.routers.ocr_render import _s3_credentials, _s3_endpoint

    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
        await s3.put_object(Bucket=bucket, Key=bronze_key, Body=body)


async def _delete_from_s3(bronze_key: str) -> None:
    import aioboto3

    from app.routers.ocr_render import _s3_credentials, _s3_endpoint

    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    try:
        async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
            await s3.delete_object(Bucket=bucket, Key=bronze_key)
    except Exception:
        pass  # cleanup; ignore


# ----- auth -----

def test_render_missing_service_key_is_401(app_client: TestClient) -> None:
    resp = app_client.get("/internal/v1/ocr/render", params={
        "report_id": "00000000-0000-0000-0000-000000000000",
        "page": 0,
    })
    # Without the header, expect 401.
    assert resp.status_code == 401


def test_render_invalid_service_key_is_401(app_client: TestClient) -> None:
    resp = app_client.get(
        "/internal/v1/ocr/render",
        params={"report_id": "00000000-0000-0000-0000-000000000000", "page": 0},
        headers={"X-Service-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# ----- 404 paths -----

def test_render_unknown_report_id_is_404(
    app_client: TestClient, service_key: str, db_available: bool
) -> None:
    if not db_available:
        pytest.skip("DB not available")

    resp = app_client.get(
        "/internal/v1/ocr/render",
        params={
            "report_id": str(uuid.uuid4()),
            "page": 0,
        },
        headers={"X-Service-Key": service_key},
    )
    assert resp.status_code == 404


# ----- happy path -----

def test_render_happy_path_returns_png(
    app_client: TestClient,
    service_key: str,
    db_available: bool,
    native_pdf_bytes: bytes,
) -> None:
    if not db_available:
        pytest.skip("DB not available")

    bronze_key = f"reports/phase59-test/{uuid.uuid4().hex[:8]}.pdf"

    async def setup_and_query() -> dict:
        pool = await _make_pool()
        try:
            await _upload_to_s3(bronze_key, native_pdf_bytes)
            report_id = await _seed_report_and_bronze(pool, bronze_key)
            try:
                # TestClient runs sync; the actual HTTP call sits below
                # outside the async context. Return what we need to
                # invoke the endpoint synchronously after setup.
                return {"report_id": report_id, "bronze_key": bronze_key}
            except Exception:
                await _delete_report(pool, report_id)
                raise
        finally:
            await pool.close()

    setup = asyncio.run(setup_and_query())
    try:
        resp = app_client.get(
            "/internal/v1/ocr/render",
            params={"report_id": setup["report_id"], "page": 0, "scale": 1.5},
            headers={"X-Service-Key": service_key},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(resp.content) > 1000  # at least non-trivial size
        assert resp.headers.get("X-Render-Bronze-Key") == bronze_key
        assert resp.headers.get("X-Render-Scale") == "1.5"
    finally:
        # Teardown
        async def cleanup():
            pool = await _make_pool()
            try:
                await _delete_report(pool, setup["report_id"])
            finally:
                await pool.close()
            await _delete_from_s3(bronze_key)
        asyncio.run(cleanup())


def test_render_page_out_of_range_is_404(
    app_client: TestClient,
    service_key: str,
    db_available: bool,
    native_pdf_bytes: bytes,
) -> None:
    if not db_available:
        pytest.skip("DB not available")

    bronze_key = f"reports/phase59-test/{uuid.uuid4().hex[:8]}.pdf"

    async def setup() -> str:
        pool = await _make_pool()
        try:
            await _upload_to_s3(bronze_key, native_pdf_bytes)
            return await _seed_report_and_bronze(pool, bronze_key)
        finally:
            await pool.close()

    report_id = asyncio.run(setup())
    try:
        resp = app_client.get(
            "/internal/v1/ocr/render",
            params={"report_id": report_id, "page": 9999, "scale": 2.0},
            headers={"X-Service-Key": service_key},
        )
        assert resp.status_code == 404
    finally:
        async def cleanup():
            pool = await _make_pool()
            try:
                await _delete_report(pool, report_id)
            finally:
                await pool.close()
            await _delete_from_s3(bronze_key)
        asyncio.run(cleanup())
