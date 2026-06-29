"""Clear embedding_id on enriched passages so the embed sweep re-encodes them.

Run this AFTER enrich_all_passages_full.py completes.  The embed sweep
(`embed_pending_passages`) only touches rows where `embedding_id IS NULL`,
so we must clear that field to force re-encoding with the new
contextualized_content text.

This script:
  1. Clears embedding_id (→ NULL) for every passage that has
     contextualized_content filled in.
  2. Optionally deletes the corresponding Qdrant points so the collection
     stays consistent (old dense vector was encoded from plain text; it
     should be replaced by the enriched-text vector).

The embed sweep will re-encode and re-upsert each point, overwriting the
stale Qdrant vector with the enriched one.

Usage (inside georag-fastapi container):
    python3 /app/scripts/reset_embeddings_for_reencode.py

Options via env:
    QDRANT_DELETE=1   — also delete stale Qdrant points (default 1)
    DRY_RUN=1         — print counts but do NOT write anything (default 0)
    BATCH_SIZE=1000   — rows per DELETE/UPDATE batch (default 1000)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("georag.reset_embeddings")

PG_DSN = (
    f"postgresql://{os.environ.get('POSTGRES_USER', 'georag')}:"
    f"{os.environ.get('POSTGRES_PASSWORD', '')}@"
    f"{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
    f"{os.environ.get('POSTGRES_DIRECT_PORT', '5432')}/"
    f"{os.environ.get('POSTGRES_DB', 'georag')}"
)

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = "georag_chunks"

QDRANT_DELETE = os.environ.get("QDRANT_DELETE", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1000"))


async def main() -> None:
    if DRY_RUN:
        log.info("DRY_RUN mode — no writes will be performed")

    pg = await asyncpg.connect(PG_DSN, statement_cache_size=0)

    # How many passages have been enriched and still have an embedding_id?
    count = await pg.fetchval(
        """
        SELECT COUNT(*)
          FROM silver.document_passages
         WHERE contextualized_content IS NOT NULL
           AND embedding_id IS NOT NULL
        """
    )
    log.info("Passages to reset: %d (have contextualized_content + embedding_id)", count)

    if count == 0:
        log.info("Nothing to reset — either enrichment hasn't run yet or all "
                 "embedding_ids are already cleared.")
        await pg.close()
        return

    if DRY_RUN:
        log.info("DRY_RUN: would clear %d embedding_ids and delete Qdrant points", count)
        await pg.close()
        return

    # ── Optionally purge stale Qdrant points ──────────────────────────────
    if QDRANT_DELETE:
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import FilterSelector, Filter, FieldCondition, MatchValue

            qc = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

            # Collect embedding_ids in batches and delete from Qdrant
            offset = 0
            deleted_total = 0
            t0 = time.time()
            while True:
                rows = await pg.fetch(
                    """
                    SELECT embedding_id
                      FROM silver.document_passages
                     WHERE contextualized_content IS NOT NULL
                       AND embedding_id IS NOT NULL
                     ORDER BY created_at ASC
                     LIMIT $1 OFFSET $2
                    """,
                    BATCH_SIZE, offset,
                )
                if not rows:
                    break

                point_ids = [str(r["embedding_id"]) for r in rows]
                try:
                    await qc.delete(
                        collection_name=QDRANT_COLLECTION,
                        points_selector=point_ids,
                        wait=True,
                    )
                    deleted_total += len(point_ids)
                except Exception as exc:
                    log.warning("qdrant_delete_batch_failed offset=%d err=%s", offset, exc)

                offset += len(rows)
                log.info(
                    "Qdrant delete progress: %d/%d (%.1f%%) elapsed=%.0fs",
                    deleted_total, count,
                    100 * deleted_total / max(count, 1),
                    time.time() - t0,
                )

            await qc.close()
            log.info("Qdrant: deleted %d stale points from %s", deleted_total, QDRANT_COLLECTION)
        except Exception as exc:
            log.error(
                "Qdrant delete failed — continuing with Postgres reset anyway. err=%s", exc
            )
    else:
        log.info("QDRANT_DELETE=0 — skipping Qdrant point deletion "
                 "(stale vectors will be overwritten on upsert)")

    # ── Clear embedding_id in Postgres ────────────────────────────────────
    log.info("Clearing embedding_id on %d enriched passages …", count)
    t1 = time.time()
    # asyncpg execute() returns a status string like "UPDATE 158233"
    result = await pg.execute(
        """
        UPDATE silver.document_passages
           SET embedding_id = NULL,
               updated_at   = NOW()
         WHERE contextualized_content IS NOT NULL
           AND embedding_id IS NOT NULL
        """
    )
    rows_updated = int(result.split()[-1]) if result else 0
    log.info(
        "Reset complete. embedding_id cleared on %d rows in %.1fs",
        rows_updated, time.time() - t1,
    )

    await pg.close()

    log.info(
        "Done. Next step: trigger embed sweep "
        "(hatchet embed_pending_passages_wf or nightly cron at 05:45 UTC)."
    )


if __name__ == "__main__":
    asyncio.run(main())
