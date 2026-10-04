"""§11.3 wave 2 — Neo4j / Qdrant / Redis workspace export helpers.

These extend `workspace_export.run_export` past Postgres so the
exported manifest can recreate a workspace's full footprint on a
target cluster.

Each helper is best-effort: an export failure (e.g. driver missing
in the worker pool, store unreachable) returns an empty list +
records the reason in the per-store stats dict. The PG export
path still completes — operators see the partial coverage in the
manifest's `partial_stores` field.

Three exports, three patterns:

  - Neo4j: cypher MATCH on workspace_id, return node props + labels +
    relationships. We export both node + relationship lists.
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
# Neo4j
# ---------------------------------------------------------------------------
async def export_neo4j_workspace(
    workspace_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Export Neo4j nodes + relationships scoped to one workspace.

    Returns ``(nodes, relationships, error)``. On any failure, returns
    ``([], [], reason)`` — the export workflow records the reason and
    continues with PG.

    Each node dict: ``{labels: [str], properties: {...}, neo4j_id: int}``
    Each rel dict: ``{type: str, start_neo4j_id: int, end_neo4j_id: int,
                       properties: {...}}``

    B1 (2026-07-28): Neo4j was removed from the stack. This helper stays
    in place for the export workflow's call signature but now returns
    the same ``([], [], reason)`` fail-open shape the try/except used to
    produce when the driver was unreachable, without the wasted
    connection attempt.
    """
    return [], [], "neo4j was removed from the stack (B1, 2026-07-28)"


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
#: Points per scroll page. Each carries a 1024-dim vector (~4 KB as floats,
#: more as Python objects), so a page is a few MB.
_QDRANT_SCROLL_PAGE = 200


async def stream_qdrant_workspace(
    workspace_id: str,
    emit: Callable[[dict[str, Any]], None],
    collection_name: str = "georag_reports",
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
                        "vector":  list(p.vector) if p.vector is not None else None,
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
    collection_name: str = "georag_reports",
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
    "export_neo4j_workspace",
    "export_qdrant_workspace",
    "stream_qdrant_workspace",
    "export_redis_workspace",
]
