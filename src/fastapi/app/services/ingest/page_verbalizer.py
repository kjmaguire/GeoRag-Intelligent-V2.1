"""Fill in page-image descriptions via a Foundry vision model (2026-08-18).

Runs as a sweep, not inside ingest, for the same three reasons the embedding
sweep does:

  - A vision call is slow (seconds per page). Putting it on the ingest critical
    path would add minutes to a document that is otherwise finished, and Kyle's
    scope=all choice means every page, not just figures.
  - Retries come free. A page whose call failed keeps `verbalized_at` NULL and
    is picked up on the next pass; nothing needs to remember it failed.
  - It doubles as the backfill. Every image passage written before
    verbalization existed is, by definition, already in the queue.

Ordering relative to embedding
------------------------------
Deliberately independent. An image passage's DENSE VECTOR comes from the page
image, not from its text, so rewriting the text does NOT invalidate the vector
and no re-embed is needed. What the rewrite does invalidate is the `text` in
the Qdrant payload — which the reranker and the answer path read — so this
sweep patches that payload in place via set_payload. Resetting embedding_id to
force a full re-embed would burn an Embed v4 image call per page to produce a
byte-identical vector.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field

import asyncpg
from qdrant_client import AsyncQdrantClient

from app.db import bind_workspace_scope
from app.services.qdrant_conn import qdrant_client_kwargs

log = logging.getLogger("georag.ingest.page_verbalizer")

_QDRANT_COLLECTION = "georag_chunks"


@dataclass
class VerbalizationSweepResult:
    workspace_id: str
    seen: int = 0
    described: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


async def verbalize_pending_pages(
    pg_conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str | None = None,
    max_pages: int | None = None,
) -> VerbalizationSweepResult:
    """Describe image passages that have no description yet.

    Returns counts rather than raising: a vision outage should show up as
    `failed > 0` in the sweep log, not as a failed cron.
    """
    from app.services.ingest import page_vision_client as vision

    result = VerbalizationSweepResult(workspace_id=workspace_id)

    if not vision.is_enabled():
        return result
    if not vision.is_configured():
        # Loud, once per sweep — a flag that is on with no key would otherwise
        # look identical to "nothing to do".
        log.error(
            "page_verbalizer: %s is on but %s/%s are not set — no pages "
            "will be described",
            vision.ENABLED_ENV, vision.ENDPOINT_ENV, vision.KEY_ENV,
        )
        return result

    await bind_workspace_scope(
        pg_conn, workspace_id=workspace_id, site="page_verbalizer", is_local=False,
    )

    if max_pages is None:
        max_pages = int(os.environ.get("IMAGE_VERBALIZATION_BATCH", "200"))

    query = (
        "SELECT dp.passage_id::text AS passage_id, "
        "       dp.document_id::text AS document_id, "
        "       dp.page_number, dp.image_object_key, dp.embedding_id "
        "  FROM silver.document_passages dp "
        "  LEFT JOIN silver.reports r ON r.report_id = dp.document_id "
        " WHERE dp.modality = 'image' "
        "   AND dp.verbalized_at IS NULL "
        "   AND dp.image_object_key IS NOT NULL "
    )
    params: list = []
    if project_id:
        query += " AND r.project_id = $1::uuid "
        params.append(project_id)
    query += f" ORDER BY dp.created_at ASC LIMIT {int(max_pages)}"

    rows = await pg_conn.fetch(query, *params)
    result.seen = len(rows)
    if not rows:
        return result

    from georag_object_storage import Bucket, get_storage_client

    storage = get_storage_client()
    qdrant: AsyncQdrantClient | None = None

    for row in rows:
        key = row["image_object_key"]
        try:
            png = await asyncio.to_thread(storage.get_bytes, Bucket.BRONZE_RASTER, key)
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            log.warning(
                "page_verbalizer: cannot fetch %s for passage %s: %s",
                key, row["passage_id"], exc,
            )
            continue

        # Sync httpx call — off the event loop so a slow VLM cannot stall the
        # worker's heartbeats (the 2026-06-27 T4 rule).
        outcome = await asyncio.to_thread(vision.verbalize_page, png)
        if not outcome.ok:
            result.failed += 1
            if outcome.error and outcome.error not in result.errors:
                result.errors.append(outcome.error)
            # verbalized_at stays NULL → retried next sweep.
            continue

        text = outcome.text
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        try:
            await pg_conn.execute(
                "UPDATE silver.document_passages "
                "   SET text = $1, text_hash = $2, verbalized_at = NOW(), "
                "       updated_at = NOW() "
                " WHERE passage_id = $3::uuid",
                text, text_hash, row["passage_id"],
            )
        except asyncpg.UniqueViolationError:
            # Two pages of one document described identically — plausible for
            # blank or boilerplate pages, where the model returns the same
            # one-liner and UNIQUE (document_id, revision_number, text_hash)
            # rejects the second. Disambiguate with the page number rather
            # than dropping the description.
            text = f"{text}\n\n(Page {row['page_number']}.)"
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            await pg_conn.execute(
                "UPDATE silver.document_passages "
                "   SET text = $1, text_hash = $2, verbalized_at = NOW(), "
                "       updated_at = NOW() "
                " WHERE passage_id = $3::uuid",
                text, text_hash, row["passage_id"],
            )
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            log.warning(
                "page_verbalizer: PG update failed for passage %s: %s",
                row["passage_id"], exc,
            )
            continue

        # Patch the Qdrant payload so retrieval sees the description rather
        # than the placeholder. Only meaningful once the point exists; an
        # unembedded row will pick the new text up when the embed sweep
        # reaches it.
        if row["embedding_id"]:
            try:
                if qdrant is None:
                    qdrant = AsyncQdrantClient(**qdrant_client_kwargs())
                await qdrant.set_payload(
                    collection_name=_QDRANT_COLLECTION,
                    payload={"text": text},
                    points=[str(row["embedding_id"])],
                    wait=False,
                )
            except Exception as exc:  # noqa: BLE001
                # PG is the source of truth and already has the description;
                # a stale payload is a retrieval-quality wart, not data loss.
                # Deliberately NOT counted as failed — verbalized_at is set,
                # so this page will not be re-described on the next sweep.
                log.warning(
                    "page_verbalizer: Qdrant payload patch failed for point %s: %s",
                    row["embedding_id"], exc,
                )

        result.described += 1

    log.info(
        "page_verbalizer.sweep workspace=%s seen=%d described=%d failed=%d",
        workspace_id, result.seen, result.described, result.failed,
    )
    return result
