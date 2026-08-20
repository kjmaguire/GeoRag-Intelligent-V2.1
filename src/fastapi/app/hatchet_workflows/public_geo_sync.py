"""Refresh public_geo.* from the surveys' live ArcGIS services.

This is the scheduler for ``app.services.public_geo.sync``. The sync logic
itself lives there and is deliberately transport-agnostic (it takes a
connection and returns stats) so it can be run from a test, a one-off script
or this cron without three copies of the mapping rules.

Why this workflow exists
------------------------
Public geoscience is a synced mirror of what provincial and federal surveys
publish. Two previous owners of that refresh are gone:

  * ``bc_minfile_pull`` (§6.2) and ``nrcan_geo_pull`` (§6.3) were retired on
    2026-05-25 in favour of a Dagster Bronze→Silver pipeline.
  * That Dagster pipeline has been dormant since 2026-07-28.

Between them the tables had no writer at all, which is why the local copy sat
three weeks stale and Azure never received a single row. Nothing was broken in
the readers — the eight citation resolvers and the Lakehouse/map controllers
read ``public_geo.*`` correctly. There was simply nothing filling it.

Not to be confused with ``public_geoscience_pull``, which is still registered
beside this one. That workflow never calls a survey: it is a webhook target
that validates a GeoJSON blob someone else already dropped in object storage
and registers it in ``bronze.provenance``. Its caller was Kestra, retired
2026-07-25, so nothing triggers it today, and it writes no canonical rows in
any case.

Schedule
--------
``30 3 * * 0`` — 03:30 UTC on Sundays. These are government feeds that change
on a monthly-to-quarterly cadence, so nightly would be pure waste against
someone else's infrastructure; weekly keeps the mirror honest without the
politeness problem. The slot sits after the backup window (02:00-03:00) and
before pg_partman_maintenance (04:15), so a multi-hour full pull does not
contend with the other nightly writers.

Failure behaviour
-----------------
Nothing here fails the run on one bad feed. ``sync_all`` catches per-source,
and the per-feature loop inside ``sync_source`` catches per-row, because a
survey going offline or publishing one malformed polygon must not stop the
other twenty-eight feeds from refreshing. What it does instead is *report*:
the returned stats carry per-source counts, unmapped status values and
truncation counts, and this workflow copies that verbatim into an audit row.
A run that fetched nothing is visible as fetched=0 rather than as a green
tick.
"""

from __future__ import annotations

import logging
import time as _t
from datetime import UTC, datetime
from typing import Any

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import _progress, hatchet
from app.services.public_geo.sync import sync_all

log = logging.getLogger("georag.hatchet.public_geo_sync")


class PublicGeoSyncInput(BaseModel):
    """Optional narrowing — left empty for the cron path, which syncs all."""

    canonical_types: list[str] | None = Field(
        default=None,
        description="Restrict to these canonical types (mine, rock_sample, …).",
    )
    jurisdiction_codes: list[str] | None = Field(
        default=None,
        description="Restrict to these jurisdictions (CA-SK, CA-BC, …).",
    )
    source_ids: list[str] | None = Field(
        default=None,
        description="Restrict to these registry source_ids — the re-run-one-feed path.",
    )
    max_features_per_source: int | None = Field(
        default=None,
        description="Cap features per feed. For smoke-testing a schema change "
                    "against the live services without pulling half a million rows.",
    )


class PublicGeoSyncOut(BaseModel):
    feeds: int
    fetched: int
    upserted: int
    errors: int
    skipped: list[str]
    per_source: list[dict[str, Any]]
    duration_ms: int
    synced_at: str  # ISO-8601 UTC


public_geo_sync = hatchet.workflow(
    name="public_geo_sync",
    on_crons=["30 3 * * 0"],  # 03:30 UTC Sundays — see module docstring
    input_validator=PublicGeoSyncInput,
)


@public_geo_sync.task(execution_timeout="4h", retries=1)
async def run_public_geo_sync(
    input: PublicGeoSyncInput, ctx: Context,
) -> PublicGeoSyncOut:
    """Pull every queryable feed and upsert it into its canonical table."""
    t0 = _t.monotonic()
    pool = await _progress.get_pool()

    # One connection held for the whole run rather than a pool round-trip per
    # feature: this is a single sequential writer and re-acquiring per row
    # would dominate the runtime.
    #
    # Tenancy note: public_geo is cross-tenant reference data with no
    # workspace_id, so no workspace GUC is bound. The pool connects as the
    # table-owning role.
    async with pool.acquire() as conn:
        result = await sync_all(
            conn,
            canonical_types=input.canonical_types,
            jurisdiction_codes=input.jurisdiction_codes,
            source_ids=input.source_ids,
            max_features_per_source=input.max_features_per_source,
        )

    duration_ms = int((_t.monotonic() - t0) * 1000)
    synced_at = datetime.now(UTC)

    try:
        await emit_audit(
            pool,
            action_type="public_geo.sync.complete",
            actor_kind="workflow",
            target_schema="public_geo",
            target_table=None,
            target_id=None,
            payload={
                "feeds": result["feeds"],
                "fetched": result["fetched"],
                "upserted": result["upserted"],
                "errors": result["errors"],
                "skipped": result["skipped"],
                # Per-source detail is the point of the audit row: a feed that
                # quietly returns zero is only visible here.
                "per_source": result["per_source"],
                "duration_ms": duration_ms,
                "synced_at": synced_at.isoformat(),
            },
        )
    except Exception:  # pragma: no cover — never fail the sync on audit-write
        log.exception("emit_audit failed (the sync itself succeeded)")

    out = PublicGeoSyncOut(
        feeds=result["feeds"],
        fetched=result["fetched"],
        upserted=result["upserted"],
        errors=result["errors"],
        skipped=result["skipped"],
        per_source=result["per_source"],
        duration_ms=duration_ms,
        synced_at=synced_at.isoformat(),
    )
    log.info(
        "public_geo_sync complete: feeds=%d fetched=%d upserted=%d errors=%d in %dms",
        out.feeds, out.fetched, out.upserted, out.errors, out.duration_ms,
    )
    return out


__all__ = ["public_geo_sync", "PublicGeoSyncInput", "PublicGeoSyncOut"]
