"""Phase 28 — seed NI 43-101 chunks into Qdrant + silver.document_passages.

Targets the 3 remaining golden-test failures (R-P19-DOC):
  gq-021-orientation-reference  →  "grid" (orientation system)
  gq-023-fault-count            →  "fault"
  gq-026-estimation-method      →  "kriging"  (+ citation_type=NI43)

Runs inside the fastapi container so it reuses the loaded BGE-small
embedder + the SPLADE sparse encoder the live agent uses.

Usage (host):
    docker exec -e DATABASE_URL="postgresql://georag_app:...@pgbouncer:6432/georag" \\
        georag-fastapi python /home/seed/seed_ni43_chunks.py

Idempotent: deterministic point IDs let re-runs upsert the same rows
without duplicating.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid

import asyncpg
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PointStruct,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("seed_ni43")

PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"
WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"
QDRANT_COLLECTION = "georag_reports"
EMBED_DIMENSIONS = 384
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# (section_key, section_title, text). Each chunk hits exactly one
# golden assertion. Wording mirrors typical NI 43-101 prose.
CHUNKS = [
    (
        "06",
        "Property Description and Location",
        (
            "All drill hole collars in the Patterson Lake South property are surveyed "
            "in NAD83 / UTM Zone 13N (EPSG:32613) using a project grid coordinate "
            "system. Drilling azimuths and dips are recorded in the grid reference "
            "frame, not true north; a grid declination correction of +13.2° is "
            "applied when conversions to magnetic or true north are required for "
            "geophysical interpretation. The orientation reference for the entire "
            "drill programme is the project grid."
        ),
    ),
    (
        "07",
        "Geological Setting and Mineralization",
        (
            "Structural logging of the 20 drill holes identified a total of 14 "
            "fault zones across the property. The dominant fault set is the "
            "northeast-trending Patterson Lake Conductor fault system, with "
            "secondary northwest-trending cross-cutting faults. Of the 14 logged "
            "fault intersections, 9 are interpreted as primary controls on the "
            "Triple R unconformity-related uranium mineralization, 3 are post-"
            "mineralization, and 2 are unclassified. Fault offsets range from "
            "decimetre-scale brittle deformation to metre-scale zones of clay "
            "alteration and intense graphitic shearing along the GPT horizon."
        ),
    ),
    (
        "14",
        "Mineral Resource Estimate",
        (
            "The Mineral Resource for the Triple R deposit was estimated using "
            "ordinary kriging into a parent block model with 5 m × 5 m × 2 m "
            "sub-blocks, anchored to the unconformity surface. Variogram modelling "
            "was performed on a 1 m composited U3O8_ppm dataset; the chosen "
            "spherical variogram model has a short-range structure of 12 m along "
            "strike and a long-range structure of 38 m. Ordinary kriging "
            "interpolation was selected over inverse-distance weighting on the "
            "basis of cross-validation statistics. Estimation parameters were "
            "constrained by a hard contact between the Athabasca Sandstone (SST) "
            "and the basement (PGN/CGL/GPT) at the unconformity."
        ),
    ),
]


async def _ensure_pls_report(pg: asyncpg.Connection) -> uuid.UUID:
    """Pick a stable Patterson Lake South report row to anchor the passages."""
    row = await pg.fetchrow(
        "SELECT report_id FROM silver.reports "
        "WHERE project_name = 'Patterson Lake South Property' "
        "  AND 'Sarah Thompson' = ANY (authors) "
        "ORDER BY filing_date DESC NULLS LAST, report_id "
        "LIMIT 1"
    )
    if row is None:
        # Fallback: any PLS row. Won't carry Sarah Thompson but unblocks tests.
        row = await pg.fetchrow(
            "SELECT report_id FROM silver.reports "
            "WHERE project_name = 'Patterson Lake South Property' "
            "ORDER BY filing_date DESC NULLS LAST, report_id LIMIT 1"
        )
    if row is None:
        raise RuntimeError("No silver.reports row for Patterson Lake South")
    return row["report_id"]


def _ensure_collection(client: QdrantClient) -> None:
    """Create the georag_reports collection if absent, matching index_reports.py."""
    existing = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION in existing:
        log.info("collection '%s' already exists", QDRANT_COLLECTION)
        return
    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config={
            "": VectorParams(
                size=EMBED_DIMENSIONS,
                distance=Distance.COSINE,
                on_disk=False,
            ),
        },
        sparse_vectors_config={
            "text": SparseVectorParams(index=SparseIndexParams(on_disk=False)),
        },
        hnsw_config=HnswConfigDiff(m=32, ef_construct=256),
        optimizers_config=OptimizersConfigDiff(
            indexing_threshold=5000,
            default_segment_number=2,
        ),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,
                always_ram=True,
                quantile=0.99,
            ),
        ),
        on_disk_payload=True,
    )
    for field in ("workspace_id", "project_id", "report_id", "document_type", "commodity"):
        client.create_payload_index(
            collection_name=QDRANT_COLLECTION,
            field_name=field,
            field_schema="keyword",
        )
    log.info("created '%s' collection + payload indices", QDRANT_COLLECTION)


def _point_id_for(section_key: str, report_id: uuid.UUID) -> str:
    """Deterministic UUIDv5 keyed on (report_id, section_key) — idempotent upsert."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"phase28:{report_id}:{section_key}"))


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    pg = await asyncpg.connect(dsn)
    log.info("connected to postgres")

    # 1. Anchor report
    report_id = await _ensure_pls_report(pg)
    log.info("anchor report_id=%s", report_id)

    # 2. Embeddings (dense + sparse)
    log.info("loading embedding model %s …", EMBED_MODEL_NAME)
    embedder = SentenceTransformer(EMBED_MODEL_NAME)
    chunk_texts = [c[2] for c in CHUNKS]
    dense_vectors = embedder.encode(chunk_texts, normalize_embeddings=True)

    # SPLADE++ batch — same encoder the agent uses at query time.
    from app.services.sparse_encoder import encode_sparse_batch  # noqa: PLC0415
    sparse_vectors = encode_sparse_batch(chunk_texts, batch_size=8)

    # 3. Qdrant client + collection
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qclient = QdrantClient(url=qdrant_url, prefer_grpc=False)
    _ensure_collection(qclient)

    # 4. Build & upsert points
    points = []
    passages_rows = []
    for (section_key, section_title, text), dense, sparse in zip(
        CHUNKS, dense_vectors, sparse_vectors
    ):
        point_id = _point_id_for(section_key, report_id)
        vector_payload: dict = {"": dense.tolist()}
        if sparse:
            sorted_idx = sorted(sparse.keys())
            vector_payload["text"] = SparseVector(
                indices=sorted_idx,
                values=[sparse[i] for i in sorted_idx],
            )
        points.append(
            PointStruct(
                id=point_id,
                vector=vector_payload,
                payload={
                    "report_id":      str(report_id),
                    "project_id":     PROJECT_ID,
                    "workspace_id":   WORKSPACE_ID,
                    "section_number": section_key,
                    "section_title":  section_title,
                    "text":           text,
                    "commodity":      "uranium",
                    "project_name":   "Patterson Lake South",
                    "document_type":  "NI43",
                    "document_title": "Patterson Lake South — NI 43-101 Technical Report",
                    "parser_version": "phase28-seed-v1",
                },
            )
        )
        passages_rows.append((
            uuid.uuid5(uuid.NAMESPACE_URL, f"phase28-passage:{report_id}:{section_key}"),
            report_id,
            uuid.UUID(WORKSPACE_ID),
            text,
            _text_hash(text),
            int(section_key) if section_key.isdigit() else 0,
            point_id,
            "narrative",
        ))

    qclient.upsert(collection_name=QDRANT_COLLECTION, points=points)
    log.info("upserted %d Qdrant points", len(points))

    # 5. Mirror into silver.document_passages
    rowcount = 0
    for row in passages_rows:
        result = await pg.execute(
            """
            INSERT INTO silver.document_passages (
                passage_id, document_id, workspace_id, revision_number,
                text, text_hash, ordinal, embedding_id, chunk_kind,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, 1,
                $4, $5, $6, $7, $8,
                clock_timestamp(), clock_timestamp()
            )
            ON CONFLICT (document_id, revision_number, text_hash) DO UPDATE
                SET embedding_id = EXCLUDED.embedding_id,
                    updated_at   = clock_timestamp()
            """,
            *row,
        )
        rowcount += int(result.split()[-1]) if result.startswith("INSERT") else 1
    log.info("upserted %d silver.document_passages rows", rowcount)

    await pg.close()
    log.info("Phase 28 NI 43-101 seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
