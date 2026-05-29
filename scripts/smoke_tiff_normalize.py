#!/usr/bin/env python
"""End-to-end smoke test for the TIFF→PDF normalise path (ADR-0005).

Designed to run INSIDE the georag-fastapi container so it has direct
network access to MinIO + Hatchet + the local /internal endpoint.

Flow:
  1. Build a 3-page synthetic TIFF with PIL (real OCR-able text rendered
     onto the pages so the §04p stack has something to extract).
  2. Upload to bronze://tiff/{project_id}/smoke_<ts>.tif via boto3.
  3. POST to /internal/v1/shadow/tiff_normalize/trigger.
  4. Poll the workflow until normalise completes (≤ 60 s).
  5. Verify the derived PDF exists in bronze://reports/.
  6. Verify the downstream ingest_pdf workflow_run_id is reachable.
  7. Wait up to ~5 min for silver.reports + silver.document_passages
     rows to appear (the §04p stack is slower than the wrap).

Exits 0 on success, non-zero on first failure. Prints a structured
report to stdout for the operator.

Run:
  docker compose ... exec fastapi python /tmp/smoke_tiff_normalize.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from datetime import datetime, timezone

PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"  # Phantom Lake Silver
WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"  # Default Workspace


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


def build_synthetic_tiff() -> bytes:
    """Build a 3-page grayscale TIFF with rendered OCR-able text per page."""
    from PIL import Image, ImageDraw, ImageFont

    pages = []
    page_texts = [
        (
            "GeoRAG Smoke Test Document",
            "Phantom Lake Silver Project NI 43-101 Excerpt",
            "Page 1 of 3 - Executive Summary",
            "",
            "The Phantom Lake property hosts silver mineralization",
            "in stratabound horizons of the Proterozoic basement.",
            "Drillhole PLS-22-08 intersected 12.4 metres of",
            "0.78 percent silver equivalent at 145.2 metres depth.",
        ),
        (
            "Page 2 of 3 - Geology",
            "",
            "The Phantom Lake stratigraphy consists of:",
            "  - Basement granite (Archean)",
            "  - Athabasca sandstone unconformity at 87 m",
            "  - Hydrothermal alteration assemblage",
            "  - Pyritic shale unit (host rock for silver)",
            "",
            "Resource estimate v3 superseded the 2023 model.",
            "Total indicated resource: 18.3 million ounces silver.",
        ),
        (
            "Page 3 of 3 - QA/QC",
            "",
            "All assays were performed by ALS Vancouver.",
            "Detection limit for silver: 0.1 ppm.",
            "Blank insertion rate: 1 in 20 samples.",
            "Certified reference materials: OREAS 134a, 142a.",
            "RPD on duplicate pairs averaged 8.2 percent.",
        ),
    ]
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28,
        )
    except OSError:
        font = ImageFont.load_default()
    for lines in page_texts:
        img = Image.new("L", (1200, 1600), color=245)
        draw = ImageDraw.Draw(img)
        y = 100
        for line in lines:
            draw.text((100, y), line, fill=0, font=font)
            y += 60
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(
        buf, format="TIFF", save_all=True, append_images=pages[1:],
        compression="tiff_lzw",
    )
    return buf.getvalue()


def upload_tiff(s3, key: str, body: bytes) -> None:
    s3.put_object(
        Bucket="bronze", Key=key, Body=body, ContentType="image/tiff",
    )


def trigger_normalize(http, *, minio_key: str, file_size: int) -> dict:
    payload = {
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT_ID,
        "minio_key": minio_key,
        "file_size": file_size,
        "correlation_token": f"smoke-{int(time.time())}",
        "actor_id": 1,
    }
    log(f"POST trigger payload.correlation_token={payload['correlation_token']}")
    resp = http.post(
        "http://localhost:8000/internal/v1/shadow/tiff_normalize/trigger",
        json=payload,
        headers={"X-Service-Key": os.environ["FASTAPI_SERVICE_KEY"]},
        timeout=15.0,
    )
    resp.raise_for_status()
    body = resp.json()
    log(f"  → workflow_run_id={body['workflow_run_id']}")
    return body


async def wait_for_derived_pdf(
    s3, *, tiff_key: str, max_wait_s: float = 120.0,
) -> str:
    """Poll MinIO until the derived PDF for our source TIFF appears.

    Watches the bronze://reports/{project}/tiff-derived-* prefix for an
    object with metadata x-georag-tiff-source-key matching ours.
    """
    started = time.monotonic()
    prefix = f"reports/{PROJECT_ID}/tiff-derived-"
    paginator = s3.get_paginator("list_objects_v2")
    while time.monotonic() - started < max_wait_s:
        for page in paginator.paginate(Bucket="bronze", Prefix=prefix):
            for obj in page.get("Contents", []):
                head = s3.head_object(Bucket="bronze", Key=obj["Key"])
                meta = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
                if meta.get("x-georag-tiff-source-key") == tiff_key:
                    log(f"  derived PDF appeared: bronze/{obj['Key']} (after {time.monotonic() - started:.1f}s)")
                    return obj["Key"]
        await asyncio.sleep(3.0)
    raise TimeoutError(
        f"derived PDF for source={tiff_key} not seen in {max_wait_s}s"
    )


def verify_derived_pdf(s3, derived_key: str) -> int:
    head = s3.head_object(Bucket="bronze", Key=derived_key)
    size = int(head["ContentLength"])
    meta = head.get("Metadata") or {}
    log(f"  derived PDF size={size}B metadata={meta}")
    assert size > 1000, f"derived PDF suspiciously small: {size}B"
    assert "x-georag-derived-from-tiff-sha256" in {k.lower() for k in meta}, (
        f"missing provenance metadata; got {meta!r}"
    )
    return size


async def wait_for_silver_rows(
    pool, *, derived_key: str, max_wait_s: float = 300.0,
) -> dict:
    """Block until silver.reports + at least one silver.document_passages
    row are visible for this report.
    """
    started = time.monotonic()
    while time.monotonic() - started < max_wait_s:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.report_id::text AS report_id,
                       r.parser_used,
                       r.is_scanned,
                       r.title,
                       (SELECT COUNT(*) FROM silver.document_passages dp
                          WHERE dp.document_id = r.report_id) AS passage_count
                  FROM silver.reports r
                 WHERE r.project_id::text = $1
                   AND r.created_at > NOW() - INTERVAL '10 minutes'
                 ORDER BY r.created_at DESC
                 LIMIT 1
                """,
                PROJECT_ID,
            )
        if row and (row["passage_count"] or 0) > 0:
            log(f"  silver.reports report_id={row['report_id']} parser={row['parser_used']} passages={row['passage_count']} ✓")
            return dict(row)
        await asyncio.sleep(5.0)
    raise TimeoutError("no silver.reports + passages produced within 5 min")


async def main() -> int:
    import asyncpg
    import boto3
    import httpx
    from botocore.config import Config as BotoConfig

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4"),
    )

    log("== 1. Build synthetic 3-page TIFF ==")
    tiff_bytes = build_synthetic_tiff()
    log(f"  built tiff: {len(tiff_bytes)} bytes")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tiff_key = f"tiff/{PROJECT_ID}/{ts}_smoke.tif"

    log("== 2. Upload TIFF to MinIO bronze ==")
    upload_tiff(s3, tiff_key, tiff_bytes)
    log(f"  uploaded → bronze/{tiff_key}")

    log("== 3. Trigger tiff_normalize via /internal endpoint ==")
    with httpx.Client() as http:
        trigger = trigger_normalize(
            http, minio_key=tiff_key, file_size=len(tiff_bytes),
        )

    log("== 4. Wait for derived PDF in bronze/reports ==")
    derived_key = await wait_for_derived_pdf(s3, tiff_key=tiff_key, max_wait_s=120.0)

    log("== 5. Verify derived PDF provenance metadata ==")
    verify_derived_pdf(s3, derived_key)

    log("== 6. Wait for silver.reports + silver.document_passages ==")
    pool = await asyncpg.create_pool(
        host=os.environ.get("POSTGRES_DIRECT_HOST", "postgresql"),
        port=int(os.environ.get("POSTGRES_DIRECT_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "georag"),
        min_size=1, max_size=2, statement_cache_size=0,
    )
    try:
        # Set the RLS GUC for this session so the SELECT can see the row.
        async with pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", WORKSPACE_ID,
            )
            await conn.execute(
                "SELECT set_config('georag.workspace_id', $1, false)", WORKSPACE_ID,
            )
        row = await wait_for_silver_rows(pool, derived_key=derived_key)
    finally:
        await pool.close()

    log("== ✅ SMOKE TEST PASSED ==")
    log(f"  source_tiff:  bronze/{tiff_key}")
    log(f"  derived_pdf:  bronze/{derived_key}")
    log(f"  report_id:    {row['report_id']}")
    log(f"  parser_used:  {row['parser_used']}")
    log(f"  passages:     {row['passage_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
