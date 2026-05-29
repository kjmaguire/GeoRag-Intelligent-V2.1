"""Clean up stale Qdrant vectors — removes points whose source data no longer exists.

Checks every point in georag_chunks and georag_reports against the silver
tables. If a collar_id or report_id no longer exists in PostgreSQL, the
corresponding Qdrant point is deleted.

Also detects vectors embedded with an outdated model by inspecting the
embed_model payload field (if present).

Usage:
    docker exec georag-fastapi python /app/scripts/cleanup_qdrant.py
"""

import asyncio
import logging
import os

import asyncpg
from qdrant_client import AsyncQdrantClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

PG_DSN = os.environ.get("DATABASE_URL", "postgresql://georag:georag_dev_password@pgbouncer:6432/georag")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
EXPECTED_MODEL = os.environ.get("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5")


async def main():
    logger.info("Connecting to PostgreSQL and Qdrant...")
    pg = await asyncpg.connect(PG_DSN)
    qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=6333)

    total_deleted = 0

    # --- georag_chunks: check collar_id exists ---
    logger.info("Scanning georag_chunks...")
    chunks_scroll = await qdrant.scroll(collection_name="georag_chunks", limit=1000, with_payload=True)
    chunk_points = chunks_scroll[0]

    valid_collars = set()
    rows = await pg.fetch("SELECT collar_id::text FROM silver.collars")
    valid_collars = {r["collar_id"] for r in rows}

    stale_chunk_ids = []
    wrong_model_ids = []
    for pt in chunk_points:
        collar_id = pt.payload.get("collar_id")
        if collar_id and collar_id not in valid_collars:
            stale_chunk_ids.append(pt.id)
        embed_model = pt.payload.get("embed_model")
        if embed_model and embed_model != EXPECTED_MODEL:
            wrong_model_ids.append(pt.id)

    if stale_chunk_ids:
        await qdrant.delete(collection_name="georag_chunks", points_selector=stale_chunk_ids)
        logger.info("georag_chunks: deleted %d stale points (collar no longer exists)", len(stale_chunk_ids))
        total_deleted += len(stale_chunk_ids)
    else:
        logger.info("georag_chunks: no stale points found")

    if wrong_model_ids:
        logger.warning(
            "georag_chunks: %d points embedded with wrong model (expected %s). "
            "Run scripts/reembed_qdrant.py to fix.",
            len(wrong_model_ids),
            EXPECTED_MODEL,
        )

    # --- georag_reports: check report_id exists ---
    logger.info("Scanning georag_reports...")
    reports_scroll = await qdrant.scroll(collection_name="georag_reports", limit=1000, with_payload=True)
    report_points = reports_scroll[0]

    valid_reports = set()
    rows = await pg.fetch("SELECT report_id::text FROM silver.reports")
    valid_reports = {r["report_id"] for r in rows}

    stale_report_ids = []
    for pt in report_points:
        report_id = pt.payload.get("report_id")
        if report_id and report_id not in valid_reports:
            stale_report_ids.append(pt.id)

    if stale_report_ids:
        await qdrant.delete(collection_name="georag_reports", points_selector=stale_report_ids)
        logger.info("georag_reports: deleted %d stale points (report no longer exists)", len(stale_report_ids))
        total_deleted += len(stale_report_ids)
    else:
        logger.info("georag_reports: no stale points found")

    # --- Summary ---
    chunks_info = await qdrant.get_collection("georag_chunks")
    reports_info = await qdrant.get_collection("georag_reports")
    logger.info("=== Cleanup complete ===")
    logger.info("  Deleted: %d stale points", total_deleted)
    logger.info("  georag_chunks: %d points remaining", chunks_info.points_count)
    logger.info("  georag_reports: %d points remaining", reports_info.points_count)
    if wrong_model_ids:
        logger.info("  WARNING: %d points need re-embedding", len(wrong_model_ids))

    await pg.close()
    await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
