"""One-shot driver for embed_pending_passages — Qwen3 cutover 2026-06-04.

Runs `embed_pending_passages` from the canonical service module against
the default tenant workspace so the 8,331 silver rows whose
`embedding_id IS NULL` get upserted into the freshly-recreated
`georag_chunks` (1024-dim) collection.

Why not just trigger the Hatchet workflow?
  - Hatchet would route this through scheduler/queue; for a known one-shot
    cutover the direct service call is faster + log-visible in the
    container directly.

Usage (inside the fastapi container):
  python /app/scripts/_embed_silver_pending_cutover.py

This script is INTENTIONALLY ephemeral — it can be deleted once the
cutover lands; the Hatchet workflow is the durable path. Kept under
scripts/ so the operator-style flow is reproducible if rollback then
re-cutover is needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)-30s %(message)s",
)
log = logging.getLogger("cutover.embed_silver_pending")


async def main() -> int:
    from app.services.ingest.passage_embedder import embed_pending_passages

    workspace_id = os.environ.get(
        "CUTOVER_WORKSPACE_ID",
        "a0000000-0000-0000-0000-000000000001",
    )
    log.info("embed_pending start workspace_id=%s project_id=*", workspace_id)
    result = await embed_pending_passages(
        workspace_id=workspace_id,
        project_id=None,  # all projects under workspace
        batch_size=int(os.environ.get("CUTOVER_BATCH_SIZE", "32")),
    )
    log.info("embed_pending done result=%s", result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
