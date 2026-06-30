"""Real RAG-backed eval evaluator (§10.4) — doc-phase 162.

Third evaluator graduation: full retrieval-augmented eval path. Wires
`AgentDeps` (pg pool + Qdrant + Neo4j) into the eval flow and invokes
`run_deterministic_rag` per question — the same orchestrator the
chat API uses.

What's REAL in this graduation:
  - Real retrieval: Qdrant vector search, PostGIS spatial queries,
    Neo4j graph traversal
  - Real LLM call via vLLM
  - Real `GeoRAGResponse` (text + citations + confidence)
  - Refusal-correctness validator (§04i Layer 6 / §2.9) applied to
    the response text

What's still pending later graduations:
  - Citation-presence validator (Layer 2 typed output)
  - Numeric-claim validator (Layer 3)
  - Entity-resolution validator (Layer 4)
  - Chunk-provenance validator (Layer 5)
  - Retrieval-quality validator (Layer 1)

Module-level singleton: `_get_or_build_deps()` builds AgentDeps once
and caches it. SentenceTransformer load is the expensive bit (model
weights ≈ 67 MB); subsequent eval questions reuse the warm instance.

Project scoping: today the eval uses the *first* live project_id in
`silver.projects` because golden_questions don't carry per-question
project scope yet. When SME questions land with explicit project hints
(§24.1), `evaluate_question_real_rag` will read them off the question
record.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import asyncpg
import httpx

from app.services.eval.workspace_evaluator import (
    QuestionRecord,
    QuestionResult,
)

log = logging.getLogger("georag.eval.real_rag_evaluator")


# Module-level singleton for AgentDeps. Built lazily on first call; reused
# across the run. Reset by setting _DEPS_SINGLETON = None.
_DEPS_SINGLETON: Any = None
_DEPS_LOCK: asyncio.Lock = asyncio.Lock()


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _build_agent_deps() -> Any:
    """Construct AgentDeps with the minimum-viable resource set.

    Imports of asyncpg/qdrant_client/neo4j are deferred to call time
    so unit tests that don't touch RAG don't pay the import cost.

    Returns:
        An AgentDeps instance.

    Raises:
        RuntimeError if no projects exist in silver.projects (the eval
        path needs a project_id to scope retrieval).
    """
    # Local imports — keep module import lightweight for the unit-test
    # path that doesn't invoke RAG.
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient

    from app.agent.deps import AgentDeps

    pg_pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=4, statement_cache_size=0
    )

    # Doc-phase 180 — resolve project_id by data weight (collars + reports)
    # rather than `created_at ASC`. The eval should run against the
    # most-populated project so §04i validators see real retrievable data.
    async with pg_pool.acquire() as conn:
        project_row = await conn.fetchrow(
            """
            SELECT p.project_id::text AS pid,
                   COALESCE(c.cnt, 0) + COALESCE(r.cnt, 0) AS data_weight
              FROM silver.projects p
              LEFT JOIN (
                  SELECT project_id, count(*) AS cnt
                    FROM silver.collars GROUP BY project_id
              ) c ON c.project_id = p.project_id
              LEFT JOIN (
                  SELECT project_id, count(*) AS cnt
                    FROM silver.reports GROUP BY project_id
              ) r ON r.project_id = p.project_id
             ORDER BY data_weight DESC, p.created_at ASC
             LIMIT 1
            """
        )
    if project_row is None:
        await pg_pool.close()
        raise RuntimeError(
            "real_rag_v1 evaluator requires at least one row in "
            "silver.projects; none found."
        )
    project_id = project_row["pid"]

    qdrant_host = os.environ.get("QDRANT_HOST", "qdrant")
    qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
    qdrant_client = AsyncQdrantClient(host=qdrant_host, port=qdrant_port)

    neo4j_uri = (
        f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:"
        f"{os.environ.get('NEO4J_PORT', '7687')}"
    )
    neo4j_driver = AsyncGraphDatabase.driver(
        neo4j_uri,
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", ""),
        ),
    )

    # Doc-phase 169 — load embedding model + reranker. Both are CPU-only
    # so the eval worker pays the model-init cost once and reuses for the
    # life of the singleton. Each is wrapped in a try so a missing weight
    # cache or download failure leaves the field None and the orchestrator
    # falls back to its graceful-degradation paths.
    embedding_model = None
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        from app.config import settings  # noqa: PLC0415

        embedding_model = SentenceTransformer(
            settings.EMBEDDING_MODEL_NAME, device="cpu"
        )
        # Warm up so the first real call doesn't pay JIT/model-init cost.
        embedding_model.encode("warm-up", normalize_embeddings=True)
        log.info(
            "real_rag_evaluator.embedding_model_loaded model=%s dim=%d",
            settings.EMBEDDING_MODEL_NAME,
            embedding_model.get_sentence_embedding_dimension(),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "real_rag_evaluator.embedding_model_load_failed err=%s "
            "(graceful degradation)",
            e,
        )

    reranker = None
    try:
        from app.services.reranker import _get_reranker  # noqa: PLC0415

        reranker = _get_reranker()
        log.info("real_rag_evaluator.reranker_loaded via lru_cache singleton")
    except Exception as e:  # noqa: BLE001
        log.warning(
            "real_rag_evaluator.reranker_load_failed err=%s "
            "(graceful degradation)",
            e,
        )

    deps = AgentDeps(
        pg_pool=pg_pool,
        qdrant_client=qdrant_client,
        neo4j_driver=neo4j_driver,
        project_id=project_id,
        embedding_model=embedding_model,
        reranker=reranker,
    )
    log.info(
        "real_rag_evaluator.deps_built project_id=%s qdrant=%s neo4j=%s "
        "embedding=%s reranker=%s",
        project_id, qdrant_host, neo4j_uri,
        embedding_model is not None, reranker is not None,
    )
    return deps


async def _get_or_build_deps() -> Any:
    """Return the module-level singleton AgentDeps, building if needed."""
    global _DEPS_SINGLETON
    if _DEPS_SINGLETON is None:
        async with _DEPS_LOCK:
            if _DEPS_SINGLETON is None:
                _DEPS_SINGLETON = await _build_agent_deps()
    return _DEPS_SINGLETON


async def evaluate_question_real_rag(
    conn: asyncpg.Connection,
    question: QuestionRecord,
    *,
    timeout_seconds: float = 60.0,
) -> QuestionResult:
    """Real RAG-backed evaluation. Calls the deterministic RAG
    orchestrator (same one the /api/v1/query endpoint uses) and
    applies the graduated §04i validators against the response.

    Args:
        conn: asyncpg connection (passed for parity with other evaluators;
            real_rag_v1 uses its own dedicated pool from AgentDeps).
        question: golden_questions row.
        timeout_seconds: per-question timeout (RAG involves multiple
            tool calls + LLM, so this is higher than real_llm_v1's 30s).

    Returns:
        QuestionResult with refusal-correctness grading + real
        retrieval + LLM trace in actual_payload.
    """
    t_start = time.monotonic()
    actual_payload: dict[str, Any] = {
        "evaluator": "real_rag_v1",
        "doc_phase": 168,
        # Doc-phases 159+163+165+166+167+168 — full §04i 6-layer chain.
        "validators_applied": [
            "6_refusal",
            "2_citation_presence",
            "5_chunk_provenance",
            "4_entity_resolution",
            "3_numeric_claims",
            "1_retrieval_quality",
        ],
    }

    try:
        deps = await _get_or_build_deps()
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        log.warning(
            "real_rag_evaluator.deps_build_failed question_id=%s err=%s",
            question.question_id, e,
        )
        actual_payload["error"] = f"deps_build_failed: {type(e).__name__}: {e}"
        return QuestionResult(
            passed=False,
            actual_payload=actual_payload,
            failure_layer="evaluator_not_ready",
            failure_detail=str(e)[:200],
            latency_ms=elapsed_ms,
            tokens_used=0,
        )

    # Local import — defer the orchestrator's heavy import graph.
    from app.agent.orchestrator import run_deterministic_rag

    try:
        response = await asyncio.wait_for(
            run_deterministic_rag(question.question_text, deps),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        log.warning(
            "real_rag_evaluator.timeout question_id=%s after %.1fs",
            question.question_id, timeout_seconds,
        )
        actual_payload["error"] = f"rag_timeout_after_{timeout_seconds}s"
        return QuestionResult(
            passed=False,
            actual_payload=actual_payload,
            failure_layer="evaluator_not_ready",
            failure_detail=f"RAG timeout after {timeout_seconds}s",
            latency_ms=elapsed_ms,
            tokens_used=0,
        )
    except (httpx.HTTPError, Exception) as e:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        log.warning(
            "real_rag_evaluator.rag_call_failed question_id=%s err=%s",
            question.question_id, e,
        )
        actual_payload["error"] = f"rag_call_failed: {type(e).__name__}"
        return QuestionResult(
            passed=False,
            actual_payload=actual_payload,
            failure_layer="evaluator_not_ready",
            failure_detail=str(e)[:200],
            latency_ms=elapsed_ms,
            tokens_used=0,
        )

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    # Doc-phases 159+163+165+166+167+168 — full §04i 6-layer chain.
    # AND semantics: response must pass every graduated validator.
    from app.services.eval.validators import (
        chain_validators,
        validate_chunk_provenance,
        validate_citation_presence,
        validate_entity_resolution,
        validate_numeric_claims,
        validate_refusal_correctness,
        validate_retrieval_quality,
    )

    response_text = response.text or ""
    outcomes = [
        validate_refusal_correctness(
            response_text=response_text, question=question,
        ),
        validate_citation_presence(
            citations=response.citations, question=question,
        ),
        await validate_chunk_provenance(
            citations=response.citations,
            qdrant_client=deps.qdrant_client,
            question=question,
        ),
        validate_entity_resolution(
            response_text=response_text, question=question,
        ),
        validate_numeric_claims(
            response_text=response_text, question=question,
        ),
        validate_retrieval_quality(
            citations=response.citations, question=question,
        ),
    ]
    all_passed, failure_layer, failure_detail = chain_validators(outcomes)

    # Surface every validator's outcome in actual_payload for debugging.
    actual_payload["response_text"] = response_text[:1000]
    actual_payload["citation_count"] = len(response.citations)
    actual_payload["confidence"] = response.confidence
    actual_payload["sources_used_count"] = len(response.sources_used)
    actual_payload["validator_outcomes"] = [
        {
            "layer": o.layer,
            "passed": o.passed,
            "detail": o.detail,
            "failure_message": o.failure_message,
        }
        for o in outcomes
    ]

    log.info(
        "real_rag_evaluator.completed question_id=%s passed=%s "
        "citations=%d confidence=%.2f latency_ms=%d failure_layer=%s",
        question.question_id, all_passed,
        len(response.citations), response.confidence, elapsed_ms,
        failure_layer,
    )

    return QuestionResult(
        passed=all_passed,
        actual_payload=actual_payload,
        failure_layer=failure_layer,
        failure_detail=failure_detail,
        latency_ms=elapsed_ms,
        tokens_used=None,  # RAG orchestrator doesn't surface token count today
    )


__all__ = [
    "evaluate_question_real_rag",
]
