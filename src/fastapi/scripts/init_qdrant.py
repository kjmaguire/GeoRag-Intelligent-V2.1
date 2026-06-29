"""
init_qdrant.py — Bootstrap Qdrant collections for the GeoRAG embedding pipeline.

Run once against a fresh Qdrant instance (or after a wipe):

    python src/fastapi/scripts/init_qdrant.py

The script is idempotent: it skips creation for any collection that already
exists so it is safe to re-run without destroying live data.

Collections
-----------
georag_chunks
    Canonical chunked-content corpus (ADR-0010). Hybrid dense + sparse:
      - named dense "" slot, size = EMBEDDING_DIMENSION (1024, Qwen3-Embedding-0.6B
        as of the 2026-06-03 swap) — read from env so a fresh bootstrap always
        matches the live runtime writer.
      - named sparse "text" slot (SPLADE++) — REQUIRED. The 2026-06-01 incident
        was caused by bootstrapping this collection WITHOUT the sparse slot, so
        every canonical hybrid writer 400'd. Do not remove it.

georag_reports
    Legacy NI 43-101 section embeddings — a SEPARATE 384-dim bge-small vector
    space that was NOT migrated to Qwen3. Same named dense "" + sparse "text"
    shape as georag_chunks but at 384 dim. Production retrieval reads
    georag_chunks (RETRIEVAL_USE_DOCUMENT_PASSAGES=true); georag_reports is
    retained for the legacy path.

Payload indices match the fields the runtime writer actually emits
(index_document_passages._build_payload / index_reports), so filtered vector
searches skip full collection scans. (Audit 2026-06-27 C1/IND-3: the previous
version hardcoded 384 for both collections, omitted the sparse slot, and indexed
document_type/source_id/chunk_index — fields no live point carries.)
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Honor QDRANT_BASE_URL (preferred), fall back to QDRANT_HOST/PORT for
# container deployments where Qdrant is at qdrant:6333.
QDRANT_BASE_URL = (
    os.environ.get("QDRANT_BASE_URL")
    or f"http://{os.environ.get('QDRANT_HOST', 'localhost')}:{os.environ.get('QDRANT_PORT', '6333')}"
)

# georag_chunks tracks the live runtime embedding dimension (Qwen3 = 1024).
# Read from env so a fresh bootstrap can never drift from the FastAPI writer.
CHUNKS_VECTOR_SIZE = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
# georag_reports is the frozen legacy bge-small 384 space (not swapped).
REPORTS_VECTOR_SIZE = 384
DISTANCE = "Cosine"

# HTTP timeouts (seconds).  Qdrant collection creation and index builds are
# fast locally, but allow headroom for slow Docker hosts.
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


# ---------------------------------------------------------------------------
# Collection + index definitions
# ---------------------------------------------------------------------------

@dataclass
class PayloadIndex:
    field_name: str
    # Qdrant field schema types: "keyword", "integer", "float", "geo", "text"
    field_schema: str


@dataclass
class CollectionSpec:
    name: str
    vector_size: int
    distance: str
    on_disk_payload: bool
    # Every GeoRAG hybrid collection uses a named dense "" slot + named sparse
    # "text" slot. Kept configurable only so a future dense-only collection can
    # opt out explicitly rather than by omission.
    sparse: bool = True
    payload_indices: list[PayloadIndex] = field(default_factory=list)


COLLECTIONS: list[CollectionSpec] = [
    CollectionSpec(
        name="georag_chunks",
        vector_size=CHUNKS_VECTOR_SIZE,
        distance=DISTANCE,
        # On-disk payload avoids RAM exhaustion when the collection grows to
        # millions of chunk records (drill hole assays, geological logs, etc.)
        on_disk_payload=True,
        payload_indices=[
            # workspace_id MUST be the first filter on every query (GI-9
            # multi-tenancy contract). project_id scopes within a tenant.
            # report_id + section_number match what _build_payload emits and
            # back citation lookups. (document_type/source_id/chunk_index were
            # removed: no live point carries them — audit IND-3.)
            PayloadIndex("workspace_id",   "keyword"),
            PayloadIndex("project_id",     "keyword"),
            PayloadIndex("report_id",      "keyword"),
            PayloadIndex("section_number", "keyword"),
        ],
    ),
    CollectionSpec(
        name="georag_reports",
        vector_size=REPORTS_VECTOR_SIZE,
        distance=DISTANCE,
        on_disk_payload=False,
        payload_indices=[
            PayloadIndex("workspace_id",   "keyword"),
            PayloadIndex("report_id",      "keyword"),
            PayloadIndex("section_number", "integer"),
            PayloadIndex("commodity",      "keyword"),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(response: httpx.Response, context: str) -> None:
    """Raise a descriptive RuntimeError if the response signals a failure."""
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"{context} — HTTP {response.status_code}: {response.text}"
        )


async def _collection_exists(client: httpx.AsyncClient, name: str) -> bool:
    """Return True if the named collection already exists in Qdrant."""
    resp = await client.get(f"/collections/{name}")
    return resp.status_code == 200


async def _create_collection(client: httpx.AsyncClient, spec: CollectionSpec) -> None:
    """Issue a PUT /collections/{name} request to create the collection.

    Uses the NAMED dense "" slot + named sparse "text" slot shape that the
    Dagster index assets and the FastAPI hybrid_query path both rely on.
    """
    body: dict = {
        "vectors": {
            "": {
                "size": spec.vector_size,
                "distance": spec.distance,
            },
        },
        "on_disk_payload": spec.on_disk_payload,
    }
    if spec.sparse:
        body["sparse_vectors"] = {"text": {}}
    resp = await client.put(f"/collections/{spec.name}", json=body)
    _ok(resp, f"Create collection '{spec.name}'")


async def _create_payload_index(
    client: httpx.AsyncClient,
    collection_name: str,
    index: PayloadIndex,
) -> None:
    """Issue a PUT /collections/{name}/index request to create a payload index."""
    body = {
        "field_name": index.field_name,
        "field_schema": index.field_schema,
    }
    resp = await client.put(f"/collections/{collection_name}/index", json=body)
    _ok(resp, f"Index '{index.field_name}' on '{collection_name}'")


# ---------------------------------------------------------------------------
# Main bootstrap routine
# ---------------------------------------------------------------------------

async def bootstrap() -> None:
    """Create all collections and their payload indices."""
    print(f"Connecting to Qdrant at {QDRANT_BASE_URL} …")
    print(
        f"  georag_chunks dense size = {CHUNKS_VECTOR_SIZE} "
        f"(from EMBEDDING_DIMENSION); georag_reports = {REPORTS_VECTOR_SIZE}"
    )

    async with httpx.AsyncClient(
        base_url=QDRANT_BASE_URL,
        timeout=TIMEOUT,
        headers={"Content-Type": "application/json"},
    ) as client:

        # Verify Qdrant is reachable before doing anything
        try:
            health = await client.get("/healthz")
            # Qdrant returns 200 on /healthz; older builds use /
            if health.status_code not in (200,):
                alt = await client.get("/")
                _ok(alt, "Qdrant health check")
        except httpx.ConnectError as exc:
            print(f"ERROR: Cannot reach Qdrant at {QDRANT_BASE_URL} — {exc}", file=sys.stderr)
            sys.exit(1)

        print("Qdrant is reachable.\n")

        for spec in COLLECTIONS:
            print(f"--- Collection: {spec.name} ---")

            if await _collection_exists(client, spec.name):
                print("  Already exists — skipping creation.")
            else:
                await _create_collection(client, spec)
                print(
                    f"  Created  (dense: size={spec.vector_size}, "
                    f"distance={spec.distance}, sparse={spec.sparse}, "
                    f"on_disk_payload={spec.on_disk_payload})"
                )

            for idx in spec.payload_indices:
                await _create_payload_index(client, spec.name, idx)
                print(f"  Index OK  '{idx.field_name}' ({idx.field_schema})")

            print()

        # ----------------------------------------------------------------
        # Final verification — list all collections and confirm both exist
        # ----------------------------------------------------------------
        print("Verifying collections …")
        resp = await client.get("/collections")
        _ok(resp, "List collections")

        data = resp.json()
        present = {c["name"] for c in data["result"]["collections"]}

        all_ok = True
        for spec in COLLECTIONS:
            status = "OK" if spec.name in present else "MISSING"
            if status == "MISSING":
                all_ok = False
            print(f"  {status}  {spec.name}")

        if not all_ok:
            print("\nERROR: one or more collections are missing after bootstrap.", file=sys.stderr)
            sys.exit(1)

        print("\nQdrant bootstrap complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(bootstrap())
