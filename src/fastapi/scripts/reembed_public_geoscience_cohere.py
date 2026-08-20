#!/usr/bin/env python
"""Re-embed the public-geoscience pg_* Qdrant collections into Cohere Embed v4.

Why this exists
---------------
The pg_* collections were built by the Dagster asset
``index_public_geoscience_qdrant`` against BAAI/bge-small-en-v1.5 — a 384-dim
vector space, deliberately pinned there (see that asset's own "Audit
2026-06-27 (C1)" comment and its dim-parity guard). At the time that matched
the internal corpus, so the same query vector could search both.

The internal corpus has since moved twice — to Qwen3-Embedding-0.6B, then to
Cohere Embed v4 on Foundry — while these collections did not. The reader,
``app.agent.public_geoscience_tool.search_public_geoscience``, embeds its
query with the shared runtime embedder (1024-dim), so every search against a
384-dim collection fails with::

    HTTP 400: expected dim: 384, got 1024

182,826 indexed points have therefore been unreachable. This script closes
the gap by re-embedding them into Embed v4's 1024-dim space — the SAME space
the internal corpus now occupies, which restores the original "one query
vector searches both corpora" property that the bge pin existed to protect.

Where the corpus actually lives — READ THIS FIRST
-------------------------------------------------
Verified 2026-08-19, and it is not what the PR description originally claimed:

    local Docker Qdrant   182,826 points across 6 pg_* collections, all 384-dim
    Azure Qdrant          NO pg_* collections at all (only georag_chunks,
                          georag_reports)
    local Postgres        ~514k public_geo.* rows
    Azure Postgres        public_geo.* schema present, 0 rows

So the public-geoscience corpus exists ONLY on the local development machine,
in both stores. Running this script against the local stack produces 1024-dim
collections that are still local-only; it does NOT by itself make public-geo
work in production. Getting there additionally requires shipping the resulting
collections (or the source rows) to Azure — a separate data migration that
this script deliberately does not attempt.

Why it reads Qdrant rather than Postgres
----------------------------------------
Because the indexer stores ``summary_text`` in every payload, documented as
"human-readable — same as the embedded text". The exact string that produced
each 384-dim vector is therefore recoverable from Qdrant alone, which makes
the re-encode faithful rather than a reconstruction, and means the script
needs neither the Postgres rows nor any Dagster asset. It does NOT revive or
modify Dagster.

Safety
------
Writes to NEW collections (``<name><suffix>``, default suffix ``_v2``) and
never mutates or deletes the originals. Cutover is a separate, reversible
step: set ``PUBLIC_GEO_COLLECTION_SUFFIX`` on the FastAPI app so the reader
resolves the new names. Roll back by clearing that env var.

Point IDs and payloads are copied verbatim, so the new collections keep the
indexer's deterministic pg_id-derived IDs — a later Dagster re-run (if it is
ever re-pointed at Embed v4) would update in place rather than duplicate.

Usage
-----
    # See what would happen, embed nothing:
    python -m scripts.reembed_public_geoscience_cohere --dry-run

    # Real run, all collections:
    python -m scripts.reembed_public_geoscience_cohere

    # One collection, smaller batches:
    python -m scripts.reembed_public_geoscience_cohere \
        --collections pg_mine --batch-size 32

A real run uses whatever embedder EMBEDDING_* is configured for — the SAME
resolution path ``search_public_geoscience`` uses, so index-time and
query-time always agree with each other. The only hard requirement is that
the embedder is 1024-dim, so the vectors land in the space the reader
queries.

It deliberately does NOT require a particular vendor. On the local stack that
means the self-hosted 1024-dim model and **no Azure contact whatsoever** —
which is the correct way to make a local-only corpus searchable locally.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

log = logging.getLogger("georag.reembed_public_geo")

# Matches _COLLECTION_FOR_TYPE in app/agent/public_geoscience_tool.py. Listed
# explicitly rather than imported so the script states its own blast radius.
SOURCE_COLLECTIONS = [
    "pg_mine",
    "pg_mineral_occurrence",
    "pg_drillhole_collar",
    "pg_resource_potential_zone",
    "pg_rock_sample",
    "pg_assessment_survey",
    "pg_mineral_disposition",
]

TARGET_DIMENSION = 1024
DEFAULT_SUFFIX = "_v2"
DEFAULT_BATCH_SIZE = 96
SCROLL_PAGE = 256


def _require_query_parity(model: Any) -> str:
    """Assert the index-time embedder matches what the reader will query with.

    The real invariant is NOT "must be Cohere". It is "the vectors written
    here must live in the same space as the vector the reader embeds its
    query into" — cosine distance across two embedding spaces is meaningless,
    and the failure mode is quietly bad results rather than an error.

    That invariant is satisfied structurally: this script and
    ``search_public_geoscience`` both resolve their embedder through
    ``get_embedding_model()`` against the same config, so whatever backend is
    configured is used on both sides. Whichever environment you run in, index
    and query agree with each other.

    An earlier version of this hard-required EMBEDDING_BACKEND=foundry. That
    was wrong twice over: it expressed a dimension invariant as a vendor lock,
    and it made the script unrunnable in the only environment that holds the
    corpus — the local stack, which runs a self-hosted 1024-dim embedder and
    has no Azure credentials by design. Re-embedding locally with the local
    model is correct and self-consistent; it just produces local vectors,
    which is exactly what a local-only corpus needs.

    So the check that remains is the one that actually protects the data: the
    embedder's dimension must equal TARGET_DIMENSION, i.e. what the target
    collections are created with and what the reader queries with.

    Returns a short backend label, recorded in the run log so the provenance
    of a set of vectors is never ambiguous.
    """
    backend = (os.environ.get("EMBEDDING_BACKEND") or "").strip().lower() or "unset"

    dim = None
    getter = getattr(model, "get_sentence_embedding_dimension", None)
    if callable(getter):
        try:
            dim = int(getter())
        except Exception:  # noqa: BLE001 — fall through to the probe below
            dim = None
    if dim is None:
        # No dimension accessor: probe with one throwaway encode rather than
        # assume. Cheaper than discovering the mismatch 180k points later.
        dim = len(model.encode("dimension probe"))

    if dim != TARGET_DIMENSION:
        raise SystemExit(
            f"embedder reports {dim}-dim but the target collections are "
            f"{TARGET_DIMENSION}-dim (backend={backend}). Writing these "
            "vectors would recreate exactly the mismatch this script exists "
            "to fix. Point EMBEDDING_* at a "
            f"{TARGET_DIMENSION}-dim model and re-run."
        )

    log.info("embedder OK: backend=%s dim=%d", backend, dim)
    return backend


async def _ensure_target(client: Any, name: str, *, recreate: bool) -> None:
    """Create the target collection with the same slot layout as the source."""
    from qdrant_client.models import (
        Distance,
        HnswConfigDiff,
        SparseIndexParams,
        SparseVectorParams,
        VectorParams,
    )

    existing = {c.name for c in (await client.get_collections()).collections}
    if name in existing:
        if not recreate:
            log.info("target %s already exists — reusing (upserts are idempotent)", name)
            return
        log.warning("target %s exists and --recreate given — deleting first", name)
        await client.delete_collection(name)

    # Same named-slot layout as index_public_geoscience.py: unnamed dense ""
    # (which is what the reader's query_points addresses) plus a sparse "text"
    # slot. The sparse slot is created but left unpopulated — this script
    # re-encodes dense vectors only, and the reader does not use the sparse
    # branch for public-geo today.
    await client.create_collection(
        collection_name=name,
        vectors_config={
            "": VectorParams(
                size=TARGET_DIMENSION,
                distance=Distance.COSINE,
                on_disk=False,
            ),
        },
        sparse_vectors_config={
            "text": SparseVectorParams(index=SparseIndexParams(on_disk=False)),
        },
        hnsw_config=HnswConfigDiff(m=32, ef_construct=256),
    )
    log.info("created %s (%d-dim, cosine)", name, TARGET_DIMENSION)


async def _reembed_collection(
    client: Any,
    model: Any,
    source: str,
    target: str,
    *,
    batch_size: int,
    dry_run: bool,
) -> dict[str, int]:
    """Scroll one source collection, re-encode summary_text, upsert to target."""
    from qdrant_client.models import PointStruct

    stats = {"scanned": 0, "embedded": 0, "skipped_no_text": 0, "upserted": 0}
    offset: Any = None
    pending: list[tuple[Any, str, dict]] = []

    async def _flush() -> None:
        if not pending:
            return
        if dry_run:
            # Deliberately short-circuits BEFORE encode(): a dry run must not
            # spend Foundry quota. It reports how many points would be
            # re-embedded, not what they would embed to.
            stats["upserted"] += len(pending)
            pending.clear()
            return
        texts = [t for _, t, _ in pending]
        # encode() defaults to input_type="search_document", which is the
        # correct asymmetric pairing for indexed corpus text — the reader's
        # embed_query() uses "search_query" on the other side.
        vectors = await asyncio.to_thread(model.encode, texts)
        points = [
            PointStruct(id=pid, vector={"": vec.tolist()}, payload=payload)
            for (pid, _, payload), vec in zip(pending, vectors, strict=True)
        ]
        await client.upsert(collection_name=target, points=points, wait=True)
        stats["upserted"] += len(points)
        pending.clear()

    while True:
        records, offset = await client.scroll(
            collection_name=source,
            limit=SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            break

        for rec in records:
            stats["scanned"] += 1
            payload = rec.payload or {}
            text = (payload.get("summary_text") or "").strip()
            if not text:
                # Nothing to re-encode from. Counted rather than guessed at —
                # a point with no summary_text was not embedded from anything
                # this script can reproduce.
                stats["skipped_no_text"] += 1
                continue
            pending.append((rec.id, text, payload))
            stats["embedded"] += 1
            if len(pending) >= batch_size:
                await _flush()

        if offset is None:
            break

    await _flush()
    return stats


async def _run(args: argparse.Namespace) -> int:
    # A dry run embeds nothing and writes nothing — it only scrolls the source
    # collections and counts what WOULD be re-encoded. Requiring Foundry
    # credentials for that made the one safe, verifiable mode of this script
    # unusable in exactly the environment that holds the data: the corpus
    # lives in the local Docker stack, which runs EMBEDDING_BACKEND=local and
    # has no Foundry credentials at all. Gate the check on the real run.
    from qdrant_client import AsyncQdrantClient

    from app.services.qdrant_conn import qdrant_client_kwargs

    model = None
    if not args.dry_run:
        from app.services.embedding import get_embedding_model

        model = get_embedding_model(model_name=os.environ.get("EMBEDDING_MODEL_NAME", ""))
        _require_query_parity(model)
    client = AsyncQdrantClient(**qdrant_client_kwargs())

    try:
        available = {c.name for c in (await client.get_collections()).collections}
        wanted = args.collections or SOURCE_COLLECTIONS
        missing = [c for c in wanted if c not in available]
        for name in missing:
            log.warning("source collection %s does not exist — skipping", name)
        todo = [c for c in wanted if c in available]

        if not todo:
            log.error("no source collections to process")
            return 1

        totals = {"scanned": 0, "embedded": 0, "skipped_no_text": 0, "upserted": 0}
        for source in todo:
            target = f"{source}{args.suffix}"
            count = (await client.count(collection_name=source, exact=True)).count
            log.info("── %s → %s (%d points)", source, target, count)

            if not args.dry_run:
                await _ensure_target(client, target, recreate=args.recreate)

            stats = await _reembed_collection(
                client, model, source, target,
                batch_size=args.batch_size, dry_run=args.dry_run,
            )
            log.info("   %s", stats)
            for k, v in stats.items():
                totals[k] += v

        log.info("TOTALS %s%s", totals, " (DRY RUN — nothing written)" if args.dry_run else "")
        if totals["skipped_no_text"]:
            log.warning(
                "%d points had no summary_text and were not re-embedded; they "
                "will be absent from the new collections",
                totals["skipped_no_text"],
            )
        return 0
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--collections", nargs="*", default=None,
        help=f"Subset to process. Default: all of {', '.join(SOURCE_COLLECTIONS)}",
    )
    p.add_argument(
        "--suffix", default=DEFAULT_SUFFIX,
        help=f"Target collection suffix (default: {DEFAULT_SUFFIX}). Must match "
             "PUBLIC_GEO_COLLECTION_SUFFIX at cutover.",
    )
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--recreate", action="store_true",
        help="Delete an existing target collection before writing. Without "
             "this, an existing target is reused (upserts are idempotent).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Scroll + count only. Creates nothing, embeds nothing, writes "
             "nothing, and needs no Foundry credentials.",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
