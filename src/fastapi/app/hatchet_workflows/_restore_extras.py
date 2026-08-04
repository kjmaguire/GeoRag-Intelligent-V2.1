"""§11.3 wave 2 — Neo4j / Qdrant / Redis restore from a workspace_export manifest.

Companion to ``_export_extras.py``. Reads the v2.0 manifest produced
by ``workspace_export.run_export`` and applies each store's section
back to its target.

Idempotency notes:
  - Neo4j: nodes are MERGEd on a derived natural key (workspace_id +
    `id` property if present, else neo4j_id from the source). Relationships
    are MERGEd on (source, target, type).
  - Qdrant: points are UPSERTed by id (Qdrant's native semantics).
  - Redis: SET with EX matching the exported TTL.

All three helpers stream from a fetched .jsonl.gz body (passed in
already-decoded — the caller is responsible for the S3 GET).
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import os
from typing import Any

from app.services.qdrant_conn import qdrant_client_kwargs

log = logging.getLogger("georag.hatchet.restore_workspace.extras")


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------
def parse_export_jsonl_gz(body: bytes) -> tuple[
    dict[str, Any],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    """Decode the jsonl.gz body emitted by workspace_export.

    Returns ``(manifest, pg_tables, sections)`` where:
      - ``manifest`` is the first line as dict
      - ``pg_tables`` is ``{table: [row, ...]}`` — only the PG-typed
        lines (which carry a ``"table"`` key)
      - ``sections`` is ``{section: [row, ...]}`` — the §11.3-v2 extra
        store lines (which carry a ``"section"`` key)
    """
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        text = gz.read().decode("utf-8")

    lines = [l for l in text.split("\n") if l.strip()]  # noqa: E741
    if not lines:
        raise ValueError("export body is empty")

    manifest = json.loads(lines[0])
    pg_tables: dict[str, list[dict[str, Any]]] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    for raw in lines[1:]:
        obj = json.loads(raw)
        if "table" in obj:
            pg_tables.setdefault(obj["table"], []).append(obj["row"])
        elif "section" in obj:
            sections.setdefault(obj["section"], []).append(obj["row"])

    return manifest, pg_tables, sections


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------
async def restore_neo4j(
    workspace_id: str,
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
) -> dict[str, Any]:
    """MERGE nodes + relationships back into Neo4j.

    Strategy: rebuild a per-export id → neo4j_id map as we MERGE nodes,
    then MERGE relationships keyed on the (source, target, type) tuple.

    Returns ``{nodes_merged: int, rels_merged: int, error: str | None}``.

    B1 (2026-07-28): Neo4j was removed from the stack. This helper stays
    in place for the restore workflow's call signature but now returns
    the same fail-open shape the try/except used to produce when the
    driver was unreachable, without the wasted connection attempt.
    """
    return {
        "nodes_merged": 0,
        "rels_merged": 0,
        "error": "neo4j was removed from the stack (B1, 2026-07-28)",
    }


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
async def restore_qdrant(
    workspace_id: str,
    points: list[dict[str, Any]],
    collection_name: str = "georag_reports",
) -> dict[str, Any]:
    """Upsert points back into Qdrant by their original id.

    Returns ``{points_upserted: int, error: str | None}``.
    """
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import PointStruct
    except ImportError:
        return {"points_upserted": 0, "error": "qdrant client missing"}


    upserted = 0
    try:
        client = AsyncQdrantClient(**qdrant_client_kwargs())
        try:
            # Batch in chunks of 100 to keep payloads reasonable.
            for i in range(0, len(points), 100):
                batch = points[i:i + 100]
                structs = []
                for p in batch:
                    if p.get("vector") is None:
                        continue
                    # Force workspace_id in payload (override if forged)
                    payload = dict(p.get("payload") or {})
                    payload["workspace_id"] = workspace_id
                    structs.append(PointStruct(
                        id=p["id"], vector=p["vector"], payload=payload,
                    ))
                if structs:
                    await client.upsert(
                        collection_name=collection_name,
                        points=structs, wait=True,
                    )
                    upserted += len(structs)
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001
        return {"points_upserted": upserted,
                "error": f"qdrant_restore_failed: {type(exc).__name__}: {exc}"}

    return {"points_upserted": upserted, "error": None}


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
async def restore_redis(
    workspace_id: str,
    keys: list[dict[str, Any]],
) -> dict[str, Any]:
    """SET each key back. TTLs are preserved when present in the manifest.

    Wave 2 ships string keys only; hash/list/set restore is wave 3.
    """
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return {"keys_restored": 0, "error": "redis client missing"}

    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD")
    if not password:
        return {"keys_restored": 0, "error": "REDIS_PASSWORD not set"}

    restored = 0
    try:
        client = redis_asyncio.Redis(
            host=host, port=port, password=password, decode_responses=False,
        )
        try:
            for k in keys:
                if k.get("type") != "string":
                    continue
                val = base64.b64decode(k.get("value_b64", ""))
                ttl = k.get("ttl_s")
                # Only restore keys that match this workspace's namespace.
                # Cross-workspace key pollution would be a data leak.
                expected_prefix = f"georag:ws:{workspace_id}:"
                if not k["key"].startswith(expected_prefix):
                    continue
                if ttl and ttl > 0:
                    await client.set(k["key"], val, ex=int(ttl))
                else:
                    await client.set(k["key"], val)
                restored += 1
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        return {"keys_restored": restored,
                "error": f"redis_restore_failed: {type(exc).__name__}: {exc}"}

    return {"keys_restored": restored, "error": None}


__all__ = [
    "parse_export_jsonl_gz",
    "restore_neo4j",
    "restore_qdrant",
    "restore_redis",
]
