"""Populate bronze.source_files with sha256 hashes of all ingested MinIO objects.

This script scans the bronze bucket, computes sha256 for each object,
and upserts into bronze.source_files. Idempotent — re-running updates
file_size and sha256 if the object changed.

Usage:
    docker exec georag-fastapi python /app/scripts/populate_source_files.py
"""

import asyncio
import hashlib
import logging
import os

import asyncpg
from minio import Minio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "georag-admin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "georag_minio_dev")
BUCKET = os.environ.get("MINIO_BUCKET", "bronze")

PG_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://georag:georag_dev_password@pgbouncer:6432/georag",
)

# Map file extensions to MIME types
_MIME_MAP = {
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".las": "application/x-las",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".sgy": "application/x-segy",
    ".segy": "application/x-segy",
    ".geojson": "application/geo+json",
    ".shp": "application/x-shapefile",
    ".xyz": "text/plain",
}


def _mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _MIME_MAP.get(ext, "application/octet-stream")


async def main() -> None:
    logger.info("Connecting to MinIO at %s bucket=%s", MINIO_ENDPOINT, BUCKET)
    client = Minio(MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, secure=False)

    if not client.bucket_exists(BUCKET):
        logger.error("Bucket %s does not exist", BUCKET)
        return

    logger.info("Connecting to PostgreSQL…")
    pg = await asyncpg.connect(PG_DSN)

    try:
        objects = list(client.list_objects(BUCKET, recursive=True))
        logger.info("Found %d objects in %s", len(objects), BUCKET)

        upsert_sql = """
            INSERT INTO bronze.source_files (file_path, bucket, sha256, file_size, mime_type)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (bucket, file_path) DO UPDATE SET
                sha256 = EXCLUDED.sha256,
                file_size = EXCLUDED.file_size,
                mime_type = EXCLUDED.mime_type,
                ingested_at = NOW()
        """

        count = 0
        for obj in objects:
            # Skip directories
            if obj.is_dir or obj.object_name.endswith("/"):
                continue

            # Download and hash
            response = client.get_object(BUCKET, obj.object_name)
            hasher = hashlib.sha256()
            size = 0
            try:
                for chunk in response.stream(8192):
                    hasher.update(chunk)
                    size += len(chunk)
            finally:
                response.close()
                response.release_conn()

            sha = hasher.hexdigest()
            mime = _mime_for(obj.object_name)

            await pg.execute(upsert_sql, obj.object_name, BUCKET, sha, size, mime)
            count += 1
            logger.info(
                "  %s  %d bytes  sha256=%s…",
                obj.object_name,
                size,
                sha[:16],
            )

        logger.info("Upserted %d source file records", count)

        # Verify
        total = await pg.fetchval("SELECT COUNT(*) FROM bronze.source_files")
        logger.info("bronze.source_files now has %d rows", total)

    finally:
        await pg.close()

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
