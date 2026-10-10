"""§11.3 wave 2 — Qdrant / Redis restore from a workspace_export manifest.

Companion to ``_export_extras.py``. Reads the v2.0 manifest produced
by ``workspace_export.run_export`` and applies each store's section
back to its target.

Idempotency notes:
  - Qdrant: points are UPSERTed by id (Qdrant's native semantics).
  - Redis: SET with EX matching the exported TTL.

Both helpers stream from a fetched .jsonl.gz body (passed in
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

from app.hatchet_workflows._export_extras import _QDRANT_COLLECTION
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
    # Line by line off the gzip stream: the old form inflated the archive to
    # one string, split it into a list of lines, and only then parsed them --
    # the whole export held three times over before the first row was read.
    manifest: dict[str, Any] | None = None
    pg_tables: dict[str, list[dict[str, Any]]] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        for raw in gz:
            if not raw.strip():
                continue
            obj = json.loads(raw)
            if manifest is None:
                manifest = obj
            elif "table" in obj:
                pg_tables.setdefault(obj["table"], []).append(obj["row"])
            elif "section" in obj:
                sections.setdefault(obj["section"], []).append(obj["row"])

    if manifest is None:
        raise ValueError("export body is empty")
    return manifest, pg_tables, sections


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
def _vector_from_json(vector: Any) -> Any:
    """Inverse of ``_export_extras._vector_to_json``.

    A named-vector point comes back as ``{"": [floats], "text": {"indices":
    [...], "values": [...]}}``; the sparse entry has to be a ``SparseVector``
    again for the upsert to accept it.
    """
    if not isinstance(vector, dict):
        return vector
    from qdrant_client.models import SparseVector  # noqa: PLC0415

    return {
        name: (
            SparseVector(indices=v["indices"], values=v["values"])
            if isinstance(v, dict) else v
        )
        for name, v in vector.items()
    }


async def restore_qdrant(
    workspace_id: str,
    points: list[dict[str, Any]],
    collection_name: str = _QDRANT_COLLECTION,
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
                        id=p["id"], vector=_vector_from_json(p["vector"]),
                        payload=payload,
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
    "restore_qdrant",
    "restore_redis",
]
