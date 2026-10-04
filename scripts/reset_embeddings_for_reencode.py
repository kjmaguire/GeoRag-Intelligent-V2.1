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

``--all`` (ADR-0025, 2026-10-04) is the other mode, for changing the embedding
MODEL rather than the text that was embedded. The default only touches rows
with ``contextualized_content IS NOT NULL``, so after a model change it would
leave every un-enriched passage and every ``modality='image'`` page with an
old-model vector -- a collection holding two vector spaces, which still
returns results, ranked by meaningless cosines between unrelated spaces
(ADR-0025 gotcha 2). ``--all`` instead:

  1. deletes EVERY point in georag_chunks (found by scrolling Qdrant, not by
     joining on Postgres, so orphan points go too), then
  2. sets embedding_id = NULL on EVERY passage, text and image alike.

Run it only AFTER EMBEDDING_BACKEND has been changed on BOTH the fastapi
service and the hatchet-worker in the same apply, and after taking a Qdrant
snapshot (the rollback; ADR-0025 migration step 3). Retrieval is degraded
until ``embed_pending_passages`` has re-encoded everything: there is no
Qdrant alias, so the collection name is fixed (migration step 5). It is
destructive and so asks first: type the confirmation phrase, or pass
``--yes`` (required when stdin is not a terminal, as under ``docker exec``
without ``-t`` or an ECS exec).

Usage (inside georag-fastapi container):
    python3 /app/scripts/reset_embeddings_for_reencode.py            # enriched rows only
    python3 /app/scripts/reset_embeddings_for_reencode.py --all      # model change; asks first
    python3 /app/scripts/reset_embeddings_for_reencode.py --all --yes

Options via env:
    QDRANT_DELETE=1   — also delete stale Qdrant points (default 1; ignored
                        by --all, which always empties the collection)
    DRY_RUN=1         — print counts but do NOT write anything (default 0)
    BATCH_SIZE=1000   — rows per DELETE/UPDATE batch (default 1000)

Connects as POSTGRES_USER (default ``georag``, the owner). Under row-level
security a role without the workspace GUC sees no rows, so run this as the
owner, as the existing mode always has.
"""
from __future__ import annotations

import argparse
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


#: What ``--all`` makes the operator type. Not "yes": a flag name or an
#: arrow-up in shell history can repeat "yes" without anyone reading the
#: warning, and this deletes the whole dense corpus.
CONFIRM_PHRASE = "reset-all-embeddings"


def _qdrant_client():
    """Async Qdrant client with the deployment's scheme and API key.

    The pre-ADR-0025 mode built ``host=, port=`` by hand, which cannot reach an
    https or api-key Qdrant. Falls back to that when ``app`` is not importable
    (the script is copied around), so it still runs where it used to.
    """
    from qdrant_client import AsyncQdrantClient

    try:
        from app.services.qdrant_conn import qdrant_client_kwargs

        return AsyncQdrantClient(**qdrant_client_kwargs())
    except ImportError:
        return AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def _confirm_reset_all(passages: int, images: int, *, assume_yes: bool, dry_run: bool) -> bool:
    """Ask before emptying the collection. True means go ahead."""
    log.warning(
        "--all will DELETE EVERY POINT in %s and set embedding_id = NULL on %d "
        "passage(s) (%d of them modality='image'). Retrieval is degraded until "
        "embed_pending_passages re-encodes them all. Have you (1) set the new "
        "EMBEDDING_BACKEND on BOTH fastapi and hatchet-worker, and (2) taken a "
        "Qdrant snapshot? Without the snapshot, going back means another full "
        "re-embed.",
        QDRANT_COLLECTION, passages, images,
    )
    if dry_run:
        return True
    if assume_yes:
        log.info("--yes given — not prompting")
        return True
    if not sys.stdin.isatty():
        log.error(
            "stdin is not a terminal, so no confirmation can be read. Re-run "
            "with --yes if you mean it."
        )
        return False
    typed = input(f"Type {CONFIRM_PHRASE!r} to proceed: ").strip()
    if typed != CONFIRM_PHRASE:
        log.error("Confirmation phrase not entered — nothing was changed.")
        return False
    return True


async def _delete_every_point(batch_size: int) -> int:
    """Delete all points in the collection, by scrolling their ids.

    Scrolling Qdrant rather than joining on silver.document_passages is the
    point: a point whose passage row was deleted, or whose embedding_id was
    already NULL, is still a vector in the OLD space and would survive a
    Postgres-driven delete.
    """
    qc = _qdrant_client()
    deleted = 0
    try:
        before = (await qc.count(collection_name=QDRANT_COLLECTION, exact=True)).count
        while True:
            # Always from the start: what was just deleted is gone, so the next
            # page is simply what is left. Carrying next_page_offset across
            # deletes would be correct on Qdrant but is one more thing to be
            # wrong about in a script that runs once.
            points, _next = await qc.scroll(
                collection_name=QDRANT_COLLECTION,
                limit=batch_size,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                break
            await qc.delete(
                collection_name=QDRANT_COLLECTION,
                points_selector=[p.id for p in points],
                wait=True,
            )
            deleted += len(points)
            log.info("Qdrant delete progress: %d/%d points removed", deleted, before)
            if deleted > before + batch_size:
                # Points keep coming back (a writer is still running, or the
                # delete is not taking effect). Do not loop forever.
                raise RuntimeError(
                    f"deleted {deleted} points but only {before} existed: is an "
                    "embed_pending_passages sweep still writing? Stop it and re-run."
                )
        remaining = (await qc.count(collection_name=QDRANT_COLLECTION, exact=True)).count
        if remaining:
            raise RuntimeError(f"{remaining} point(s) still in {QDRANT_COLLECTION} after the delete")
    finally:
        await qc.close()
    return deleted


async def reset_all(pg: asyncpg.Connection, *, assume_yes: bool) -> None:
    """The ``--all`` mode: every point, every passage (ADR-0025 step 4)."""
    passages = await pg.fetchval("SELECT COUNT(*) FROM silver.document_passages")
    with_id = await pg.fetchval(
        "SELECT COUNT(*) FROM silver.document_passages WHERE embedding_id IS NOT NULL"
    )
    images = await pg.fetchval(
        "SELECT COUNT(*) FROM silver.document_passages WHERE modality = 'image'"
    )
    log.info(
        "Passages: %d total, %d with an embedding_id, %d modality='image'",
        passages, with_id, images,
    )
    if not _confirm_reset_all(passages, images, assume_yes=assume_yes, dry_run=DRY_RUN):
        await pg.close()
        sys.exit(2)
    if DRY_RUN:
        log.info("DRY_RUN: would delete every Qdrant point and clear %d embedding_ids", passages)
        await pg.close()
        return

    # Qdrant first, Postgres second: the reverse order, interrupted, leaves
    # passages marked "to embed" beside points still serving the old space --
    # the partial state this mode exists to avoid. Interrupted this way round,
    # a rerun simply finishes the job.
    deleted = await _delete_every_point(BATCH_SIZE)
    log.info("Qdrant: deleted %d point(s) from %s", deleted, QDRANT_COLLECTION)

    t1 = time.time()
    result = await pg.execute(
        """
        UPDATE silver.document_passages
           SET embedding_id = NULL,
               updated_at   = NOW()
         WHERE embedding_id IS NOT NULL
        """
    )
    rows_updated = int(result.split()[-1]) if result else 0
    left = await pg.fetchval(
        "SELECT COUNT(*) FROM silver.document_passages WHERE embedding_id IS NOT NULL"
    )
    log.info(
        "Reset complete. embedding_id cleared on %d rows in %.1fs; %d still set.",
        rows_updated, time.time() - t1, left,
    )
    await pg.close()
    if left:
        log.error("%d passage(s) still have an embedding_id — investigate before re-embedding", left)
        sys.exit(1)
    log.info(
        "Done. Next: trigger embed_pending_passages (hatchet embed_pending_passages_wf or "
        "the nightly cron) and verify with ADR-0025 migration step 6: every point has "
        "embed_model = the new model, no passage has embedding_id IS NULL."
    )


async def main(*, reset_everything: bool = False, assume_yes: bool = False) -> None:
    if DRY_RUN:
        log.info("DRY_RUN mode — no writes will be performed")

    pg = await asyncpg.connect(PG_DSN, statement_cache_size=0)

    if reset_everything:
        await reset_all(pg, assume_yes=assume_yes)
        return

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear embedding_id so embed_pending_passages re-encodes (see module docstring).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="reset_everything",
        help=(
            "Model change (ADR-0025): delete EVERY Qdrant point and null embedding_id on EVERY "
            "passage, text and image, not only enriched rows. Destructive; asks first."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the --all confirmation prompt (required when stdin is not a terminal).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    _args = _parse_args()
    if _args.yes and not _args.reset_everything:
        log.warning("--yes only affects --all; ignored")
    asyncio.run(main(reset_everything=_args.reset_everything, assume_yes=_args.yes))
