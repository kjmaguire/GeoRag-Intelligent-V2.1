"""Index layer asset -- embed NI 43-101 report sections into Qdrant.

Reads a report row from silver.reports, builds a chunk text string for each
section, generates 384-dim DENSE embeddings (BAAI/bge-small-en-v1.5) AND
SPARSE embeddings (SPLADE++), upserts multi-vector points into the
`georag_reports` Qdrant collection, and writes the resulting point IDs back
to silver.reports.embedding_ids.

Module 4 Chunk 2 update: doc-side sparse write
-----------------------------------------------
Each Qdrant point now carries two vector slots:
  - "" (default unnamed): dense float[384] from bge-small-en-v1.5
  - "text": sparse SparseVector from SPLADE++ (token_id -> weight dict)

Every payload includes:
  - workspace_id: workspace UUID for multi-tenant isolation (GI-9).
    Default workspace for legacy data: a0000000-0000-0000-0000-000000000001.
    For new ingestion paths, read from the project row in silver.projects.
  - parser_version: SPLADE model version tag for cache invalidation.

Collection contract (must already exist -- provisioned externally):
  - 384 dimensions, cosine distance (default "" vector)
  - sparse "text" vector slot for SPLADE++ weights
  - Payload indices: report_id (keyword), section_number (integer),
                     commodity (keyword), workspace_id (keyword)

document_type is set to "NI43" on every payload so the RAG retriever can
apply citation typing (Section 04i requirement).

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.silver_reports import silver_reports
from georag_dagster.assets.sparse_encoder import (
    SPARSE_MODEL_VERSION,
    encode_sparse_batch,
)
from georag_dagster.resources import S3Resource, PostgresResource, QdrantResource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

import os

QDRANT_COLLECTION = "georag_reports"

# Default workspace UUID for legacy data (pre-Module 9 ingestion paths).
# For new ingestion, the workspace_id is read from silver.projects.workspace_id.
DEFAULT_WORKSPACE_UUID = "a0000000-0000-0000-0000-000000000001"
# Read from env so Dagster uses the same model as FastAPI query-time embedding.
# Prevents vector space collision when new documents are indexed after a model upgrade.
EMBED_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5")
EMBED_DIMENSIONS = 384
EMBED_BATCH_SIZE = 32
UPSERT_BATCH_SIZE = 100
MAX_CHUNK_CHARS = 1500       # primary chunk size
CHUNK_OVERLAP_CHARS = 200    # overlap between consecutive chunks for context continuity
PAYLOAD_TEXT_SNIPPET = 500   # chars stored in Qdrant payload for display

# ---------------------------------------------------------------------------
# Module-level model cache — mirrors the pattern from index_qdrant.py
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
# SQL
# ---------------------------------------------------------------------------

# Fetch a report row by partial title match or exact report_id.
# Partial title match is case-insensitive ILIKE so the config value does not
# need to be an exact string. The caller should provide enough characters to
# avoid collisions. report_id is preferred when available (set config to the
# UUID string and leave report_title blank).
FETCH_REPORT_SQL = """
SELECT
    report_id,
    title,
    company,
    commodity,
    project_name,
    region,
    sections_text
FROM silver.reports
WHERE
    CASE
        WHEN %(report_id)s IS NOT NULL AND %(report_id)s != ''
            THEN report_id::text = %(report_id)s
        ELSE title ILIKE %(title_pattern)s
    END
ORDER BY title
LIMIT 1
;
"""

UPDATE_EMBEDDING_IDS_SQL = """
UPDATE silver.reports
   SET embedding_ids = %(embedding_ids)s::text[],
       updated_at    = NOW()
WHERE report_id = %(report_id)s
;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class IndexReportsConfig(Config):
    """Runtime configuration for the index_reports asset."""

    # Human-readable title (or partial title) to look up the report row.
    # Used when report_id is blank.
    report_title: str = ""

    # Exact UUID of the report row. When provided this is preferred over
    # report_title for the lookup.
    report_id: str = ""

    # P0 #1 — Qdrant project-scope payload stamp.
    # UUID of the project the report belongs to. Embedded into the Qdrant
    # point payload as ``project_id`` so the FastAPI ``search_documents`` tool
    # can scope retrieval to the requesting project when
    # ``QDRANT_DOCUMENT_PROJECT_SCOPE`` is set to ``project_or_public`` or
    # ``strict``. Set to ``"public"`` (the default) for NI 43-101 filings
    # that should be visible to any project in the deployment.
    project_id: str = "public"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deterministic_point_id(report_id: str, section_key: str) -> str:
    """Return a deterministic UUID string for a (report_id, section_key) pair.

    Uses SHA-256 truncated to 128 bits so the same section always maps to the
    same Qdrant point. This makes re-runs idempotent at the vector level — the
    upsert simply overwrites the existing point rather than creating a duplicate.
    """
    digest = hashlib.sha256(f"{report_id}::{section_key}".encode()).hexdigest()
    # Format as a valid UUID: 8-4-4-4-12 hex chars
    return str(uuid.UUID(digest[:32]))


def _build_chunk_texts(
    title: str,
    section_number: str,
    section_title: str,
    section_body: str,
) -> list[str]:
    """Build chunk strings with sliding-window overlap for embedding.

    Long sections are split into MAX_CHUNK_CHARS windows with
    CHUNK_OVERLAP_CHARS overlap so that no context boundary falls
    mid-sentence without the adjacent chunk preserving continuity.

    Returns a list of 1+ chunk strings. Short sections return a single chunk.
    """
    prefix = f"{title}, Section {section_number}: {section_title}. "
    body = section_body.strip()
    available = MAX_CHUNK_CHARS - len(prefix)

    if available <= 0 or not body:
        return [prefix]

    if len(body) <= available:
        return [prefix + body]

    # Sliding window with overlap
    chunks = []
    step = max(available - CHUNK_OVERLAP_CHARS, 100)
    start = 0
    while start < len(body):
        window = body[start : start + available]
        chunks.append(prefix + window)
        start += step

    return chunks


def _build_chunk_text(
    title: str,
    section_number: str,
    section_title: str,
    section_body: str,
) -> str:
    """Build a single chunk string (backward-compat wrapper).

    For callers that expect a single string. Returns the first chunk
    from the sliding-window chunker.
    """
    prefix = f"{title}, Section {section_number}: {section_title}. "
    body = section_body.strip()
    available = MAX_CHUNK_CHARS - len(prefix)
    if available > 0:
        body = body[:available]
    return prefix + body


def _embed_in_batches(
    texts: list[str],
    context: AssetExecutionContext,
) -> list:
    """Encode texts in EMBED_BATCH_SIZE batches, logging progress."""
    model = _get_model()
    embeddings = []
    total = len(texts)

    for start in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        batch_embeddings = model.encode(batch, batch_size=EMBED_BATCH_SIZE)
        embeddings.extend(batch_embeddings)
        done = min(start + EMBED_BATCH_SIZE, total)
        context.log.info("index_reports: embedded %d / %d chunks", done, total)

    return embeddings


def _upsert_in_batches(
    client: Any,
    points: list,
    context: AssetExecutionContext,
) -> None:
    """Upsert Qdrant points in UPSERT_BATCH_SIZE batches, logging progress."""
    total = len(points)
    for start in range(0, total, UPSERT_BATCH_SIZE):
        batch = points[start : start + UPSERT_BATCH_SIZE]
        client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        done = min(start + UPSERT_BATCH_SIZE, total)
        context.log.info("index_reports: upserted %d / %d points into Qdrant", done, total)


# Payload fields we filter on in `search_documents`. Any search that
# filters on these must have a payload index or Qdrant falls back to
# scanning every row. Missing indexes on these keys is the Qdrant
# equivalent of forgetting a B-tree on a frequently-WHERE'd PG column.
_PAYLOAD_KEYWORD_INDICES = [
    "workspace_id",     # GI-9 multi-tenant isolation (mandatory on every filter)
    "project_id",       # QDRANT_DOCUMENT_PROJECT_SCOPE filter (P0 #1)
    "report_id",        # UNIQUE per-report dedupe
    "commodity",        # narrow-searches by commodity focus
    "document_type",    # narrow to NI43 vs PUB vs PGEO
]
_PAYLOAD_INTEGER_INDICES = [
    "section_number",   # sort/filter by section
]


def _ensure_collection(client: Any, context: AssetExecutionContext) -> None:
    """Create or patch the georag_reports collection + its payload indices.

    Module 4 Chunk 2: collection now requires named vectors:
      - "" (empty string): dense bge-small-en-v1.5 embeddings, 384 dim
      - "text": sparse SPLADE++ SparseVector

    If the collection does not exist, it is created with both slots.
    If the collection exists, the optimizer config is patched (safe).
    HNSW config is immutable after creation; m=32 is set at creation.

    Payload indices ensured:
      - workspace_id (keyword) -- GI-9 multi-tenant isolation
      - project_id (keyword)   -- project-scope filter
      - report_id (keyword)    -- per-report dedupe
      - commodity (keyword)    -- commodity filter
      - document_type (keyword)-- NI43 vs PUB vs PGEO
      - section_number (integer)
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
    if QDRANT_COLLECTION not in existing:
        # Create with named dense "" + sparse "text" slots.
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
        context.log.info("index_reports: created '%s' collection with dense+sparse slots", QDRANT_COLLECTION)
    else:
        client.update_collection(
            collection_name=QDRANT_COLLECTION,
            optimizers_config=OptimizersConfigDiff(
                indexing_threshold=5000,
                default_segment_number=2,
            ),
        )
        context.log.info(
            "index_reports: patched '%s' indexing_threshold + segment_number",
            QDRANT_COLLECTION,
        )

    for field in _PAYLOAD_KEYWORD_INDICES:
        client.create_payload_index(
            collection_name=QDRANT_COLLECTION,
            field_name=field,
            field_schema="keyword",
        )
    for field in _PAYLOAD_INTEGER_INDICES:
        client.create_payload_index(
            collection_name=QDRANT_COLLECTION,
            field_name=field,
            field_schema="integer",
        )
    context.log.info(
        "index_reports: payload indices ensured -- keywords=%s integers=%s",
        _PAYLOAD_KEYWORD_INDICES, _PAYLOAD_INTEGER_INDICES,
    )


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="index",
    deps=[silver_reports],
    description=(
        "Embed NI 43-101 report sections into Qdrant georag_reports collection. "
        "Uses all-MiniLM-L6-v2 (384 dim, cosine). Updates silver.reports.embedding_ids "
        "with the resulting point UUIDs for citation provenance tracking."
    ),
)
def index_reports(
    context: AssetExecutionContext,
    config: IndexReportsConfig,
    postgres: PostgresResource,
    qdrant: QdrantResource,
    minio: "S3Resource" = None,  # type: ignore[assignment]
) -> MaterializeResult:
    """Query silver.reports → build section chunks → embed → upsert into Qdrant."""

    report_id_filter = config.report_id.strip() if config.report_id else None
    title_filter = f"%{config.report_title.strip()}%" if config.report_title else "%"

    context.log.info(
        "index_reports: looking up report — report_id='%s', title_pattern='%s'",
        report_id_filter or "(none)",
        title_filter,
    )

    # --- Fetch report row from silver ---
    row: dict | None = None
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                FETCH_REPORT_SQL,
                {
                    "report_id":     report_id_filter or "",
                    "title_pattern": title_filter,
                },
            )
            result = cur.fetchone()
            if result:
                row = dict(result)

    if row is None:
        context.log.warning(
            "index_reports: no report found for report_id='%s' / title_pattern='%s' "
            "— Qdrant index unchanged.",
            report_id_filter,
            title_filter,
        )
        return MaterializeResult(
            metadata={
                "embedded_chunks": MetadataValue.int(0),
                "model_name":      MetadataValue.text(EMBED_MODEL_NAME),
                "collection_name": MetadataValue.text(QDRANT_COLLECTION),
            }
        )

    report_id_str = str(row["report_id"])
    title = row.get("title") or ""
    commodity = row.get("commodity") or ""
    project_name = row.get("project_name") or ""

    # sections_text is a JSONB column; psycopg2 RealDictCursor deserialises it
    # to a Python dict automatically.
    sections_text: dict = row.get("sections_text") or {}

    context.log.info(
        "index_reports: found report_id='%s', title='%s', %d sections",
        report_id_str,
        title[:60],
        len(sections_text),
    )

    if not sections_text:
        context.log.warning(
            "index_reports: sections_text is empty for report_id='%s' — nothing to embed.",
            report_id_str,
        )
        return MaterializeResult(
            metadata={
                "report_id":       MetadataValue.text(report_id_str),
                "embedded_chunks": MetadataValue.int(0),
                "model_name":      MetadataValue.text(EMBED_MODEL_NAME),
                "collection_name": MetadataValue.text(QDRANT_COLLECTION),
            }
        )

    # --- Build chunk texts ---
    # sections_text keys are section numbers ("1", "2", ...) or named keys
    # like "preamble" / "document" for unnumbered leading text.
    chunk_texts: list[str] = []
    section_keys: list[str] = []

    for section_key, section_body in sections_text.items():
        if not section_body or not section_body.strip():
            continue

        # Determine a human-readable section title for the chunk prefix.
        # The key itself is used when we don't have the title separately
        # (sections_text stores body only; title was not persisted to the dict).
        # The chunk format remains informative even without a separate title.
        if section_key.isdigit():
            section_label = f"Section {section_key}"
        else:
            section_label = section_key.capitalize()

        chunk = _build_chunk_text(
            title=title,
            section_number=section_key,
            section_title=section_label,
            section_body=section_body,
        )
        chunk_texts.append(chunk)
        section_keys.append(section_key)

    context.log.info(
        "index_reports: built %d chunk texts from %d sections",
        len(chunk_texts),
        len(sections_text),
    )

    if not chunk_texts:
        context.log.warning(
            "index_reports: all sections were empty for report_id='%s'", report_id_str
        )
        return MaterializeResult(
            metadata={
                "report_id":       MetadataValue.text(report_id_str),
                "embedded_chunks": MetadataValue.int(0),
                "model_name":      MetadataValue.text(EMBED_MODEL_NAME),
                "collection_name": MetadataValue.text(QDRANT_COLLECTION),
            }
        )

    # --- Generate dense embeddings ---
    context.log.info(
        "index_reports: embedding %d chunks (dense) with %s...", len(chunk_texts), EMBED_MODEL_NAME
    )
    embeddings = _embed_in_batches(chunk_texts, context)

    # --- Generate sparse embeddings (Module 4 Chunk 2) ---
    context.log.info(
        "index_reports: encoding %d chunks (sparse SPLADE++ %s)...",
        len(chunk_texts),
        SPARSE_MODEL_VERSION,
    )
    sparse_vectors = encode_sparse_batch(chunk_texts, batch_size=16)
    context.log.info(
        "index_reports: sparse encoding complete -- avg non-zero terms=%.0f",
        sum(len(sv) for sv in sparse_vectors) / max(len(sparse_vectors), 1),
    )

    # Resolve workspace_id: prefer the project row if available.
    # Fall back to the default workspace UUID for legacy/public data.
    resolved_workspace_id = DEFAULT_WORKSPACE_UUID
    if config.project_id and config.project_id != "public":
        # We don't have a DB connection here for async lookup -- the project
        # config carries workspace_id for new ingestion paths. For legacy
        # paths (project_id="public" or unresolved), use the default.
        # Module 9 will pass workspace_id directly through the config.
        pass

    # --- Build Qdrant PointStructs (multi-vector: dense "" + sparse "text") ---
    from qdrant_client.models import PointStruct, SparseVector  # noqa: PLC0415

    points: list[PointStruct] = []
    for section_key, chunk_text, embedding, sparse_vec in zip(
        section_keys, chunk_texts, embeddings, sparse_vectors
    ):
        point_id = _deterministic_point_id(report_id_str, section_key)

        # section_number payload field must be int when the key is numeric;
        # keep as string for named keys (Qdrant payload is schema-less).
        if section_key.isdigit():
            section_number_payload = int(section_key)
        else:
            section_number_payload = section_key  # type: ignore[assignment]

        # Build the multi-vector dict:
        #   "" (empty string key) -- dense float[384] for cosine ANN
        #   "text"                -- sparse SPLADE++ for BM25-style recall
        vector_payload: dict = {"": embedding.tolist()}
        if sparse_vec:
            # Only include sparse slot when SPLADE produced non-zero terms.
            # Empty sparse vectors cause Qdrant to error on upsert.
            sorted_indices = sorted(sparse_vec.keys())
            sorted_values = [sparse_vec[i] for i in sorted_indices]
            vector_payload["text"] = SparseVector(
                indices=sorted_indices,
                values=sorted_values,
            )

        points.append(
            PointStruct(
                id=point_id,
                vector=vector_payload,
                payload={
                    "report_id":       report_id_str,
                    "project_id":      config.project_id,  # P0 #1 scope filter
                    "workspace_id":    resolved_workspace_id,  # GI-9 isolation
                    "section_number":  section_number_payload,
                    "section_title":   (
                        f"Section {section_key}" if str(section_key).isdigit()
                        else str(section_key).capitalize()
                    ),
                    "text":            chunk_text[:PAYLOAD_TEXT_SNIPPET],
                    "commodity":       commodity,
                    "project_name":    project_name,
                    "document_type":   "NI43",
                    "document_title":  title,
                    "indexed_at":      datetime.now(timezone.utc).isoformat(),
                    "embed_model":     EMBED_MODEL_NAME,
                    "parser_version":  SPARSE_MODEL_VERSION,  # for cache invalidation
                },
            )
        )

    # --- Upsert into Qdrant ---
    qdrant_client = qdrant.get_client()

    # Qdrant review #1/#2 — idempotent collection-ensure + payload-index
    # check before every upsert. Safe re-runs heal drift (missing index
    # on project_id, stale optimizer config) without requiring a
    # separate bootstrap step.
    _ensure_collection(qdrant_client, context)

    context.log.info(
        "index_reports: upserting %d points into '%s'...", len(points), QDRANT_COLLECTION
    )
    _upsert_in_batches(qdrant_client, points, context)
    context.log.info("index_reports: Qdrant upsert complete")

    # --- Write point IDs back to silver.reports.embedding_ids ---
    point_id_strings = [p.id for p in points]

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                UPDATE_EMBEDDING_IDS_SQL,
                {
                    "embedding_ids": point_id_strings,
                    "report_id":     report_id_str,
                },
            )
        conn.commit()

    context.log.info(
        "index_reports: updated silver.reports.embedding_ids with %d point IDs for report_id='%s'",
        len(point_id_strings),
        report_id_str,
    )

    # --- Figure extraction (integrated, not manual) ---
    # If the source PDF is available in MinIO, extract and index figures
    # alongside text chunks. This replaces the standalone index_figures.py script.
    figure_count = 0
    try:
        import tempfile
        # Find the PDF in bronze bucket using boto3 paginator
        s3_client = minio.get_client()
        paginator = s3_client.get_paginator("list_objects_v2")
        pdf_key: str | None = None
        for page in paginator.paginate(Bucket="bronze", Prefix="reports/"):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".pdf"):
                    pdf_key = obj["Key"]
                    break
            if pdf_key is not None:
                break

        if pdf_key is not None:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                pdf_bytes = minio.download_bytes("bronze", pdf_key)
                tmp.write(pdf_bytes)
                tmp_path = tmp.name

            try:
                # Import the figure extractor from FastAPI (shared code)
                import sys
                sys.path.insert(0, "/app")
                from app.agent.figure_extractor import (
                    extract_figures_from_pdf,
                    generate_figure_descriptions,
                )

                figures = extract_figures_from_pdf(tmp_path)
                if figures:
                    figures = generate_figure_descriptions(figures, title)
                    # Embed figure descriptions -- dense + sparse
                    fig_texts = [f["description"] for f in figures]
                    fig_embeddings = _embed_in_batches(fig_texts, context)
                    fig_sparse_vecs = encode_sparse_batch(fig_texts, batch_size=16)
                    for fig, fig_emb, fig_sparse in zip(figures, fig_embeddings, fig_sparse_vecs):
                        fig_point_id = str(uuid.uuid4())
                        fig_vector_payload: dict = {"": fig_emb.tolist()}
                        if fig_sparse:
                            sorted_idx = sorted(fig_sparse.keys())
                            fig_vector_payload["text"] = SparseVector(
                                indices=sorted_idx,
                                values=[fig_sparse[i] for i in sorted_idx],
                            )
                        points.append(
                            PointStruct(
                                id=fig_point_id,
                                vector=fig_vector_payload,
                                payload={
                                    "report_id": report_id_str,
                                    "project_id": config.project_id,
                                    "workspace_id": resolved_workspace_id,
                                    "section_number": f"Figure (page {fig['page']})",
                                    "section_title": f"Extracted figure - page {fig['page']}",
                                    "text": fig["description"][:PAYLOAD_TEXT_SNIPPET],
                                    "commodity": commodity,
                                    "project_name": project_name,
                                    "document_type": "NI43",
                                    "document_title": title,
                                    "content_type": "figure_description",
                                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                                    "embed_model": EMBED_MODEL_NAME,
                                    "parser_version": SPARSE_MODEL_VERSION,
                                },
                            )
                        )
                        point_id_strings.append(fig_point_id)
                    figure_count = len(figures)
                    context.log.info("index_reports: extracted %d figures from PDF", figure_count)
            finally:
                import os
                os.unlink(tmp_path)
    except Exception as fig_exc:
        context.log.warning("index_reports: figure extraction failed (non-blocking): %s", fig_exc)

    return MaterializeResult(
        metadata={
            "report_id":        MetadataValue.text(report_id_str),
            "title":            MetadataValue.text(title[:80]),
            "embedded_chunks":  MetadataValue.int(len(points)),
            "figure_count":     MetadataValue.int(figure_count),
            "dense_model":      MetadataValue.text(EMBED_MODEL_NAME),
            "sparse_model":     MetadataValue.text(SPARSE_MODEL_VERSION),
            "collection_name":  MetadataValue.text(QDRANT_COLLECTION),
            "workspace_id":     MetadataValue.text(resolved_workspace_id),
            "embed_batch_size":  MetadataValue.int(EMBED_BATCH_SIZE),
            "upsert_batch_size": MetadataValue.int(UPSERT_BATCH_SIZE),
        }
    )
