"""score_answer_quality Hatchet workflow — nightly answer quality scoring.

Runs at 06:00 UTC daily. Fetches recent audit.query_audit_log rows where
faithfulness_score IS NULL and sources_used IS NOT NULL, calls Qwen3 to
score faithfulness + context precision, and writes the scores back.

This is a catch-up path. In-request scoring can be added later.
"""
from __future__ import annotations

import json
import logging
import os

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.eval.answer_quality import score_answer_quality

log = logging.getLogger("georag.hatchet.score_answer_quality")


class ScoreAnswerQualityInput(BaseModel):
    """Input for the daily 06:00 UTC quality-scoring cron.

    `workspace_id` defaults to None which means "all workspaces" —
    every unscored row across the cluster. The previous default
    (hardcoded `a0000000-...001` default-tenant UUID) silently
    excluded every non-default tenant from scoring, so any non-
    default workspace's `audit.query_audit_log` rows accumulated
    forever with `faithfulness_score IS NULL`. 2026-06-03 audit
    pass — see AUDIT_AND_FIX_REPORT.md Theme H spinoffs.

    When invoked manually with an explicit workspace_id, scopes to
    that workspace.
    """

    workspace_id: str | None = Field(default=None)
    batch_size: int = Field(default=20)
    max_age_hours: int = Field(default=24)


class ScoreAnswerQualityOutput(BaseModel):
    rows_scored: int = 0
    rows_skipped: int = 0
    errors: list[str] = Field(default_factory=list)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


score_answer_quality_wf = hatchet.workflow(
    name="score_answer_quality",
    on_crons=["0 6 * * *"],
    input_validator=ScoreAnswerQualityInput,
)


@score_answer_quality_wf.task(execution_timeout="1h", schedule_timeout="1h", retries=0)
async def run(
    input: ScoreAnswerQualityInput, ctx: Context
) -> ScoreAnswerQualityOutput:
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    rows_scored = 0
    rows_skipped = 0
    errors: list[str] = []

    try:
        # workspace_id is optional — None means "all workspaces".
        # When set, scoped to a single workspace (manual reruns,
        # backfills). Conditional WHERE keeps the index path tight.
        if input.workspace_id is None:
            rows = await conn.fetch(
                """
                SELECT audit_id::text,
                       sources_used
                FROM audit.query_audit_log
                WHERE faithfulness_score IS NULL
                  AND sources_used IS NOT NULL
                  AND created_at > NOW() - ($1 || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT $2
                """,
                str(input.max_age_hours),
                input.batch_size,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT audit_id::text,
                       sources_used
                FROM audit.query_audit_log
                WHERE faithfulness_score IS NULL
                  AND sources_used IS NOT NULL
                  AND workspace_id = $1::uuid
                  AND created_at > NOW() - ($2 || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT $3
                """,
                input.workspace_id,
                str(input.max_age_hours),
                input.batch_size,
            )
        log.info("score_answer_quality.start rows=%d", len(rows))

        for row in rows:
            try:
                sources = row["sources_used"]
                if isinstance(sources, str):
                    sources = json.loads(sources)
                if not isinstance(sources, list):
                    rows_skipped += 1
                    continue

                # Extract passage texts from sources_used
                passages = []
                for src in sources:
                    if isinstance(src, dict):
                        text = src.get("text") or src.get("content") or src.get("passage") or ""
                        if text:
                            passages.append(str(text)[:500])

                if not passages:
                    rows_skipped += 1
                    continue

                # Score (question and answer not available here — use passages only)
                scores = await score_answer_quality(
                    question="(from audit log — question encrypted)",
                    passages=passages,
                    answer="(from audit log — answer encrypted)",
                )

                await conn.execute(
                    "UPDATE audit.query_audit_log "
                    "   SET faithfulness_score = $1, "
                    "       context_precision_score = $2 "
                    " WHERE audit_id = $3::uuid",
                    scores.faithfulness_score,
                    scores.context_precision_score,
                    row["audit_id"],
                )
                rows_scored += 1

            except Exception as exc:
                rows_skipped += 1
                errors.append(f"audit_id={row['audit_id'][:8]}:{type(exc).__name__}:{exc}")
                log.warning(
                    "score_answer_quality.row_failed id=%s err=%s",
                    row["audit_id"][:8],
                    exc,
                )

    finally:
        await conn.close()

    log.info(
        "score_answer_quality.complete scored=%d skipped=%d errors=%d",
        rows_scored,
        rows_skipped,
        len(errors),
    )

    return ScoreAnswerQualityOutput(
        rows_scored=rows_scored,
        rows_skipped=rows_skipped,
        errors=errors,
    )


__all__ = [
    "score_answer_quality_wf",
    "ScoreAnswerQualityInput",
    "ScoreAnswerQualityOutput",
]
