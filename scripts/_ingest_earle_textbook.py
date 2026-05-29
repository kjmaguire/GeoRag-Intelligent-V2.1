#!/usr/bin/env python
"""Plan §6c — ingest the 17 allowed Earle Physical Geology 2e chapters.

Flow per chapter (using the existing /internal/v1/shadow/ingest_pdf/trigger
endpoint that Laravel ShadowRouter uses — same auth/path the production
upload pipeline takes):

  1. SHA-256 the chapter PDF on disk
  2. Upload to bronze MinIO at
     ``s3://bronze/reports/{project_id}/textbook/earle/ch{NN}.pdf``
  3. POST to ``/internal/v1/shadow/ingest_pdf/trigger`` with the
     IngestPdfInput payload (workspace_id + project_id + minio_key +
     file_size + correlation_token + actor_id=None)
  4. Poll silver.reports for the SHA to land (timeout 10 min)
  5. UPDATE the row to set license / license_url / attribution_text /
     source_url + report_type='TEXTBOOK_OER' (idempotent — guarded by
     WHERE license IS NULL)

Sequential — waits for each chapter's silver.reports row before firing
the next, so we don't blow the parse subprocess pool's heartbeat (per
MEMORY:project_pipeline_resilience_2026_05_22).

Idempotent in both directions:
  * Bronze key includes chapter number so re-uploads overwrite the
    same key (safe).
  * silver.reports has UNIQUE(workspace_id, project_id, source_file_sha256)
    so re-running detects existing rows and skips re-ingest.
  * OER metadata UPDATE has WHERE license IS NULL guard.

Usage
-----

    docker exec georag-fastapi bash -c \\
        "WORKSPACE_ID=a0000000-0000-0000-0000-000000000001 \\
         PROJECT_ID=db8ae12a-0767-441d-9171-065c5f501dde \\
         BRONZE_DIR=/host/bronze/textbooks/earle_physical_geology \\
         python /app/scripts/_ingest_earle_textbook.py"
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("ingest_earle_textbook")

# --- per-chapter OER metadata constants -----------------------------------
LICENSE_SPDX = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
SOURCE_URL = "https://opentextbc.ca/physicalgeology2ed/"
REPORT_TYPE_OER = "TEXTBOOK_OER"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(bronze_dir: Path) -> dict[str, Any]:
    m = bronze_dir / "manifest.json"
    if not m.is_file():
        raise FileNotFoundError(
            f"missing chapter manifest at {m}. Run "
            f"src/fastapi/scripts/_earle_chapter_splitter.py first."
        )
    return json.loads(m.read_text())


def _upload_to_bronze(s3, bucket: str, key: str, path: Path) -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    logger.info("  → bronze upload s3://%s/%s (%.1f MB)", bucket, key, size_mb)
    s3.upload_file(str(path), bucket, key)


async def _trigger_ingest_pdf(
    http_client,
    fastapi_base: str,
    service_key: str,
    workspace_id: uuid.UUID,
    project_id: str,
    minio_key: str,
    file_size: int,
    actor_id: int | None,
) -> tuple[str, str]:
    """POST to /internal/v1/shadow/ingest_pdf/trigger.

    Returns (workflow_run_id, correlation_token).
    """
    correlation_token = uuid.uuid4().hex
    payload = {
        "workspace_id":      str(workspace_id),
        "project_id":        project_id,
        "minio_key":         minio_key,
        "file_size":         file_size,
        "correlation_token": correlation_token,
        "actor_id":          actor_id,
    }
    url = f"{fastapi_base}/internal/v1/shadow/ingest_pdf/trigger"
    resp = await http_client.post(
        url,
        json=payload,
        headers={"X-Service-Key": service_key},
        timeout=60.0,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"ingest_pdf trigger failed: {resp.status_code} {resp.text}"
        )
    body = resp.json()
    return body["workflow_run_id"], body["correlation_token"]


async def _wait_for_silver_report(
    conn,
    workspace_id: uuid.UUID,
    project_id: str,
    sha256: str,
    timeout_s: int = 900,
) -> uuid.UUID | None:
    """Poll silver.reports for source_file_sha256 to land."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_log = 0.0
    while asyncio.get_running_loop().time() < deadline:
        row = await conn.fetchrow(
            """
            SELECT report_id FROM silver.reports
            WHERE workspace_id = $1
              AND project_id = $2
              AND source_file_sha256 = $3
            LIMIT 1
            """,
            workspace_id, project_id, sha256,
        )
        if row:
            return row["report_id"]
        now = asyncio.get_running_loop().time()
        if now - last_log >= 30.0:
            logger.info("    waiting for silver.reports.report_id ...")
            last_log = now
        await asyncio.sleep(10)
    return None


async def _apply_oer_metadata(
    conn,
    report_id: uuid.UUID,
    attribution_text: str,
) -> None:
    # silver.reports.updated_at is `timestamp(0) WITHOUT time zone`; pass
    # an offset-naive UTC datetime so asyncpg's encoder doesn't choke on
    # "can't subtract offset-naive and offset-aware datetimes". The wall
    # value is identical to datetime.now(timezone.utc) with tzinfo stripped.
    naive_utc = datetime.utcnow().replace(microsecond=0)
    await conn.execute(
        """
        UPDATE silver.reports
        SET license          = $2,
            license_url      = $3,
            attribution_text = $4,
            source_url       = $5,
            report_type      = $6,
            updated_at       = $7
        WHERE report_id = $1
          AND license IS NULL
        """,
        report_id,
        LICENSE_SPDX,
        LICENSE_URL,
        attribution_text,
        SOURCE_URL,
        REPORT_TYPE_OER,
        naive_utc,
    )
    logger.info("  → OER metadata applied to report_id=%s", report_id)


async def main_async(args):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    workspace_id = uuid.UUID(os.environ["WORKSPACE_ID"])
    project_id = os.environ["PROJECT_ID"]
    actor_id = int(os.environ["ACTOR_ID"]) if os.environ.get("ACTOR_ID") else None
    bronze_dir = Path(args.bronze_dir or os.environ.get("BRONZE_DIR", ""))
    if not bronze_dir.is_dir():
        raise FileNotFoundError(f"bronze_dir not found: {bronze_dir}")

    manifest = _load_manifest(bronze_dir)
    allowed = [c for c in manifest["chapters"] if c["allowed"]]
    logger.info("manifest: %d allowed / %d total chapters",
                len(allowed), len(manifest["chapters"]))

    import asyncpg, boto3, httpx  # noqa: PLC0415

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:8333"),
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )
    bronze_bucket = os.environ.get("S3_BUCKET_BRONZE", "bronze")
    fastapi_base = os.environ.get("FASTAPI_BASE", "http://localhost:8000")
    service_key = os.environ["FASTAPI_SERVICE_KEY"]

    if args.dry_run:
        for ch in allowed:
            logger.info("[DRY] Ch.%d %s pages %d-%d → %s",
                        ch["number"], ch["title"],
                        ch["start_page"], ch["end_page"],
                        Path(ch["output"]).name)
        return 0

    dsn = os.environ.get("POSTGRES_DSN") or (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
        f"{os.environ['POSTGRES_PASSWORD']}@"
        f"{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
        f"{os.environ.get('POSTGRES_DIRECT_PORT', 5432)}/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )

    conn = await asyncpg.connect(dsn)
    async with httpx.AsyncClient() as http_client:
        try:
            for ch in allowed:
                ch_num = ch["number"]
                pdf_filename = Path(ch["output"]).name
                output_path = bronze_dir / pdf_filename
                if not output_path.is_file():
                    logger.error("  Ch.%d MISSING: %s — skipping", ch_num, output_path)
                    continue

                sha = _sha256(output_path)
                # Idempotency check
                existing = await conn.fetchrow(
                    "SELECT report_id FROM silver.reports "
                    "WHERE workspace_id = $1 AND project_id = $2 AND source_file_sha256 = $3",
                    workspace_id, project_id, sha,
                )
                if existing:
                    logger.info("Ch.%d already-ingested report_id=%s — applying OER metadata only",
                                ch_num, existing["report_id"])
                    await _apply_oer_metadata(conn, existing["report_id"],
                                              ch["attribution_text"])
                    continue

                minio_key = (
                    f"reports/{project_id}/textbook/earle/"
                    f"earle-physical-geology-ch{ch_num:02d}.pdf"
                )
                file_size = output_path.stat().st_size
                logger.info("Ch.%d (%s) sha=%s size=%d", ch_num, ch["title"],
                            sha[:12], file_size)

                _upload_to_bronze(s3, bronze_bucket, minio_key, output_path)
                run_id, corr = await _trigger_ingest_pdf(
                    http_client, fastapi_base, service_key,
                    workspace_id, project_id, minio_key, file_size, actor_id,
                )
                logger.info("  → workflow_run_id=%s correlation=%s", run_id, corr)

                report_id = await _wait_for_silver_report(
                    conn, workspace_id, project_id, sha, timeout_s=args.wait_seconds,
                )
                if report_id is None:
                    logger.error("  Ch.%d TIMEOUT (%ds) waiting for silver.reports row",
                                 ch_num, args.wait_seconds)
                    continue
                await _apply_oer_metadata(conn, report_id, ch["attribution_text"])

            logger.info(
                "DONE. Ingested all allowed chapters. Embed sweep runs on "
                "the 10-min embed_pending_passages cron (see MEMORY: "
                "project_pipeline_resilience). Or trigger now: "
                "docker exec georag-fastapi python -c 'from app.hatchet_workflows."
                "embed_pending_passages import embed_pending_passages; import asyncio; "
                "asyncio.run(embed_pending_passages.aio_run_no_wait({}))'"
            )
        finally:
            await conn.close()
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bronze-dir", default=None,
                   help="Host path to chapter PDFs + manifest. "
                        "Defaults to $BRONZE_DIR env var.")
    p.add_argument("--wait-seconds", type=int, default=900,
                   help="Per-chapter timeout for silver.reports row to land")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
