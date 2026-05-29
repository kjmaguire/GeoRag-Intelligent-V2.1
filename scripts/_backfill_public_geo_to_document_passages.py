#!/usr/bin/env python
"""TIER 0b — backfill Qdrant `pg_*` summary_text into silver.document_passages.

Pass 3 of the 2026-05-28 audit found six Qdrant collections holding
**150,304 unused points**, each with a `summary_text` field containing
real natural-language prose:

  pg_mineral_occurrence    71,056   BC MINFILE + SK MDI + provincial registries
  pg_drillhole_collar      33,490   public drillholes
  pg_rock_sample           29,875   government rock samples
  pg_assessment_survey     14,835   survey footprints
  pg_resource_potential_zone  908   resource potential zones
  pg_mine                     140   known mine sites

Never trained on. Not in silver.document_passages. Not reachable from
chat's `search_documents` path. This script fixes that:

  1. Scrolls each pg_* collection's points (with payload + no vector).
  2. For each point, INSERTs into silver.document_passages with:
       * passage_id = uuid5(NAMESPACE_OID, f"qdrant:{collection}:{point_id}")
       * workspace_id from payload (or default workspace)
       * chunk_kind = 'public_geo_synthesis'
       * parser_used = 'pg_qdrant_backfill_v1'
       * text = sanitised(summary_text)
       * + payload-derived fields stored as JSONB in threshold_payload
  3. Embed cron picks up the new passages → georag_chunks.

Idempotent via uuid5 — re-runs UPSERT, embedding_id is NULLed when text
changes so the embed cron re-embeds.

Sanitisation strips U+0000 + other C0 control characters before insert
so we don't recreate the silver.document_passages null-byte problem
TIER 0e ran into.

Usage
-----

    docker exec georag-fastapi bash -c \\
        "python /app/scripts/_backfill_public_geo_to_document_passages.py"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("backfill_public_geo")

# ADR-0010 §A canonical chunk_kind discriminator
CHUNK_KIND = "public_geo_synthesis"
PARSER_USED = "pg_qdrant_backfill_v1"
NAMESPACE = uuid.NAMESPACE_OID

# C0 control chars + U+0000 — Postgres TEXT wire protocol rejects U+0000.
_BAD_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitise(text: str | None) -> str:
    if text is None:
        return ""
    return _BAD_CHARS_RE.sub("", text).strip()


def _derive_passage_id(collection: str, point_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"qdrant:{collection}:{point_id}")


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:64]


_UPSERT_SQL = """
INSERT INTO silver.document_passages (
    passage_id, document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind,
    created_at, updated_at
)
VALUES (
    %(passage_id)s, NULL, %(workspace_id)s, 1,
    %(text)s, %(text_hash)s, %(ordinal)s, %(chunk_kind)s,
    NOW(), NOW()
)
ON CONFLICT (passage_id) DO UPDATE SET
    text         = EXCLUDED.text,
    text_hash    = EXCLUDED.text_hash,
    chunk_kind   = EXCLUDED.chunk_kind,
    updated_at   = NOW(),
    embedding_id = CASE
        WHEN silver.document_passages.text_hash = EXCLUDED.text_hash
        THEN silver.document_passages.embedding_id
        ELSE NULL
    END
"""


def _bulk_upsert(cur, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    import psycopg2.extras  # noqa: PLC0415
    psycopg2.extras.execute_batch(cur, _UPSERT_SQL, rows, page_size=500)
    return len(rows)


def _scroll_collection(client, collection: str, batch_size: int = 500):
    """Generator yielding (point_id, payload) tuples from a Qdrant collection."""
    offset = None
    total = 0
    while True:
        result = client.scroll(
            collection_name=collection,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points, next_offset = result
        if not points:
            break
        for p in points:
            yield str(p.id), (p.payload or {})
        total += len(points)
        if total % 5000 == 0:
            logger.info("    %s: scrolled %d points so far", collection, total)
        if next_offset is None:
            break
        offset = next_offset


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--qdrant-host",
                   default=os.environ.get("QDRANT_HOST", "qdrant"))
    p.add_argument("--qdrant-port", type=int,
                   default=int(os.environ.get("QDRANT_PORT", 6333)))
    p.add_argument("--default-workspace-id",
                   default="a0000000-0000-0000-0000-000000000001",
                   help="Workspace_id for points whose payload doesn't carry one")
    p.add_argument("--collections",
                   default="pg_mineral_occurrence,pg_drillhole_collar,pg_rock_sample,pg_assessment_survey,pg_resource_potential_zone,pg_mine",
                   help="Comma-separated list of pg_* collections to backfill")
    p.add_argument("--batch-size", type=int, default=500)
    args = p.parse_args()

    import psycopg2  # noqa: PLC0415
    from qdrant_client import QdrantClient  # noqa: PLC0415

    qclient = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)

    pgconn = psycopg2.connect(
        host=os.environ.get("POSTGRES_DIRECT_HOST", "postgresql"),
        port=int(os.environ.get("POSTGRES_DIRECT_PORT", 5432)),
        user=os.environ.get("POSTGRES_USER", "georag"),
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DB", "georag"),
    )
    pgconn.autocommit = False

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    logger.info("backfilling %d collection(s): %s", len(collections), collections)

    total_upserted = 0
    per_collection_counts: dict[str, dict[str, int]] = {}

    try:
        for col in collections:
            logger.info("--- %s ---", col)
            scrolled = 0
            upserted = 0
            skipped_no_text = 0
            batch: list[dict[str, Any]] = []

            for point_id, payload in _scroll_collection(qclient, col, args.batch_size):
                scrolled += 1
                summary = _sanitise(payload.get("summary_text") or payload.get("summary") or "")
                if not summary or len(summary) < 30:
                    skipped_no_text += 1
                    continue

                ws_id = (
                    payload.get("workspace_id")
                    or args.default_workspace_id
                )

                row = {
                    "passage_id":   str(_derive_passage_id(col, point_id)),
                    "workspace_id": str(ws_id),
                    "text":         summary,
                    "text_hash":    _text_hash(summary),
                    "ordinal":      0,
                    "chunk_kind":   CHUNK_KIND,
                    "parser_used":  PARSER_USED,
                }
                batch.append(row)

                if len(batch) >= args.batch_size:
                    with pgconn.cursor() as cur:
                        _bulk_upsert(cur, batch)
                    pgconn.commit()
                    upserted += len(batch)
                    batch.clear()
                    if upserted % 5000 == 0:
                        logger.info("    %s: %d upserted", col, upserted)

            # Flush tail
            if batch:
                with pgconn.cursor() as cur:
                    _bulk_upsert(cur, batch)
                pgconn.commit()
                upserted += len(batch)

            per_collection_counts[col] = {
                "scrolled":         scrolled,
                "skipped_no_text":  skipped_no_text,
                "upserted":         upserted,
            }
            total_upserted += upserted
            logger.info("    %s done: scrolled=%d skipped=%d upserted=%d",
                        col, scrolled, skipped_no_text, upserted)

        logger.info("BACKFILL COMPLETE — %d passages across %d collections",
                    total_upserted, len(collections))

        # Manifest
        manifest = {
            "asset":            "TIER 0b — public_geo Qdrant → silver.document_passages backfill",
            "collections":      per_collection_counts,
            "total_upserted":   total_upserted,
            "chunk_kind":       CHUNK_KIND,
            "parser_used":      PARSER_USED,
            "captured_at":      datetime.now(timezone.utc).isoformat(),
        }
        with open("/tmp/public_geo_backfill_manifest.json", "w") as fh:
            json.dump(manifest, fh, indent=2)
        logger.info("wrote /tmp/public_geo_backfill_manifest.json")

    finally:
        pgconn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
