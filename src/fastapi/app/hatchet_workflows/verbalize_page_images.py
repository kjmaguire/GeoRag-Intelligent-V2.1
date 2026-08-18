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
import os

import asyncpg
from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy, Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.ingest.page_verbalizer import verbalize_pending_pages

log = logging.getLogger("georag.hatchet.verbalize_page_images")


def _dsn() -> str:
    return (
        f"postgres://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:5432/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


class VerbalizePageImagesInput(BaseModel):
    workspace_id: str = Field(default="")
    # "*" fans out over every project with undescribed image passages, which
    # is what the cron sends.
    project_id: str = Field(default="*")
    max_pages: int | None = Field(default=None)


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
    # fallback every cron tick piles into one null concurrency slot and
    # blocks forever.
    concurrency=ConcurrencyExpression(
        expression="input.workspace_id != '' ? input.workspace_id : 'cron'",
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
