"""enrich_passage_context Hatchet workflow — contextual retrieval enrichment.

COST NOTE (2026-08-21): until today this workflow had never enriched a
passage — see context_enricher's module docstring for the three stacked
reasons. Now that it can, it spends one LLM generation per passage, so
`max_passages` carries a deliberately conservative default (2,000 per
project per run) instead of the previous `None`. A full historical
backfill of an existing corpus is a real, sizeable spend and is an
operator decision, not something a cron should start on its own: raise
the cap, or trigger the workflow directly with `max_passages: null`,
once someone has agreed to the bill.

Runs daily at 04:30 UTC (before embed_pending_passages at 05:45 UTC)
to generate LLM context headers for un-enriched passages.

Contextual retrieval: Anthropic technique. Each chunk gets a 2-3 sentence
context header summarising its place in the source document. The enriched
text (header + original) is stored in contextualized_content and used by
passage_embedder.py in place of the raw text.
"""
from __future__ import annotations

import logging

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field, model_validator

from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.services.ingest.context_enricher import enrich_passage_context

log = logging.getLogger("georag.hatchet.enrich_passage_context")


class EnrichPassageContextInput(BaseModel):
    # REC#1 (2026-06-03) made this REQUIRED, so that a dispatcher which
    # forgot to thread the workspace through could not silently scope its
    # work to the default tenant.
    #
    # 2026-08-21 — that is still the rule for the single-project path, but a
    # required field cannot be expressed as a cron payload: a declarative
    # `on_crons` trigger sends NO input (hatchet_sdk hardcodes
    # `cron_input=None`), so `{}` is all the validator ever sees and every
    # cron run died on ValidationError before it could do anything.
    #
    # The default is "" rather than a bootstrap tenant UUID precisely so it
    # cannot become a silent default-tenant scope: the ONLY payload that
    # tolerates an empty workspace_id is the project_id="*" fan-out, which
    # derives the real workspace per project from the passage rows. Anything
    # naming a specific project is rejected at construction time by
    # _require_workspace_unless_fanout below.
    workspace_id: str = Field(
        default="",
        description=(
            'Workspace UUID. Required for a single-project run; leave empty '
            'only on the project_id="*" fan-out, which resolves the '
            'workspace per project.'
        ),
    )
    project_id: str = Field(default="*")
    batch_size: int = Field(
        default=8,
        description=(
            "Header generations issued concurrently. The service used to "
            "iterate a batch one passage at a time, which made this "
            "decorative; it is real concurrency now."
        ),
    )
    max_passages: int | None = Field(
        default=2_000,
        description=(
            "Per-project cap for one run. None = no limit."
        ),
    )

    @model_validator(mode="after")
    def _require_workspace_unless_fanout(self) -> EnrichPassageContextInput:
        """REC#1, expressed as a rule instead of a required field.

        Same contract as EmbedPendingPassagesInput: naming a specific
        project without a workspace raises at construction time, so a
        dispatcher still cannot omit it and land on a default tenant. Only
        the project_id="*" fan-out may omit it, and that path resolves the
        workspace per project — which is also the only shape a cron can
        send, since `on_crons` carries no input at all.
        """
        if self.project_id != "*" and not self.workspace_id:
            raise ValueError(
                "workspace_id is required when project_id names a specific "
                'project; it may only be omitted on the project_id="*" '
                "fan-out, which resolves the workspace per project."
            )
        return self


class EnrichPassageContextOutput(BaseModel):
    projects_processed: int = 0
    total_enriched: int = 0
    total_skipped: int = 0
    errors: list[str] = Field(default_factory=list)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


enrich_passage_context_wf = hatchet.workflow(
    name="enrich_passage_context",
    # 14:45 UTC.
    #
    # This has now been wrong twice, in opposite directions, and the
    # reasoning is worth keeping because the ground keeps moving:
    #
    #   04:30  inside the ORIGINAL 00:00-10:00 UTC shutdown window, so it
    #          could not connect even after the selection bug was fixed.
    #   10:30  correct for that window — "after the server is back, before
    #          the backups". On 2026-08-21 the window moved to Pacific
    #          (06:00/07:00-13:00/14:00 UTC) and 10:30 became the middle
    #          of it.
    #   14:45  after the LATER of the two startup candidate hours, which
    #          is the only time a schedule can be sure of without knowing
    #          which side of a DST boundary it will run on.
    #
    # Running after embed_pending_passages' 05:45 daily tick rather than
    # before it is fine: embed also runs */10, so enrichment written at
    # 14:45 is picked up within ten minutes rather than waiting a day.
    #
    # tests/test_crons_avoid_the_shutdown_window.py reads the window from
    # the job YAML, so the next move is caught rather than reasoned about.
    on_crons=["45 14 * * *"],
    input_validator=EnrichPassageContextInput,
    # 2026-08-21 — see verbalize_page_images.py for the full write-up. Short
    # version: a cron trigger sends no input, so `input.workspace_id` is an
    # ABSENT key; cel-go raises `no such key` and the engine records the run
    # as FAILED before dispatch (pkg/repository/task.go:2103). That is why
    # this workflow had not run once in the retained log window. `has()`
    # makes the lookup absence-safe; 'cron' is the group for fan-out runs,
    # which are system-wide rather than per-workspace anyway.
    concurrency=ConcurrencyExpression(
        expression=(
            "has(input.workspace_id) && string(input.workspace_id) != '' "
            "? string(input.workspace_id) : 'cron'"
        ),
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@enrich_passage_context_wf.task(execution_timeout="3h", schedule_timeout="3h", retries=0)
async def run(
    input: EnrichPassageContextInput, ctx: Context
) -> EnrichPassageContextOutput:
    # targets is (workspace_id, project_id). The workspace is carried
    # alongside the project rather than taken from the input, because the
    # fan-out spans every workspace: enriching project P under some other
    # workspace's id binds the wrong RLS scope and quietly enriches nothing.
    # Same shape as the verbalize_page_images sweep. (Before 2026-08-21 this
    # passed input.workspace_id for every project — latent, because the cron
    # that would have exposed it had never fired.)
    if input.project_id == "*":
        conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT r.project_id::text AS pid, "
                "       dp.workspace_id::text AS wid "
                "  FROM silver.document_passages dp "
                "  JOIN silver.reports r ON r.report_id = dp.document_id "
                " WHERE dp.contextualized_content IS NULL AND r.project_id IS NOT NULL"
            )
            targets = [(r["wid"], r["pid"]) for r in rows if r["wid"]]
        finally:
            await conn.close()
    else:
        # workspace_id is guaranteed non-empty here — the input model's
        # _require_workspace_unless_fanout validator rejects a specific
        # project without one before this task ever runs.
        targets = [(input.workspace_id, input.project_id)]

    project_ids = [pid for _, pid in targets]
    log.info("enrich_passage_context.start projects=%d", len(project_ids))

    total_enriched = 0
    total_skipped = 0
    errors: list[str] = []

    for wid, pid in targets:
        try:
            r = await enrich_passage_context(
                workspace_id=wid,
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
