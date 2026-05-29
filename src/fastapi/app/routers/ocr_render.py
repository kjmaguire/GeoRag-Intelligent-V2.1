"""§04p page-render endpoint for the Silver Review UI (doc-phase 59).

Master-plan §3 Step 8b. Provides the bridge between the React detail
panel (doc-phase 60) and ``app.ocr.render.render_page()``.

The endpoint:
1. Looks up the bronze S3 key for the given report_id from
   ``silver.parser_run_artifacts.raw_output_uri`` (populated by the
   preflight artifact row during ingest, see doc-phase 59 persist
   layer changes).
2. Downloads the PDF bytes from S3 (SeaweedFS).
3. Writes to a temp file.
4. Invokes ``render_page(tmp, page, scale)``.
5. Returns the PNG bytes with ``Content-Type: image/png``.

Auth: same X-Service-Key gate as other /internal routes
(``FASTAPI_SERVICE_KEY`` env var). Laravel admin proxies the request
with the service key header attached.

Workspace scoping: admin views read across all workspaces (per
doc-phase 58's design decision). The endpoint does NOT set
``app.workspace_id`` GUC because the lookup query doesn't need it —
the lookup is by report_id which is globally unique.

Caching: not implemented in v1. Each render call re-downloads from S3
and re-renders. Per the smoke-bench, render is ~50-80 ms/page at
scale=2.0; S3 download is ~50-200 ms on the local docker network.
Total ~100-300 ms per call. Acceptable for an admin tool. If real
usage warrants caching, doc-phase 60+ can add a SeaweedFS-backed
render cache keyed on (report_id, page, scale).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import Response

from app.config import settings
from app.ocr.render import render_page


log = logging.getLogger("georag.ocr_render")

router = APIRouter(prefix="/internal/v1/ocr", tags=["ocr"])


def _check_service_key(x_service_key: str | None = Header(default=None)) -> None:
    expected = settings.FASTAPI_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FASTAPI_SERVICE_KEY not configured",
        )
    if x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Service-Key",
        )


def _dsn() -> str:
    """Direct-Postgres DSN. Matches `_persist._dsn()`."""
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _s3_endpoint() -> str:
    return os.environ.get(
        "S3_ENDPOINT_URL",
        os.environ.get("MINIO_ENDPOINT", "http://minio:8333"),
    )


def _s3_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD", ""),
    )


async def _lookup_bronze_key(conn: asyncpg.Connection, report_id: str) -> str | None:
    """Find the bronze S3 key for a report via parser_run_artifacts.

    Returns the most recent preflight row's raw_output_uri. NULL if no
    preflight row exists or if the preflight row was written before
    bronze-key tracking landed (doc-phase 59) — those rows have
    raw_output_uri = NULL.
    """
    return await conn.fetchval(
        """
        SELECT raw_output_uri
        FROM silver.parser_run_artifacts
        WHERE report_id = $1::uuid
          AND parser_used = 'preflight'
          AND raw_output_uri IS NOT NULL
        ORDER BY started_at DESC
        LIMIT 1
        """,
        report_id,
    )


async def _download_from_s3(minio_key: str) -> bytes:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=minio_key)
        return await resp["Body"].read()


@router.get(
    "/render",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG-encoded page"},
        401: {"description": "missing or invalid X-Service-Key"},
        404: {"description": "report not found or no bronze key tracked"},
        422: {"description": "invalid query params"},
        500: {"description": "S3 download or render error"},
    },
)
async def render(
    report_id: str = Query(..., min_length=36, max_length=36),
    page: int = Query(..., ge=0, le=10000),
    scale: float = Query(2.0, gt=0.0, le=10.0),
    _: None = Depends(_check_service_key),
) -> Response:
    """Render one page of a Bronze-stored PDF to PNG.

    Query params:
        report_id: UUID of the silver.reports row.
        page: 0-indexed page number.
        scale: pypdfium2 render scale (default 2.0 ≈ 144 DPI).
    """
    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        async with pool.acquire() as conn:
            bronze_key = await _lookup_bronze_key(conn, report_id)
    finally:
        await pool.close()

    if not bronze_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no bronze S3 key tracked for this report. "
                "Either the report was ingested before doc-phase 59's "
                "bronze-key tracking landed, or preflight has not run."
            ),
        )

    try:
        pdf_bytes = await _download_from_s3(bronze_key)
    except Exception as exc:
        log.exception("S3 fetch failed report=%s key=%s", report_id, bronze_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"S3 fetch failed: {type(exc).__name__}",
        ) from exc

    # Write to temp and render. tmp_path explicit so we can clean up
    # deterministically in the finally block.
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="ocr_render_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_bytes(pdf_bytes)
        try:
            png = await render_page(tmp_path, page, scale=scale)
        except IndexError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log.exception("render failed report=%s page=%s", report_id, page)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"render failed: {type(exc).__name__}",
            ) from exc
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",  # 5 min — light cache
            "X-Render-Bronze-Key": bronze_key,
            "X-Render-Scale": str(scale),
        },
    )
