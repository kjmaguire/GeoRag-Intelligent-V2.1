"""Ingest LAS well logs into silver.well_log_curves.

LAS (Log ASCII Standard) is how every downhole geophysical tool delivers its
data — gamma, resistivity, density, sonic — and it is the last of the four
formats the 2026-07-28 Dagster removal left without a consumer. The parser
survived and is tested; what it lacked was a caller.

Shape, and why it is not the tabular workflow
---------------------------------------------
A LAS file is not rows. It is a set of CURVES sampled down one hole: each
curve is a named channel with its own unit, null sentinel and depth/value
arrays. ``silver.well_log_curves`` stores it that way too — one row per
curve, with ``depths`` and ``values`` as arrays rather than a row per
sample. A 3,000 m hole logged every 15 cm is 20,000 samples per curve and
a dozen curves; storing that as 240,000 rows would be the wrong shape for
both the table and the reader.

That difference is why this is its own workflow rather than a branch inside
``ingest_tabular`` — the parse, the target and the row granularity all
differ. What it does share is the hole: curves attach to a collar, resolved
the same way, so a LAS file for a hole nobody uploaded is reported rather
than dropped.

Re-ingest replaces
------------------
``well_log_curves`` has no natural key, so a re-upload would append a second
copy of every curve. Same reasoning as the interval tables in
``ingest_tabular``: duplicated curves are silent and corrupt exactly what
gets read, so an upload replaces the curves recorded for the holes it
mentions and reports how many it replaced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time as _t
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.hatchet_workflows import _progress, hatchet
from app.hatchet_workflows.ingest_tabular import _collar_index, _resolve_collar

log = logging.getLogger("georag.hatchet.ingest_well_logs")

SUPPORTED_EXTENSIONS = frozenset({".las"})

#: A curve with no samples carries no information and would fail the
#: NOT NULL on sample_count/min_depth/max_depth anyway.
_MIN_SAMPLES = 1


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


class IngestWellLogsInput(BaseModel):
    workspace_id: str
    project_id: str
    minio_key: str
    run_id: str | None = None
    #: Which hole these curves belong to. LAS well names are free text and
    #: frequently do not match the collar file's hole_id, so the caller can
    #: say outright rather than relying on the ~W section matching.
    hole_id: str | None = Field(default=None)

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)
        return v


class IngestWellLogsOut(BaseModel):
    run_id: str | None
    well_name: str | None
    hole_id: str | None
    curves_written: int = 0
    curves_replaced: int = 0
    curves_skipped: int = 0
    total_curves_in_file: int = 0
    #: True when no collar matched, so nothing could be attached.
    orphaned: bool = False
    las_version: str | None = None
    parse_quality_pct: float | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


_CURVE_SQL = """
INSERT INTO silver.well_log_curves (
    curve_id, workspace_id, collar_id, curve_name, curve_unit,
    curve_description, min_depth, max_depth, step, null_value,
    sample_count, las_version, source_file, depths, values,
    created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4,
    $5, $6, $7, $8, $9,
    $10, $11, $12, $13::double precision[], $14::double precision[],
    NOW(), NOW()
)
"""


ingest_well_logs = hatchet.workflow(
    name="ingest_well_logs",
    input_validator=IngestWellLogsInput,
)


@ingest_well_logs.task(execution_timeout="1h", retries=1)
async def run_ingest_well_logs(
    input: IngestWellLogsInput, ctx: Context,
) -> IngestWellLogsOut:
    """Download one LAS file, parse its curves and attach them to a collar."""
    from georag_geoparsers.las_parser import parse_las_file  # noqa: PLC0415

    t0 = _t.monotonic()
    store = get_storage_client()
    filename = input.minio_key.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"ingest_well_logs cannot handle {suffix!r} ({filename}); "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    run_id = input.run_id or await _progress.start_run(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
    )

    warnings: list[dict[str, Any]] = []
    written = replaced = skipped = 0
    orphaned = False
    result: Any = None
    hole_id = input.hole_id

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_las_") as tmpdir:
            local = str(Path(tmpdir) / filename)
            await asyncio.to_thread(
                store.get_file, Bucket.BRONZE, input.minio_key, local,
            )

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="parse")

            result = await asyncio.to_thread(parse_las_file, local)
            skipped = result.skipped_curves
            # The caller's hole_id wins over the file's ~W well name: LAS
            # well names are free text ("EAGLE PT #1") and rarely match the
            # collar file's identifier.
            hole_id = input.hole_id or result.well_name

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="persist")

            conn = await asyncpg.connect(_build_dsn())
            try:
                await bind_workspace_scope(
                    conn,
                    workspace_id=input.workspace_id,
                    site="hatchet.ingest_well_logs",
                    is_local=False,
                )

                index = await _collar_index(conn, input.project_id)
                collar_id = _resolve_collar(index, hole_id)

                if collar_id is None:
                    # Curves cannot exist without a hole — collar_id is NOT
                    # NULL and a log with no hole is not interpretable. Report
                    # it as orphaned rather than inventing a collar.
                    orphaned = True
                    warnings.append({
                        "code": "no_matching_collar",
                        "detail": (
                            f"No collar in this project matches hole_id "
                            f"{hole_id!r}. Upload the collar file first, or "
                            f"pass hole_id explicitly — LAS well names are "
                            f"free text and often differ from the collar id."
                        ),
                    })
                else:
                    usable = [
                        c for c in result.curves
                        if c.sample_count >= _MIN_SAMPLES
                        and c.name != result.depth_curve_name
                    ]

                    async with conn.transaction():
                        replaced = int(
                            await conn.fetchval(
                                "WITH d AS (DELETE FROM silver.well_log_curves "
                                "WHERE collar_id = $1::uuid RETURNING 1) "
                                "SELECT count(*) FROM d",
                                collar_id,
                            ) or 0
                        )
                        for c in usable:
                            await conn.execute(
                                _CURVE_SQL,
                                input.workspace_id, collar_id,
                                c.name, c.unit, c.description,
                                float(c.min_depth), float(c.max_depth),
                                float(c.step) if c.step is not None else None,
                                float(c.null_value), int(c.sample_count),
                                result.las_version, filename,
                                [float(d) for d in c.depths],
                                [float(v) for v in c.values],
                            )
                            written += 1
            finally:
                await conn.close()

        if run_id:
            await _progress.mark_completed_by_run(run_id=run_id)

    except Exception as exc:
        if run_id:
            await _progress.mark_failed_by_run(
                run_id=run_id, error_text=str(exc)[:1000],
            )
        log.exception("ingest_well_logs failed for %s", input.minio_key)
        raise

    out = IngestWellLogsOut(
        run_id=run_id,
        well_name=getattr(result, "well_name", None),
        hole_id=hole_id,
        curves_written=written,
        curves_replaced=replaced,
        curves_skipped=skipped,
        total_curves_in_file=getattr(result, "total_curves_in_file", 0),
        orphaned=orphaned,
        las_version=getattr(result, "las_version", None),
        parse_quality_pct=getattr(result, "parse_quality_pct", None),
        warnings=warnings[:20],
        duration_ms=int((_t.monotonic() - t0) * 1000),
    )
    log.info("ingest_well_logs complete: %s", out.model_dump())
    return out


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "IngestWellLogsInput",
    "IngestWellLogsOut",
    "ingest_well_logs",
]
