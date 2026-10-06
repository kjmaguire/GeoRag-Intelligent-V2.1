"""§11.3 wave 2 — Qdrant / Redis workspace export helpers.

These extend `workspace_export.run_export` past Postgres so the
exported manifest can recreate a workspace's full footprint on a
target cluster.

Each helper is best-effort: an export failure (e.g. driver missing
in the worker pool, store unreachable) returns an empty list +
records the reason in the per-store stats dict. The PG export
path still completes — operators see the partial coverage in the
manifest's `partial_stores` field.

Two exports, two patterns (Neo4j was removed from the stack on 2026-07-28;
there is no graph section):

  - Qdrant: scroll API with workspace_id payload filter, paginate
    until empty. Vectors + payload exported.
  - Redis: SCAN for `georag:ws:<uuid>:*` keys, then bulk-GET. Cache-
    only (we never restore Redis as authoritative).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from app.services.qdrant_conn import qdrant_client_kwargs

log = logging.getLogger("georag.hatchet.workspace_export.extras")


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
#: Points per scroll page. Each carries a 1024-dim vector (~4 KB as floats,
#: more as Python objects), so a page is a few MB.
_QDRANT_SCROLL_PAGE = 200

#: The collection ingest writes to (passage_embedder). It used to default to
#: the legacy ``georag_reports``, which no writer has touched since ADR-0010
#: and which does not exist on a fresh deploy, so every export either carried
#: zero points or recorded a qdrant failure in ``partial_stores``.
_QDRANT_COLLECTION = "georag_chunks"


def _vector_to_json(vector: Any) -> Any:
    """A Qdrant point's vector(s) as JSON-safe data.

    ``georag_chunks`` points carry NAMED vectors: the dense one under the
    empty name and a SPLADE++ ``SparseVector`` under ``"text"``. The old
    ``list(p.vector)`` turned that mapping into its key names
    (``["", "text"]``) and, for a sparse value, could not be serialised at
    all.
    """
    if vector is None:
        return None
    if isinstance(vector, dict):
        return {
            name: (
                {"indices": list(v.indices), "values": list(v.values)}
                if hasattr(v, "indices") and hasattr(v, "values")
                else list(v)
            )
            for name, v in vector.items()
        }
    return list(vector)


async def stream_qdrant_workspace(
    workspace_id: str,
    emit: Callable[[dict[str, Any]], None],
    collection_name: str = _QDRANT_COLLECTION,
) -> tuple[int, str | None]:
    """Scroll one workspace's Qdrant points page by page into ``emit``.

    ``emit`` receives each point dict -- ``{id, vector: [float], payload:
    {...}}`` -- as its page arrives and nothing is retained here, so memory
    is one page rather than every vector in the workspace.

    Returns ``(points_emitted, error)``. On failure ``error`` is the reason
    and ``points_emitted`` is however many had been emitted before it; the
    CALLER owns what to do with that partial output (``run_export`` discards
    it, which is what the list-returning version always did).
    """
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue
    except ImportError:
        return 0, "qdrant client not available"

    emitted = 0
    try:
        client = AsyncQdrantClient(**qdrant_client_kwargs())
        try:
            scroll_filter = Filter(must=[
                FieldCondition(
                    key="workspace_id",
                    match=MatchValue(value=workspace_id),
                )
            ])
            next_page: Any | None = None
            while True:
                batch, next_page = await client.scroll(
                    collection_name=collection_name,
                    scroll_filter=scroll_filter,
                    with_vectors=True, with_payload=True,
                    limit=_QDRANT_SCROLL_PAGE, offset=next_page,
                )
                if not batch:
                    break
                for p in batch:
                    emit({
                        "id":      p.id if isinstance(p.id, (int, str)) else str(p.id),
                        "vector":  _vector_to_json(p.vector),
                        "payload": dict(p.payload or {}),
                    })
                    emitted += 1
                if next_page is None:
                    break
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001
        # Collection-missing is a common case (a fresh workspace with no
        # reports). Treat it as 0 points, not an error.
        msg = f"{type(exc).__name__}: {exc}"
        if "not found" in msg.lower() or "doesn't exist" in msg.lower():
            return 0, None
        return emitted, f"qdrant_export_failed: {msg}"

    return emitted, None


async def export_qdrant_workspace(
    workspace_id: str,
    collection_name: str = _QDRANT_COLLECTION,
) -> tuple[list[dict[str, Any]], str | None]:
    """Export Qdrant points (id + vector + payload) for one workspace.

    Returns ``(points, error)``. Each point dict:
        ``{id, vector: [float], payload: {...}}``

    Holds every point in memory; ``run_export`` uses ``stream_qdrant_workspace``
    instead. Kept for callers that want the list, with the old contract: on
    error the points are discarded and ``([], reason)`` comes back.
    """
    points: list[dict[str, Any]] = []
    _, error = await stream_qdrant_workspace(workspace_id, points.append, collection_name)
    if error:
        return [], error
    return points, None


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
async def export_redis_workspace(
    workspace_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Export Redis keys scoped to one workspace (cache only).

    Returns ``(keys, error)``. Each key dict:
        ``{key: str, value_b64: str, ttl_s: int | None, type: str}``

    Restored as best-effort cache priming — we never treat Redis as
    authoritative state.
    """
    try:
        import base64

        import redis.asyncio as redis_asyncio
    except ImportError:
        return [], "redis client not available"

    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD")
    if not password:
        return [], "REDIS_PASSWORD not set"

    keys_out: list[dict[str, Any]] = []
    pattern = f"georag:ws:{workspace_id}:*"
    try:
        client = redis_asyncio.Redis(
            host=host, port=port, password=password, decode_responses=False,
        )
        try:
            async for raw_key in client.scan_iter(match=pattern, count=500):
                key_str = raw_key.decode() if isinstance(raw_key, (bytes, bytearray)) else str(raw_key)
                ktype_raw = await client.type(raw_key)
                ktype = ktype_raw.decode() if isinstance(ktype_raw, (bytes, bytearray)) else str(ktype_raw)
                ttl = await client.ttl(raw_key)
                # Wave 2 ships only string-typed keys (the most common
                # cache shape). Hash/list/set restoration is wave 3.
                if ktype != "string":
                    continue
                raw_val = await client.get(raw_key)
                if raw_val is None:
                    continue
                keys_out.append({
                    "key":       key_str,
                    "type":      ktype,
                    "ttl_s":     int(ttl) if ttl and ttl > 0 else None,
                    "value_b64": base64.b64encode(raw_val).decode("ascii"),
                })
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        return [], f"redis_export_failed: {type(exc).__name__}: {exc}"

    return keys_out, None


__all__ = [
    "export_qdrant_workspace",
    "stream_qdrant_workspace",
    "export_redis_workspace",
]
