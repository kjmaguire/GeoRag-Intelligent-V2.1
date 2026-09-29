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

CANONICAL SILVER, NOT JUST GOLD
===============================

One promotion here is silver → silver: ``silver.lithology_logs`` (the
legacy table the live tabular ingest writes) into ``silver.lithology``
(the §04e-canonical table — Kyle's 2026-05-20 decision, reaffirmed
2026-08-25). Everything reader-side already points at the canonical
table: nl_summaries' lithology synthesizer, the DrillholeDetail quality
badge, CsvLithologyExporter and the citation resolvers. Its only writer
was the retired ``bronze_to_silver/lithology.py`` Dagster asset, so from
2026-07-28 the table sat empty while every one of those readers returned
nothing. The rock-code resolution (exact, then rapidfuzz ≥ 60 across the
dual NRCAN/GSC catalogue) is ported from that asset.

``silver.lithology.id`` is the legacy row's ``log_id``, on purpose:
nl_summaries derives its passage ids from this column, so a re-promotion
over unchanged legacy rows re-writes the same ids and the corpus does not
churn. The id only changes when the legacy row itself is rewritten (a
re-upload), which is exactly when the passage SHOULD re-key.

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

WHAT THE STRIP LOG IS GIVEN
===========================

``gold.drillhole_intervals_visual`` is what the Workspace LOGS panel, the hole
page's strip log and the 3-D bands draw. Per hole it now carries:

  * ``lithology`` rows - code, a label (the description, else the rock name),
    and ``color_hint``, which is a display colour ONLY when the data gave one
    as a hex code. A described colour ("dark grey") is not a display colour and
    is not turned into one; it stays in silver (lithology.colour) for the
    tooltip, and the strip log assigns a stable legend colour per code. It used
    to write the colour TEXT, or the rock code when there was none, into a
    VARCHAR(20) the front end uses as a CSS fill - invalid as a fill, and a
    colour of 21+ characters ("Dark greenish grey to black") failed the INSERT
    for the whole project.
  * ``alteration`` rows - one per interval, ``alteration_payload`` =
    ``{"alterations": [{"type", "intensity", "minerals", "notes"}, ...]}``, so
    two alterations over one interval share a row (the table's unique key is
    per interval and kind).
  * ``sample_window`` rows, as before.

  * ``mineralization`` rows - one per interval, ``mineralization_payload`` =
    ``{"minerals": [{"mineral", "abundance_pct", "form", "grain_size",
    "notes"}, ...]}``, ordered by silver ``created_at, id``. silver.mineralization
    holds one row PER MINERAL, and the table's unique key is per interval and
    kind, so every mineral logged over one interval shares a row (the same shape
    as alteration). ``lithology_label`` is the one-line summary ("Pyrite 3%;
    Chalcopyrite"). The ``mineralization`` interval_kind and its payload column
    were added by ``2026_09_29_120000`` (§04e, SME-approved 2026-09-29, Kyle);
    until that migration is applied the mineralization statements fail, so the
    migration must run before this image rolls (CD runs ``artisan migrate``
    first).

Mineralization is rebuilt like alteration - a pure function of silver - so a
re-uploaded log with different intervals or minerals leaves no ghost bands. The
gamma-derived ``DERIVED-*`` bands are ``lithology`` rows and are untouched: the
rebuild deletes ``interval_kind = 'mineralization'`` only.

IDEMPOTENCY
===========

Traces key on ``survey_hash`` — SHA-256 over the hole's ordered survey
stations. Unchanged surveys re-hash identically and the row is left alone,
so a nightly run over an untouched project writes nothing. Intervals upsert
on the ``(collar_id, depth_from, depth_to, interval_kind)`` unique index.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg
from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy, Context
from pydantic import BaseModel, Field

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.promote_silver_to_gold")

#: How long a caller may wait on the DISPATCH of this workflow.
#:
#: Defined here, beside the workflow it bounds, so the two dispatchers
#: (ingest_tabular per project, nightly_ingestion_integrity per workspace)
#: cannot drift apart.
#:
#: ``aio_run_no_wait`` skips waiting for this workflow's RESULT but still
#: awaits the dispatch RPC, and against an unreachable Hatchet that RPC
#: retries for ~17s before raising. Awaiting it unbounded makes the
#: dispatch a hidden dependency of every caller: a Hatchet outage holds a
#: worker slot per file for as long as the SDK retries. This promotion is
#: idempotent and the nightly sweep re-runs it, so abandoning a dispatch
#: costs a delay, never data.
PROMOTION_DISPATCH_TIMEOUT_S = 5.0

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
    #: Traces whose azimuths were corrected from a DECLARED reference (true or
    #: magnetic north, or another projected grid) to the collar's local UTM
    #: grid. Zero unless silver.projects or silver.surveys declares one (GIS-12).
    traces_azimuth_corrected: int = 0
    #: Traces whose azimuth reference was DECLARED (survey file or project)
    #: but could not be applied — magnetic north with no
    #: silver.projects.magnetic_declination. Built uncorrected; counted so the
    #: gap is reported rather than hidden.
    traces_azimuth_reference_unapplied: int = 0
    intervals_written: int = 0
    #: 'alteration' rows rebuilt this run (a subset of nothing above: they are
    #: rebuilt, not upserted, so the count is the project's whole set).
    alteration_intervals_written: int = 0
    #: 'mineralization' rows rebuilt this run - one per (hole, from, to), each
    #: carrying every mineral logged over the interval. Rebuilt like alteration.
    mineralization_intervals_written: int = 0
    #: Lithology intervals that shared a (collar, from, to) key with another and
    #: were folded into one gold band. The table's unique key is per interval.
    lithology_duplicate_intervals: int = 0
    structures_written: int = 0
    projects_seen: int = 0
    lithology_rows_promoted: int = 0
    #: Rows whose lithology_code matched nothing in silver.rock_codes, not
    #: even fuzzily. They still promote — rock_code NULL is the catalogue-gap
    #: signal the DataQualityBadge counts — but the number stays visible.
    lithology_codes_unresolved: int = 0


promote_silver_to_gold = hatchet.workflow(
    name="promote_silver_to_gold",
    input_validator=PromoteSilverToGoldInput,
    # HAT-8 (2026-09-29) — one promotion per workspace at a time. Every
    # ingest_tabular completion dispatches one, so collar + survey +
    # lithology + structure CSVs uploaded back to back (or a ZIP whose
    # tabular children finish together) ran 2-4 promotes of the same
    # project at once: the concurrent clear-and-rebuilds of
    # gold.structure_measurements_visual both inserted (a doubled
    # stereonet), and the concurrent silver.lithology inserts collided on
    # the primary key and failed one run.
    #
    # Keyed on the WORKSPACE, not the project: the nightly Tier 3 sweep
    # dispatches project_id=None for the whole workspace, and a per-project
    # key would not serialise it against a per-project run.
    #
    # GROUP_ROUND_ROBIN queues rather than cancels, so the newest dispatch
    # (which reads the newest silver) always runs. CANCEL_NEWEST would drop
    # exactly that one. The SDK's CANCEL_QUEUED_EXCEPT_NEWEST would coalesce
    # the queue, but nothing here has run it against the pinned
    # hatchet-lite engine, and a strategy the engine rejects fails
    # PutWorkflow for the whole worker.
    concurrency=ConcurrencyExpression(
        expression=(
            "has(input.workspace_id) && string(input.workspace_id) != '' "
            "? string(input.workspace_id) : 'none'"
        ),
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


async def dispatch_promotion(
    *,
    workspace_id: str,
    project_id: str | None = None,
) -> bool:
    """Ask Hatchet to run this promotion, without waiting on the answer.

    Returns True when the dispatch was accepted. Never raises: a promotion
    that cannot be dispatched must not fail the caller, because by the time
    anyone dispatches it the source rows are already committed to silver
    and the nightly sweep re-runs the promotion regardless.

    Both the wait and the swallow live here rather than at the call sites.
    They were duplicated at two of them, which is how the bound would have
    been added to one and forgotten at the other.

    Args:
        workspace_id: Tenant whose rows to promote. Required — the tables
            read are fail-CLOSED under RLS, so an unbound scope promotes
            nothing and reports success.
        project_id: Narrow the promotion to one project. None sweeps the
            whole workspace.
    """
    try:
        await asyncio.wait_for(
            promote_silver_to_gold.aio_run_no_wait(
                PromoteSilverToGoldInput(
                    workspace_id=workspace_id,
                    project_id=project_id,
                )
            ),
            timeout=PROMOTION_DISPATCH_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning(
            "promote_silver_to_gold: dispatch failed for workspace %s "
            "project %s — %s (the source rows are in silver; the nightly "
            "sweep will promote them)",
            workspace_id, project_id, exc,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Canonical lithology (silver.lithology_logs → silver.lithology)
# ---------------------------------------------------------------------------

#: Fuzzy-match floor, ported from the retired asset's config default. 60
#: catches "granitic" → "Granite" and "qtz monz" → "Quartz Monzonite"
#: without bleeding into unrelated rocks.
_ROCK_CODE_FUZZY_THRESHOLD = 60

#: Which catalogue system wins when the same string exists in both. NRCAN
#: was the retired asset's default and nothing has decided otherwise.
_ROCK_CODE_PREFERRED_SYSTEM = "NRCAN"


def build_rock_code_lookup(
    rows: list[dict],
    preferred_system: str = _ROCK_CODE_PREFERRED_SYSTEM,
) -> dict[str, dict]:
    """Index silver.rock_codes rows for resolution.

    Legacy ``lithology_code`` is sometimes a catalogue CODE ("SST") and
    sometimes a NAME ("Sandstone"), so both are indexed for exact match.
    The preferred system wins a collision; iteration order handles that by
    writing preferred entries last.
    """
    by_code: dict[str, tuple[str, str]] = {}
    by_name: dict[str, tuple[str, str]] = {}
    ordered = sorted(
        rows, key=lambda r: (r.get("system") or "") == preferred_system,
    )
    for r in ordered:
        code = (r.get("code") or "").strip()
        name = (r.get("name") or "").strip()
        if not code:
            continue
        by_code[code.lower()] = (code, name or code)
        if name:
            by_name[name.lower()] = (code, name)
    return {"by_code": by_code, "by_name": by_name}


def resolve_rock_code(
    raw: str | None,
    lookup: dict[str, dict],
    fuzzy_threshold: int = _ROCK_CODE_FUZZY_THRESHOLD,
) -> tuple[str | None, float | None, str | None]:
    """(rock_code, confidence, rock_name) for a legacy lithology string.

    Exact code or name match → confidence 1.0. Otherwise rapidfuzz
    token-set ratio across the catalogue names, accepted at ≥ threshold
    with confidence = score / 100. No match → (None, None, raw) so the
    canonical row still carries the geologist's own word for the rock
    rather than losing it.
    """
    text = (raw or "").strip()
    if not text:
        return None, None, None
    key = text.lower()
    hit = lookup["by_code"].get(key) or lookup["by_name"].get(key)
    if hit:
        return hit[0], 1.0, hit[1]

    by_name = lookup["by_name"]
    if by_name:
        from rapidfuzz import fuzz, process  # noqa: PLC0415

        match = process.extractOne(
            key, list(by_name), scorer=fuzz.token_set_ratio,
            score_cutoff=fuzzy_threshold,
        )
        if match is not None:
            matched_key, score = match[0], match[1]
            code, name = by_name[matched_key]
            return code, round(score / 100.0, 4), name
    return None, None, text


#: workspace_id is taken from the COLLAR, same reasoning as the gold
#: interval SQL below: a mis-stamped legacy row must not write a canonical
#: row into another tenant. The interval filter mirrors the canonical
#: table's CHECK (to_depth > from_depth) so one bad legacy row cannot fail
#: the batch.
_LITHOLOGY_LEGACY_FETCH = """
SELECT l.log_id, c.workspace_id, l.collar_id, l.from_depth, l.to_depth,
       l.lithology_code, l.lithology_description,
       l.grain_size, l.color, l.hardness, l.weathering
  FROM silver.lithology_logs l
  JOIN silver.collars c ON c.collar_id = l.collar_id
 WHERE c.project_id = $1::uuid
   AND l.from_depth IS NOT NULL
   AND l.to_depth IS NOT NULL
   AND l.to_depth > l.from_depth
"""

#: The canonical table is a per-project PROJECTION of the legacy table, so
#: the promotion deletes the project's canonical rows and re-derives them —
#: the same replace-not-append semantics the legacy writer itself uses.
_LITHOLOGY_CANONICAL_DELETE = """
DELETE FROM silver.lithology l
 USING silver.collars c
 WHERE c.collar_id = l.collar_id
   AND c.project_id = $1::uuid
"""

#: texture / logged_by / logged_date stay NULL: the legacy table never
#: recorded them and inventing values would violate §04e. rqd / recovery
#: stay behind in the legacy table for the same reason — the canonical
#: schema has no column for them.
_LITHOLOGY_CANONICAL_INSERT = """
INSERT INTO silver.lithology (
    id, workspace_id, collar_id, from_depth, to_depth,
    rock_code, rock_code_confidence, rock_name, description,
    colour, grain_size, hardness, weathering, created_at
) VALUES (
    $1::uuid, $2::uuid, $3::uuid, $4, $5,
    $6, $7, $8, $9, $10, $11, $12, $13, NOW()
)
"""


async def _promote_lithology_canonical(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    code_lookup: dict[str, dict],
    out: PromoteSilverToGoldOutput,
) -> None:
    """Derive one project's silver.lithology from silver.lithology_logs."""
    legacy = await conn.fetch(_LITHOLOGY_LEGACY_FETCH, project_id)

    params = []
    for r in legacy:
        code, confidence, name = resolve_rock_code(
            r["lithology_code"], code_lookup,
        )
        if code is None and (r["lithology_code"] or "").strip():
            out.lithology_codes_unresolved += 1
        params.append((
            str(r["log_id"]), str(r["workspace_id"]), str(r["collar_id"]),
            r["from_depth"], r["to_depth"],
            code, confidence, name,
            r["lithology_description"],
            r["color"], r["grain_size"], r["hardness"], r["weathering"],
        ))

    async with conn.transaction():
        await conn.execute(_LITHOLOGY_CANONICAL_DELETE, project_id)
        if params:
            await conn.executemany(_LITHOLOGY_CANONICAL_INSERT, params)
    out.lithology_rows_promoted += len(params)


# ---------------------------------------------------------------------------
# Desurvey
# ---------------------------------------------------------------------------

#: Bumped whenever the GEOMETRY BUILDER changes, not the survey data.
#:
#: `survey_hash` is the skip key: a trace whose stored hash still matches is
#: left alone. That is right for unchanged surveys and wrong for a corrected
#: builder — v1 wrote every trace in the wrong projection (see
#: `_collar_local_utm`) and the surveys behind them had not changed, so a
#: re-run recognised them as current and skipped all of them. Folding this
#: constant into the digest makes a builder fix invalidate its own output.
_TRACE_BUILDER_VERSION = 2


def _survey_hash(
    stations: list[tuple[float, float | None, float | None]],
    *,
    origin: tuple[float | None, float | None, float | None] = (None, None, None),
) -> str:
    """Digest of everything the trace geometry is built from.

    Three inputs, because a trace is a function of all three and the digest
    is the only thing standing between a stale trace and a skipped re-run:

      * the SURVEY STATIONS, sorted by depth and rendered with fixed
        separators so row order out of the database cannot change the digest;
      * the BUILDER VERSION, so a corrected builder invalidates its own
        output (see ``_TRACE_BUILDER_VERSION``);
      * the COLLAR ORIGIN — lon, lat and elevation.

    The origin is not decoration. A collar MOVES in two ordinary situations:
    it was ingested under an assumed CRS and is re-uploaded with the right
    one, or it is re-surveyed. In both the stations are untouched, so a
    digest over stations alone still matches and the trace is skipped as
    "unchanged" while sitting at the old position — the hole moves on the map
    and its trace stays behind.

    This is not hypothetical. RedStar's five collars are stored
    ``georef_method='assumed'`` at EPSG:32613 with their true position 2,500
    km west; the moment the project's CRS is set and the file re-uploaded,
    every one of them moves and every trace would have been left behind.
    """
    payload = json.dumps(
        {"v": _TRACE_BUILDER_VERSION, "o": list(origin), "s": sorted(stations)},
        separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _collar_local_utm(lon: float, lat: float) -> int:
    """The UTM zone the collar actually sits in.

    WHY THIS EXISTS
        The trace is metre offsets from the collar, so it has to be assembled
        in a projected CRS — but there is no column recording which one the
        collar's easting/northing were surveyed in. The old
        `silver.collars.geom` was no help: it was declared
        ``geometry(POINT, 32613)`` and every collar was ST_Transform-ed into
        that zone on insert, so ``ST_SRID(geom)`` returned 32613 for a hole
        anywhere on earth. It was retired 2026-09-29; geom_4326 is the only
        collar geometry.

        v1 of this module read ``ST_SRID(c.geom)`` as the SOURCE srid and fed
        it the raw easting/northing. For Athabasca that is accidentally right.
        For Alaska it read UTM zone 4N eastings as zone 13N and put the traces
        at longitude −106.558 for collars whose true position is −160.558 —
        measured on live Azure 2026-08-25, the same 2,500 km error this
        session fixed in ingest_tabular, reintroduced one workflow over.

        The fix removes the need to know the source CRS at all. Take the
        collar's TRUE position from ``geom_4326`` (correct however it was
        surveyed), pick the UTM zone that position falls in, and assemble the
        offsets there. Offsets are collar-relative, so the only requirement on
        the CRS is that it be metric and locally accurate — which its own zone
        is, by construction.

    Zone from longitude, hemisphere from latitude: EPSG 326xx north, 327xx
    south. Longitude is clamped rather than wrapped — a collar at exactly
    +180 would otherwise compute zone 61.
    """
    zone = int((lon + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


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
    rows: Sequence[Mapping[str, Any]],
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


#: $4 is a LINESTRING Z of metre OFFSETS about an origin of (0, 0) — not
#: absolute coordinates. $5 is the collar's own UTM zone, $6/$7 its true
#: lon/lat out of geom_4326.
#:
#: ST_Translate moves the offsets onto the collar's real position expressed
#: in that zone, then one ST_Transform takes the whole line to 4326. The two
#: argument form of ST_Translate leaves Z alone, which is what we want: Z is
#: already an absolute elevation and is not a projected quantity.
#:
#: Building it this way is what removes the need to know the CRS the collar
#: was surveyed in — see `_collar_local_utm` for why that is not recoverable
#: from the row.
_TRACE_UPSERT = """
INSERT INTO silver.drill_traces (
    trace_id, collar_id, workspace_id, project_id,
    geom, computed_at, survey_hash, dogleg_max_deg, trace_quality, created_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,
    ST_Transform(
        ST_Translate(
            ST_SetSRID(ST_GeomFromText($4), $5::int),
            ST_X(ST_Transform(ST_SetSRID(ST_MakePoint($6, $7), 4326), $5::int)),
            ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint($6, $7), 4326), $5::int))
        ),
        4326
    ),
    NOW(), $8, $9, $10, NOW()
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

    # lon/lat off geom_4326, NOT easting/northing.
    #
    # geom_4326 is transformed from the collar's real source CRS at insert,
    # so it is correct wherever the hole was surveyed. easting/northing are
    # raw numbers whose projection is not recorded anywhere on the row —
    # reading them as though they were in ST_SRID(geom) (the retired 32613
    # column) is what put Alaskan traces 2,500 km east. See
    # `_collar_local_utm`.
    #
    # A collar with no geom_4326 is skipped rather than guessed at: without a
    # position there is nothing to hang metre offsets on.
    #
    # Azimuth reference (GIS-12; Kyle, 2026-09-29): a survey station's own
    # silver.surveys.azimuth_reference (from the file) wins over the
    # project's orientation_reference; with neither recognised, azimuths are
    # taken as grid north of the collar's own UTM zone — the as-built
    # default. See app/services/ingest/azimuth_reference.py.
    from app.services.ingest.azimuth_reference import (  # noqa: PLC0415
        apply as apply_azimuth_correction,
    )
    from app.services.ingest.azimuth_reference import (  # noqa: PLC0415
        azimuth_correction,
        correct_survey_rows,
    )

    try:
        project_row = await conn.fetchrow(
            "SELECT orientation_reference, magnetic_declination, crs_epsg "
            "FROM silver.projects WHERE project_id = $1::uuid",
            project_id,
        )
    except Exception as exc:  # noqa: BLE001 — unreadable means "none declared", the default
        log.warning("promote.traces: project azimuth reference unreadable (%s)", exc)
        project_row = None
    orientation_reference = project_row["orientation_reference"] if project_row else None
    magnetic_declination = project_row["magnetic_declination"] if project_row else None
    project_epsg = project_row["crs_epsg"] if project_row else None

    collars = await conn.fetch(
        """
        SELECT c.collar_id, c.elevation,
               c.total_depth, c.azimuth, c.dip,
               ST_X(c.geom_4326) AS lon,
               ST_Y(c.geom_4326) AS lat,
               t.survey_hash AS existing_hash
          FROM silver.collars c
          LEFT JOIN silver.drill_traces t ON t.collar_id = c.collar_id
         WHERE c.project_id = $1::uuid
           AND c.geom_4326 IS NOT NULL
        """,
        project_id,
    )

    for c in collars:
        lon = float(c["lon"])
        lat = float(c["lat"])
        collar_elev = float(c["elevation"]) if c["elevation"] is not None else 0.0
        local_epsg = _collar_local_utm(lon, lat)

        surveys = await conn.fetch(
            "SELECT depth, azimuth, dip, azimuth_reference FROM silver.surveys "
            "WHERE collar_id = $1::uuid ORDER BY depth",
            c["collar_id"],
        )
        # Declared reference -> the local zone's grid, per STATION. Applied
        # BEFORE cleaning and hashing, so declaring (or changing) a reference
        # rebuilds the trace; with none declared the stations, and the hash,
        # are unchanged.
        corrected = correct_survey_rows(
            surveys,
            project_reference=orientation_reference,
            magnetic_declination=magnetic_declination,
            project_epsg=project_epsg,
            local_epsg=local_epsg,
            lon=lon, lat=lat,
        )
        stations = _clean_stations(corrected.rows)
        azimuth_corrected = corrected.corrected
        unapplied = list(corrected.unapplied_notes)

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
            quality = "single_survey_vertical"
            # The collar azimuth comes from the collar table, not a survey
            # file, so only the project's declaration applies to it.
            correction = azimuth_correction(
                orientation_reference=orientation_reference,
                magnetic_declination=magnetic_declination,
                project_epsg=project_epsg,
                local_epsg=local_epsg,
                lon=lon, lat=lat,
            )
            azimuth_corrected = bool(correction.degrees)
            unapplied = [correction.note] if correction.note else []
            stations = [
                (d, apply_azimuth_correction(a, correction), p) for d, a, p in fallback
            ]

        if azimuth_corrected:
            out.traces_azimuth_corrected += 1
        if unapplied:
            # Declared but not applied (magnetic with no project declination):
            # the trace is still built, uncorrected, and the gap is counted
            # so it reaches the run report instead of being smoothed away.
            out.traces_azimuth_reference_unapplied += 1
            log.warning(
                "promote.traces: azimuth reference not applied collar=%s (%s)",
                c["collar_id"], "; ".join(unapplied),
            )

        # Hashed BEFORE the skip test, and over the collar origin as well as
        # the stations — a collar that moves must invalidate its own trace.
        digest = _survey_hash(
            [(d, a, p) for d, a, p in stations],
            origin=(lon, lat, collar_elev),
        )
        if c["existing_hash"] == digest:
            out.traces_unchanged += 1
            continue

        # Origin (0, 0): the interpolator returns collar + offset for east and
        # north, so zeroing the collar makes it return the OFFSETS directly.
        # That is what the SQL translates onto the collar's real position, and
        # it is why no source CRS is needed.
        positions = minimum_curvature(
            collar_easting=0.0,
            collar_northing=0.0,
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
        #
        # X and Y here are metre OFFSETS about (0, 0) — the SQL translates
        # them onto the collar. Z is already absolute and is not translated.
        wkt = "LINESTRING Z (" + ", ".join(
            f"{p.east_m} {p.north_m} {collar_elev + p.elev_m}" for _, p in positions
        ) + ")"

        await conn.execute(
            _TRACE_UPSERT,
            c["collar_id"], workspace_id, project_id,
            wkt, _collar_local_utm(lon, lat), lon, lat,
            digest, dogleg_max, quality,
        )
        out.traces_written += 1


# ---------------------------------------------------------------------------
# Visual intervals
# ---------------------------------------------------------------------------

#: Lithology bands — read from the CANONICAL silver.lithology, not the
#: legacy lithology_logs it is derived from. The canonical step above runs
#: first in the same project iteration, so this always sees the
#: freshly-promoted rows; reading the legacy table here would fork the
#: gold layer from every other reader the moment rock-code resolution
#: rewrites a code. rock_code arrives already resolved, which is why the
#: old rc join is gone. Neither table carries project_id — it hangs off
#: the collar — so the project scope comes through the join, and
#: workspace_id is taken from the COLLAR rather than the row so a
#: mis-stamped interval cannot write a band into another tenant's project.
_INTERVALS_LITHOLOGY = """
INSERT INTO gold.drillhole_intervals_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth_from, depth_to, interval_kind,
    lithology_code, lithology_label, color_hint,
    assay_payload, alteration_payload, structure_payload,
    computed_at, created_at
)
SELECT gen_random_uuid(), b.collar_id, c.workspace_id, c.project_id,
       b.depth_from, b.depth_to, 'lithology',
       LEFT(COALESCE(b.rock_code, b.rock_name, ''), 32),
       COALESCE(NULLIF(b.description, ''), b.rock_name, b.rock_code),
       CASE WHEN b.colour ~ '^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$'
            THEN lower(b.colour) END,
       '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
       NOW(), NOW()
  FROM (
        SELECT DISTINCT ON (l.collar_id, round(l.from_depth, 3), round(l.to_depth, 3))
               l.collar_id, round(l.from_depth, 3) AS depth_from,
               round(l.to_depth, 3) AS depth_to,
               l.rock_code, l.rock_name, l.description, l.colour
          FROM silver.lithology l
          JOIN silver.collars cl ON cl.collar_id = l.collar_id
         WHERE cl.project_id = $1::uuid
         ORDER BY l.collar_id, round(l.from_depth, 3), round(l.to_depth, 3),
                  l.created_at, l.id
       ) b
  JOIN silver.collars c ON c.collar_id = b.collar_id
 WHERE c.project_id = $1::uuid
   AND b.depth_from IS NOT NULL
   AND b.depth_to IS NOT NULL
   AND b.depth_to > b.depth_from
   AND b.depth_from >= 0
   AND b.depth_to < 10000000
ON CONFLICT (collar_id, depth_from, depth_to, interval_kind) DO UPDATE SET
    lithology_code  = EXCLUDED.lithology_code,
    lithology_label = EXCLUDED.lithology_label,
    -- A display colour already on the row (the gamma-derived bands carry
    -- curated hex colours that silver.lithology never had) is not blanked by
    -- a promotion that has none; anything that is not a hex colour is.
    color_hint      = CASE
        WHEN EXCLUDED.color_hint IS NOT NULL THEN EXCLUDED.color_hint
        WHEN gold.drillhole_intervals_visual.color_hint
             ~ '^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$'
            THEN gold.drillhole_intervals_visual.color_hint
    END,
    computed_at     = EXCLUDED.computed_at
"""

#: Intervals silver.lithology no longer has. The upsert above never removes a
#: row, so a corrected log with different boundaries left the OLD bands beside
#: the new ones - two overlapping columns on the strip log. Gamma-derived bands
#: (DERIVED-*) are owned by derive_intervals, which clears and rewrites them
#: itself, so they are left alone.
_INTERVALS_LITHOLOGY_STALE = """
DELETE FROM gold.drillhole_intervals_visual g
 WHERE g.project_id = $1::uuid
   AND g.interval_kind = 'lithology'
   AND (g.lithology_code IS NULL OR g.lithology_code NOT LIKE 'DERIVED-%')
   AND NOT EXISTS (
        SELECT 1 FROM silver.lithology l
         WHERE l.collar_id = g.collar_id
           AND round(l.from_depth, 3) = g.depth_from
           AND round(l.to_depth, 3) = g.depth_to
   )
"""

#: Duplicate (collar, from, to) lithology intervals, counted so the fold into
#: one gold band is reported rather than silent.
_LITHOLOGY_DUPLICATES = """
SELECT count(*) - count(DISTINCT (l.collar_id, round(l.from_depth, 3),
                                  round(l.to_depth, 3))) AS duplicates
  FROM silver.lithology l
  JOIN silver.collars c ON c.collar_id = l.collar_id
 WHERE c.project_id = $1::uuid
"""

#: Alteration intervals, rebuilt per project. One gold row per (collar, from,
#: to) - the table's unique key - so two alterations logged over the same
#: interval share a row and travel in ``alteration_payload``. silver.alteration
#: carries no project_id: the scope comes through the collar, and workspace_id
#: from the COLLAR as for every band above.
#:
#: Rebuilt (clear + insert in one transaction) rather than upserted: the rows
#: are a pure function of silver.alteration, and an upsert would leave the
#: intervals of a corrected log beside the old ones.
_INTERVALS_ALTERATION_CLEAR = """
DELETE FROM gold.drillhole_intervals_visual
 WHERE project_id = $1::uuid AND interval_kind = 'alteration'
"""

_INTERVALS_ALTERATION = """
INSERT INTO gold.drillhole_intervals_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth_from, depth_to, interval_kind,
    lithology_code, lithology_label, color_hint,
    assay_payload, alteration_payload, structure_payload,
    computed_at, created_at
)
SELECT gen_random_uuid(), a.collar_id, c.workspace_id, c.project_id,
       a.depth_from, a.depth_to, 'alteration',
       NULL,
       LEFT(string_agg(
           a.alteration_type
           || CASE WHEN NULLIF(a.intensity, '') IS NOT NULL
                   THEN ' (' || a.intensity || ')' ELSE '' END,
           '; ' ORDER BY a.created_at, a.id), 500),
       NULL,
       '{}'::jsonb,
       jsonb_build_object('alterations', jsonb_agg(
           jsonb_build_object(
               'type', a.alteration_type,
               'intensity', a.intensity,
               'minerals', COALESCE(to_jsonb(a.minerals), '[]'::jsonb),
               'notes', a.notes)
           ORDER BY a.created_at, a.id)),
       '{}'::jsonb,
       NOW(), NOW()
  FROM (
        SELECT x.id, x.collar_id, x.alteration_type, x.intensity, x.minerals,
               x.notes, x.created_at,
               round(x.from_depth, 3) AS depth_from,
               round(x.to_depth, 3) AS depth_to
          FROM silver.alteration x
          JOIN silver.collars cx ON cx.collar_id = x.collar_id
         WHERE cx.project_id = $1::uuid
           AND x.from_depth >= 0
           AND x.to_depth < 10000000
           AND round(x.to_depth, 3) > round(x.from_depth, 3)
       ) a
  JOIN silver.collars c ON c.collar_id = a.collar_id
 WHERE c.project_id = $1::uuid
 GROUP BY a.collar_id, c.workspace_id, c.project_id, a.depth_from, a.depth_to
"""

#: Mineralization intervals, rebuilt per project exactly as alteration is (§04e
#: ``mineralization`` kind, SME-approved 2026-09-29). silver.mineralization is
#: one row PER MINERAL; the gold table's unique key is (collar, from, to, kind),
#: so every mineral over one interval shares a row and travels in
#: ``mineralization_payload``. Scope comes through the collar (the table has no
#: project_id) and workspace_id from the COLLAR, so a mis-stamped row cannot
#: write a band into another tenant's project. Same depth guards as alteration:
#: a NUMERIC(10,3) overflow or a zero-width interval after rounding would fail
#: the one INSERT ... SELECT and promote nothing for the project.
#:
#: The clear only ever touches ``interval_kind = 'mineralization'``; lithology
#: (including the DERIVED-* gamma bands derive_intervals owns), alteration and
#: sample_window rows are other kinds and are not in its WHERE clause.
_INTERVALS_MINERALIZATION_CLEAR = """
DELETE FROM gold.drillhole_intervals_visual
 WHERE project_id = $1::uuid AND interval_kind = 'mineralization'
"""

_INTERVALS_MINERALIZATION = """
INSERT INTO gold.drillhole_intervals_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth_from, depth_to, interval_kind,
    lithology_code, lithology_label, color_hint,
    assay_payload, alteration_payload, structure_payload,
    mineralization_payload,
    computed_at, created_at
)
SELECT gen_random_uuid(), m.collar_id, c.workspace_id, c.project_id,
       m.depth_from, m.depth_to, 'mineralization',
       NULL,
       LEFT(string_agg(
           m.mineral
           || CASE WHEN m.abundance_pct IS NOT NULL
                   THEN ' ' || trim_scale(m.abundance_pct)::text || '%'
                   ELSE '' END,
           '; ' ORDER BY m.created_at, m.id), 500),
       NULL,
       '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
       jsonb_build_object('minerals', jsonb_agg(
           jsonb_build_object(
               'mineral', m.mineral,
               'abundance_pct', m.abundance_pct,
               'form', m.form,
               'grain_size', m.grain_size,
               'notes', m.notes)
           ORDER BY m.created_at, m.id)),
       NOW(), NOW()
  FROM (
        SELECT x.id, x.collar_id, x.mineral, x.abundance_pct, x.form,
               x.grain_size, x.notes, x.created_at,
               round(x.from_depth, 3) AS depth_from,
               round(x.to_depth, 3) AS depth_to
          FROM silver.mineralization x
          JOIN silver.collars cx ON cx.collar_id = x.collar_id
         WHERE cx.project_id = $1::uuid
           AND x.from_depth >= 0
           AND x.to_depth < 10000000
           AND round(x.to_depth, 3) > round(x.from_depth, 3)
       ) m
  JOIN silver.collars c ON c.collar_id = m.collar_id
 WHERE c.project_id = $1::uuid
 GROUP BY m.collar_id, c.workspace_id, c.project_id, m.depth_from, m.depth_to
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
#: run the projection still gets x/y.
#:
#: TWO THINGS THIS STATEMENT MUST SURVIVE, both learned when silver.structure
#: gained a real writer (ingest_tabular, 2026-09-29):
#:
#:   * gold.structure_measurements_visual has a CHECK on structure_type (twelve
#:     values), on dip (0-90), on dip direction (0-360) and on depth (>= 0),
#:     and this is ONE INSERT ... SELECT - a single out-of-vocabulary type or
#:     out-of-range angle anywhere in the project fails the whole statement
#:     and promotes nothing. silver.structure.structure_type is free text.
#:     So a type outside the vocabulary is carried as 'other' (inventing a
#:     mapping here would contradict §04e; the writer already maps the
#:     conventional synonyms, and silver keeps the original), and an
#:     out-of-range angle is carried as NULL rather than failing the batch.
#:   * The table has no unique key, so the ``ON CONFLICT DO NOTHING`` this
#:     statement used to end with could never conflict and every promotion run
#:     APPENDED a second copy of every measurement - a doubled stereonet
#:     after the first re-ingest. The gold rows are a pure function of
#:     silver, so the project's rows are cleared and rebuilt inside one
#:     transaction (_STRUCTURES_VISUAL_CLEAR).
_STRUCTURES_VISUAL_CLEAR = """
DELETE FROM gold.structure_measurements_visual WHERE project_id = $1::uuid
"""

_STRUCTURES_VISUAL = """
INSERT INTO gold.structure_measurements_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth, structure_type, strike_deg, dip_deg, dip_direction_deg,
    plunge_deg, trend_deg, stereonet_x, stereonet_y, projection,
    computed_at, created_at
)
SELECT gen_random_uuid(), s.collar_id, c.workspace_id, c.project_id,
       s.depth, s.structure_type,
       CASE WHEN s.dip_dir IS NULL THEN NULL
            ELSE MOD((s.dip_dir - 90 + 360)::numeric, 360) END,
       s.dip, s.dip_dir,
       NULL, NULL,
       CASE WHEN s.dip IS NULL OR s.dip_dir IS NULL THEN NULL ELSE
            SQRT(2) * SIN(RADIANS((90 - s.dip) / 2.0))
                    * SIN(RADIANS(MOD((s.dip_dir + 180)::numeric, 360)))
       END,
       CASE WHEN s.dip IS NULL OR s.dip_dir IS NULL THEN NULL ELSE
            SQRT(2) * SIN(RADIANS((90 - s.dip) / 2.0))
                    * COS(RADIANS(MOD((s.dip_dir + 180)::numeric, 360)))
       END,
       'equal_area', NOW(), NOW()
  FROM (
        SELECT st.collar_id, st.depth,
               CASE WHEN st.structure_type IN (
                        'fault', 'shear', 'fracture', 'joint', 'vein',
                        'foliation', 'cleavage', 'bedding', 'contact',
                        'fold_axis', 'lineation', 'other')
                    THEN st.structure_type ELSE 'other' END AS structure_type,
               CASE WHEN st.true_dip BETWEEN 0 AND 90
                    THEN st.true_dip END AS dip,
               CASE WHEN st.true_dip_dir BETWEEN 0 AND 360
                    THEN st.true_dip_dir END AS dip_dir
          FROM silver.structure st
         WHERE st.depth IS NOT NULL
           AND st.depth >= 0
           AND st.depth < 10000000
       ) s
  JOIN silver.collars c ON c.collar_id = s.collar_id
 WHERE c.project_id = $1::uuid
"""


# HAT-3 (2026-09-29): schedule_timeout matches ingest_pdf. With the
# per-workspace queue above, a promotion routinely waits behind another,
# and Hatchet's 5-minute default would cancel the queued one.
@promote_silver_to_gold.task(execution_timeout="20m", schedule_timeout="2h")
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

        # One catalogue fetch serves every project: rock_codes is
        # workspace-scoped and small, and the resolver wants it in memory.
        code_lookup = build_rock_code_lookup([
            dict(r) for r in await conn.fetch(
                "SELECT code, name, system FROM silver.rock_codes "
                "WHERE workspace_id = $1::uuid",
                input.workspace_id,
            )
        ])

        for project_id in project_ids:
            out.projects_seen += 1
            # Canonical silver first: nothing downstream depends on it, but
            # a run that dies mid-way should have promoted the table every
            # text-retrieval reader points at before drawing pictures.
            await _promote_lithology_canonical(
                conn,
                project_id=project_id,
                code_lookup=code_lookup,
                out=out,
            )
            await _promote_traces(
                conn,
                workspace_id=input.workspace_id,
                project_id=project_id,
                out=out,
            )
            duplicates = int(
                await conn.fetchval(_LITHOLOGY_DUPLICATES, project_id) or 0
            )
            if duplicates:
                out.lithology_duplicate_intervals += duplicates
                log.warning(
                    "promote_silver_to_gold: project %s has %d lithology "
                    "interval(s) sharing a (hole, from, to) with another; "
                    "each set was folded into one gold band",
                    project_id, duplicates,
                )
            # Upsert, then drop the bands silver no longer has, in one
            # transaction so the strip log never sees a half-rebuilt hole.
            async with conn.transaction():
                status = await conn.execute(_INTERVALS_LITHOLOGY, project_id)
                out.intervals_written += _affected(status)
                await conn.execute(_INTERVALS_LITHOLOGY_STALE, project_id)
            status = await conn.execute(_INTERVALS_SAMPLES, project_id)
            out.intervals_written += _affected(status)
            # Alteration: a pure function of silver.alteration, rebuilt.
            async with conn.transaction():
                await conn.execute(_INTERVALS_ALTERATION_CLEAR, project_id)
                status = await conn.execute(_INTERVALS_ALTERATION, project_id)
            out.alteration_intervals_written += _affected(status)
            # Mineralization: likewise a pure function of silver.mineralization.
            async with conn.transaction():
                await conn.execute(_INTERVALS_MINERALIZATION_CLEAR, project_id)
                status = await conn.execute(_INTERVALS_MINERALIZATION, project_id)
            out.mineralization_intervals_written += _affected(status)
            # Clear-and-rebuild in one transaction: see _STRUCTURES_VISUAL for
            # why an append (the old ON CONFLICT DO NOTHING) duplicated rows.
            async with conn.transaction():
                await conn.execute(_STRUCTURES_VISUAL_CLEAR, project_id)
                status = await conn.execute(_STRUCTURES_VISUAL, project_id)
            out.structures_written += _affected(status)
    finally:
        await conn.close()

    log.info(
        "promote_silver_to_gold: %d project(s), %d trace(s) written "
        "(%d unchanged, %d without geometry), %d interval(s), "
        "%d alteration / %d mineralization interval(s), %d structure(s), "
        "%d canonical lithology row(s) (%d unresolved code(s))",
        out.projects_seen, out.traces_written, out.traces_unchanged,
        out.traces_skipped_no_geometry, out.intervals_written,
        out.alteration_intervals_written, out.mineralization_intervals_written,
        out.structures_written, out.lithology_rows_promoted,
        out.lithology_codes_unresolved,
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
    "PROMOTION_DISPATCH_TIMEOUT_S",
    "dispatch_promotion",
    "promote_silver_to_gold",
    "PromoteSilverToGoldInput",
    "PromoteSilverToGoldOutput",
]
