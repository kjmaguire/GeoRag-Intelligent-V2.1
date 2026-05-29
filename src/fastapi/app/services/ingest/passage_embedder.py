"""silver.document_passages → Qdrant georag_chunks embedding sync.

Doc-phase 181 — Phase D. ADR-0010 cutover (2026-05-28): collection
renamed from ``georag_reports`` → ``georag_chunks`` to match the
canonical chunked-content corpus. The retire commit (e018694) dropped
the old collection from Qdrant but this writer kept the old name —
caught when the Earle textbook ingest produced 864 unembedded
passages with the runtime warning
``embed_pending.upsert_failed err=Unexpected Response: 404 ...
Collection georag_reports doesn't exist``.

For every passage row in `silver.document_passages` where
`embedding_id IS NULL`:

  1. Encode the text via BGE-small (dense, 384-dim, normalized)
  2. Encode via SPLADE++ (sparse, named "text")
  3. Upsert to Qdrant `georag_chunks` with payload:
       { report_id, project_id, workspace_id,
         section_number, section_title, text }
  4. Update `silver.document_passages.embedding_id` with the Qdrant point ID

Collection schema (existing):
  - vectors_config: {'': VectorParams(size=384, distance=Cosine)}
  - sparse_vectors: {'text': SparseVectorParams(...)}

Section fields:
  Passages from PDFs/XLSX don't carry true §15 section structure, so
  we use:
    section_number = ordinal (passage index within document)
    section_title  = parent report.title

This matches the orchestrator's payload-extraction logic in
`tools.search_documents`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass

import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, SparseVector

log = logging.getLogger("georag.ingest.passage_embedder")


_QDRANT_COLLECTION = "georag_chunks"


@dataclass
class EmbeddingSyncResult:
    workspace_id: str
    project_id: str | None
    passages_seen: int = 0
    passages_embedded: int = 0
    passages_skipped: int = 0
    qdrant_points_upserted: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _passage_to_point_id(passage_id: str) -> str:
    """Derive a deterministic UUID5-style point_id from passage_id.

    Qdrant requires UUID or unsigned int IDs; passage_id is already a
    UUID string in silver, so we use it directly. Wrapped in str() for
    uniformity.
    """
    return str(passage_id)


def _dsn() -> str:
    return (
        f"postgres://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:5432/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


async def embed_pending_passages(
    *,
    workspace_id: str,
    project_id: str | None = None,
    embedding_model=None,
    qdrant_client: AsyncQdrantClient | None = None,
    batch_size: int = 32,
    max_passages: int | None = None,
) -> EmbeddingSyncResult:
    """Walk un-embedded passages for a project and push to Qdrant.

    Args:
        workspace_id: silver.workspaces UUID for scoping
        project_id: silver.projects UUID. If None, embeds all passages
            in the workspace.
        embedding_model: SentenceTransformer instance. Loaded if None.
        qdrant_client: AsyncQdrantClient. Connected if None.
        batch_size: number of passages to encode in one BGE batch
        max_passages: cap for smoke tests; None = no limit

    Returns:
        EmbeddingSyncResult with per-stage counts.
    """
    result = EmbeddingSyncResult(workspace_id=workspace_id, project_id=project_id)

    # ── Load models / clients if not provided ─────────────────────
    if embedding_model is None:
        from sentence_transformers import SentenceTransformer
        from app.config import settings
        log.info("embed_pending.loading_embedding_model name=%s",
                 settings.EMBEDDING_MODEL_NAME)
        embedding_model = SentenceTransformer(
            settings.EMBEDDING_MODEL_NAME, device="cpu",
        )

    own_qdrant = False
    if qdrant_client is None:
        qdrant_client = AsyncQdrantClient(
            host=os.environ.get("QDRANT_HOST", "qdrant"),
            port=int(os.environ.get("QDRANT_PORT", "6333")),
        )
        own_qdrant = True

    # ── Load passage rows ─────────────────────────────────────────
    pg_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Set RLS GUCs. Wrapped in an explicit transaction so SET LOCAL
        # takes effect on subsequent queries within the same session.
        await pg_conn.execute(
            "SELECT set_config('georag.workspace_id', $1, false)", workspace_id,
        )
        await pg_conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        if project_id:
            await pg_conn.execute(
                "SELECT set_config('georag.project_id', $1, false)", project_id,
            )
        else:
            await pg_conn.execute("RESET georag.project_id")

        # LEFT JOIN to silver.reports so passages without a parent
        # document_id (e.g. chunk_kind='public_geo_synthesis' from the
        # TIER 0b Qdrant backfill, or kg_narrative / structured_summary
        # from ADR-0012 synthesizers) still get embedded. Title falls
        # back to the chunk_kind label; project_id stays NULL for
        # cross-project public-geoscience corpora.
        query = (
            "SELECT dp.passage_id::text AS passage_id, "
            "       dp.document_id::text AS document_id, "
            "       dp.text, dp.ordinal, dp.page_first, dp.page_last, "
            # Phase 3 (2026-05-22) — OCR provenance travels with the
            # qdrant point so retrieval can weight low-confidence
            # passages down without a Postgres join. NULL means the
            # passage came from the text layer (no OCR involved).
            "       dp.ocr_confidence, dp.ocr_method, "
            "       dp.chunk_kind, "
            "       COALESCE(r.title, dp.chunk_kind, 'Passage') AS report_title, "
            "       r.project_id::text AS project_id "
            "  FROM silver.document_passages dp "
            "  LEFT JOIN silver.reports r ON r.report_id = dp.document_id "
            " WHERE dp.embedding_id IS NULL "
        )
        params: list = []
        if project_id:
            # When a specific project is requested, we keep the original
            # INNER-JOIN semantics: only passages with a parent report
            # in that project. Public-geo passages have no project so
            # they fall outside this scope (intentionally).
            query += " AND r.project_id = $1::uuid "
            params.append(project_id)
        query += " ORDER BY dp.created_at ASC"
        if max_passages:
            query += f" LIMIT {int(max_passages)}"

        rows = await pg_conn.fetch(query, *params)
        result.passages_seen = len(rows)

        if not rows:
            log.info("embed_pending.no_pending_passages workspace=%s project=%s",
                     workspace_id, project_id)
            return result

        # ── Encode in batches ─────────────────────────────────────
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start:batch_start + batch_size]
            texts = [r["text"] for r in batch]

            # Dense encode (BGE-small, normalized)
            try:
                dense_vectors = embedding_model.encode(
                    texts, normalize_embeddings=True, show_progress_bar=False,
                ).tolist()
            except Exception as e:
                result.errors.append(f"dense_encode_failed:{type(e).__name__}:{e}")
                result.passages_skipped += len(batch)
                continue

            # Sparse encode (SPLADE++)
            from app.services.sparse_encoder import encode_sparse
            sparse_vectors = []
            for txt in texts:
                try:
                    sparse_vectors.append(encode_sparse(txt))
                except Exception as e:
                    log.warning("embed_pending.sparse_encode_failed err=%s", e)
                    sparse_vectors.append({})

            # Build Qdrant points
            points: list[PointStruct] = []
            for row, dv, sv in zip(batch, dense_vectors, sparse_vectors):
                point_id = _passage_to_point_id(row["passage_id"])
                vector_dict: dict = {"": dv}
                if sv:
                    vector_dict["text"] = SparseVector(
                        indices=list(sv.keys()),
                        values=list(sv.values()),
                    )
                # Phase 3 (2026-05-22) — surface ocr_confidence as a
                # plain float (not Decimal) since qdrant payload values
                # must be JSON-serializable. NULL stays None.
                _conf = row["ocr_confidence"]
                payload = {
                    "report_id": row["document_id"],
                    "project_id": row["project_id"],
                    "workspace_id": workspace_id,
                    "section_number": str(row["ordinal"]),
                    "section_title": row["report_title"] or "Passage",
                    "text": row["text"],
                    "page_first": row["page_first"],
                    "page_last": row["page_last"],
                    "ocr_confidence": float(_conf) if _conf is not None else None,
                    "ocr_method": row["ocr_method"],
                    # ADR-0010 §A discriminator — lets the orchestrator
                    # filter / score public_geo_synthesis differently
                    # from narrative report chunks.
                    "chunk_kind": row.get("chunk_kind") if isinstance(row, dict) else row["chunk_kind"],
                }
                points.append(PointStruct(
                    id=point_id, vector=vector_dict, payload=payload,
                ))

            # Upsert
            try:
                await qdrant_client.upsert(
                    collection_name=_QDRANT_COLLECTION,
                    points=points, wait=True,
                )
                result.qdrant_points_upserted += len(points)
            except Exception as e:
                result.errors.append(f"upsert_failed:{type(e).__name__}:{e}")
                result.passages_skipped += len(batch)
                log.warning("embed_pending.upsert_failed err=%s", e)
                continue

            # Update silver.document_passages.embedding_id
            for row, point in zip(batch, points):
                try:
                    await pg_conn.execute(
                        "UPDATE silver.document_passages "
                        "   SET embedding_id = $1, updated_at = NOW() "
                        " WHERE passage_id = $2::uuid",
                        point.id, row["passage_id"],
                    )
                    result.passages_embedded += 1
                except Exception as e:
                    result.errors.append(
                        f"pg_update_failed:{row['passage_id']}:{type(e).__name__}:{e}"
                    )
                    log.warning(
                        "embed_pending.pg_update_failed passage=%s err=%s",
                        row["passage_id"], e,
                    )

            log.info(
                "embed_pending.batch_done batch=%d/%d embedded=%d",
                batch_start // batch_size + 1,
                (len(rows) + batch_size - 1) // batch_size,
                result.passages_embedded,
            )
    finally:
        await pg_conn.close()
        if own_qdrant:
            await qdrant_client.close()

    log.info(
        "embed_pending.completed workspace=%s project=%s "
        "seen=%d embedded=%d skipped=%d upserted=%d errors=%d",
        workspace_id, project_id,
        result.passages_seen, result.passages_embedded,
        result.passages_skipped, result.qdrant_points_upserted,
        len(result.errors),
    )
    return result


__all__ = ["embed_pending_passages", "EmbeddingSyncResult"]
