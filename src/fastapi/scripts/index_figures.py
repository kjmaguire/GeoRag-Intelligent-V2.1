"""Extract and index figures from all PDFs in MinIO bronze bucket.

Usage:
    docker exec georag-fastapi python /app/scripts/index_figures.py
"""

import asyncio
import logging
import os
import tempfile

from minio import Minio
from qdrant_client import AsyncQdrantClient
from sentence_transformers import SentenceTransformer

from app.agent.figure_extractor import extract_and_index_figures

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "georag-admin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "georag_minio_dev")
BUCKET = "bronze"
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")

PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"
REPORT_ID = "44a67709-b846-42ec-a361-9faa6e224170"
REPORT_TITLE = "NI 43-101 Technical Report"


async def main():
    logger.info("Loading embedding model...")
    embedder = SentenceTransformer("BAAI/bge-small-en-v1.5")

    logger.info("Connecting to Qdrant...")
    qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=6333)

    logger.info("Connecting to MinIO...")
    minio = Minio(MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, secure=False)

    # Find all PDFs in the reports/ prefix
    total = 0
    for obj in minio.list_objects(BUCKET, prefix="reports/", recursive=True):
        if not obj.object_name.endswith(".pdf"):
            continue

        logger.info("Processing %s...", obj.object_name)

        # Download to temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            response = minio.get_object(BUCKET, obj.object_name)
            for chunk in response.stream(8192):
                tmp.write(chunk)
            response.close()
            response.release_conn()
            tmp_path = tmp.name

        try:
            count = await extract_and_index_figures(
                pdf_path=tmp_path,
                report_id=REPORT_ID,
                project_id=PROJECT_ID,
                report_title=REPORT_TITLE,
                qdrant_client=qdrant,
                embedding_model=embedder,
            )
            total += count
            logger.info("  Indexed %d figures from %s", count, obj.object_name)
        finally:
            os.unlink(tmp_path)

    logger.info("Done — %d total figures indexed.", total)
    await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
