"""Contextual retrieval enrichment (Anthropic technique).

For each passage in silver.document_passages where contextualized_content
IS NULL, calls Qwen3 to generate a 2-3 sentence context header that
situates the chunk within its source document. The enriched text
(header + original) is written back to contextualized_content.

passage_embedder.py reads COALESCE(contextualized_content, text) so
enriched passages automatically get better embeddings on next embed run.

Doc: https://www.anthropic.com/news/contextual-retrieval
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import asyncpg
import httpx

from app.db import bind_workspace_scope

log = logging.getLogger("georag.ingest.context_enricher")

_MAX_ENRICHED_LENGTH = 4096
_MAX_PASSAGES_CONTEXT = 3_000  # cap total passage text sent to LLM


def _dsn() -> str:
    return (
        f"postgres://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:5432/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


@dataclass
class ContextEnrichmentResult:
    workspace_id: str
    project_id: str | None
    passages_seen: int = 0
    passages_enriched: int = 0
    passages_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _make_enrichment_prompt(
    document_title: str,
    ordinal: int,
    total_passages: int,
    text: str,
) -> str:
    text_snippet = text[:_MAX_PASSAGES_CONTEXT]
    return (
        "You are a geology document analyst. Given a document title and a passage "
        "from that document, write a brief context description (2-3 sentences) that "
        "situates the passage within the document. Focus on: what section this likely "
        "comes from, what specific topic it covers, and why it matters to a mining geologist.\n\n"
        f"Document: {document_title}\n"
        f"Position: passage {ordinal + 1} of {total_passages}\n\n"
        f"Passage:\n{text_snippet}\n\n"
        "Context header (2-3 sentences, no bullet points, plain prose):"
    )


async def _call_vllm_for_context(
    prompt: str,
    http_client: httpx.AsyncClient,
    vllm_url: str,
    vllm_model: str,
) -> str:
    payload = {
        "model": vllm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 150,
        "stream": False,
    }
    resp = await http_client.post(
        f"{vllm_url}/chat/completions",
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _combine_enriched(context_header: str, original_text: str) -> str:
    combined = f"{context_header[:300]}\n\n{original_text}"
    return combined[:_MAX_ENRICHED_LENGTH]


async def enrich_passage_context(
    *,
    workspace_id: str,
    project_id: str | None = None,
    batch_size: int = 8,
    max_passages: int | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ContextEnrichmentResult:
    """Enrich pending passages with LLM-generated context headers."""
    from app.config import settings

    result = ContextEnrichmentResult(workspace_id=workspace_id, project_id=project_id)

    own_client = False
    if http_client is None:
        http_client = httpx.AsyncClient()
        own_client = True

    vllm_url = settings.VLLM_URL
    vllm_model = settings.VLLM_MODEL

    pg_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await bind_workspace_scope(pg_conn, workspace_id=workspace_id, site="ingest.context_enricher")

        query = (
            "SELECT dp.passage_id::text, dp.text, dp.ordinal, "
            "       COALESCE(r.title, dp.chunk_kind, 'Document') AS document_title, "
            "       COUNT(*) OVER (PARTITION BY dp.document_id) AS total_passages "
            "  FROM silver.document_passages dp "
            "  LEFT JOIN silver.reports r ON r.report_id = dp.document_id "
            " WHERE dp.contextualized_content IS NULL "
            "   AND dp.embedding_id IS NULL "
        )
        params: list = []
        if project_id:
            query += " AND r.project_id = $1::uuid "
            params.append(project_id)
        query += " ORDER BY dp.created_at ASC"
        if max_passages:
            query += f" LIMIT {int(max_passages)}"

        rows = await pg_conn.fetch(query, *params)
        result.passages_seen = len(rows)

        if not rows:
            log.info(
                "context_enricher.no_pending workspace=%s project=%s",
                workspace_id, project_id,
            )
            return result

        log.info(
            "context_enricher.start workspace=%s project=%s pending=%d",
            workspace_id, project_id, len(rows),
        )

        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            for row in batch:
                try:
                    prompt = _make_enrichment_prompt(
                        document_title=row["document_title"],
                        ordinal=row["ordinal"],
                        total_passages=row["total_passages"],
                        text=row["text"],
                    )
                    context_header = await _call_vllm_for_context(
                        prompt, http_client, vllm_url, vllm_model
                    )
                    enriched = _combine_enriched(context_header, row["text"])
                    await pg_conn.execute(
                        "UPDATE silver.document_passages "
                        "   SET contextualized_content = $1 "
                        " WHERE passage_id = $2::uuid",
                        enriched,
                        row["passage_id"],
                    )
                    result.passages_enriched += 1
                except Exception as exc:
                    result.passages_skipped += 1
                    result.errors.append(
                        f"passage={row['passage_id'][:8]}:{type(exc).__name__}:{exc}"
                    )
                    log.warning(
                        "context_enricher.passage_failed pid=%s err=%s",
                        row["passage_id"][:8], exc,
                    )

    finally:
        await pg_conn.close()
        if own_client:
            await http_client.aclose()

    log.info(
        "context_enricher.complete enriched=%d skipped=%d errors=%d",
        result.passages_enriched, result.passages_skipped, len(result.errors),
    )
    return result


__all__ = ["enrich_passage_context", "ContextEnrichmentResult"]
