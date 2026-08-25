"""Promote silver drill data into the visual (gold) tables the Workspace reads.

WHY THIS EXISTS
===============

Five of the Workspace's six modes — SECTION, 3D, STRUCTURE, LOGS and
COMPARE — do not read ``silver`` at all. They read pre-joined visual tables:

    gold.drillhole_intervals_visual   strip logs, ore bands, section fill
    gold.structure_measurements_visual  stereonet / disc overlays
    silver.drill_traces               3-D hole paths + the MVT tile function

Every one of those was written by a **Dagster asset**
(``silver_drill_traces``, ``gold_cross_section_panels``,
``gold_structure_measurements_visual``). Dagster was retired on 2026-07-28
and nothing replaced the promotion step, so from that day the tables had no
writer at all. Measured against the live Azure database on 2026-08-25:

    gold.drillhole_intervals_visual   0 rows
    gold.cross_section_panels         0 rows
    gold.structure_measurements_visual 0 rows
    gold.assay_composites             0 rows
    gold.significant_intersections    0 rows
    silver.drill_traces               0 rows

— beside 5 collars and 10 surveys that had ingested cleanly. The tabs were
not broken and the ingest was not broken: the step BETWEEN them was gone.
That is why a delivery could ingest, appear on the map, and leave every
downhole view blank. It would have done so for every project, forever, no
matter what was uploaded.

``app.services.mv_refresh.REGISTRY`` is not this. It holds exactly one
entry (``silver.mv_collar_summary``) and refreshes a materialised view; it
has never touched a gold table.

WHAT THIS DOES NOT DO
=====================

Two of the retired assets are deliberately NOT ported here:

  * ``gold.cross_section_panels`` is a saved-section artefact — a panel is
    created when a user draws a section line, not derived from silver — so
    an empty table is its correct resting state and SectionView builds its
    geometry from collars + traces at request time.
  * ``gold.assay_composites`` / ``gold.significant_intersections`` need a
    per-project cut-off grade and compositing length. Those are SME inputs
    (§04e), not defaults this module gets to invent. Promoting them with
    guessed parameters would put numbers a geologist did not choose behind
    a "significant intersection" label, which is worse than an empty panel.

Both are named in the run's counters as ``skipped`` so the gap stays
visible rather than looking like a table that simply had nothing in it.

IDEMPOTENCY
===========

Traces key on ``survey_hash`` — SHA-256 over the hole's ordered survey
stations. Unchanged surveys re-hash identically and the row is left alone,
so a nightly run over an untouched project writes nothing. Intervals upsert
on the ``(collar_id, depth_from, depth_to, interval_kind)`` unique index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.promote_silver_to_gold")

#: Dogleg severity (degrees per 30 m) above which a trace is flagged rather
#: than trusted. The CHECK on silver.drill_traces.trace_quality accepts
#: exactly three values; this is the threshold between two of them, and it
#: is the industry-conventional one the retired asset also used.
_HIGH_DOGLEG_DEG_PER_30M = 15.0

#: Collar orientation is optional in silver, so a hole with no surveys can
#: still be traced when it has azimuth + dip + total_depth. Below this
#: depth there is nothing worth drawing.
_MIN_TRACEABLE_DEPTH_M = 0.1


class PromoteSilverToGoldInput(BaseModel):
    """Scope for one promotion run.

    ``project_id`` is optional so the same workflow serves both callers:
    the ingest path passes one project (the one that just changed), the
    nightly cron passes none and sweeps every project in the workspace.
    """

    workspace_id: str
    project_id: str | None = Field(
        default=None,
        description="Single project to promote. None sweeps the workspace.",
    )


class PromoteSilverToGoldOutput(BaseModel):
    traces_written: int = 0
    traces_unchanged: int = 0
    traces_skipped_no_geometry: int = 0
    intervals_written: int = 0
    structures_written: int = 0
    projects_seen: int = 0


promote_silver_to_gold = hatchet.workflow(
    name="promote_silver_to_gold",
    input_validator=PromoteSilverToGoldInput,
)


# ---------------------------------------------------------------------------
# Desurvey
# ---------------------------------------------------------------------------

def _survey_hash(stations: list[tuple[float, float | None, float | None]]) -> str:
    """Stable digest of a hole's survey set.

    Sorted by depth and rendered through ``json.dumps`` with fixed
    separators so the same stations always produce the same bytes
    regardless of row order out of the database.
    """
    payload = json.dumps(sorted(stations), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _dogleg_deg_per_30m(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    """Dogleg severity between two stations, degrees per 30 m.

    ``a`` and ``b`` are ``(depth_m, azimuth_deg, dip_deg)``. Returns 0.0
    when the two stations sit at the same depth — a duplicate reading is
    not an infinitely sharp bend.
    """
    d1, az1, dip1 = a
    d2, az2, dip2 = b
    dl = d2 - d1
    if dl <= 0:
        return 0.0

    # Inclination from vertical. silver stores dip DOWN-NEGATIVE, so a
    # vertical hole is -90 and inclination is 0.
    i1 = math.radians(90.0 + dip1)
    i2 = math.radians(90.0 + dip2)
    a1 = math.radians(az1)
    a2 = math.radians(az2)

    cos_beta = (
        math.cos(i2 - i1)
        - math.sin(i1) * math.sin(i2) * (1 - math.cos(a2 - a1))
    )
    # Float error can push this a hair outside [-1, 1] on a perfectly
    # straight hole, and acos() raises rather than saturating.
    cos_beta = max(-1.0, min(1.0, cos_beta))
    beta = math.degrees(math.acos(cos_beta))
    return beta * 30.0 / dl


def _straight_line_stations(
    azimuth: float | None,
    dip: float | None,
    total_depth: float | None,
) -> list[tuple[float, float, float]] | None:
    """Two stations describing a hole that was never surveyed.

    ADR-0007 PR-4: a collar carrying azimuth + dip + total_depth describes a
    straight hole well enough to draw. Without all three there is nothing to
    draw and the caller counts the collar as skipped rather than inventing a
    vertical hole at an unknown depth.
    """
    if azimuth is None or dip is None or total_depth is None:
        return None
    if total_depth < _MIN_TRACEABLE_DEPTH_M:
        return None
    return [(0.0, azimuth, dip), (float(total_depth), azimuth, dip)]


def _clean_stations(
    rows: list[asyncpg.Record],
) -> list[tuple[float, float, float]]:
    """Usable survey stations, deduplicated by depth and sorted.

    Rejects the four cases the retired asset enumerated: a NULL azimuth or
    dip, a dip above horizontal or past vertical, and a duplicate depth
    (last row wins, matching "keep latest updated_at").
    """
    by_depth: dict[float, tuple[float, float, float]] = {}
    for r in rows:
        az = r["azimuth"]
        dip = r["dip"]
        if az is None or dip is None:
            continue
        if dip > 0 or dip < -90:
            continue
        depth = float(r["depth"])
        by_depth[depth] = (depth, float(az), float(dip))
    return [by_depth[d] for d in sorted(by_depth)]


_TRACE_UPSERT = """
INSERT INTO silver.drill_traces (
    trace_id, collar_id, workspace_id, project_id,
    geom, computed_at, survey_hash, dogleg_max_deg, trace_quality, created_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,
    ST_Transform(ST_SetSRID(ST_GeomFromText($4), $5::int), 4326),
    NOW(), $6, $7, $8, NOW()
)
ON CONFLICT (collar_id) DO UPDATE SET
    geom           = EXCLUDED.geom,
    computed_at    = EXCLUDED.computed_at,
    survey_hash    = EXCLUDED.survey_hash,
    dogleg_max_deg = EXCLUDED.dogleg_max_deg,
    trace_quality  = EXCLUDED.trace_quality
"""


async def _promote_traces(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    out: PromoteSilverToGoldOutput,
) -> None:
    """Desurvey every collar in one project into silver.drill_traces."""
    from georag_geoparsers._survey_interp import (  # noqa: PLC0415
        SurveyStation,
        minimum_curvature,
    )

    collars = await conn.fetch(
        """
        SELECT c.collar_id, c.easting, c.northing, c.elevation,
               c.total_depth, c.azimuth, c.dip,
               ST_SRID(c.geom) AS srid,
               t.survey_hash AS existing_hash
          FROM silver.collars c
          LEFT JOIN silver.drill_traces t ON t.collar_id = c.collar_id
         WHERE c.project_id = $1::uuid
           AND c.easting IS NOT NULL
           AND c.northing IS NOT NULL
        """,
        project_id,
    )

    for c in collars:
        surveys = await conn.fetch(
            "SELECT depth, azimuth, dip FROM silver.surveys "
            "WHERE collar_id = $1::uuid ORDER BY depth",
            c["collar_id"],
        )
        stations = _clean_stations(surveys)

        quality = "ok"
        if len(stations) < 2:
            # 0- or 1-survey hole. Both fall back to the collar's own
            # orientation; a single station at depth 0 carries no more
            # information than the collar row already does.
            fallback = _straight_line_stations(
                float(c["azimuth"]) if c["azimuth"] is not None else None,
                float(c["dip"]) if c["dip"] is not None else None,
                float(c["total_depth"]) if c["total_depth"] is not None else None,
            )
            if fallback is None:
                out.traces_skipped_no_geometry += 1
                continue
            stations = fallback
            quality = "single_survey_vertical"

        digest = _survey_hash([(d, a, p) for d, a, p in stations])
        if c["existing_hash"] == digest:
            out.traces_unchanged += 1
            continue

        collar_elev = float(c["elevation"]) if c["elevation"] is not None else 0.0
        positions = minimum_curvature(
            collar_easting=float(c["easting"]),
            collar_northing=float(c["northing"]),
            collar_elevation=collar_elev,
            stations=[
                SurveyStation(depth_m=d, azimuth_deg=a, dip_deg=p)
                for d, a, p in stations
            ],
        )
        if len(positions) < 2:
            out.traces_skipped_no_geometry += 1
            continue

        dogleg_max = 0.0
        for i in range(len(stations) - 1):
            dogleg_max = max(dogleg_max, _dogleg_deg_per_30m(stations[i], stations[i + 1]))
        if quality == "ok" and dogleg_max > _HIGH_DOGLEG_DEG_PER_30M:
            quality = "high_dogleg_warning"

        # `collar_elev` is added back HERE, not left to the interpolator.
        #
        # minimum_curvature() takes `collar_elevation` and does not apply it:
        # XYZ.elev_m is documented as "Elevation offset from collar: 0 at the
        # collar, negative downhole", and measured, a hole from a collar at
        # 100 m RL ends at elev_m = -100.0 for a 100 m vertical hole. East and
        # north ARE absolute in the same return value, so the tuple mixes two
        # frames.
        #
        # The retired Dagster asset wrote `xyz.elev_m` straight into the WKT
        # and inherited that: every trace it produced started at Z = 0
        # regardless of topography, which flattens a whole camp onto one
        # datum in the 3-D view. Fixing the shared interpolator would change
        # behaviour under callers and tests that are not ours, so the offset
        # is resolved at the one place that needs an absolute elevation.
        wkt = "LINESTRING Z (" + ", ".join(
            f"{p.east_m} {p.north_m} {collar_elev + p.elev_m}" for _, p in positions
        ) + ")"

        # The trace is built in the collar's own CRS (metres), then
        # transformed. Reading metre offsets as degrees is exactly the class
        # of bug that put an Alaskan shapefile at longitude 400,797.
        await conn.execute(
            _TRACE_UPSERT,
            c["collar_id"], workspace_id, project_id,
            wkt, int(c["srid"] or 4326),
            digest, dogleg_max, quality,
        )
        out.traces_written += 1


# ---------------------------------------------------------------------------
# Visual intervals
# ---------------------------------------------------------------------------

#: Lithology bands. `silver.lithology_logs` carries no project_id — it hangs
#: off the collar — so the project scope comes through the join, and
#: workspace_id is taken from the COLLAR rather than the log row so a
#: mis-stamped log cannot write a band into another tenant's project.
_INTERVALS_LITHOLOGY = """
INSERT INTO gold.drillhole_intervals_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth_from, depth_to, interval_kind,
    lithology_code, lithology_label, color_hint,
    assay_payload, alteration_payload, structure_payload,
    computed_at, created_at
)
SELECT gen_random_uuid(), l.collar_id, c.workspace_id, c.project_id,
       l.from_depth, l.to_depth, 'lithology',
       LEFT(COALESCE(l.lithology_code, ''), 32),
       COALESCE(NULLIF(l.lithology_description, ''), l.lithology_code),
       LEFT(COALESCE(NULLIF(l.color, ''), rc.code), 32),
       '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
       NOW(), NOW()
  FROM silver.lithology_logs l
  JOIN silver.collars c ON c.collar_id = l.collar_id
  LEFT JOIN silver.rock_codes rc
         ON rc.workspace_id = c.workspace_id
        AND rc.code = l.lithology_code
 WHERE c.project_id = $1::uuid
   AND l.from_depth IS NOT NULL
   AND l.to_depth IS NOT NULL
   AND l.to_depth > l.from_depth
   AND l.from_depth >= 0
ON CONFLICT (collar_id, depth_from, depth_to, interval_kind) DO UPDATE SET
    lithology_code  = EXCLUDED.lithology_code,
    lithology_label = EXCLUDED.lithology_label,
    color_hint      = EXCLUDED.color_hint,
    computed_at     = EXCLUDED.computed_at
"""

#: Sampled windows. `commodity_assays` is already JSONB on silver.samples,
#: so the payload is carried across rather than re-derived — the strip log
#: colours by grade and needs the values, not a boolean.
_INTERVALS_SAMPLES = """
INSERT INTO gold.drillhole_intervals_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth_from, depth_to, interval_kind,
    lithology_code, lithology_label, color_hint,
    assay_payload, alteration_payload, structure_payload,
    computed_at, created_at
)
SELECT gen_random_uuid(), s.collar_id, c.workspace_id, c.project_id,
       s.from_depth, s.to_depth, 'sample_window',
       NULL, LEFT(COALESCE(s.sample_type, 'sample'), 120), NULL,
       COALESCE(s.commodity_assays, '{}'::jsonb), '{}'::jsonb, '{}'::jsonb,
       NOW(), NOW()
  FROM silver.samples s
  JOIN silver.collars c ON c.collar_id = s.collar_id
 WHERE c.project_id = $1::uuid
   AND s.from_depth IS NOT NULL
   AND s.to_depth IS NOT NULL
   AND s.to_depth > s.from_depth
   AND s.from_depth >= 0
ON CONFLICT (collar_id, depth_from, depth_to, interval_kind) DO UPDATE SET
    assay_payload   = EXCLUDED.assay_payload,
    lithology_label = EXCLUDED.lithology_label,
    computed_at     = EXCLUDED.computed_at
"""

#: Stereonet-ready structure. The equal-area (Schmidt) pole projection is
#: computed in SQL so the gold row is self-contained: a client that cannot
#: run the projection still gets x/y. `structure_type` is copied, not
#: mapped — inventing a taxonomy here would contradict §04e.
_STRUCTURES_VISUAL = """
INSERT INTO gold.structure_measurements_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth, structure_type, strike_deg, dip_deg, dip_direction_deg,
    plunge_deg, trend_deg, stereonet_x, stereonet_y, projection,
    computed_at, created_at
)
SELECT gen_random_uuid(), st.collar_id, c.workspace_id, c.project_id,
       st.depth, st.structure_type,
       CASE WHEN st.true_dip_dir IS NULL THEN NULL
            ELSE MOD((st.true_dip_dir - 90 + 360)::numeric, 360) END,
       st.true_dip, st.true_dip_dir,
       NULL, NULL,
       CASE WHEN st.true_dip IS NULL OR st.true_dip_dir IS NULL THEN NULL ELSE
            SQRT(2) * SIN(RADIANS((90 - st.true_dip) / 2.0))
                    * SIN(RADIANS(MOD((st.true_dip_dir + 180)::numeric, 360)))
       END,
       CASE WHEN st.true_dip IS NULL OR st.true_dip_dir IS NULL THEN NULL ELSE
            SQRT(2) * SIN(RADIANS((90 - st.true_dip) / 2.0))
                    * COS(RADIANS(MOD((st.true_dip_dir + 180)::numeric, 360)))
       END,
       'equal_area', NOW(), NOW()
  FROM silver.structure st
  JOIN silver.collars c ON c.collar_id = st.collar_id
 WHERE c.project_id = $1::uuid
   AND st.depth IS NOT NULL
ON CONFLICT DO NOTHING
"""


@promote_silver_to_gold.task(execution_timeout="20m")
async def promote(
    input: PromoteSilverToGoldInput, ctx: Context,
) -> PromoteSilverToGoldOutput:
    out = PromoteSilverToGoldOutput()

    conn: asyncpg.Connection = await asyncpg.connect(
        build_dsn(), statement_cache_size=0,
    )
    try:
        # Session scope, not SET LOCAL: the loop below runs many autocommit
        # statements and a transaction-scoped GUC is discarded immediately,
        # leaving every read fail-closed against these policies. Same
        # reasoning as ingest_zip_archive's connection.
        await bind_workspace_scope(
            conn,
            workspace_id=input.workspace_id,
            site="hatchet.promote_silver_to_gold",
            is_local=False,
        )

        if input.project_id is not None:
            project_ids = [input.project_id]
        else:
            project_ids = [
                str(r["project_id"])
                for r in await conn.fetch(
                    "SELECT project_id FROM silver.projects WHERE workspace_id = $1::uuid",
                    input.workspace_id,
                )
            ]

        for project_id in project_ids:
            out.projects_seen += 1
            await _promote_traces(
                conn,
                workspace_id=input.workspace_id,
                project_id=project_id,
                out=out,
            )
            for sql in (_INTERVALS_LITHOLOGY, _INTERVALS_SAMPLES):
                status = await conn.execute(sql, project_id)
                out.intervals_written += _affected(status)
            status = await conn.execute(_STRUCTURES_VISUAL, project_id)
            out.structures_written += _affected(status)
    finally:
        await conn.close()

    log.info(
        "promote_silver_to_gold: %d project(s), %d trace(s) written "
        "(%d unchanged, %d without geometry), %d interval(s), %d structure(s)",
        out.projects_seen, out.traces_written, out.traces_unchanged,
        out.traces_skipped_no_geometry, out.intervals_written,
        out.structures_written,
    )
    return out


def _affected(status: str) -> int:
    """Row count out of an asyncpg command tag such as ``INSERT 0 42``.

    Returns 0 for anything unparseable rather than raising: a miscounted
    statistic must not fail a promotion that actually wrote rows.
    """
    parts = status.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        # Nothing actionable — the statement ran, only the statistic is
        # lost — but a promotion reporting 0 rows when it wrote thousands
        # is exactly the kind of quiet wrongness this module exists to end,
        # so it goes in the log rather than nowhere.
        log.debug("promote_silver_to_gold: unparseable command tag %r", status)
        return 0


__all__ = [
    "promote_silver_to_gold",
    "PromoteSilverToGoldInput",
    "PromoteSilverToGoldOutput",
]
