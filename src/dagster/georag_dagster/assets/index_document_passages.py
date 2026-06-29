"""Index layer asset — embed silver.document_passages into Qdrant `georag_chunks`.

ADR-0010 Session A. silver.document_passages is the canonical chunked-
content corpus going forward; this asset embeds it into Qdrant so the
agentic-retrieval graph + reranker chain can both read from a single
source of truth.

Mirrors the ``index_reports`` pattern for vector embedding:
  - Dense float[384] via BAAI/bge-small-en-v1.5 in the unnamed "" slot
  - Sparse SPLADE++ via encode_sparse_batch in the "text" slot

Key differences from index_reports:
  - Reads silver.document_passages (per-passage rows) instead of
    silver.reports.sections_text (JSONB sections per report). One DB
    row → one Qdrant point; no per-asset chunking inside this code.
  - Uses passage_id directly as the Qdrant point ID. No derivation
    via deterministic_point_id() — document_passages.passage_id is
    already a stable UUID we own end-to-end.
  - Payload carries the citation-precision fields §04i requires:
    page_first, page_last, bbox_*, parser_confidence, ocr_confidence,
    ocr_method, ocr_status, chunk_kind, parent_chunk_id, text_hash.
    Plus the full chunk text (no truncation snippet — the whole point
    of switching to chunks is downstream tools get the real content).
  - Asset materialises the FULL table in one run with batched upserts
    rather than per-report. Backfill is one materialise; incremental
    re-runs are idempotent via passage_id-keyed upsert.

Re-runs are idempotent: same passage_id → same Qdrant point, upsert
overwrites in place. The text_hash on the row provides the
content-fingerprint that lets downstream eval detect "we re-embedded
the same content".

NOTE: Do NOT add ``from __future__ import annotations`` here. Dagster
1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

import os
from datetime import datetime, timezone
from typing import Any

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.sparse_encoder import (
    SPARSE_MODEL_VERSION,
    encode_sparse_batch,
)
from georag_dagster.resources import PostgresResource, QdrantResource


# ---------------------------------------------------------------------------
# Constants — keep in lockstep with index_reports.py so the vector space is
# shared across the legacy georag_reports collection and the new
# georag_chunks collection (callers can reuse the same query embedding).
# ---------------------------------------------------------------------------

QDRANT_COLLECTION = "georag_chunks"

EMBED_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "Qwen/Qwen3-Embedding-0.6B")
# Audit 2026-06-27 (C1): dimension MUST track the live georag_chunks collection
# (1024-dim, Qwen3-Embedding-0.6B, swapped 2026-06-03). Read from env so this
# stays in lockstep with the FastAPI runtime writer; default 1024 (NOT 384/bge —
# the canonical chat corpus was migrated off bge-small). The parity guard in
# _ensure_collection refuses to embed into / recreate the collection at a
# mismatched dimension so a stale value can never silently break retrieval.
EMBED_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
EMBED_BATCH_SIZE = 32
UPSERT_BATCH_SIZE = 100

# Payload snippet — index_reports.py truncates to 500 chars to keep
# payload size bounded; ADR-0010 commits to storing the full text
# because the chunks are bounded (average 1,644 chars, max 23,721
# observed on live data). The on_disk_payload=True flag on the
# collection means Qdrant pages payload to disk so the RAM footprint
# stays manageable. Setting None here = store the whole text field.
PAYLOAD_TEXT_LIMIT: int | None = None


# Payload fields the search_documents tool filters on. Keyword-typed
# fields need explicit payload indices or Qdrant falls back to scanning
# every row.
_PAYLOAD_KEYWORD_INDICES = [
    "workspace_id",     # GI-9 multi-tenant isolation (mandatory on every filter)
    "document_id",      # FK to silver.reports — used by per-document scoped queries
    "chunk_kind",       # narrow to narrative / table / section / paragraph
    "ocr_status",       # quality-filter to drop pending_reocr / low_confidence
    "ocr_method",       # filter by extraction provenance
    "parent_chunk_id",  # §3d parent expansion lookups
    "document_type",    # NI43 / PUB / PGEO — carries over from index_reports payload
]

_PAYLOAD_INTEGER_INDICES = [
    "page_first",       # narrow by page range
    "page_last",
    "ordinal",          # sort within a document
    "revision_number",  # filter to current revision
]


# ---------------------------------------------------------------------------
# Module-level model cache — same pattern as index_reports
# ---------------------------------------------------------------------------

_MODEL = None


def _get_model():
    """Return the cached SentenceTransformer, loading it on first call."""
    global _MODEL  # noqa: PLW0603
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        _MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    return _MODEL


# ---------------------------------------------------------------------------
# SQL — fetch document_passages rows + the document_type label we want on
# every Qdrant payload. document_type lives in silver.reports.document_class
# (or fallback to the same constant 'NI43' used by index_reports for legacy
# rows without a class).
# ---------------------------------------------------------------------------

# Selecting only the columns the Qdrant payload + embedding need. Joining
# silver.reports is optional — if a passage has no matching report row
# (orphan FK, shouldn't happen but RLS or partial-delete could produce it)
# the document_type just defaults to NULL and the payload carries None.
SELECT_PASSAGES_SQL = """
SELECT
    p.passage_id::text          AS passage_id,
    p.document_id::text         AS document_id,
    p.workspace_id::text        AS workspace_id,
    p.revision_number           AS revision_number,
    p.text                      AS text,
    p.text_hash                 AS text_hash,
    p.ordinal                   AS ordinal,
    p.chunk_kind                AS chunk_kind,
    p.page_first                AS page_first,
    p.page_last                 AS page_last,
    p.bbox_x0                   AS bbox_x0,
    p.bbox_y0                   AS bbox_y0,
    p.bbox_x1                   AS bbox_x1,
    p.bbox_y1                   AS bbox_y1,
    p.parser_confidence         AS parser_confidence,
    p.ocr_confidence            AS ocr_confidence,
    p.ocr_method                AS ocr_method,
    p.ocr_status                AS ocr_status,
    p.parent_chunk_id::text     AS parent_chunk_id,
    COALESCE(r.commodity, '')      AS commodity,
    COALESCE(r.project_name,'')    AS project_name,
    COALESCE(r.title, '')          AS document_title,
    -- Plan §1c — report_type is set by the document classifier
    -- (NI43 / textbook_oer / etc). Defaults to 'NI43' on rows
    -- pre-classifier so the legacy chat citation surface keeps
    -- rendering the same as before.
    COALESCE(r.report_type, 'NI43') AS document_type,
    -- Plan §6c — OER attribution columns. NULL on the dominant
    -- NI 43-101 path (workspace owns the content, no licence to
    -- surface). Non-NULL for textbook / open-government / PD
    -- content where the chat citation renderer needs to embed
    -- the licence + attribution text.
    r.authors                       AS authors,
    r.license                       AS license,
    r.license_url                   AS license_url,
    r.attribution_text              AS attribution_text,
    r.source_url                    AS source_url
FROM silver.document_passages p
LEFT JOIN silver.reports r ON r.report_id = p.document_id
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class IndexDocumentPassagesConfig(Config):
    """Asset config. Defaults to a full backfill; operators can scope by
    workspace_id during the rollout or for tenant-specific re-indexing."""

    workspace_id: str | None = None
    """Optional workspace_id filter. None = scan every workspace."""

    document_id: str | None = None
    """Optional document_id filter. Useful for re-indexing one report
    after a re-chunk. None = scan every document."""

    skip_ocr_pending: bool = True
    """When True, exclude passages with ocr_status='pending_reocr'. The
    text on those rows is from a low-confidence first-pass OCR and
    won't survive the reocr cycle; indexing them now would just churn
    Qdrant when reocr_complete lands."""


# ---------------------------------------------------------------------------
# Collection management — drop-and-recreate when schema is wrong
# ---------------------------------------------------------------------------

def _ensure_collection(client: Any, context: AssetExecutionContext) -> None:
    """Create or migrate georag_chunks to the ADR-0010 schema.

    Behaviour:
      • Missing → create with dense "" + sparse "text" slots
      • Exists with WRONG schema (missing sparse, or 0 points) → drop +
        recreate. Safe because the only acceptable state is the ADR-0010
        shape; existing 0-point collections from prior experiments are
        not worth preserving.
      • Exists with correct schema → patch optimizer config + ensure
        payload indices

    Payload indices ensured every run (Qdrant create_payload_index is
    idempotent — re-create is a no-op).
    """
    from qdrant_client.models import (  # noqa: PLC0415
        Distance,
        HnswConfigDiff,
        OptimizersConfigDiff,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
        SparseIndexParams,
        SparseVectorParams,
        VectorParams,
    )

    existing = {c.name for c in client.get_collections().collections}
    needs_create = QDRANT_COLLECTION not in existing

    if not needs_create:
        info = client.get_collection(QDRANT_COLLECTION)
        # Dimension-parity guard (audit 2026-06-27 C1). Never embed into — or
        # recreate — a collection whose dense vector size disagrees with the
        # configured embedding dimension. This is the backstop against the
        # 384-vs-1024 regression: a stale EMBED_DIMENSIONS (or a Dagster
        # container missing EMBEDDING_DIMENSION env) would otherwise either
        # 400 every upsert or silently recreate the collection at the wrong
        # size and break live retrieval.
        _vparams = info.config.params.vectors
        _existing_dim = (
            _vparams[""].size if isinstance(_vparams, dict) else getattr(_vparams, "size", None)
        )
        if _existing_dim is not None and _existing_dim != EMBED_DIMENSIONS:
            raise RuntimeError(
                f"{QDRANT_COLLECTION} dense dim={_existing_dim} but configured "
                f"EMBED_DIMENSIONS={EMBED_DIMENSIONS} (model {EMBED_MODEL_NAME}). "
                "Refusing to embed/recreate at a mismatched dimension. Set the "
                "EMBEDDING_DIMENSION / EMBEDDING_MODEL_NAME env on the Dagster "
                "worker to match the live collection, or migrate deliberately."
            )
        # Detect wrong-schema state: no sparse vectors configured, OR
        # zero points (= empty / never used).
        has_sparse = bool(info.config.params.sparse_vectors)
        is_empty = (info.points_count or 0) == 0
        if not has_sparse and is_empty:
            context.log.warning(
                "index_document_passages: existing '%s' collection has "
                "wrong schema (no sparse slot) and is empty — dropping "
                "+ recreating per ADR-0010",
                QDRANT_COLLECTION,
            )
            client.delete_collection(collection_name=QDRANT_COLLECTION)
            needs_create = True
        elif not has_sparse:
            # Non-empty but wrong schema — caller mistake. Surface a
            # loud warning rather than silently dropping data.
            context.log.error(
                "index_document_passages: '%s' exists with %d points "
                "but the schema is missing the sparse 'text' slot. "
                "Refusing to drop a non-empty collection automatically. "
                "Either clear the collection manually, OR rename the "
                "ADR-0010 target collection.",
                QDRANT_COLLECTION, info.points_count,
            )
            raise RuntimeError(
                f"{QDRANT_COLLECTION} schema mismatch; cannot safely migrate"
            )

    if needs_create:
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
                "text": SparseVectorParams(
                    index=SparseIndexParams(on_disk=False),
                )
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
        context.log.info(
            "index_document_passages: created '%s' with dense+sparse slots",
            QDRANT_COLLECTION,
        )
    else:
        client.update_collection(
            collection_name=QDRANT_COLLECTION,
            optimizers_config=OptimizersConfigDiff(
                indexing_threshold=5000,
                default_segment_number=2,
            ),
        )
        context.log.info(
            "index_document_passages: patched '%s' optimizer config",
            QDRANT_COLLECTION,
        )

    # Payload indices — idempotent creates.
    for field in _PAYLOAD_KEYWORD_INDICES:
        try:
            client.create_payload_index(
                collection_name=QDRANT_COLLECTION,
                field_name=field,
                field_schema="keyword",
            )
        except Exception as exc:  # noqa: BLE001
            # Qdrant returns a non-error if the index already exists, but
            # some versions raise. Log + continue.
            context.log.debug(
                "index_document_passages: payload index %s (keyword) — %s",
                field, exc,
            )
    for field in _PAYLOAD_INTEGER_INDICES:
        try:
            client.create_payload_index(
                collection_name=QDRANT_COLLECTION,
                field_name=field,
                field_schema="integer",
            )
        except Exception as exc:  # noqa: BLE001
            context.log.debug(
                "index_document_passages: payload index %s (integer) — %s",
                field, exc,
            )
    context.log.info(
        "index_document_passages: payload indices ensured — "
        "keywords=%s integers=%s",
        _PAYLOAD_KEYWORD_INDICES, _PAYLOAD_INTEGER_INDICES,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _embed_in_batches(
    texts: list[str],
    context: AssetExecutionContext,
) -> list:
    """Encode texts in EMBED_BATCH_SIZE batches with progress logging."""
    model = _get_model()
    embeddings: list = []
    total = len(texts)
    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[start:start + EMBED_BATCH_SIZE]
        batch_embeddings = model.encode(batch, batch_size=EMBED_BATCH_SIZE)
        embeddings.extend(batch_embeddings)
        done = min(start + EMBED_BATCH_SIZE, total)
        if done % (EMBED_BATCH_SIZE * 10) == 0 or done == total:
            context.log.info(
                "index_document_passages: embedded %d / %d chunks", done, total,
            )
    return embeddings


def _upsert_in_batches(
    client: Any,
    points: list,
    context: AssetExecutionContext,
) -> None:
    """Upsert Qdrant points in UPSERT_BATCH_SIZE batches with progress logging."""
    total = len(points)
    for start in range(0, total, UPSERT_BATCH_SIZE):
        batch = points[start:start + UPSERT_BATCH_SIZE]
        client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        done = min(start + UPSERT_BATCH_SIZE, total)
        if done % (UPSERT_BATCH_SIZE * 5) == 0 or done == total:
            context.log.info(
                "index_document_passages: upserted %d / %d points",
                done, total,
            )


def _build_payload(row: dict, payload_text: str) -> dict:
    """Construct the Qdrant payload dict for one passage row.

    Filters out None values where Qdrant would otherwise store a null
    that complicates downstream filtering. Keys with semantic None
    (e.g. parent_chunk_id IS NULL for root chunks) are preserved as
    None because the search_documents tool checks for IS NULL on them.
    """
    return {
        "passage_id":        row["passage_id"],
        "document_id":       row["document_id"],
        # ADR-0010 cross-collection compat alias — FastAPI search_documents +
        # response_assembler + cleanup_qdrant + nightly_ingestion_integrity all
        # read `report_id` from the legacy georag_reports payload. Carry the
        # same UUID under both keys so the hard flag flip needs zero downstream
        # mapping changes. silver.document_passages.document_id IS the FK to
        # silver.reports.report_id — same value, different key name.
        "report_id":         row["document_id"],
        "workspace_id":      row["workspace_id"],
        "revision_number":   row["revision_number"],
        "chunk_kind":        row.get("chunk_kind"),
        "page_first":        row.get("page_first"),
        "page_last":         row.get("page_last"),
        # ADR-0010 compat alias — the legacy georag_reports payload uses
        # `page` (single integer). DocumentChunk + Citation models read it
        # under that key. Mirror page_first → page so the same downstream
        # code path serves both collections without branching.
        "page":              row.get("page_first"),
        "bbox_x0":           float(row["bbox_x0"]) if row.get("bbox_x0") is not None else None,
        "bbox_y0":           float(row["bbox_y0"]) if row.get("bbox_y0") is not None else None,
        "bbox_x1":           float(row["bbox_x1"]) if row.get("bbox_x1") is not None else None,
        "bbox_y1":           float(row["bbox_y1"]) if row.get("bbox_y1") is not None else None,
        "parser_confidence": float(row["parser_confidence"]) if row.get("parser_confidence") is not None else None,
        "ocr_confidence":    float(row["ocr_confidence"]) if row.get("ocr_confidence") is not None else None,
        "ocr_method":        row.get("ocr_method"),
        "ocr_status":        row.get("ocr_status"),
        "parent_chunk_id":   row.get("parent_chunk_id"),
        "text_hash":         row["text_hash"],
        "ordinal":           row["ordinal"],
        "text":              payload_text,
        # Carryover provenance from silver.reports (matches the
        # index_reports payload shape so downstream code reading both
        # collections doesn't need a dispatch on collection name).
        "commodity":         row.get("commodity") or None,
        "project_name":      row.get("project_name") or None,
        "document_title":    row.get("document_title") or None,
        # Plan §1c — document_type is the classifier-stamped value from
        # silver.reports.report_type (was hardcoded 'NI43' before
        # 2026-05-28). Used by §3b authority ranking + by the chat
        # citation surface to pick the right rendering template.
        "document_type":     row.get("document_type") or "NI43",
        # Plan §6c — OER attribution. Surfaced into the Qdrant payload
        # so the chat citation renderer can embed licence + attribution
        # without a round-trip to silver.reports. NULL for the
        # dominant NI 43-101 path (workspace owns the content).
        "authors":           list(row["authors"]) if row.get("authors") else None,
        "license":           row.get("license"),
        "license_url":       row.get("license_url"),
        "attribution_text":  row.get("attribution_text"),
        "source_url":        row.get("source_url"),
        "indexed_at":        datetime.now(timezone.utc).isoformat(),
        "embed_model":       EMBED_MODEL_NAME,
        "parser_version":    SPARSE_MODEL_VERSION,
    }


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="index",
    description=(
        "ADR-0010 Session A — embed silver.document_passages into Qdrant "
        "georag_chunks. Dense (bge-small-en-v1.5, 384 dim, cosine) + "
        "sparse (SPLADE++). One row → one Qdrant point keyed by "
        "passage_id. Idempotent re-runs overwrite in place."
    ),
    compute_kind="qdrant",
)
def index_document_passages(
    context: AssetExecutionContext,
    config: IndexDocumentPassagesConfig,
    postgres: PostgresResource,
    qdrant: QdrantResource,
) -> MaterializeResult:
    """Scan silver.document_passages → embed → upsert into georag_chunks."""
    sql = SELECT_PASSAGES_SQL
    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if config.workspace_id:
        where_clauses.append("p.workspace_id = %(ws)s::uuid")
        params["ws"] = config.workspace_id
    if config.document_id:
        where_clauses.append("p.document_id = %(doc)s::uuid")
        params["doc"] = config.document_id
    if config.skip_ocr_pending:
        where_clauses.append(
            "(p.ocr_status IS NULL OR p.ocr_status != 'pending_reocr')"
        )
    if where_clauses:
        sql += "\nWHERE " + " AND ".join(where_clauses)
    sql += "\nORDER BY p.document_id, p.ordinal"

    context.log.info(
        "index_document_passages: scanning silver.document_passages "
        "(workspace_filter=%s document_filter=%s skip_pending_ocr=%s)",
        config.workspace_id or "all",
        config.document_id or "all",
        config.skip_ocr_pending,
    )

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        context.log.warning(
            "index_document_passages: no rows matched the filter — "
            "nothing to index"
        )
        return MaterializeResult(
            metadata={
                "passages_indexed":   MetadataValue.int(0),
                "model_name":         MetadataValue.text(EMBED_MODEL_NAME),
                "collection_name":    MetadataValue.text(QDRANT_COLLECTION),
            }
        )

    context.log.info(
        "index_document_passages: %d passages to embed across %d documents",
        len(rows), len({r["document_id"] for r in rows}),
    )

    # --- Ensure the Qdrant collection is in the right shape ---
    qdrant_client = qdrant.get_client()
    _ensure_collection(qdrant_client, context)

    # --- Embed (dense + sparse) ---
    texts = [r["text"] or "" for r in rows]
    context.log.info(
        "index_document_passages: encoding %d chunks (dense %s)...",
        len(texts), EMBED_MODEL_NAME,
    )
    dense_embeddings = _embed_in_batches(texts, context)

    context.log.info(
        "index_document_passages: encoding %d chunks (sparse SPLADE++ %s)...",
        len(texts), SPARSE_MODEL_VERSION,
    )
    sparse_vectors = encode_sparse_batch(texts, batch_size=16)
    avg_terms = sum(len(sv) for sv in sparse_vectors) / max(len(sparse_vectors), 1)
    context.log.info(
        "index_document_passages: sparse encoding done — avg non-zero terms=%.0f",
        avg_terms,
    )

    # --- Build PointStructs ---
    from qdrant_client.models import PointStruct, SparseVector  # noqa: PLC0415

    points: list[PointStruct] = []
    skipped_empty = 0
    for row, embedding, sparse_vec, payload_text in zip(
        rows, dense_embeddings, sparse_vectors, texts,
    ):
        if not payload_text.strip():
            skipped_empty += 1
            continue

        vector_payload: dict = {"": embedding.tolist()}
        if sparse_vec:
            sorted_indices = sorted(sparse_vec.keys())
            sorted_values = [sparse_vec[i] for i in sorted_indices]
            vector_payload["text"] = SparseVector(
                indices=sorted_indices,
                values=sorted_values,
            )

        # Payload text — full content per ADR-0010 (no PAYLOAD_TEXT_SNIPPET
        # truncation like index_reports.py uses). Storage is bounded by the
        # row text length, capped by on_disk_payload=True in the collection
        # config.
        display_text = (
            payload_text if PAYLOAD_TEXT_LIMIT is None
            else payload_text[:PAYLOAD_TEXT_LIMIT]
        )
        points.append(
            PointStruct(
                id=row["passage_id"],
                vector=vector_payload,
                payload=_build_payload(row, display_text),
            )
        )

    if skipped_empty:
        context.log.warning(
            "index_document_passages: skipped %d row(s) with empty text — "
            "they're in document_passages but have no embeddable content",
            skipped_empty,
        )

    # --- Upsert ---
    context.log.info(
        "index_document_passages: upserting %d points into '%s'",
        len(points), QDRANT_COLLECTION,
    )
    _upsert_in_batches(qdrant_client, points, context)
    context.log.info("index_document_passages: upsert complete")

    # --- Final state summary ---
    final_info = qdrant_client.get_collection(QDRANT_COLLECTION)
    return MaterializeResult(
        metadata={
            "passages_scanned":        MetadataValue.int(len(rows)),
            "passages_indexed":        MetadataValue.int(len(points)),
            "passages_skipped_empty":  MetadataValue.int(skipped_empty),
            "documents_touched":       MetadataValue.int(len({r["document_id"] for r in rows})),
            "collection_total_points": MetadataValue.int(final_info.points_count or 0),
            "model_name":              MetadataValue.text(EMBED_MODEL_NAME),
            "sparse_model_version":    MetadataValue.text(SPARSE_MODEL_VERSION),
            "collection_name":         MetadataValue.text(QDRANT_COLLECTION),
            "avg_sparse_terms":        MetadataValue.float(round(avg_terms, 1)),
            "workspace_filter":        MetadataValue.text(
                config.workspace_id or "(all workspaces)",
            ),
            "document_filter":         MetadataValue.text(
                config.document_id or "(all documents)",
            ),
        },
    )
