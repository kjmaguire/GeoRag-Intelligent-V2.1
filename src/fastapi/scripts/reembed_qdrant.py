"""Re-embed all Qdrant points with BAAI/bge-small-en-v1.5.

This script is a one-shot migration tool that replaces the all-MiniLM-L6-v2
vectors already stored in the ``georag_chunks`` and ``georag_reports``
collections with vectors produced by BAAI/bge-small-en-v1.5.

Both models produce 384-dimensional cosine-normalized vectors so no collection
recreation is required — we simply UPSERT each point with the same ID, same
payload, and a new vector.

Usage (from inside the container):
    docker exec georag-fastapi python /app/scripts/reembed_qdrant.py

Environment variables (read from the running container's environment):
    QDRANT_HOST  — default "qdrant"
    QDRANT_PORT  — default 6333

The script reads QDRANT_HOST / QDRANT_PORT directly from os.environ so it
does not require a .env file and can be run from any working directory.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reembed_qdrant")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
COLLECTIONS = ["georag_chunks", "georag_reports"]
SCROLL_LIMIT = 100  # points per scroll page
UPSERT_BATCH = 50   # points per upsert call


def _load_model():  # type: ignore[return]
    """Load the bge-small-en-v1.5 model and run a warm-up encode."""
    logger.info("Loading embedding model: %s", EMBEDDING_MODEL)
    t0 = time.perf_counter()
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
        model.encode("warm-up", normalize_embeddings=True)
        elapsed = time.perf_counter() - t0
        dim = model.get_sentence_embedding_dimension()
        logger.info("Model ready — dim=%d, loaded in %.2fs", dim, elapsed)
        return model
    except Exception:
        logger.exception("Failed to load model — aborting")
        sys.exit(1)


def _qdrant_client():  # type: ignore[return]
    """Create a synchronous Qdrant client (sync is fine for a one-shot script)."""
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415

        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=30)
        # Quick health check.
        collections = client.get_collections()
        names = [c.name for c in collections.collections]
        logger.info("Qdrant connected — collections: %s", names)
        return client
    except Exception:
        logger.exception("Failed to connect to Qdrant at %s:%s — aborting", QDRANT_HOST, QDRANT_PORT)
        sys.exit(1)


def _reembed_collection(
    client: Any,
    model: Any,
    collection_name: str,
) -> int:
    """Re-embed all points in ``collection_name`` and upsert them in place.

    Returns the total number of points re-embedded.
    """
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    # Verify the collection exists before attempting to scroll.
    try:
        info = client.get_collection(collection_name)
        total_points = info.points_count
    except Exception:
        logger.warning("Collection '%s' not found — skipping", collection_name)
        return 0

    logger.info(
        "Collection '%s' — %d points to re-embed",
        collection_name,
        total_points,
    )

    offset = None
    total_reembedded = 0
    batch_num = 0

    while True:
        # Scroll through all points, fetching payload (for the text field) but
        # not vectors (we don't need the old vectors).
        scroll_result = client.scroll(
            collection_name=collection_name,
            limit=SCROLL_LIMIT,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        points_batch, next_offset = scroll_result

        if not points_batch:
            break

        batch_num += 1
        logger.info(
            "  Batch %d — scrolled %d points (offset=%s)",
            batch_num,
            len(points_batch),
            offset,
        )

        # Build (text, point_id, payload) tuples for this scroll page.
        texts: list[str] = []
        point_ids: list[Any] = []
        payloads: list[dict] = []

        for point in points_batch:
            payload = point.payload or {}
            text = payload.get("text", "")
            if not text:
                # A point with no text payload cannot be re-embedded; skip it
                # and log a warning so the operator knows.
                logger.warning(
                    "  Point %s in '%s' has no 'text' payload — skipping",
                    point.id,
                    collection_name,
                )
                continue
            texts.append(text)
            point_ids.append(point.id)
            payloads.append(payload)

        if not texts:
            logger.info("  No embeddable texts in this batch — continuing")
            offset = next_offset
            if next_offset is None:
                break
            continue

        # Batch-encode all texts in this scroll page.
        t_encode = time.perf_counter()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=False,
        )
        encode_elapsed = time.perf_counter() - t_encode
        logger.info(
            "  Encoded %d texts in %.2fs",
            len(texts),
            encode_elapsed,
        )

        # Upsert in sub-batches to avoid large single requests to Qdrant.
        upserted = 0
        for i in range(0, len(texts), UPSERT_BATCH):
            sub_ids = point_ids[i : i + UPSERT_BATCH]
            sub_vectors = vectors[i : i + UPSERT_BATCH]
            sub_payloads = payloads[i : i + UPSERT_BATCH]

            upsert_points = [
                PointStruct(
                    id=pid,
                    vector=vec.tolist(),
                    payload=pay,
                )
                for pid, vec, pay in zip(sub_ids, sub_vectors, sub_payloads)
            ]

            client.upsert(
                collection_name=collection_name,
                points=upsert_points,
                wait=True,
            )
            upserted += len(upsert_points)

        total_reembedded += upserted
        logger.info(
            "  Upserted %d points (total so far: %d / %d)",
            upserted,
            total_reembedded,
            total_points,
        )

        offset = next_offset
        if next_offset is None:
            break

    logger.info(
        "Collection '%s' done — %d/%d points re-embedded",
        collection_name,
        total_reembedded,
        total_points,
    )
    return total_reembedded


def main() -> None:
    """Entry point — re-embed all configured collections."""
    t_start = time.perf_counter()

    model = _load_model()
    client = _qdrant_client()

    grand_total = 0
    for collection in COLLECTIONS:
        count = _reembed_collection(client, model, collection)
        grand_total += count

    elapsed = time.perf_counter() - t_start
    logger.info(
        "Re-embedding complete — %d points across %d collections in %.1fs",
        grand_total,
        len(COLLECTIONS),
        elapsed,
    )


if __name__ == "__main__":
    main()
