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

  1. Encode the text (dense, 1024-dim, normalized), branching inline on
     EMBEDDING_BACKEND: "cohere" -> Cohere Embed 5 on Cohere's own API
     (input_type="search_document"; ADR-0025), "bedrock" -> Cohere Embed v4
     on Bedrock (the rollback), else the self-hosted
     Qwen/Qwen3-Embedding-0.6B fallback. Documents are encoded RAW — the
     query-side "Instruct: ...\nQuery: ..." template (self-hosted path) /
     input_type="search_query" (hosted paths) is asymmetric and applied only
     on retrieval (see tools.search_documents). Documents must NOT carry the
     query-side treatment or the query/document vectors live in different
     subspaces.
  2. Encode via SPLADE++ (sparse, named "text")
  3. Upsert to Qdrant `georag_chunks` with payload:
       { report_id, project_id, workspace_id,
         section_number, section_title, text, embed_model, ... }
     ``embed_model`` names the model that produced the dense vector, so a
     collection holding two vector spaces can be detected from the data
     (ADR-0025 migration step 6).
  4. Update `silver.document_passages.embedding_id` with the Qdrant point ID -
     only while the row's contextualized_content is still the one that was
     encoded (``_WRITEBACK_SQL``); an enrichment that landed meanwhile has
     cleared embedding_id on purpose and keeps it cleared.

Collection schema (post 2026-06-03 Qwen3-Embedding swap):
  - vectors_config: {'': VectorParams(size=1024, distance=Cosine)}
  - sparse_vectors: {'text': SparseVectorParams(...)}
  Re-create via scripts/init_qdrant.py with GEORAG_VECTOR_SIZE=1024.

Section fields:
  Passages from PDFs/XLSX don't carry true §15 section structure, so
  we use:
    section_number = ordinal (passage index within document)
    section_title  = parent report.title

This matches the orchestrator's payload-extraction logic in
`tools.search_documents`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, SparseVector

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.services.qdrant_conn import qdrant_client_kwargs

log = logging.getLogger("georag.ingest.passage_embedder")


_QDRANT_COLLECTION = "georag_chunks"

#: The embedding_id writeback, CONDITIONAL on the content that was encoded
#: (audit finding 19). The sweep encodes COALESCE(contextualized_content, text)
#: as read at fetch time; context_enricher can rewrite contextualized_content
#: and clear embedding_id while the batch is in flight, and an unconditional
#: UPDATE then stamped the row "embedded" with a vector made from the OLD
#: content - undoing the enricher's reset, so the enrichment was never
#: embedded. With the guard the writeback of a raced row matches nothing,
#: embedding_id stays NULL, and the next sweep encodes the new content.
#: (`passages_embedded` counts attempted writebacks; a raced row is simply
#: re-embedded on the next sweep.) $3 is the value read at fetch, NULL included.
_WRITEBACK_SQL = (
    "UPDATE silver.document_passages "
    "   SET embedding_id = $1, updated_at = NOW() "
    " WHERE passage_id = $2::uuid "
    "   AND contextualized_content IS NOT DISTINCT FROM $3"
)

# Payload keys retrieval reads at app/agent/tools.py:1731-1743. A point
# missing any of these effectively disappears from chat — empty text →
# reranker drops it → empty context → orchestrator's "I don't have data
# on that in this project" refusal fires. Asserted on every batch's
# built payload (cheap, catches programmer errors) and re-asserted by
# scrolling one freshly-written point from the first batch (catches
# silent schema/serialization corruption like the 2026-06-01 outage
# where every canonical writer 400ed on the missing sparse "text" slot
# and the system silently degraded to minimal payloads).
# Unconditional: without these the point is unusable no matter what it is.
_REQUIRED_PAYLOAD_KEYS = ("text", "workspace_id")

# `report_id` is required only for a passage that HAS a parent document.
#
# It used to be unconditional, and the orphan pass exists specifically to
# embed passages where document_id IS NULL — public_geo_synthesis,
# kg_narrative and structured_summary chunks, which are syntheses rather than
# extracts and have no report to point at. So the first orphan row raised
# `payload_contract_violated` and aborted the entire per-workspace call; the
# error was appended to an errors list nothing alerts on, and those passages
# stayed unembedded on every ten-minute tick, forever. They are invisible to
# the orphan_sweep recovery layer too, whose SELECT INNER JOINs
# silver.reports.
#
# The check still catches the failure it was built for — a writer dropping a
# report_id it HAD — because a row with a document_id must still carry one.
_REQUIRED_WHEN_PARENTED = ("report_id",)

# A citation needs something to name the source. A parented passage gets it
# from the report; an orphan has to bring its own, or it reaches the reader
# as "Report " with nothing after it.
_ORPHAN_TITLE_KEYS = ("document_title", "section_title", "project_name")


#: Rows fetched per page of the pending backlog. The batches inside a page
#: still run `concurrency` at a time; this only bounds how much passage text
#: is resident at once.
PENDING_PAGE_SIZE = 2000


def pending_page_query(
    base_query: str,
    base_params: list,
    *,
    cursor: tuple[datetime | None, str] | None,
    limit: int,
) -> tuple:
    """``(sql, *params)`` for one page of the pending-passage backlog.

    ``base_query`` is the SELECT ... WHERE through the project filter (its
    only placeholder, if any, is ``$1``). The page is ordered by
    ``(created_at, passage_id)`` ascending and resumes strictly after
    ``cursor`` -- the last row of the previous page -- which is the shape of
    ``idx_document_passages_pending_created (workspace_id, created_at,
    passage_id) WHERE embedding_id IS NULL``.

    ``created_at`` is nullable in this table, and in a row-value comparison a
    NULL makes the whole predicate NULL, which would silently drop those rows
    from every page after the first. ORDER BY ascending sorts NULLs LAST, so
    they form a tail group: while the cursor is in the non-NULL group the
    predicate also admits ``created_at IS NULL``, and once the cursor is in
    the NULL group it pages on ``passage_id`` alone.
    """
    params: list = list(base_params)
    sql = base_query
    if cursor is not None:
        cursor_created_at, cursor_passage_id = cursor
        n = len(params)
        if cursor_created_at is None:
            sql += f" AND dp.created_at IS NULL AND dp.passage_id > ${n + 1}::uuid "
            params.append(cursor_passage_id)
        else:
            sql += (
                f" AND ((dp.created_at, dp.passage_id) > (${n + 1}::timestamp, ${n + 2}::uuid) "
                f"      OR dp.created_at IS NULL) "
            )
            params.extend([cursor_created_at, cursor_passage_id])
    sql += f" ORDER BY dp.created_at ASC, dp.passage_id ASC LIMIT {int(limit)}"
    return (sql, *params)


#: Passages whose sparse encode already failed in this process, kept ONLY to
#: log each one once. Not a retry counter and never consulted for behaviour.
_SPARSE_FAILURE_LOGGED: set[str] = set()
_SPARSE_FAILURE_LOG_CAP = 10_000


def _log_sparse_failure_once(passage_id: str) -> None:
    """One ERROR per passage per process for a sparse-encode failure.

    A passage whose sparse encode fails is left unembedded (RAG-11: never
    written dense-only) and its dense vector is thrown away, so the next sweep
    -- every ten minutes -- encodes it again, forever, if the failure is about
    that passage rather than about the sidecar. A transient sidecar outage and
    a passage the encoder cannot handle look identical from here.

    TODO(embed_attempts): the right fix is a per-row attempt counter that
    stops retrying after N tries and raises a DQ flag. silver.document_passages
    has no column for it and no JSONB/metadata column to hold one (checked
    2026-10-04: the table carries only embedding_id, ocr_*, page_*, modality,
    image_object_key, contextualized_content, parent_chunk_id, verbalized_at,
    project_id), and an in-memory or Redis tally is not acceptable because the
    count must survive restarts and be shared by workers. Needed:
    ``silver.document_passages.embed_attempts smallint NOT NULL DEFAULT 0`` (a
    Laravel migration), incremented here and excluded from the pending query
    once it passes the cap. Until it exists this only makes the loop visible.
    """
    if passage_id in _SPARSE_FAILURE_LOGGED:
        return
    if len(_SPARSE_FAILURE_LOGGED) >= _SPARSE_FAILURE_LOG_CAP:
        _SPARSE_FAILURE_LOGGED.clear()
    _SPARSE_FAILURE_LOGGED.add(passage_id)
    log.error(
        "embed_pending.sparse_encode_retry_loop passage=%s: sparse encode "
        "failed; the passage stays unembedded and is retried every sweep with "
        "no attempt limit (needs silver.document_passages.embed_attempts -- "
        "see _log_sparse_failure_once). Logged once per passage per process.",
        passage_id,
    )


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


def _is_request_rejection(exc: BaseException) -> bool:
    """True when the embedding host refused the REQUEST as invalid.

    Bedrock answers a body it will not take (an over-long or otherwise
    unacceptable text) with ``ValidationException``; Cohere's own API and the
    self-hosted sidecar answer 400/422. Everything else -- throttling, 5xx, auth, a network
    error -- is about the call, not the texts, and bisecting would only
    multiply it.
    """
    from app.services.embedding import CohereEmbeddingHttpError

    if isinstance(exc, CohereEmbeddingHttpError):
        # Cohere's own API: _CohereEmbedding raises this for any non-retryable
        # non-2xx, so the status is all there is to classify on.
        return exc.status_code in (400, 422)
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        return isinstance(error, dict) and error.get("Code") == "ValidationException"
    status = getattr(response, "status_code", None)
    return status in (400, 422)


def encode_isolating_rejections(encode, texts: list[str]) -> list[list[float] | None]:
    """``encode(texts)``, bisecting on a rejection to isolate the bad text(s).

    VEN-14 (2026-09-29): one text the host rejects used to fail its whole
    batch on every sweep, and because the batch is always the same
    oldest-first rows, its good neighbours were never embedded either.
    Bisection finds the offending text(s) in O(log n) extra calls; their
    slots come back None and the caller leaves those rows unembedded (the
    next sweep retries them) while the rest of the batch is written.
    """
    try:
        return list(encode(texts))
    except Exception as exc:
        if not _is_request_rejection(exc):
            raise
        if len(texts) <= 1:
            log.warning(
                "embed_pending.dense_text_rejected chars=%d err=%s",
                len(texts[0]) if texts else 0,
                type(exc).__name__,
            )
            return [None] * len(texts)
        mid = len(texts) // 2
        return encode_isolating_rejections(encode, texts[:mid]) + encode_isolating_rejections(
            encode, texts[mid:]
        )


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


def embed_model_tag(model) -> str:
    """The ``embed_model`` payload value for points ``model`` writes.

    The hosted adapters expose ``model_name`` (``embed-v5.0-pro``,
    ``cohere.embed-v4:0``). The local SentenceTransformer and the sidecar proxy
    do not, and they only ever serve ``settings.EMBEDDING_MODEL_NAME``.
    """
    name = getattr(model, "model_name", None)
    if isinstance(name, str) and name:
        return name
    from app.config import settings

    return settings.EMBEDDING_MODEL_NAME


def load_embedding_model():
    """Construct the backend-aware embedding model (F28, 2026-08-11).

    Extracted from ``embed_pending_passages`` so the Hatchet cron can build
    the model ONCE per sweep and pass it into every per-project call —
    previously the model (SentenceTransformer weights or the hosted client)
    was re-constructed for every project on every 10-minute tick.

    This deliberately does NOT delegate to ``embedding.get_embedding_model``:
    the ingest path prefers CUDA for the local backend where the query path
    pins CPU, and it ignores ``EMBEDDING_SERVICE_URL``. What both paths MUST
    agree on is the hosted backend, because a mismatch writes one vector
    space and queries another (ADR-0021 migration step 2).
    """
    from app.services._bedrock import reject_retired_backend
    from app.services.embedding import EMBEDDING_BACKEND

    reject_retired_backend(EMBEDDING_BACKEND, setting="EMBEDDING_BACKEND")

    if EMBEDDING_BACKEND == "cohere":
        # The SAME object get_embedding_model() builds on the query path: both
        # go through build_cohere_embedding(), so model, dimension and key
        # cannot drift between the two (ADR-0025 gotcha 1).
        from app.services.embedding import build_cohere_embedding

        return build_cohere_embedding()
    if EMBEDDING_BACKEND == "bedrock":
        # No local model load at all — Cohere Embed v4 on Amazon Bedrock.
        # .encode(texts, normalize_embeddings=True, show_progress_bar=False)
        # is a drop-in call (input_type defaults to "search_document",
        # correct for ingestion).
        from app.services._bedrock import (
            assert_no_retired_foundry_env,
            bedrock_region,
        )
        from app.services.embedding import (
            BEDROCK_EMBED_MODEL_ID,
            _BedrockEmbedding,
        )
        assert_no_retired_foundry_env(context="EMBEDDING_BACKEND=bedrock")
        if not BEDROCK_EMBED_MODEL_ID:
            raise RuntimeError(
                "EMBEDDING_BACKEND=bedrock but BEDROCK_EMBED_MODEL_ID is empty"
            )
        log.info(
            "embed_pending.loading_embedding_model backend=bedrock model=%s region=%s",
            BEDROCK_EMBED_MODEL_ID, bedrock_region(),
        )
        return _BedrockEmbedding(BEDROCK_EMBED_MODEL_ID)

    import torch
    from sentence_transformers import SentenceTransformer

    from app.config import settings
    # Use CUDA when available — A4500 does ~144 chunks/sec vs ~4 on CPU.
    # Falls back to CPU gracefully if no GPU present or CUDA unavailable.
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("embed_pending.loading_embedding_model name=%s device=%s",
             settings.EMBEDDING_MODEL_NAME, _device)
    return SentenceTransformer(
        settings.EMBEDDING_MODEL_NAME,
        revision=settings.EMBEDDING_MODEL_REVISION,
        trust_remote_code=False,
        device=_device,
    )


async def embed_pending_passages(
    *,
    workspace_id: str,
    project_id: str | None = None,
    embedding_model=None,
    qdrant_client: AsyncQdrantClient | None = None,
    batch_size: int = 64,
    max_passages: int | None = None,
    concurrency: int | None = None,
) -> EmbeddingSyncResult:
    """Walk un-embedded passages for a project and push to Qdrant.

    Args:
        workspace_id: silver.workspaces UUID for scoping
        project_id: silver.projects UUID. If None, embeds all passages
            in the workspace.
        embedding_model: SentenceTransformer instance. Loaded if None.
        qdrant_client: AsyncQdrantClient. Connected if None.
        batch_size: passages per Cohere/encode batch (Cohere Embed v2 API
            caps a single request at 96 texts — keep at or below that)
        max_passages: cap for smoke tests; None = no limit
        concurrency: batches processed in parallel (default: env
            EMBED_CONCURRENCY, else 3). The sync Cohere HTTP call and the
            SPLADE forward run in a thread pool so batches genuinely
            overlap; per-batch SPLADE stays a per-text loop, so peak
            memory is `concurrency` × one 512-token forward — nowhere
            near the batched 32×512 pass that OOM-killed the 8 Gi worker
            on 2026-08-07.

    Returns:
        EmbeddingSyncResult with per-stage counts.
    """
    result = EmbeddingSyncResult(workspace_id=workspace_id, project_id=project_id)

    # ── Load models / clients if not provided ─────────────────────
    if embedding_model is None:
        embedding_model = load_embedding_model()

    own_qdrant = False
    if qdrant_client is None:
        qdrant_client = AsyncQdrantClient(**qdrant_client_kwargs())
        own_qdrant = True

    # ── Load passage rows ─────────────────────────────────────────
    pg_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # REC#2 Phase-2 (2026-06-03) note (since corrected): that migration
        # tightened this to is_local=true (`SET LOCAL`), reasoning it was
        # transaction-scoped-and-therefore-safer. It wasn't — `pg_conn` here
        # is a dedicated, non-pooled connection with no wrapping transaction
        # (same fact the sibling app.project_id bind below already documents
        # and handles correctly with is_local=false). `SET LOCAL` outside a
        # transaction block is a silent no-op in Postgres, so app.workspace_id
        # was never actually set on this connection — and because
        # database/raw/phase0's tenant_isolation policy fails OPEN when the
        # GUC reads NULL, every embed_pending_passages sweep silently read
        # and embedded EVERY workspace's unembedded passages, tagged in
        # Qdrant with the caller's workspace_id (line ~292 below) rather than
        # each row's true owner — a real cross-tenant content leak, not a
        # theoretical one. is_local=false matches this connection's actual
        # lifecycle: no transaction to scope to, so bind to the session,
        # which lives exactly as long as `pg_conn` does.
        await bind_workspace_scope(
            pg_conn, workspace_id=workspace_id, site="passage_embedder",
            is_local=False,
        )
        if project_id:
            # Session-scoped (false) is correct here, not a hazard: `pg_conn`
            # is a dedicated, non-pooled connection with no wrapping
            # transaction, so `is_local=true`/SET LOCAL would be silently
            # discarded before the next statement runs (Postgres: "SET LOCAL
            # used outside a transaction block will appear to have no
            # effect"). Session scope persists for this connection's life,
            # which is exactly what's needed. Also: no RLS policy anywhere
            # in this codebase reads app.project_id (verified 2026-07-30) —
            # project filtering here uses an explicit $1::uuid query param,
            # not this GUC — so this value is set defensively/for
            # completeness, not for tenant-isolation enforcement.
            await pg_conn.execute(
                "SELECT set_config('app.project_id', $1, false)", project_id,
            )
        else:
            await pg_conn.execute("RESET app.project_id")

        # LEFT JOIN to silver.reports so passages without a parent
        # document_id (e.g. chunk_kind='public_geo_synthesis' from the
        # TIER 0b Qdrant backfill, or kg_narrative / structured_summary
        # from ADR-0012 synthesizers) still get embedded. Title falls
        # back to the chunk_kind label; project_id stays NULL for
        # cross-project public-geoscience corpora.
        query = (
            "SELECT dp.passage_id::text AS passage_id, "
            "       dp.document_id::text AS document_id, "
            "       dp.contextualized_content, dp.text, dp.ordinal, dp.page_first, dp.page_last, "
            # Phase 3 (2026-05-22) — OCR provenance travels with the
            # qdrant point so retrieval can weight low-confidence
            # passages down without a Postgres join. NULL confidence means
            # no engine confidence exists — text layer, or Cohere Parse,
            # which reports none (ADR-0019); ocr_method discriminates.
            "       dp.ocr_confidence, dp.ocr_method, dp.ocr_status, "
            "       dp.chunk_kind, dp.created_at AS created_at, "
            # Multimodal (2026-08-18) — modality selects the encoder below:
            # 'text' takes the batched Cohere text path, 'image' fetches the
            # stored page render and takes the single-image path. Both land
            # in the same 1024-dim dense slot.
            "       dp.modality, dp.page_number, dp.image_object_key, "
            "       COALESCE(r.title, dp.chunk_kind, 'Passage') AS report_title, "
            # Audit RAG-9: a synthesized passage has no parent report, so
            # r.project_id is NULL and the point was written with no
            # project — which project_or_public retrieval admits into
            # EVERY project's search. dp.project_id (written by
            # nl_summaries from the source collar) fills it.
            "       COALESCE(r.project_id, dp.project_id)::text AS project_id "
            "  FROM silver.document_passages dp "
            "  LEFT JOIN silver.reports r ON r.report_id = dp.document_id "
            " WHERE dp.embedding_id IS NULL "
            # Skip passages whose OCR text is known-bad: 'pending_reocr'
            # means the quality agent has queued a replacement (the row
            # gets re-embedded once it flips to 'reocr_complete');
            # 'rejected' is forward-compat. 'low_confidence' passages ARE
            # embedded, because on a corpus of 1960s scans the flagged page
            # is sometimes the only page that mentions the thing at all.
            #
            # This comment claimed retrieval down-weighted them "via the
            # ocr_status payload field below". It was written aspirationally
            # and stayed wrong for months: the field WAS written to the
            # payload, and nothing read it — DocumentChunk had no ocr_status
            # attribute, so a page the router had tiered unreadable competed
            # on equal footing and reached the model unmarked. True as of
            # 2026-08-21: agent/tools.py reads ocr_status onto the chunk,
            # sorts flagged pages below clean ones, and DocumentChunk.
            # annotated_text prefixes them with an explicit warning before
            # they enter the context window.
            "   AND (dp.ocr_status IS NULL "
            "        OR dp.ocr_status NOT IN ('rejected', 'pending_reocr')) "
        )
        base_query = query
        base_params: list = []
        if project_id:
            # When a specific project is requested, we keep the original
            # INNER-JOIN semantics: only passages with a parent report
            # in that project. Public-geo passages have no project so
            # they fall outside this scope (intentionally).
            base_query += " AND COALESCE(r.project_id, dp.project_id) = $1::uuid "
            base_params.append(project_id)

        # Paged, not loaded whole. This used to fetch the ENTIRE pending
        # backlog (`max_passages` defaults to None) into memory in one
        # round-trip: after a bulk import or a re-embed that is every
        # passage's full text plus contextualized_content, held at once on
        # the worker. Pages of PENDING_PAGE_SIZE rows are keyset-paged on
        # (created_at, passage_id), the shape of
        # idx_document_passages_pending_created. A keyset cursor (rather than
        # re-running "WHERE embedding_id IS NULL LIMIT n") is what keeps a
        # passage that was SKIPPED this run (rejected text, failed sparse
        # encode, failed image) from being fetched again on the next page and
        # looping forever.
        async def _fetch_page(
            cursor: tuple[datetime | None, str] | None, limit: int,
        ) -> list:
            return await pg_conn.fetch(
                *pending_page_query(
                    base_query, base_params, cursor=cursor, limit=limit,
                )
            )

        page_limit = (
            min(PENDING_PAGE_SIZE, int(max_passages))
            if max_passages else PENDING_PAGE_SIZE
        )
        rows = await _fetch_page(None, page_limit)

        if not rows:
            log.info("embed_pending.no_pending_passages workspace=%s project=%s",
                     workspace_id, project_id)
            return result

        # ── Encode in batches ─────────────────────────────────────
        # 2026-08-11 throughput rework: batches now overlap. The dense
        # (sync Cohere HTTP) and sparse (SPLADE CPU forward) stages run in
        # a small thread pool so `concurrency` batches are in flight at
        # once, and Qdrant upserts after the first batch use wait=False —
        # on the Azure Files (SMB) volume a wait=True flush dominated the
        # whole pipeline (~20-40 s/batch vs ~13 s of actual encode work).
        # Durability is unchanged: writes still land in Qdrant's WAL, and
        # the embed_pending cron's drift self-heal re-embeds anything a
        # crash loses. The FIRST batch keeps wait=True so the post-upsert
        # payload-contract verification below stays race-free.
        if concurrency is None:
            concurrency = max(1, int(os.environ.get("EMBED_CONCURRENCY", "3")))

        from app.services.sparse_encoder import encode_sparse

        _embed_model_tag = embed_model_tag(embedding_model)

        def _encode_dense_sync(texts: list[str]):
            return embedding_model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False,
            ).tolist()

        def _encode_image_sync(rows: list) -> list[list[float] | None]:
            """Dense-encode page-image passages, one embed call per page.

            Returns None in a slot whose image could not be embedded (missing
            object, unreadable render, model rejection). The caller SKIPS
            those rows rather than substituting a text vector: a page-image
            passage whose vector came from its placeholder caption would be
            silently wrong — it would rank on the words "page image" instead
            of on what the page depicts, and nothing downstream could tell.
            Leaving embedding_id NULL lets the next sweep retry it.
            """
            from georag_object_storage import Bucket, get_storage_client  # noqa: PLC0415

            out: list[list[float] | None] = []
            storage = None
            for row in rows:
                key = row["image_object_key"]
                if not key:
                    log.warning(
                        "embed_pending.image_missing_key passage=%s", row["passage_id"],
                    )
                    out.append(None)
                    continue
                try:
                    if storage is None:
                        storage = get_storage_client()
                    png = storage.get_bytes(Bucket.BRONZE_RASTER, key)
                    out.append(embedding_model.embed_image(png).tolist())
                except AttributeError:
                    # The active embedding backend is not a hosted Cohere one
                    # (local SentenceTransformer or the sidecar proxy — neither
                    # has embed_image). This is a configuration error, not a data
                    # error, and it would otherwise repeat per row per sweep.
                    log.error(
                        "embed_pending.image_backend_unsupported — image passages "
                        "require EMBEDDING_BACKEND=cohere (Cohere Embed 5) or "
                        "bedrock (Cohere Embed v4); "
                        "skipping %d image passage(s) in this batch", len(rows),
                    )
                    out.extend([None] * (len(rows) - len(out)))
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "embed_pending.image_encode_failed passage=%s key=%s err=%s",
                        row["passage_id"], key, e,
                    )
                    out.append(None)
            return out

        def _encode_sparse_sync(texts: list[str]) -> list[dict | None]:
            # Per-text loop preserved — see the 2026-08-07 OOM note in the
            # docstring. Peak memory stays bounded per in-flight batch
            # (long passages are windowed inside encode_sparse, RAG-10).
            #
            # None marks a FAILED encode, distinct from an empty vector.
            # It used to be `{}` for both, and the caller then wrote a
            # dense-only point and set embedding_id — so a sparse sidecar
            # outage mid-ingest (it runs desired=1 on Fargate Spot) left
            # every passage in flight permanently without a sparse leg,
            # with nothing to retry it (audit RAG-11).
            out: list[dict | None] = []
            for txt in texts:
                try:
                    out.append(encode_sparse(txt))
                except Exception as e:
                    log.warning("embed_pending.sparse_encode_failed err=%s", e)
                    out.append(None)
            return out

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=concurrency)
        pg_lock = asyncio.Lock()
        batches: list = []
        total_batches = 0  # re-bound per page below; read by _process_batch's log line

        async def _process_batch(batch_no: int, batch, *, first: bool) -> None:
            # Multimodal split (2026-08-18). Text and image passages take
            # different encoders and CANNOT share a request — Embed v4
            # rejects mixed text+image input outright ("cannot have both text
            # and image inputs"). Positions are tracked explicitly so the
            # results re-merge in batch order.
            text_idx = [i for i, r in enumerate(batch) if (r["modality"] or "text") != "image"]
            image_idx = [i for i, r in enumerate(batch) if (r["modality"] or "text") == "image"]

            dense_by_idx: dict[int, list[float]] = {}
            sparse_by_idx: dict[int, dict] = {}

            if text_idx:
                texts = [
                    batch[i]["contextualized_content"] or batch[i]["text"]
                    for i in text_idx
                ]
                # Dense encode (Cohere Embed v4 / SentenceTransformer)
                try:
                    dense_vectors = await loop.run_in_executor(
                        executor, encode_isolating_rejections, _encode_dense_sync, texts,
                    )
                except Exception as e:
                    result.errors.append(f"dense_encode_failed:{type(e).__name__}:{e}")
                    result.passages_skipped += len(batch)
                    return
                # A rejected text's slot is None; the merge below stores it
                # and the point builder skips it, so only that row stays
                # unembedded (VEN-14).
                _rejected = sum(1 for v in dense_vectors if v is None)
                if _rejected:
                    result.passages_skipped += _rejected
                    result.errors.append(f"dense_text_rejected:{_rejected}")

                # Sparse encode (SPLADE++) — text only. An image passage's
                # text is a caption or a generated description, so SPLADE on
                # it would inject lexical matches for words that appear
                # nowhere on the page. Image points are dense-only; Qdrant's
                # RRF fusion unions the two prefetches, so a point absent
                # from the sparse branch still ranks on its dense score.
                sparse_vectors = await loop.run_in_executor(
                    executor, _encode_sparse_sync, texts,
                )
                sparse_failed = 0
                for pos, i in enumerate(text_idx):
                    sv = sparse_vectors[pos] if pos < len(sparse_vectors) else None
                    if sv is None:
                        # RAG-11: a text passage whose sparse encode FAILED
                        # is not written dense-only. It stays unembedded
                        # (embedding_id NULL) so the next sweep retries it —
                        # the same rule the image path follows below, and
                        # the ingest-side mirror of GI-11.
                        sparse_failed += 1
                        _log_sparse_failure_once(batch[i]["passage_id"])
                        continue
                    dense_by_idx[i] = dense_vectors[pos]
                    if sv:
                        sparse_by_idx[i] = sv
                if sparse_failed:
                    result.passages_skipped += sparse_failed
                    result.errors.append(
                        f"sparse_encode_failed:{sparse_failed} passage(s) left "
                        f"unembedded for retry"
                    )

            if image_idx:
                image_vectors = await loop.run_in_executor(
                    executor, _encode_image_sync, [batch[i] for i in image_idx],
                )
                for pos, i in enumerate(image_idx):
                    iv = image_vectors[pos] if pos < len(image_vectors) else None
                    if iv is None:
                        # Left unembedded on purpose — see _encode_image_sync.
                        result.passages_skipped += 1
                        continue
                    dense_by_idx[i] = iv

            # Build Qdrant points.
            # `point_passage_ids` is parallel to `points` and is what the
            # embedding_id writeback below keys on. Do NOT reintroduce a
            # positional zip(batch, points): a batch row can now be skipped
            # (an image whose render failed to embed), which makes `points`
            # shorter than `batch` and shifts every subsequent pairing —
            # writing point N's id onto passage N+1. That corrupts silently:
            # every row still looks embedded, and retrieval returns the wrong
            # text for the vector.
            points: list[PointStruct] = []
            point_passage_ids: list[str] = []
            #: Parallel to `points`: the contextualized_content each vector was
            #: ENCODED from (NULL = the bare text). The writeback is conditional
            #: on it still being the row's content (see _WRITEBACK_SQL).
            point_encoded: list[str | None] = []
            for idx, row in enumerate(batch):
                dv = dense_by_idx.get(idx)
                if dv is None:
                    continue
                sv = sparse_by_idx.get(idx)
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
                    "ocr_status": row["ocr_status"],
                    # ADR-0010 §A discriminator — lets the orchestrator
                    # filter / score public_geo_synthesis differently
                    # from narrative report chunks.
                    "chunk_kind": row.get("chunk_kind") if isinstance(row, dict) else row["chunk_kind"],
                    # Multimodal discriminator. Retrieval and the Reader use
                    # this to know a hit is a page render — so the UI can show
                    # the page instead of a caption, and so an operator
                    # reading a citation knows whether the text is quoted from
                    # the document or generated from a picture of it.
                    "modality": row["modality"] or "text",
                    "page_number": row["page_number"],
                    "image_object_key": row["image_object_key"],
                    # The model that produced THIS point's dense vector — text
                    # and image points alike (ADR-0025). A collection holding
                    # two vector spaces still returns results, ranked by
                    # meaningless cosines; this tag is what makes "zero points
                    # lack embed_model = <new model>" a count rather than an
                    # inference.
                    "embed_model": _embed_model_tag,
                }
                # Pre-upsert payload contract assertion. Failing here is a
                # programmer error (the writer dropped a key it shouldn't have);
                # the right move is to abort the whole run loudly rather than
                # quietly ship points the retrieval layer can't use.
                def _absent(key: str) -> bool:
                    return key not in payload or payload[key] in (None, "")

                _required = list(_REQUIRED_PAYLOAD_KEYS)
                if not _absent("report_id"):
                    # Has a parent — hold it to the parented contract.
                    _required.extend(_REQUIRED_WHEN_PARENTED)
                elif all(_absent(k) for k in _ORPHAN_TITLE_KEYS):
                    # A genuine orphan, but with nothing to name it by. Let
                    # it through and say so rather than aborting the run:
                    # an unnamed synthesis chunk is still better retrieval
                    # than a passage that is never embedded at all, and the
                    # citation falls back to the section label.
                    log.warning(
                        "embed_pending: orphan passage %s has no document_id "
                        "and no title/section/project to cite it by — its "
                        "citation will be unnamed.",
                        row["passage_id"],
                    )

                _missing_keys = [k for k in _required if _absent(k)]
                if _missing_keys:
                    raise RuntimeError(
                        f"embed_pending.payload_contract_violated: passage "
                        f"{row['passage_id']} missing required keys {_missing_keys} "
                        f"(have={sorted(payload.keys())}). Aborting to prevent "
                        f"silent retrieval degradation."
                    )
                points.append(PointStruct(
                    id=point_id, vector=vector_dict, payload=payload,
                ))
                point_passage_ids.append(row["passage_id"])
                point_encoded.append(row["contextualized_content"])

            # Upsert. wait only on the first batch (needed for the verify
            # read below); later batches return as soon as Qdrant accepts
            # the write, dodging the SMB flush latency per batch.
            try:
                await qdrant_client.upsert(
                    collection_name=_QDRANT_COLLECTION,
                    points=points, wait=first,
                )
                result.qdrant_points_upserted += len(points)
            except Exception as e:
                result.errors.append(f"upsert_failed:{type(e).__name__}:{e}")
                result.passages_skipped += len(batch)
                log.warning("embed_pending.upsert_failed err=%s", e)
                return

            # Post-upsert verification on the FIRST successful batch of each
            # run only — retrieve one freshly-written point and confirm Qdrant
            # stored the payload contract intact. One round-trip per run
            # (~5ms) catches silent schema/serialization corruption (the
            # 2026-06-01 outage: missing sparse "text" slot caused canonical
            # upserts to 400 and an unknown code path stripped payload to
            # make uploads succeed).
            if points and first:
                try:
                    _verify = await qdrant_client.retrieve(
                        collection_name=_QDRANT_COLLECTION,
                        ids=[points[0].id],
                        with_payload=True,
                        with_vectors=False,
                    )
                    if not _verify:
                        raise RuntimeError(
                            f"embed_pending.verify_failed: just-upserted point "
                            f"{points[0].id} not retrievable. Qdrant state is "
                            f"inconsistent — aborting before more bad data lands."
                        )
                    _vp = _verify[0].payload or {}
                    _vmissing = [k for k in _REQUIRED_PAYLOAD_KEYS if k not in _vp or _vp[k] in (None, "")]
                    if _vmissing:
                        raise RuntimeError(
                            f"embed_pending.verify_failed: just-upserted point "
                            f"{points[0].id} payload missing {_vmissing} "
                            f"(stored keys={sorted(_vp.keys())}). The Qdrant "
                            f"collection schema or some intermediary is "
                            f"stripping payload — aborting before more bad "
                            f"data lands. Check georag_chunks sparse 'text' "
                            f"vector slot config and any non-canonical upsert "
                            f"paths."
                        )
                except RuntimeError:
                    raise
                except Exception as _vexc:
                    # Qdrant unreachable mid-run is a transient issue, not a
                    # contract violation — log and continue rather than aborting
                    # the whole embed.
                    log.warning(
                        "embed_pending.verify_skipped err=%s", _vexc,
                    )

            # Update silver.document_passages.embedding_id — one batched
            # executemany round-trip; falls back to per-row on failure so a
            # single bad row can't lose the whole batch's writeback.
            _wb = [
                (point.id, passage_id, encoded)
                for point, passage_id, encoded in zip(
                    points, point_passage_ids, point_encoded, strict=True,
                )
            ]
            try:
                # pg_conn is a single shared asyncpg connection — one
                # query at a time; the lock serialises concurrent batches'
                # writebacks (they're the cheapest stage, so this doesn't
                # bottleneck the pipeline).
                async with pg_lock:
                    await pg_conn.executemany(_WRITEBACK_SQL, _wb)
                result.passages_embedded += len(_wb)
            except Exception as batch_exc:
                log.warning(
                    "embed_pending.pg_update_batch_failed err=%s — per-row fallback",
                    batch_exc,
                )
                for point_id, passage_id, encoded in _wb:
                    try:
                        async with pg_lock:
                            await pg_conn.execute(
                                _WRITEBACK_SQL, point_id, passage_id, encoded,
                            )
                        result.passages_embedded += 1
                    except Exception as e:
                        result.errors.append(
                            f"pg_update_failed:{passage_id}:{type(e).__name__}:{e}"
                        )
                        log.warning(
                            "embed_pending.pg_update_failed passage=%s err=%s",
                            passage_id, e,
                        )

            log.info(
                "embed_pending.batch_done batch=%d/%d embedded=%d",
                batch_no + 1,
                total_batches,
                result.passages_embedded,
            )

        sem = asyncio.Semaphore(concurrency)

        async def _guarded(i: int, b) -> None:
            async with sem:
                await _process_batch(i, b, first=False)

        try:
            fetched_total = 0
            first_page = True
            while rows:
                result.passages_seen += len(rows)
                fetched_total += len(rows)
                batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
                total_batches = len(batches)
                rest = batches
                if first_page:
                    # First batch of the RUN runs alone: wait=True upsert +
                    # payload-contract verify establish the collection is
                    # healthy before fanning out.
                    await _process_batch(0, batches[0], first=True)
                    rest = batches[1:]
                if rest:
                    offset = 1 if first_page else 0
                    # gather without return_exceptions: a payload-contract
                    # RuntimeError must abort the run loudly (matching the old
                    # serial behaviour); per-batch encode/upsert failures are
                    # already caught inside _process_batch.
                    await asyncio.gather(
                        *(_guarded(i, b) for i, b in enumerate(rest, start=offset))
                    )
                first_page = False

                # Exhausted: a short page, or the caller's max_passages cap.
                if len(rows) < page_limit:
                    break
                remaining = (
                    int(max_passages) - fetched_total if max_passages else None
                )
                if remaining is not None and remaining <= 0:
                    break
                last = rows[-1]
                rows = await _fetch_page(
                    (last["created_at"], last["passage_id"]),
                    min(PENDING_PAGE_SIZE, remaining)
                    if remaining is not None else PENDING_PAGE_SIZE,
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
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


__all__ = ["embed_pending_passages", "load_embedding_model", "EmbeddingSyncResult"]
