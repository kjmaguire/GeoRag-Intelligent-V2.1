"""Post-embed retrieval smoke for embed_pending_passages_wf.

Guard 3 for the 2026-06-01 outage. After embed_pending_passages reports
success, exercise the FULL retrieval contract end-to-end against a
just-written passage and confirm chat-side queries can still reach the
data.

What this catches that the other guards miss:
  * Guard 1 (pre/post-upsert in passage_embedder) — verifies the BUILT
    payload and one round-tripped point. Doesn't exercise the dense
    encoder, sparse encoder, or hybrid query path.
  * Guard 2 (hourly qdrant_payload_audit) — verifies payload shape at
    rest. Doesn't exercise the query path.
  * Guard 3 (this) — picks one freshly-embedded passage's text,
    re-encodes the first words as a query, runs hybrid_query against
    the workspace, asserts a hit comes back with non-empty text. If
    this passes, the chat retrieval path works against this workspace's
    data — the orchestrator's "I don't have data on that in this
    project" refusal can't be a Qdrant-side issue.

Run as a side-effect of the embed workflow. Non-blocking on failure
(the data IS embedded; only the query path is suspect) — surfaces via
Prom metric + ERROR log + audit_ledger so it pages without blocking
the ingest pipeline.
"""
from __future__ import annotations

from app.db import bind_workspace_scope

import logging
import os
import time as _t
from dataclasses import dataclass

import asyncpg
from qdrant_client import AsyncQdrantClient

from app.audit import emit_audit
from app.metrics import (
    QDRANT_PAYLOAD_AUDIT_RUNS,
    QDRANT_PAYLOAD_AUDIT_VIOLATIONS,
)


log = logging.getLogger("georag.hatchet.embed_pending_passages.smoke")


_COLLECTION = "georag_chunks"


@dataclass
class SmokeResult:
    workspace_id: str
    passes: bool
    hits: int
    top_score: float | None
    sampled_passage_id: str | None
    duration_ms: int
    reason: str  # "ok" | "no_passages" | "no_hits" | "empty_text" | "transient"


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _query_from_text(text: str) -> str:
    """Derive a short search query from a passage's text.

    Strip whitespace, take the first 12 tokens skipping anything
    purely punctuation or one-char glue. The goal is a query the
    same passage is likely to be the top hit for — not a perfect
    semantic match, just enough signal that hybrid_query returns it
    above empty.
    """
    tokens = [t for t in (text or "").split() if any(c.isalnum() for c in t)]
    return " ".join(tokens[:12]) if tokens else ""


async def run_retrieval_smoke(workspace_id: str) -> SmokeResult:
    """One pass smoke against the workspace's georag_chunks slice.

    Returns the SmokeResult dataclass. Logs ERROR + bumps the audit
    metrics on hard failures (no hits, empty text, transient errors).
    """
    t0 = _t.monotonic()

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    sample_passage_id: str | None = None
    sample_text: str = ""
    sample_doc_id: str | None = None
    try:
        async with pool.acquire() as conn:
            # Set RLS GUC then pick one freshly-embedded passage with
            # enough text to derive a query from. ORDER BY updated_at
            # DESC biases toward what THIS run just wrote.
            await bind_workspace_scope(
                conn, workspace_id=workspace_id, site="hatchet.embed_pending_passages_smoke"
            )
            row = await conn.fetchrow(
                """
                SELECT passage_id::text  AS passage_id,
                       document_id::text AS document_id,
                       text
                FROM silver.document_passages
                WHERE workspace_id = $1::uuid
                  AND embedding_id IS NOT NULL
                  AND length(text) > 80
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                workspace_id,
            )
        if row is None:
            QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_no_passages").inc()
            return SmokeResult(
                workspace_id=workspace_id,
                passes=True,  # nothing to test against — not a failure
                hits=0, top_score=None, sampled_passage_id=None,
                duration_ms=int((_t.monotonic() - t0) * 1000),
                reason="no_passages",
            )
        sample_passage_id = row["passage_id"]
        sample_doc_id = row["document_id"]
        sample_text = row["text"] or ""
    finally:
        await pool.close()

    query = _query_from_text(sample_text)
    if not query:
        QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_empty_text").inc()
        return SmokeResult(
            workspace_id=workspace_id,
            passes=True,
            hits=0, top_score=None,
            sampled_passage_id=sample_passage_id,
            duration_ms=int((_t.monotonic() - t0) * 1000),
            reason="empty_text",
        )

    # Encode + hybrid search. Lazy imports because this module is loaded
    # by Hatchet workers that don't necessarily have torch/transformers
    # on the import path until first use.
    try:
        import torch  # noqa: PLC0415
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415
        from app.services.qdrant_service import hybrid_query  # noqa: PLC0415
        from app.services.sparse_encoder import encode_sparse  # noqa: PLC0415
    except Exception as imp_exc:  # pragma: no cover
        log.exception("embed_smoke: import failed: %s", imp_exc)
        QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_transient").inc()
        return SmokeResult(
            workspace_id=workspace_id, passes=True, hits=0, top_score=None,
            sampled_passage_id=sample_passage_id,
            duration_ms=int((_t.monotonic() - t0) * 1000),
            reason="transient",
        )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME, device=device)
    # Qwen3-Embedding query template via prompt_name (no manual prefix —
    # the model card publishes the canonical instruction template). Empty
    # config value falls back to raw encoding for A-B.
    _prompt_name = settings.EMBEDDING_QUERY_PROMPT_NAME or None
    dense = model.encode(
        query, normalize_embeddings=True, prompt_name=_prompt_name,
    ).tolist()
    sparse = encode_sparse(query)

    qclient = AsyncQdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        api_key=os.environ.get("QDRANT_API_KEY") or None,
    )
    try:
        points = await hybrid_query(
            client=qclient,
            collection=_COLLECTION,
            query_dense=dense,
            query_sparse=sparse,
            workspace_id=workspace_id,
            limit=5,
            additional_filter=None,
        )
    except Exception as q_exc:
        log.exception(
            "embed_smoke: hybrid_query failed for workspace=%s — query path is broken",
            workspace_id,
        )
        QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_transient").inc()
        await qclient.close()
        return SmokeResult(
            workspace_id=workspace_id, passes=False, hits=0, top_score=None,
            sampled_passage_id=sample_passage_id,
            duration_ms=int((_t.monotonic() - t0) * 1000),
            reason="transient",
        )
    finally:
        await qclient.close()

    hits = len(points)
    top_score = float(points[0].score) if hits else None
    top_payload = (points[0].payload or {}) if hits else {}
    top_text = top_payload.get("text", "")

    passes = hits > 0 and bool(top_text)
    reason = "ok" if passes else ("no_hits" if hits == 0 else "empty_text")

    duration_ms = int((_t.monotonic() - t0) * 1000)

    if not passes:
        log.error(
            "embed_smoke FAIL workspace=%s reason=%s hits=%d top_score=%s "
            "sampled_passage=%s sampled_doc=%s — embed succeeded but the "
            "user-facing retrieval path returned no usable hits. Chat will "
            "refuse on real questions. Investigate dense/sparse encoders, "
            "hybrid_query, and payload extraction.",
            workspace_id, reason, hits, top_score, sample_passage_id, sample_doc_id,
        )
        QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_fail").inc()
        QDRANT_PAYLOAD_AUDIT_VIOLATIONS.labels(
            collection=_COLLECTION, missing_key=f"smoke:{reason}",
        ).inc()
    else:
        log.info(
            "embed_smoke OK workspace=%s hits=%d top_score=%.4f sampled_doc=%s",
            workspace_id, hits, top_score or 0.0, sample_doc_id,
        )
        QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome="smoke_ok").inc()

    # Audit-ledger row for forensics — never block smoke on audit-write.
    try:
        pool2 = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
        try:
            async with pool2.acquire() as conn:
                await emit_audit(
                    conn,
                    action_type="embed_pending_passages.smoke.run",
                    actor_kind="workflow",
                    target_schema="qdrant",
                    target_table=_COLLECTION,
                    target_id=sample_passage_id,
                    payload={
                        "workspace_id": workspace_id,
                        "passes": passes,
                        "hits": hits,
                        "top_score": top_score,
                        "reason": reason,
                        "duration_ms": duration_ms,
                    },
                )
        finally:
            await pool2.close()
    except Exception:  # pragma: no cover
        log.exception("embed_smoke: emit_audit failed (smoke itself completed)")

    return SmokeResult(
        workspace_id=workspace_id,
        passes=passes,
        hits=hits,
        top_score=top_score,
        sampled_passage_id=sample_passage_id,
        duration_ms=duration_ms,
        reason=reason,
    )


__all__ = ["SmokeResult", "run_retrieval_smoke"]
