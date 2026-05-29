"""
init_qdrant.py — Bootstrap Qdrant collections for the GeoRAG embedding pipeline.

Run once against a fresh Qdrant instance (or after a wipe):

    python src/fastapi/scripts/init_qdrant.py

The script is idempotent: it skips creation for any collection that already
exists so it is safe to re-run without destroying live data.

Collections
-----------
georag_chunks
    Primary store for document chunk embeddings (drill reports, assay logs, etc.).
    Vector size 384 matches the all-MiniLM-L6-v2 placeholder model used for
    Milestone 1.  Milestone 2 benchmarking will determine the production model;
    if the vector size changes at that point this script must be re-run against
    a clean instance.

georag_reports
    NI 43-101 report section embeddings.  Same vector size and distance metric
    as georag_chunks.

Payload indices are created on the fields most likely to appear in filtered
vector searches so Qdrant can skip full collection scans.
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

# Vector space shared by both Milestone-1 collections.
# NOTE: all-MiniLM-L6-v2 is a placeholder; Milestone 2 benchmarking decides
# the production model.  If the model changes and the vector size differs,
# drop and recreate the affected collection.
VECTOR_SIZE = 384
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
    payload_indices: list[PayloadIndex] = field(default_factory=list)


COLLECTIONS: list[CollectionSpec] = [
    CollectionSpec(
        name="georag_chunks",
        vector_size=VECTOR_SIZE,
        distance=DISTANCE,
        # On-disk payload avoids RAM exhaustion when the collection grows to
        # millions of chunk records (drill hole assays, geological logs, etc.)
        on_disk_payload=True,
        payload_indices=[
            # Eval 15 follow-up (2026-05-20) — workspace_id MUST be the
            # first filter on every query (multi-tenancy contract). Without
            # this payload index, every workspace-scoped Prefetch ran a
            # full collection scan + post-filter, which got progressively
            # worse as the corpus grew. Adding the keyword index gives
            # Qdrant an O(log n) lookup per workspace.
            PayloadIndex("workspace_id", "keyword"),
            PayloadIndex("project_id",    "keyword"),
            PayloadIndex("document_type", "keyword"),
            PayloadIndex("source_id",     "keyword"),
            PayloadIndex("chunk_index",   "integer"),
        ],
    ),
    CollectionSpec(
        name="georag_reports",
        vector_size=VECTOR_SIZE,
        distance=DISTANCE,
        on_disk_payload=False,
        payload_indices=[
            # Eval 15 follow-up — same rationale as georag_chunks above.
            PayloadIndex("workspace_id",    "keyword"),
            PayloadIndex("report_id",       "keyword"),
            PayloadIndex("section_number",  "integer"),
            PayloadIndex("commodity",       "keyword"),
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
    """Issue a PUT /collections/{name} request to create the collection."""
    body = {
        "vectors": {
            "size": spec.vector_size,
            "distance": spec.distance,
        },
        "on_disk_payload": spec.on_disk_payload,
    }
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
                print(f"  Already exists — skipping creation.")
            else:
                await _create_collection(client, spec)
                print(
                    f"  Created  (vectors: size={spec.vector_size}, "
                    f"distance={spec.distance}, "
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
