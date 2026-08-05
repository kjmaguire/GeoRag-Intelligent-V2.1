"""Re-embed all Qdrant points with Qwen/Qwen3-Embedding-0.6B.

2026-06-04 — UPDATED for the bge-small → Qwen3-Embedding swap.
The 1024-dim Qwen3 vector does NOT fit in the old 384-dim bge collection,
so this script checks the collection's configured vector dim against the
loaded model before encoding.

2026-07-02 review fix — dim-mismatch handling is two-tier:
  - CANONICAL collections (georag_chunks) and any collection named
    explicitly on the command line MUST be re-embedded: a dim-mismatch
    (or missing-collection) skip makes the run exit non-zero, telling the
    operator to run ``init_qdrant.py --recreate`` first. Exiting 0 after
    silently skipping the canonical corpus is how the 2026-06-01 incident
    happened (surface-level "success", retrieval refused every question).
  - Legacy/non-canonical collections (georag_reports, a deliberately
    separate 384-dim bge space) soft-skip with a warning — re-embedding
    them at 1024 would be wrong.

Usage: ``python reembed_qdrant.py [collection ...]`` — with no args, all
COLLECTIONS are processed and only CANONICAL_COLLECTIONS are must-succeed;
with args, only the named collections run and ALL of them are must-succeed.

Migration sequence
------------------
    # 1. Snapshot the existing collection (rollback insurance):
    curl -X POST "http://localhost:6333/collections/georag_chunks/snapshots"

    # 2. Recreate the collection at 1024-dim:
    docker exec georag-fastapi python /app/scripts/init_qdrant.py --recreate

    # 3. Re-embed all points (this script):
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
# Qwen/Qwen3-Embedding-0.6B per the 2026-06-04 dual model swap. Family-
# aligned with Qwen3-14B-AWQ synthesizer + Qwen3-Reranker-0.6B.
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EXPECTED_VECTOR_DIM = 1024  # Qwen3-Embedding-0.6B dim. Assert on first load.
COLLECTIONS = ["georag_chunks", "georag_reports"]
# The canonical corpus (ADR-0010) — production retrieval reads it, so a run
# that skips it must NOT exit 0 (see module docstring / 2026-06-01 incident).
CANONICAL_COLLECTIONS = {"georag_chunks"}
SCROLL_LIMIT = 100  # points per scroll page
UPSERT_BATCH = 50   # points per upsert call


def _load_model():  # type: ignore[return]
    """Load the configured embedding model and run a warm-up encode.

    Routes through app.services.embedding.get_embedding_model() — same
    EMBEDDING_BACKEND precedence app/main.py and passage_embedder.py already
    use. This script used to hardcode a local Qwen3-Embedding-0.6B load
    regardless of backend, so EMBEDDING_BACKEND=foundry deployments (this
    Azure cutover included) would re-embed the corpus with the WRONG model —
    the exact "surface-level success, retrieval refused every question"
    failure mode this file's own module docstring warns about, just moved
    one step earlier in the pipeline.
    """
    t0 = time.perf_counter()
    try:
        from app.services.embedding import get_embedding_model  # noqa: PLC0415

        model = get_embedding_model(EMBEDDING_MODEL)
        model.encode("warm-up", normalize_embeddings=True)
        elapsed = time.perf_counter() - t0
        dim = model.get_sentence_embedding_dimension()
        logger.info("Model ready — dim=%s, loaded in %.2fs", dim, elapsed)
        return model
    except Exception:
        logger.exception("Failed to load model — aborting")
        sys.exit(1)


def _qdrant_client():  # type: ignore[return]
    """Create a synchronous Qdrant client (sync is fine for a one-shot script).

    Routes through app.services.qdrant_conn.qdrant_client_kwargs() — this
    script used to build host/port directly and never picked up https/
    api_key, so it could not reach Azure Container Apps' internal ingress
    (HTTPS-only on 443) at all.
    """
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415

        from app.services.qdrant_conn import qdrant_client_kwargs  # noqa: PLC0415

        client = QdrantClient(**qdrant_client_kwargs(), timeout=30)
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
) -> tuple[int, bool]:
    """Re-embed all points in ``collection_name`` and upsert them in place.

    Returns ``(count, skipped)``: the number of points re-embedded, and
    whether the collection was skipped entirely (missing, or vector-dim
    mismatch). The caller decides whether a skip is fatal — it is for the
    canonical / explicitly-requested collections.
    """
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    # Verify the collection exists before attempting to scroll.
    try:
        info = client.get_collection(collection_name)
        total_points = info.points_count
    except Exception:
        logger.warning("Collection '%s' not found — skipping", collection_name)
        return 0, True

    # 2026-06-04 guard — verify collection vector dim matches the loaded
    # model. The Qwen3-Embedding swap changed dim 384→1024, and an in-place
    # UPSERT into a 384-dim collection with a 1024-dim vector returns HTTP
    # 400 from Qdrant ("Wrong vector size"). Catch it here with a clear
    # operator action message rather than failing mid-batch.
    try:
        # Qdrant client surface: info.config.params.vectors.size for
        # single-vector collections; .vectors[name].size for multi-vector.
        params = info.config.params
        vectors_cfg = params.vectors
        if hasattr(vectors_cfg, "size"):
            collection_dim = vectors_cfg.size
        elif isinstance(vectors_cfg, dict):
            # Named vectors — assume single 'default' or take first.
            first_named = next(iter(vectors_cfg.values()))
            collection_dim = first_named.size
        else:
            collection_dim = None
        if collection_dim is not None and collection_dim != EXPECTED_VECTOR_DIM:
            # Audit 2026-06-29: SKIP a dim-mismatched collection rather than
            # aborting the whole run mid-loop. georag_reports is a SEPARATE
            # bge/384-dim corpus (per C1) — re-embedding it at 1024 would be
            # wrong. Review fix 2026-07-02: the skip is reported to main(),
            # which exits non-zero if the skipped collection is canonical or
            # was explicitly requested — a silent exit-0 skip of the canonical
            # corpus is the 2026-06-01 incident failure mode.
            logger.warning(
                "Collection '%s' has vector dim=%d but model %s produces "
                "dim=%d — SKIPPING (separate-corpus dim mismatch). Recreate "
                "explicitly via init_qdrant.py --recreate if a dim change is "
                "intended.",
                collection_name, collection_dim, EMBEDDING_MODEL, EXPECTED_VECTOR_DIM,
            )
            return 0, True
    except SystemExit:
        raise
    except Exception:
        logger.warning(
            "Could not verify vector dim on '%s'; proceeding (may fail on UPSERT)",
            collection_name,
        )

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
        # Audit 2026-06-29: batch_size is env-tunable. Qwen3-Embedding-0.6B on
        # CPU with long (~400-token) chunks spikes activation memory at
        # batch_size=32 and OOM-killed a 6 GiB container. Default 8 keeps the
        # peak well under a modest container limit.
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=int(os.environ.get("REEMBED_BATCH_SIZE", "8")),
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
    return total_reembedded, False


def main() -> None:
    """Entry point — re-embed all configured collections.

    Optional CLI args name specific collections to re-embed; explicitly
    requested collections are always must-succeed. With no args, all
    COLLECTIONS run and only CANONICAL_COLLECTIONS are must-succeed.
    """
    t_start = time.perf_counter()

    requested = sys.argv[1:]
    collections = requested or COLLECTIONS
    required = set(requested) if requested else set(CANONICAL_COLLECTIONS)

    model = _load_model()
    client = _qdrant_client()

    grand_total = 0
    skipped: list[str] = []
    for collection in collections:
        count, was_skipped = _reembed_collection(client, model, collection)
        grand_total += count
        if was_skipped:
            skipped.append(collection)

    elapsed = time.perf_counter() - t_start
    logger.info(
        "Re-embedding complete — %d points across %d collections in %.1fs"
        " (skipped: %s)",
        grand_total,
        len(collections),
        elapsed,
        ", ".join(skipped) or "none",
    )

    fatal = [name for name in skipped if name in required]
    if fatal:
        logger.error(
            "FATAL: required collection(s) %s were skipped (missing or "
            "vector-dim mismatch) — no points were re-embedded for them. Run "
            "`python scripts/init_qdrant.py --recreate` to drop + recreate "
            "the collection at the model dim FIRST, then re-run this script. "
            "Exiting non-zero so a forgotten recreate cannot masquerade as "
            "success (2026-06-01 incident: a mis-shaped georag_chunks passed "
            "surface checks while retrieval refused every question).",
            ", ".join(fatal),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
