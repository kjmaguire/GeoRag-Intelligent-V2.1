"""Scheduled page-image verbalization (2026-08-18).

Wraps `services.ingest.page_verbalizer.verbalize_pending_pages` in a Hatchet
workflow so descriptions get generated on a schedule instead of on the ingest
critical path. Mirrors embed_pending_passages_wf deliberately — same
per-workspace singleton concurrency, same "*" fan-out over projects, same
idempotence (a page with `verbalized_at` set is never re-described), so a
frequent cron is cheap when there is nothing pending.

Cadence is hourly, not every 10 minutes like the embed sweep. Verbalization is
not on any user-visible critical path: until a page is described it is still
retrievable by its image vector, just with placeholder text. Hourly keeps the
spend predictable at IMAGE_EMBED_PAGE_SCOPE=all, where a single 500-page
report adds 500 vision calls.

Inert until switched on: `verbalize_pending_pages` returns immediately when
IMAGE_VERBALIZATION_ENABLED is unset, so registering this workflow changes
nothing on a deployment that has not opted in.
"""

from __future__ import annotations

import logging

import asyncpg
from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy, Context
from pydantic import BaseModel, Field, model_validator

from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.services.ingest.page_verbalizer import verbalize_pending_pages

log = logging.getLogger("georag.hatchet.verbalize_page_images")


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


class VerbalizePageImagesInput(BaseModel):
    workspace_id: str = Field(
        default="",
        description=(
            'Workspace UUID. Required for a single-project run; leave empty '
            'only on the project_id="*" fan-out, which resolves the '
            "workspace per project."
        ),
    )
    # "*" fans out over every project with undescribed image passages, which
    # is what the cron sends.
    project_id: str = Field(default="*")
    max_pages: int | None = Field(default=None)

    @model_validator(mode="after")
    def _require_workspace_unless_fanout(self) -> VerbalizePageImagesInput:
        """Same REC#1 contract as the embed and enrich sweeps.

        Previously a single-project call with no workspace was accepted and
        then silently skipped by the `if not workspace_id: continue` in the
        loop below — a no-op run that reported success. Raising here makes
        the dispatcher's mistake visible at the call site instead.
        """
        if self.project_id != "*" and not self.workspace_id:
            raise ValueError(
                "workspace_id is required when project_id names a specific "
                'project; it may only be omitted on the project_id="*" '
                "fan-out, which resolves the workspace per project."
            )
        return self


class VerbalizePageImagesOutput(BaseModel):
    projects_processed: int = 0
    pages_seen: int = 0
    pages_described: int = 0
    pages_failed: int = 0
    errors: list[str] = Field(default_factory=list)


verbalize_page_images_wf = hatchet.workflow(
    name="verbalize_page_images",
    # Hourly at :20, offset from the embed sweep's :00/:10 ticks so the two
    # don't contend for the worker's slots.
    on_crons=["20 * * * *"],
    input_validator=VerbalizePageImagesInput,
    # Same rationale as embed_pending_passages_wf, including the 2026-06-01
    # null-key bug: cron runs carry no workspace_id, so without the literal
    # fallback every cron tick piles into one concurrency slot.
    #
    # 2026-08-21 — `has()` is load-bearing, and its absence is why this
    # workflow had never run once. A declarative `on_crons` trigger sends NO
    # input at all (the SDK hardcodes `cron_input=None`,
    # hatchet_sdk/runnables/workflow.py:257), so `input` is `{}` and
    # `input.workspace_id` is an ABSENT key, not an empty one. cel-go raises
    # `no such key: workspace_id`, and the engine does not queue or retry
    # that — pkg/repository/task.go:2103 turns a concurrency-expression error
    # into an initial state of FAILED, before any worker is involved. The
    # run never reaches this file, which is why nothing was in the logs.
    # The old `!= ''` guard tested for the wrong thing: empty, not missing.
    #
    # `has()` is a standard CEL macro and the engine's own test suite covers
    # exactly this shape (internal/cel/cel_test.go:40), so it compiles at
    # registration — which matters, because an expression that fails to
    # compile fails the whole PutWorkflow call and takes the worker down
    # with it. `string()` is belt-and-braces: the engine rejects a non-string
    # concurrency key (task.go:2108), so coerce rather than fail the run.
    concurrency=ConcurrencyExpression(
        expression=(
            "has(input.workspace_id) && string(input.workspace_id) != '' "
            "? string(input.workspace_id) : 'cron'"
        ),
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@verbalize_page_images_wf.task(execution_timeout="2h", schedule_timeout="2h", retries=0)
async def run(
    input: VerbalizePageImagesInput, ctx: Context
) -> VerbalizePageImagesOutput:
    from app.services.ingest import page_vision_client as vision

    out = VerbalizePageImagesOutput()

    # Cheap pre-check so a disabled deployment doesn't open a PG connection
    # every hour for nothing.
    if not vision.is_enabled():
        return out

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        if input.project_id == "*":
            rows = await conn.fetch(
                "SELECT DISTINCT r.project_id::text AS pid, "
                "       dp.workspace_id::text AS wid "
                "  FROM silver.document_passages dp "
                "  JOIN silver.reports r ON r.report_id = dp.document_id "
                " WHERE dp.modality = 'image' "
                "   AND dp.verbalized_at IS NULL "
                "   AND dp.image_object_key IS NOT NULL "
                "   AND r.project_id IS NOT NULL"
            )
            targets = [(r["wid"], r["pid"]) for r in rows]
        else:
            targets = [(input.workspace_id, input.project_id)]

        for workspace_id, project_id in targets:
            if not workspace_id:
                continue
            try:
                result = await verbalize_pending_pages(
                    conn,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    max_pages=input.max_pages,
                )
            except Exception as exc:  # noqa: BLE001
                # One project's failure must not abandon the rest of the
                # sweep — the same posture the embed sweep takes.
                log.warning(
                    "verbalize_page_images: project %s failed: %s", project_id, exc,
                )
                out.errors.append(f"{project_id}:{type(exc).__name__}")
                continue

            out.projects_processed += 1
            out.pages_seen += result.seen
            out.pages_described += result.described
            out.pages_failed += result.failed
            for err in result.errors:
                if err not in out.errors:
                    out.errors.append(err)
    finally:
        await conn.close()

    log.info(
        "verbalize_page_images.sweep projects=%d seen=%d described=%d failed=%d",
        out.projects_processed, out.pages_seen, out.pages_described, out.pages_failed,
    )
    return out
