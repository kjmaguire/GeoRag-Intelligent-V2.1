"""§6.3 — NRCan / GEO.ca Hatchet pull cron.

Schedule: ``0 7 1 * *`` UTC (first of month at 07:00 UTC, one hour
after the BC MINFILE pull so the two crons don't compete for the
same ArcGIS REST connection pool).

Twin of ``bc_minfile_pull`` — same audit-anchored graceful-failure
contract, same source-registry-driven URL resolution. NRCan endpoints
served via the Federal-government ArcGIS REST stack
(maps.canada.ca, atlas.gc.ca, geo.ca).

Sources walked
==============

  - nrcan_canadian_mines       → pg_mine
  - nrcan_geo_bedrock_geology  → pg_bedrock_geology

Operators update ``public_geo.sources.service_url`` when the
upstream service moves; the cron picks up the new URL on the next
firing without any redeploy.

Why a separate workflow from bc_minfile_pull?
=============================================

The two crons have different cadences in the registry (NRCan = annual
for bedrock, quarterly for mines; BC = monthly). Splitting them per
jurisdiction keeps the Hatchet schedule explicit and the per-source
audit grouping clean for operators reading the audit explorer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

# Reuse the BC MINFILE helpers — the per-page ArcGIS REST walk +
# source-registry lookup + failure-mode taxonomy are jurisdiction-
# agnostic. Re-exposing under stable names keeps tests and acceptance
# harnesses portable across both workflows.
from app.hatchet_workflows.bc_minfile_pull import (
    SourcePullResult,
    _build_dsn,
    _load_source,
    _pull_one_source,
)

log = logging.getLogger("georag.hatchet.nrcan_geo_pull")


class NrcanGeoPullInput(BaseModel):
    source_ids: list[str] = Field(
        default_factory=lambda: [
            "nrcan_canadian_mines",
            "nrcan_geo_bedrock_geology",
        ],
        description="Which nrcan_* sources to pull. Empty = walk every "
                    "nrcan_* row in public_geo.sources.",
    )
    page_size: int = Field(
        default=1000, ge=100, le=10_000,
        description="Records per ArcGIS page. Service may cap lower.",
    )


class NrcanGeoPullOutput(BaseModel):
    sources_attempted: int
    sources_succeeded: int
    sources_failed: int
    per_source: list[SourcePullResult]
    sampled_at: datetime


nrcan_geo_pull = hatchet.workflow(
    name="nrcan_geo_pull",
    on_crons=["0 7 1 * *"],
    input_validator=NrcanGeoPullInput,
)


@nrcan_geo_pull.task(execution_timeout="60m")
async def run_pull(input: NrcanGeoPullInput, ctx: Context) -> NrcanGeoPullOutput:
    sampled_at = datetime.now(tz=UTC)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        if input.source_ids:
            target_ids = input.source_ids
        else:
            rows = await conn.fetch(
                "SELECT source_id FROM public_geo.sources "
                "WHERE source_id LIKE 'nrcan_%' ORDER BY source_id"
            )
            target_ids = [r["source_id"] for r in rows]

        per_source: list[SourcePullResult] = []
        for source_id in target_ids:
            src = await _load_source(conn, source_id)
            if src is None:
                per_source.append(SourcePullResult(
                    source_id=source_id,
                    outcome="not_registered",
                    detail="source_id not present in public_geo.sources",
                ))
                continue

            result = await _pull_one_source(conn, src, input.page_size)
            per_source.append(result)

            if result.outcome == "completed":
                await conn.execute(
                    "UPDATE public_geo.sources "
                    "SET last_refreshed_at = now() WHERE source_id = $1",
                    source_id,
                )

            await emit_audit(
                conn,
                action_type=f"public_geo.pull.nrcan_geo.{result.outcome}",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="public_geo",
                target_table="sources",
                target_id=source_id,
                payload={
                    "source_id":        source_id,
                    "jurisdiction":     src.get("jurisdiction_code"),
                    "service_url":      src.get("service_url"),
                    "license_url":      src.get("license_url"),
                    "license_summary":  src.get("license_summary"),
                    "feature_count":    result.feature_count,
                    "pages_fetched":    result.pages_fetched,
                    "duration_s":       result.duration_s,
                    "detail":           result.detail,
                },
            )

        succ = sum(1 for r in per_source if r.outcome == "completed")
        fail = len(per_source) - succ
        log.info(
            "nrcan_geo_pull: attempted=%d succ=%d fail=%d",
            len(per_source), succ, fail,
        )
        return NrcanGeoPullOutput(
            sources_attempted=len(per_source),
            sources_succeeded=succ,
            sources_failed=fail,
            per_source=per_source,
            sampled_at=sampled_at,
        )
    finally:
        await conn.close()


__all__ = [
    "nrcan_geo_pull",
    "NrcanGeoPullInput",
    "NrcanGeoPullOutput",
]
