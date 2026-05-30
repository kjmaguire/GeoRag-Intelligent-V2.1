"""enrich_passage_context Hatchet workflow — contextual retrieval enrichment.

Runs daily at 04:30 UTC (before embed_pending_passages at 05:45 UTC)
to generate LLM context headers for un-enriched passages.

Contextual retrieval: Anthropic technique. Each chunk gets a 2-3 sentence
context header summarising its place in the source document. The enriched
text (header + original) is stored in contextualized_content and used by
passage_embedder.py in place of the raw text.
"""
from __future__ import annotations

import logging
import os

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.ingest.context_enricher import enrich_passage_context

log = logging.getLogger("georag.hatchet.enrich_passage_context")


class EnrichPassageContextInput(BaseModel):
    workspace_id: str = Field(
        default="a0000000-0000-0000-0000-000000000001",
    )
    project_id: str = Field(default="*")
    batch_size: int = Field(default=8)
    max_passages: int | None = Field(default=None)


class EnrichPassageContextOutput(BaseModel):
    projects_processed: int = 0
    total_enriched: int = 0
    total_skipped: int = 0
    errors: list[str] = Field(default_factory=list)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


enrich_passage_context_wf = hatchet.workflow(
    name="enrich_passage_context",
    on_crons=["30 4 * * *"],
    input_validator=EnrichPassageContextInput,
    concurrency=ConcurrencyExpression(
        expression="input.workspace_id",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@enrich_passage_context_wf.task(execution_timeout="3h", schedule_timeout="3h", retries=0)
async def run(
    input: EnrichPassageContextInput, ctx: Context
) -> EnrichPassageContextOutput:
    if input.project_id == "*":
        conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT r.project_id::text AS pid "
                "  FROM silver.document_passages dp "
                "  JOIN silver.reports r ON r.report_id = dp.document_id "
                " WHERE dp.contextualized_content IS NULL AND r.project_id IS NOT NULL"
            )
            project_ids = [r["pid"] for r in rows]
        finally:
            await conn.close()
    else:
        project_ids = [input.project_id]

    log.info("enrich_passage_context.start projects=%d", len(project_ids))

    total_enriched = 0
    total_skipped = 0
    errors: list[str] = []

    for pid in project_ids:
        try:
            r = await enrich_passage_context(
                workspace_id=input.workspace_id,
                project_id=pid,
                batch_size=input.batch_size,
                max_passages=input.max_passages,
            )
            total_enriched += r.passages_enriched
            total_skipped += r.passages_skipped
            errors.extend(r.errors)
        except Exception as e:
            errors.append(f"project={pid}:{type(e).__name__}:{e}")
            log.warning(
                "enrich_passage_context.project_failed pid=%s err=%s", pid, e
            )

    log.info(
        "enrich_passage_context.complete projects=%d enriched=%d skipped=%d errors=%d",
        len(project_ids), total_enriched, total_skipped, len(errors),
    )

    return EnrichPassageContextOutput(
        projects_processed=len(project_ids),
        total_enriched=total_enriched,
        total_skipped=total_skipped,
        errors=errors,
    )


__all__ = [
    "enrich_passage_context_wf",
    "EnrichPassageContextInput",
    "EnrichPassageContextOutput",
]
