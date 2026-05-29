#!/usr/bin/env python
"""TIER 0b follow-on — embed the 150,304 public_geo_synthesis passages
that the standard embed cron skips because they have no parent
silver.reports row (document_id IS NULL).

Patch landed in src/fastapi/app/services/ingest/passage_embedder.py
(LEFT JOIN to silver.reports) and the embed_pending_passages workflow
(orphan-pass step). This is the one-shot equivalent so we don't have
to restart the running hatchet-worker-ai mid-cycle.

Connects as the owner role `georag` because silver.workspaces' RLS
policy uses `chr(0)` as a sentinel that PG18 rejects under the
runtime `georag_app` role (see 2026-05-28 RLS chr(0) audit).

Usage
-----
    docker exec -e POSTGRES_OWNER_USER=georag \
                -e POSTGRES_OWNER_PASSWORD=... \
                georag-fastapi \
        python /app/scripts/_embed_public_geo_passages.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("embed_public_geo_passages")


def _patch_dsn():
    """Force passage_embedder to use the owner role."""
    owner_user = os.environ.get("POSTGRES_OWNER_USER", "georag")
    owner_pass = (
        os.environ.get("POSTGRES_OWNER_PASSWORD")
        or os.environ["POSTGRES_PASSWORD"]
    )
    os.environ["POSTGRES_USER"] = owner_user
    os.environ["POSTGRES_PASSWORD"] = owner_pass


async def main_async(args):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _patch_dsn()

    sys.path.insert(0, "/app")
    from app.services.ingest.passage_embedder import embed_pending_passages

    logger.info(
        "starting public-geo embed sweep workspace=%s max=%s",
        args.workspace_id, args.max,
    )

    result = await embed_pending_passages(
        workspace_id=args.workspace_id,
        project_id=None,            # cross-project / orphan pass
        batch_size=args.batch_size,
        max_passages=args.max,
    )

    logger.info(
        "DONE  seen=%d embedded=%d upserted=%d skipped=%d errors=%d",
        result.passages_seen, result.passages_embedded,
        result.qdrant_points_upserted, result.passages_skipped,
        len(result.errors),
    )
    if result.errors:
        for e in result.errors[:10]:
            logger.warning("  err: %s", e)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace-id",
                   default="a0000000-0000-0000-0000-000000000001")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max", type=int, default=None,
                   help="Optional cap (smoke test). None = no limit.")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
