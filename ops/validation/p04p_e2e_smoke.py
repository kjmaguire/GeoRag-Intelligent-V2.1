"""§04p end-to-end smoke test (doc-phase 66).

Validates the full ingest_pdf → orchestrator → persist → silver-rows
→ render endpoint chain on the PLS-2024 fixture.

Flow:
1. Upload PLS-2024 to the bronze S3 bucket with a smoke-test key
2. POST /internal/v1/shadow/ingest_pdf/trigger
3. Poll silver.reports for the resulting row (max ~120 sec)
4. Once present, verify §04p silver rows landed:
   - parser_run_artifacts (at least 2: preflight + parser)
   - ocr_page_quality (one per page)
   - document_ingestion_quality (one row)
   - ingest_extractions (>0 rows)
5. Hit GET /internal/v1/ocr/render → expect PNG bytes
6. Cleanup: DELETE the silver.reports row (CASCADE clears silver_*)
7. Cleanup: DELETE the bronze S3 object

Designed to run from inside georag-fastapi (already has asyncpg,
aioboto3, httpx + module imports). Print pass/fail per check.

Safety:
- Test workspace/project: pre-existing dev fixture
  (a0000000-... workspace)
- Unique correlation_token per run (prevents collision)
- Best-effort cleanup wrapped in try/except
- Test row uses a distinctive title prefix ("p04p-e2e-smoke-")
  so leftovers from past runs are identifiable
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"
NATIVE_PDF = Path("/app/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf")
POLL_TIMEOUT_SEC = 180
POLL_INTERVAL_SEC = 3


def _checkmark(ok: bool) -> str:
    return "✓" if ok else "✗"


def _print_check(label: str, ok: bool, detail: str = "") -> None:
    mark = _checkmark(ok)
    color = "\033[32m" if ok else "\033[31m"
    reset = "\033[0m"
    if detail:
        print(f"  {color}{mark}{reset} {label} — {detail}")
    else:
        print(f"  {color}{mark}{reset} {label}")


async def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _upload_to_s3(bronze_key: str, body: bytes) -> None:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY")
            or os.environ.get("MINIO_ROOT_PASSWORD", ""),
        region_name="us-east-1",
    )
    endpoint = os.environ.get("S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT", "http://minio:8333")
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=endpoint) as s3:
        await s3.put_object(Bucket=bucket, Key=bronze_key, Body=body)


async def _delete_from_s3(bronze_key: str) -> None:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY")
            or os.environ.get("MINIO_ROOT_PASSWORD", ""),
        region_name="us-east-1",
    )
    endpoint = os.environ.get("S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT", "http://minio:8333")
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    try:
        async with sess.client("s3", endpoint_url=endpoint) as s3:
            await s3.delete_object(Bucket=bucket, Key=bronze_key)
    except Exception:
        pass


async def _wait_for_silver_report(conn: Any, sha256: str, timeout: int) -> str | None:
    """Poll silver.reports for a row matching source_file_sha256."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = await conn.fetchrow(
            "SELECT report_id FROM silver.reports WHERE source_file_sha256 = $1",
            sha256,
        )
        if row is not None:
            return str(row["report_id"])
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return None


async def main() -> int:
    import asyncpg
    import hashlib
    import httpx

    print("=== §04p end-to-end smoke test ===")
    print()

    # ------ Stage 0: sanity ------
    if not NATIVE_PDF.exists():
        print(f"FATAL: fixture not found at {NATIVE_PDF}")
        return 2

    pdf_body = NATIVE_PDF.read_bytes()
    sha256 = hashlib.sha256(pdf_body).hexdigest()
    correlation_token = f"p04p-e2e-{uuid.uuid4().hex[:8]}"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    bronze_key = f"reports/p04p-e2e-smoke/{timestamp}_PLS-2024.pdf"

    print(f"  correlation_token: {correlation_token}")
    print(f"  bronze_key:        {bronze_key}")
    print(f"  sha256:            {sha256[:16]}...")
    print()

    fail_count = 0
    cleanup_keys: dict[str, Any] = {"bronze_key": bronze_key, "report_id": None}

    # ------ Stage 1: upload bronze PDF ------
    try:
        await _upload_to_s3(bronze_key, pdf_body)
        _print_check("Stage 1: bronze PDF uploaded", True)
    except Exception as exc:
        _print_check("Stage 1: bronze PDF upload", False, str(exc))
        return 3

    # ------ Stage 2: trigger ingest_pdf workflow ------
    service_key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not service_key:
        _print_check("Stage 2: FASTAPI_SERVICE_KEY env", False, "not set")
        await _delete_from_s3(bronze_key)
        return 4

    payload = {
        "workspace_id": TEST_WORKSPACE_ID,
        "project_id": "00000000-0000-0000-0000-00000000ffff",  # test project
        "minio_key": bronze_key,
        "file_size": len(pdf_body),
        "correlation_token": correlation_token,
        "actor_id": None,
    }
    fastapi_base = os.environ.get("FASTAPI_BASE_URL", "http://fastapi:8000")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{fastapi_base}/internal/v1/shadow/ingest_pdf/trigger",
                json=payload,
                headers={"X-Service-Key": service_key},
            )
        ok = resp.status_code == 202
        _print_check(
            "Stage 2: ingest_pdf workflow triggered",
            ok,
            f"HTTP {resp.status_code} {resp.text[:120]}",
        )
        if not ok:
            fail_count += 1
    except Exception as exc:
        _print_check("Stage 2: ingest_pdf trigger", False, str(exc))
        fail_count += 1

    # ------ Stage 3: poll silver.reports ------
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        report_id = await _wait_for_silver_report(conn, sha256, POLL_TIMEOUT_SEC)
        ok = report_id is not None
        _print_check(
            f"Stage 3: silver.reports row appeared (poll up to {POLL_TIMEOUT_SEC}s)",
            ok,
            f"report_id={report_id}" if ok else "timeout",
        )
        if not ok:
            fail_count += 1
            await _delete_from_s3(bronze_key)
            return fail_count
        cleanup_keys["report_id"] = report_id
    finally:
        await conn.close()

    # ------ Stage 4: verify §04p silver rows ------
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        counts: dict[str, int] = {}
        for table in (
            "parser_run_artifacts",
            "ocr_page_quality",
            "document_ingestion_quality",
            "ingest_extractions",
        ):
            counts[table] = await conn.fetchval(
                f"SELECT COUNT(*) FROM silver.{table} WHERE report_id = $1::uuid",
                report_id,
            )

        _print_check(
            "Stage 4a: parser_run_artifacts >= 2",
            counts["parser_run_artifacts"] >= 2,
            f"count={counts['parser_run_artifacts']}",
        )
        if counts["parser_run_artifacts"] < 2:
            fail_count += 1

        _print_check(
            "Stage 4b: ocr_page_quality >= 1",
            counts["ocr_page_quality"] >= 1,
            f"count={counts['ocr_page_quality']}",
        )
        if counts["ocr_page_quality"] < 1:
            fail_count += 1

        _print_check(
            "Stage 4c: document_ingestion_quality == 1",
            counts["document_ingestion_quality"] == 1,
            f"count={counts['document_ingestion_quality']}",
        )
        if counts["document_ingestion_quality"] != 1:
            fail_count += 1

        _print_check(
            "Stage 4d: ingest_extractions >= 1",
            counts["ingest_extractions"] >= 1,
            f"count={counts['ingest_extractions']}",
        )
        if counts["ingest_extractions"] < 1:
            fail_count += 1

        # Also: bronze key tracked in raw_output_uri?
        bronze_tracked = await conn.fetchval(
            """
            SELECT raw_output_uri
            FROM silver.parser_run_artifacts
            WHERE report_id = $1::uuid
              AND parser_used = 'preflight'
              AND raw_output_uri IS NOT NULL
            LIMIT 1
            """,
            report_id,
        )
        _print_check(
            "Stage 4e: bronze key tracked in parser_run_artifacts",
            bronze_tracked == bronze_key,
            f"got: {bronze_tracked}",
        )
        if bronze_tracked != bronze_key:
            fail_count += 1

    finally:
        await conn.close()

    # ------ Stage 5: render endpoint ------
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{fastapi_base}/internal/v1/ocr/render",
                params={"report_id": report_id, "page": 0, "scale": 1.5},
                headers={"X-Service-Key": service_key},
            )
        ok = resp.status_code == 200 and resp.content[:8] == b"\x89PNG\r\n\x1a\n"
        _print_check(
            "Stage 5: render endpoint returns PNG for page 0",
            ok,
            f"HTTP {resp.status_code}, {len(resp.content)} bytes, magic={resp.content[:8].hex() if resp.content else 'empty'}",
        )
        if not ok:
            fail_count += 1
    except Exception as exc:
        _print_check("Stage 5: render endpoint", False, str(exc))
        fail_count += 1

    # ------ Cleanup ------
    print()
    print("=== Cleanup ===")
    try:
        conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
        try:
            await conn.execute(
                "DELETE FROM silver.reports WHERE report_id = $1::uuid",
                report_id,
            )
            _print_check("Deleted silver.reports row (CASCADE clears §04p rows)", True)
        finally:
            await conn.close()
    except Exception as exc:
        _print_check("Delete silver.reports", False, str(exc))

    try:
        await _delete_from_s3(bronze_key)
        _print_check("Deleted bronze S3 object", True)
    except Exception as exc:
        _print_check("Delete bronze S3 object", False, str(exc))

    print()
    print(f"=== Summary: {'PASS' if fail_count == 0 else f'{fail_count} FAILURES'} ===")
    return fail_count


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
