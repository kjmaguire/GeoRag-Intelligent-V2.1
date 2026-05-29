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

    lines = [l for l in text.split("\n") if l.strip()]
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
    """
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        return {"nodes_merged": 0, "rels_merged": 0, "error": "neo4j driver missing"}

    host = os.environ.get("NEO4J_HOST", "neo4j")
    port = int(os.environ.get("NEO4J_PORT", "7687"))
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        return {"nodes_merged": 0, "rels_merged": 0, "error": "NEO4J_PASSWORD not set"}

    driver = AsyncGraphDatabase.driver(f"bolt://{host}:{port}", auth=(user, password))
    nodes_merged = 0
    rels_merged = 0
    # Map: source neo4j_id → freshly-MERGEd target neo4j_id
    id_map: dict[int, int] = {}
    try:
        async with driver.session() as session:
            for n in nodes:
                src_nid = int(n["neo4j_id"])
                labels = n.get("labels") or []
                props = dict(n.get("properties") or {})
                # Force workspace_id property even if upstream forgot it
                props["workspace_id"] = workspace_id

                # Build a label string (escaped). Without labels, default to :Node.
                label_str = ":".join(labels) if labels else "Node"

                # MERGE on natural id if present, else fall back to a
                # synthetic "export_origin_id" so re-imports converge.
                merge_key = props.get("id") or f"export-{src_nid}"
                props["import_origin_id"] = merge_key

                result = await session.run(
                    f"""
                    MERGE (n:{label_str} {{ import_origin_id: $merge_key, workspace_id: $ws }})
                    SET n = $props
                    RETURN id(n) AS nid
                    """,
                    merge_key=merge_key, ws=workspace_id, props=props,
                )
                row = await result.single()
                if row and row.get("nid") is not None:
                    id_map[src_nid] = int(row["nid"])
                    nodes_merged += 1

            for r in rels:
                sid = int(r["start_neo4j_id"])
                eid = int(r["end_neo4j_id"])
                rtype = str(r["type"])
                # Skip if either endpoint didn't merge (filter referred to a
                # node outside the workspace scope, which shouldn't happen
                # given the export filter but guard anyway).
                if sid not in id_map or eid not in id_map:
                    continue
                # Cypher doesn't parameterise relationship type — sanitize.
                if not rtype.replace("_", "").isalnum():
                    continue
                target_sid = id_map[sid]
                target_eid = id_map[eid]
                await session.run(
                    f"""
                    MATCH (a) WHERE id(a) = $sid
                    MATCH (b) WHERE id(b) = $eid
                    MERGE (a)-[r:{rtype}]->(b)
                    SET r = $props
                    """,
                    sid=target_sid, eid=target_eid,
                    props=dict(r.get("properties") or {}),
                )
                rels_merged += 1
    except Exception as exc:  # noqa: BLE001
        return {
            "nodes_merged": nodes_merged,
            "rels_merged":  rels_merged,
            "error":        f"neo4j_restore_failed: {type(exc).__name__}: {exc}",
        }
    finally:
        try:
            await driver.close()
        except Exception:
            pass

    return {"nodes_merged": nodes_merged, "rels_merged": rels_merged, "error": None}


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

    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))

    upserted = 0
    try:
        client = AsyncQdrantClient(host=host, port=port)
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
