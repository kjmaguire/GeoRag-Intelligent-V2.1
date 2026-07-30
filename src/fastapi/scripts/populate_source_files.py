"""Populate bronze.source_files with sha256 hashes of all ingested bronze-bucket objects.

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
from georag_object_storage import Bucket, StorageConfig
from georag_object_storage.sync_client import S3CompatibleStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

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
    config = StorageConfig.from_env()
    store = S3CompatibleStorage(config)
    bucket_name = config.bucket_name(Bucket.BRONZE)
    logger.info("Connecting to object storage at %s bucket=%s", config.endpoint_url, bucket_name)

    if not store.bucket_exists(Bucket.BRONZE):
        logger.error("Bucket %s does not exist", bucket_name)
        return

    logger.info("Connecting to PostgreSQL…")
    pg = await asyncpg.connect(PG_DSN)

    try:
        keys = [key for key in store.list_keys(Bucket.BRONZE) if not key.endswith("/")]
        logger.info("Found %d objects in %s", len(keys), bucket_name)

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
        for key in keys:
            data = store.get_bytes(Bucket.BRONZE, key)
            sha = hashlib.sha256(data).hexdigest()
            size = len(data)
            mime = _mime_for(key)

            await pg.execute(upsert_sql, key, bucket_name, sha, size, mime)
            count += 1
            logger.info(
                "  %s  %d bytes  sha256=%s…",
                key,
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
