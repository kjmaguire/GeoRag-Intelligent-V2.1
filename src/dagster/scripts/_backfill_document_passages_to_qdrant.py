"""ADR-0010 Session A backfill driver — BATCH-RESUMABLE.

One-shot script that mirrors the index_document_passages Dagster asset
against live silver + Qdrant. Bypasses the Dagster CLI's env-var
post-processing issues from §35.

Key behaviour change from the v1 driver:
  • Batches dense+sparse+upsert per 320 passages so partial completion
    is preserved in Qdrant. If the script is killed mid-way, re-running
    skips passages already present (idempotency via passage_id).
  • Pre-scan Qdrant to find which passage_ids already exist, then
    skip those when building requests. Re-runs after a kill complete
    in O(remaining) time instead of starting over.

Usage:
    docker exec georag-dagster-webserver bash -c \\
        'cd //opt/dagster/app && PYTHONPATH=. python \\
         scripts/_backfill_document_passages_to_qdrant.py'

Output goes to stdout — capture with the usual docker exec semantics.
"""

import os
import sys
import time
from pathlib import Path

# Make the parent dagster/ dir importable when this script is invoked
# directly via `python scripts/<file>.py` (which only adds scripts/ to
# sys.path). Equivalent to running with PYTHONPATH=. but baked in.
_DAGSTER_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_DAGSTER_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_DAGSTER_APP_ROOT))

import psycopg2
import psycopg2.extras
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, SparseVector

from georag_dagster.assets.index_document_passages import (
    EMBED_BATCH_SIZE,
    EMBED_MODEL_NAME,
    PAYLOAD_TEXT_LIMIT,
    QDRANT_COLLECTION,
    SELECT_PASSAGES_SQL,
    _build_payload,
    _ensure_collection,
    _get_model,
)
from georag_dagster.assets.sparse_encoder import (
    SPARSE_MODEL_VERSION,
    encode_sparse_batch,
)


BATCH_SIZE = 320  # passages per dense+sparse+upsert cycle


class _Ctx:
    """Stand-in for AssetExecutionContext — only needs `.log`."""

    class _Log:
        def info(self, msg, *args):
            print("[INFO]", msg % args if args else msg, flush=True)

        def warning(self, msg, *args):
            print("[WARN]", msg % args if args else msg, flush=True)

        def error(self, msg, *args):
            print("[ERR ]", msg % args if args else msg, flush=True)

        def debug(self, msg, *args):
            pass

    log = _Log()


def _fetch_existing_qdrant_ids(qclient: QdrantClient) -> set[str]:
    """Scroll georag_chunks and return the set of point_ids already present.
    Used for resumability — re-runs skip these passages."""
    existing: set[str] = set()
    offset = None
    page_size = 1000
    while True:
        points, offset = qclient.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=page_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for p in points:
            existing.add(str(p.id))
        if offset is None:
            break
    return existing


def main() -> int:
    ctx = _Ctx()
    pg_dsn = (
        "postgresql://georag_app:georag-app-dev-2026"
        "@postgresql:5432/georag"
    )

    # --- 1) Pull all candidate passage rows once -------------------------
    ctx.log.info("connecting to postgres ...")
    sql = SELECT_PASSAGES_SQL + (
        "\nWHERE p.ocr_status IS NULL OR p.ocr_status != 'pending_reocr'"
        "\nORDER BY p.document_id, p.ordinal"
    )
    with psycopg2.connect(pg_dsn) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            all_rows = [dict(r) for r in cur.fetchall()]
    ctx.log.info(
        "scanned %d passages across %d documents",
        len(all_rows), len({r["document_id"] for r in all_rows}),
    )
    if not all_rows:
        ctx.log.warning("no rows to index — nothing to do")
        return 0

    # --- 2) Ensure Qdrant collection -------------------------------------
    qclient = QdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
    )
    ctx.log.info("ensuring '%s' collection schema ...", QDRANT_COLLECTION)
    _ensure_collection(qclient, ctx)

    # --- 3) Resumability — drop rows whose passage_id is already in Qdrant
    existing_ids = _fetch_existing_qdrant_ids(qclient)
    ctx.log.info(
        "found %d passage_ids already in '%s' (skip on this run)",
        len(existing_ids), QDRANT_COLLECTION,
    )
    rows = [r for r in all_rows if r["passage_id"] not in existing_ids]
    ctx.log.info(
        "%d passages remain to embed (skipped %d already-done)",
        len(rows), len(all_rows) - len(rows),
    )
    if not rows:
        ctx.log.info("all passages already in Qdrant — backfill is complete")
        return 0

    # --- 4) Warm the sentence-transformers model -------------------------
    ctx.log.info("loading dense model %s ...", EMBED_MODEL_NAME)
    model = _get_model()
    ctx.log.info("dense model loaded; entering batch loop "
                 "(batch_size=%d total_batches=%d)",
                 BATCH_SIZE, (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE)

    # --- 5) Batch loop: dense → sparse → upsert per BATCH_SIZE passages --
    started = time.time()
    total_upserted = 0
    total_skipped_empty = 0
    sparse_term_acc = 0
    sparse_term_n = 0
    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start:batch_start + BATCH_SIZE]
        texts = [r["text"] or "" for r in batch]

        t_dense_0 = time.time()
        dense = model.encode(texts, batch_size=EMBED_BATCH_SIZE)
        t_dense = time.time() - t_dense_0

        t_sparse_0 = time.time()
        sparse = encode_sparse_batch(texts, batch_size=16)
        t_sparse = time.time() - t_sparse_0
        sparse_term_acc += sum(len(s) for s in sparse)
        sparse_term_n += len(sparse)

        points: list[PointStruct] = []
        for row, emb, sv, payload_text in zip(batch, dense, sparse, texts):
            if not payload_text.strip():
                total_skipped_empty += 1
                continue
            vec: dict = {"": emb.tolist()}
            if sv:
                idx = sorted(sv.keys())
                vec["text"] = SparseVector(
                    indices=idx,
                    values=[sv[i] for i in idx],
                )
            display = (
                payload_text if PAYLOAD_TEXT_LIMIT is None
                else payload_text[:PAYLOAD_TEXT_LIMIT]
            )
            points.append(PointStruct(
                id=row["passage_id"],
                vector=vec,
                payload=_build_payload(row, display),
            ))

        t_upsert_0 = time.time()
        if points:
            qclient.upsert(collection_name=QDRANT_COLLECTION, points=points)
        t_upsert = time.time() - t_upsert_0
        total_upserted += len(points)

        elapsed = time.time() - started
        done = batch_start + len(batch)
        remaining = len(rows) - done
        rate = done / max(elapsed, 1)
        eta_sec = remaining / max(rate, 0.01)
        ctx.log.info(
            "batch [%d:%d] done=%d/%d upserted=%d "
            "dense=%.1fs sparse=%.1fs upsert=%.2fs "
            "rate=%.1f/s ETA=%.0fs",
            batch_start, batch_start + len(batch),
            done, len(rows), len(points),
            t_dense, t_sparse, t_upsert, rate, eta_sec,
        )

    # --- 6) Verify ------------------------------------------------------
    final = qclient.get_collection(QDRANT_COLLECTION)
    ctx.log.info(
        "DONE — '%s' has %d points (upserted=%d this run, "
        "skipped_empty=%d, avg_sparse_terms=%.0f)",
        QDRANT_COLLECTION, final.points_count, total_upserted,
        total_skipped_empty,
        sparse_term_acc / max(sparse_term_n, 1),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
