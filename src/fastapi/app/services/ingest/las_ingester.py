"""LAS file ingester.

Doc-phase 179 — Phase B Tier 1.

Reads LAS 2.0 well-log files via `lasio`, lands:
  - One row in `silver.projects` per unique (company × field) pair
  - One row in `silver.collars` per unique hole_id within a project
  - N rows in `silver.well_log_curves` per LAS file (one per curve)
  - One row in `bronze.provenance` per ingested record

Collar location -- this ingester never invents one, and it carries no
dataset-specific placement (no PLSS section table, no per-company defaults).
A collar is placed only from, in order:

  0. A collar that already exists in the project (matched on hole_id, then
     on the canonical hole_id) is used as it stands. The right way to load
     LAS files is collar table first, curves second.
  1. Coordinates in the LAS ~WELL section:
       * LATI/LONG with a datum (GDAT) -> georef_method='declared';
       * X/Y/EAST/NORTH with a CRS the header names (EPSG/CRS/HZCS item) or
         the operator declared for the upload (``source_epsg``) -> 'declared';
       * X/Y/EAST/NORTH with no stated CRS, when the PROJECT carries an
         explicit ``crs_epsg`` -> 'assumed' plus ``las_collar_crs_assumed``.
     LATI/LONG with no usable datum is read as WGS84 and flagged the same
     way ('assumed' + ``las_collar_crs_assumed``).
  2. Otherwise the file is REFUSED with ``las_collar_unlocated`` naming the
     file and the well. It used to land at a Wyoming default coordinate
     (480000, 4660000 in EPSG:32613), and later at a PLSS-section centroid
     for one hard-coded Wyoming section -- a hole from anywhere drawn in
     Carbon County, WY, or a location the data never gave. silver.collars
     easting / northing are NOT NULL (2026_04_09_180100_create_collars_table),
     and a 0 or any other placeholder there is the same fabrication, so there
     is no "collar without a location" to fall back to and the schema is left
     alone. The refusal is not a loss: callers that hold the file keep it and
     attach it when the collar is written (services/ingest/las_pending.py).

The geometry is constructed at insert time via PostGIS ST_MakePoint +
ST_Transform, straight from the source CRS to EPSG:4326 (`geom_4326`, the
only collar geometry since the 32613 `geom` column was retired 2026-09-29).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg
import lasio
from georag_geoparsers._area_of_use import within_area
from georag_geoparsers.las_parser import (
    las_depth_unit_warning,
    resolve_las_depth_unit,
    unit_to_metres_factor,
)

log = logging.getLogger("georag.ingest.las")

#: silver.collars.geom_4326 is geometry(Point, 4326) — the only collar
#: geometry (the 32613 `geom` column was retired 2026-09-29, §04e).
COLLAR_GEOM_SRID = 4326

#: ~WELL mnemonics that carry a location. Deliberately not "E" / "N" / "LOC":
#: LOC is free text, not a coordinate, and single letters collide with other
#: headers.
_X_MNEMONICS = ("X", "XCOORD", "X_COORD", "EAST", "EASTING", "EASTINGS")
_Y_MNEMONICS = ("Y", "YCOORD", "Y_COORD", "NORTH", "NORTHING", "NORTHINGS")
_LAT_MNEMONICS = ("LATI", "LAT", "LATITUDE")
_LON_MNEMONICS = ("LONG", "LON", "LONGITUDE")
#: Header items that may state the coordinate reference.
_CRS_MNEMONICS = (
    "EPSG", "SRID", "CRS", "HZCS", "COORDSYS", "COORD_SYS", "PROJ", "PROJECTION",
)
_DATUM_MNEMONICS = ("GDAT", "DATUM", "GEODETIC")

_NULL_TOKENS = frozenset(
    {"", "NA", "N/A", "NAN", "NULL", "NONE", "UNKNOWN", "-", "--"},
)


@dataclass
class LASIngestResult:
    """Outcome of a single LAS file ingestion.

    ``warnings`` are ``{"code", "detail"}`` dicts in the same shape
    ingest_tabular reports, so the archive workflow can carry them onto its
    ingest_progress row. A skipped file always says why in one of them.
    """
    file_path: str
    hole_id: str
    project_id: str | None
    collar_id: str | None
    curves_inserted: int
    skipped: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    #: How a collar CREATED by this file was placed ('declared' / 'assumed');
    #: None when the collar already existed or the file was skipped.
    georef_method: str | None = None


@dataclass
class _Placement:
    """Where a new collar goes, and how much to believe it."""
    source_x: float          # lon or easting in `source_epsg`
    source_y: float          # lat or northing in `source_epsg`
    source_epsg: int
    easting: float           # value for silver.collars.easting
    northing: float          # value for silver.collars.northing
    georef_method: str       # 'declared' | 'assumed' (chk_collars_georef_method)
    warning: dict[str, str] | None = None


def _parse_las_date(d: str | None) -> date | None:
    """Parse a LAS DATE field. Common formats: '08/13/2012', '2012-08-13'."""
    if not d or d.upper() in ("NA", "N/A", ""):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(d.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Header coordinates
# ---------------------------------------------------------------------------

def _header_values(well: Any) -> dict[str, str]:
    """Upper-cased mnemonic -> stripped string value for the ~WELL section."""
    out: dict[str, str] = {}
    for item in well:
        mnemonic = str(getattr(item, "mnemonic", "") or "").strip().upper()
        if not mnemonic or mnemonic in out:
            continue
        out[mnemonic] = str(getattr(item, "value", "") or "").strip()
    return out


def _first(values: dict[str, str], names: tuple[str, ...]) -> tuple[str, str] | None:
    """The first of ``names`` present with a non-null value, as (name, value)."""
    for name in names:
        raw = values.get(name)
        if raw is not None and raw.upper() not in _NULL_TOKENS:
            return name, raw
    return None


def _to_float(raw: str) -> float | None:
    """A finite float from a header value, or None. No DMS, no guessing."""
    try:
        value = float(raw.replace(",", "").strip())
    except ValueError:
        log.debug("las_ingester.header_value_not_numeric raw=%r", raw)
        return None
    if not math.isfinite(value):
        return None
    # -999.25 / -9999 are LAS null sentinels, not coordinates.
    if value in (-999.25, -999.0, -9999.0, -99999.0):
        return None
    return value


def _crs_epsg_from_text(text: str, *, projected: bool | None) -> int | None:
    """An EPSG code from header text ('26913', 'EPSG:26913', 'NAD83 / UTM zone 13N').

    ``projected`` restricts the answer to projected (True) / geographic
    (False) systems. None when the text names nothing pyproj can resolve.
    """
    raw = text.strip()
    if raw.upper() in _NULL_TOKENS:
        return None
    from pyproj import CRS  # noqa: PLC0415 — heavy import, only needed here
    from pyproj.exceptions import CRSError  # noqa: PLC0415

    candidate = f"EPSG:{raw}" if raw.isdigit() else raw
    try:
        crs = CRS.from_user_input(candidate)
    except (CRSError, ValueError, TypeError):
        log.debug("las_ingester.crs_text_unresolved text=%r", raw)
        return None
    if projected is not None and crs.is_projected != projected:
        return None
    return crs.to_epsg()


def _within_crs_area(epsg: int, x: float, y: float) -> bool:
    """Whether (x, y) transforms to a valid lon/lat near the CRS's home area.

    Catches swapped axes and gross unit errors, which land far outside the
    CRS's published area. It does NOT catch a wrong UTM zone or a wrong
    datum (GIS-13): a zone's area of use spans every valid easting, so
    zone-12 coordinates read as zone 13 pass here while landing ~360 km
    east. That is what the project-reference check in
    ``_placement_plausibility`` (backed by services/ingest/collar_crs.py) is for.
    """
    from pyproj import CRS, Transformer  # noqa: PLC0415

    try:
        crs = CRS.from_epsg(epsg)
        lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)
    except Exception as exc:  # noqa: BLE001 — any pyproj failure means "not placeable"
        log.debug("las_ingester.crs_transform_failed epsg=%s x=%s y=%s err=%s", epsg, x, y, exc)
        return False
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return False
    if abs(lat) > 90 or abs(lon) > 180:
        return False
    area = crs.area_of_use
    # Antimeridian-aware (NAD83's area is west 167.65 / east -40.73): a plain
    # west <= lon <= east refused every point of such a CRS (GIS audit 2026-10).
    if area is not None and not within_area(area, lon, lat, slack_deg=3.0):
        return False
    return True


def _to_collar_srid(epsg: int, x: float, y: float) -> tuple[float, float]:
    """(lon, lat) in the collar geometry's SRID (4326), axis order x/y."""
    from pyproj import Transformer  # noqa: PLC0415

    e, n = Transformer.from_crs(
        f"EPSG:{epsg}", f"EPSG:{COLLAR_GEOM_SRID}", always_xy=True,
    ).transform(x, y)
    return float(e), float(n)


async def _project_crs_epsg(conn: asyncpg.Connection, project_id: str) -> int | None:
    """silver.projects.crs_epsg, or None when it is unset or unreadable."""
    try:
        value = await conn.fetchval(
            "SELECT crs_epsg FROM silver.projects WHERE project_id = $1::uuid",
            project_id,
        )
    except asyncpg.PostgresError as exc:
        log.warning("las_ingester.project_crs_lookup_failed project=%s err=%s", project_id, exc)
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _placement_from_header(
    conn: asyncpg.Connection,
    *,
    values: dict[str, str],
    project_id: str,
    source_epsg: int | None,
    hole_id: str,
) -> tuple[_Placement | None, list[str]]:
    """Coordinates the LAS header itself carries, or (None, why-not notes).

    Geographic (LATI/LONG) is tried before projected (X/Y): a file with both
    states the unambiguous one. The notes name every header value that was
    present but unusable, so the refusal/assumption warning can say so.
    """
    notes: list[str] = []
    datum = _first(values, _DATUM_MNEMONICS)
    crs_item = _first(values, _CRS_MNEMONICS)

    lat_item = _first(values, _LAT_MNEMONICS)
    lon_item = _first(values, _LON_MNEMONICS)
    if lat_item and lon_item:
        lat, lon = _to_float(lat_item[1]), _to_float(lon_item[1])
        if lat is None or lon is None:
            notes.append(
                f"{lat_item[0]}={lat_item[1]!r} / {lon_item[0]}={lon_item[1]!r} "
                "are not decimal degrees",
            )
        elif abs(lat) > 90 or abs(lon) > 180 or (lat == 0.0 and lon == 0.0):
            notes.append(f"{lat_item[0]}={lat} / {lon_item[0]}={lon} is not a valid position")
        else:
            epsg = None
            if datum:
                epsg = _crs_epsg_from_text(datum[1], projected=False)
            if epsg is None and crs_item:
                epsg = _crs_epsg_from_text(crs_item[1], projected=False)
            declared = epsg is not None
            if epsg is None:
                epsg = 4326
            # Transformed only to prove the position is representable; the
            # row keeps the SOURCE values (GIS-6: easting/northing are "as
            # the source gave them" on every path, geom_4326 is the truth).
            e, n = _to_collar_srid(epsg, lon, lat)
            if not (math.isfinite(e) and math.isfinite(n)):
                notes.append("the position does not transform to WGS84 (EPSG:4326)")
            else:
                warning = None
                if not declared:
                    stated = f" (its {datum[0]} reads {datum[1]!r}, which is not recognised)" if datum else ""
                    warning = {
                        "code": "las_collar_crs_assumed",
                        "detail": (
                            f"Well {hole_id!r}: the LAS header gives {lat_item[0]}/{lon_item[0]} "
                            f"but no usable datum{stated}; they were read as WGS84 (EPSG:4326). "
                            "Upload the collar table with a declared CRS if that is wrong."
                        ),
                    }
                return _Placement(
                    source_x=lon, source_y=lat, source_epsg=epsg,
                    easting=lon, northing=lat,
                    georef_method="declared" if declared else "assumed",
                    warning=warning,
                ), notes

    x_item = _first(values, _X_MNEMONICS)
    y_item = _first(values, _Y_MNEMONICS)
    if x_item and y_item:
        x, y = _to_float(x_item[1]), _to_float(y_item[1])
        if x is None or y is None:
            notes.append(f"{x_item[0]}={x_item[1]!r} / {y_item[0]}={y_item[1]!r} are not numbers")
        elif x == 0.0 and y == 0.0:
            notes.append(f"{x_item[0]}/{y_item[0]} are both 0")
        else:
            epsg = None
            origin = ""
            if crs_item:
                epsg = _crs_epsg_from_text(crs_item[1], projected=True)
                origin = f"the LAS header ({crs_item[0]}={crs_item[1]!r})"
            if epsg is None and source_epsg:
                epsg = source_epsg
                origin = "the CRS declared with this upload"
            declared = epsg is not None
            if epsg is None:
                epsg = await _project_crs_epsg(conn, project_id)
                origin = "the project's CRS"
            if epsg is None:
                notes.append(
                    f"{x_item[0]}/{y_item[0]} are present but no CRS is stated in the "
                    "header, on the upload, or on the project",
                )
            elif not _within_crs_area(epsg, x, y):
                notes.append(
                    f"{x_item[0]}={x} / {y_item[0]}={y} do not fall inside the area "
                    f"EPSG:{epsg} covers (wrong CRS, feet read as metres, or swapped axes)",
                )
            else:
                warning = None
                if not declared:
                    warning = {
                        "code": "las_collar_crs_assumed",
                        "detail": (
                            f"Well {hole_id!r}: the LAS header gives {x_item[0]}/{y_item[0]} "
                            f"but states no CRS; {origin} (EPSG:{epsg}) was used. "
                            "Upload the collar table with a declared CRS if that is wrong."
                        ),
                    }
                return _Placement(
                    source_x=x, source_y=y, source_epsg=epsg,
                    easting=x, northing=y,
                    georef_method="declared" if declared else "assumed",
                    warning=warning,
                ), notes

    return None, notes


async def _placement_plausibility(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    hole_id: str,
    placement: _Placement,
    file_name: str,
) -> list[dict[str, str]]:
    """Warnings when a header-placed collar lands somewhere unbelievable.

    GIS-13 / GIS-15: the LATI/LONG branch had no area check at all, so a
    positive-west longitude (common in older US headers) put a Wyoming well
    in China with only 'assumed' to show for it; the X/Y branch's area check
    cannot see a wrong UTM zone. Both are now compared with the project's
    boundary or its other placed collars (services/ingest/collar_crs.py).
    Warn only — the collar is still created.
    """
    from app.services.ingest.collar_crs import (  # noqa: PLC0415
        plausibility_warnings,
        project_reference,
    )

    reference = await project_reference(conn, project_id, exclude_hole_ids=[hole_id])
    found, _flagged = await asyncio.to_thread(
        plausibility_warnings,
        epsg=placement.source_epsg,
        points=[(hole_id, placement.source_x, placement.source_y)],
        reference=reference,
        label=file_name,
    )
    return [{"code": w["code"], "detail": w["detail"]} for w in found]


async def _get_or_create_project(
    conn: asyncpg.Connection,
    *,
    project_name: str,
    company: str,
    region: str | None,
    workspace_id: str,
    commodity: str | None = None,
) -> str:
    """Idempotently fetch or create a `silver.projects` row.

    `commodity` defaults to None, not a literal. This ingester was written
    for the Wyoming Cameco / WSGS uranium archive and the default used to
    be "uranium", which stamped that commodity onto the project row of any
    LAS file — gold, copper, lithium — that reached this path. The column
    is nullable and NULL is the honest value for "the LAS file does not
    say". LAS 2.0 has no commodity field in the ~W section, so the only
    truthful source is the caller. Mirrors the same fix applied to
    silver.reports.commodity in xlsx_ingester (2026-08-21).

    Returns the project_id (UUID as string).
    """
    slug = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-")[:200]
    row = await conn.fetchrow(
        "SELECT project_id::text AS project_id FROM silver.projects WHERE slug = $1 LIMIT 1",
        slug,
    )
    if row:
        return row["project_id"]
    # orientation_reference: BOH, the platform default (Project::
    # DEFAULT_ORIENTATION_REFERENCE). This wrote 'grid_north', a north
    # reference outside the BOH|TOH vocabulary (audit 2026-09-29 PG-14).
    row = await conn.fetchrow(
        """
        INSERT INTO silver.projects
            (project_id, project_name, slug, company, region, commodity,
             orientation_reference, status, workspace_id,
             created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5,
                'BOH', 'active', $6::uuid,
                NOW(), NOW())
        RETURNING project_id::text AS project_id
        """,
        project_name, slug, company, region, commodity, workspace_id,
    )
    # crs_epsg is deliberately NOT written. This row used to be stamped
    # 32613 / 'EPSG:32613' (the first archive's zone) whatever the data was,
    # which made "the project has an explicit CRS" true of every project this
    # ingester ever created. NULL means the project has none declared.
    log.info("las_ingester.project_created name=%s slug=%s", project_name, slug)
    return row["project_id"]


def _canonical_hole_id(hole_id: str) -> str | None:
    """Strip separators + uppercase.

    Mirrors the rule baked into the CSV parser
    (parsers/_hole_id.py::canonicalize) so the chat retrieval path can join
    on silver.collars.hole_id_canonical without waiting on a backfill sweep.
    """
    return re.sub(r"[ \-_./]+", "", (hole_id or "").strip()).upper() or None


async def _find_collar(
    conn: asyncpg.Connection, *, project_id: str, hole_id: str,
) -> str | None:
    """The project's existing collar for this hole, or None.

    Exact hole_id first; then the canonical form, so a LAS whose WELL reads
    'SRE09_6' finds the collar the collar table loaded as 'SRE09-6' instead
    of minting a second, unlocated one beside it.
    """
    row = await conn.fetchrow(
        """
        SELECT collar_id::text AS collar_id
          FROM silver.collars
         WHERE project_id = $1::uuid AND hole_id = $2
         LIMIT 1
        """,
        project_id, hole_id,
    )
    if row:
        return row["collar_id"]
    canonical = _canonical_hole_id(hole_id)
    if not canonical:
        return None
    row = await conn.fetchrow(
        """
        SELECT collar_id::text AS collar_id
          FROM silver.collars
         WHERE project_id = $1::uuid AND hole_id_canonical = $2
         ORDER BY created_at
         LIMIT 1
        """,
        project_id, canonical,
    )
    return row["collar_id"] if row else None


async def _create_collar(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    hole_id: str,
    placement: _Placement,
    total_depth: float | None,
    drill_date: date | None,
    workspace_id: str,
) -> str:
    """Insert a `silver.collars` row at an already-justified placement.

    Idempotent on (project_id, hole_id_canonical) — the collar key since
    2026-09-29 (§04e): a concurrent writer that got there first, under any
    spelling of the hole id, wins and its collar_id is returned, untouched.
    """
    hole_id_canonical = _canonical_hole_id(hole_id)
    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, hole_id_canonical, project_id, easting, northing, total_depth,
             hole_type, status, drill_date, georef_method,
             geom_4326, workspace_id, created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3::uuid, $4, $5, $6,
                'exploration', 'active', $7, $8,
                ST_Transform(ST_SetSRID(ST_MakePoint($9, $10), $11::int), 4326),
                $12::uuid, NOW(), NOW())
        ON CONFLICT (project_id, hole_id_canonical) WHERE hole_id_canonical IS NOT NULL
        DO UPDATE SET updated_at = silver.collars.updated_at
        RETURNING collar_id::text AS collar_id
        """,
        hole_id, hole_id_canonical, project_id, placement.easting, placement.northing,
        total_depth, drill_date, placement.georef_method,
        placement.source_x, placement.source_y, placement.source_epsg,
        workspace_id,
    )
    return row["collar_id"]


async def _insert_curve(
    conn: asyncpg.Connection,
    *,
    collar_id: str,
    curve_name: str,
    curve_unit: str | None,
    curve_description: str | None,
    depths: list[float],
    values: list[float],
    las_version: str,
    source_file: str,
    workspace_id: str,
    null_value: float = -999.25,
) -> None:
    """Insert or replace a `silver.well_log_curves` row for one LAS curve.

    ``depths`` must already be METRES (see ``ingest_las_file``); the row is
    stamped ``depth_unit = 'm'``.
    """
    # Doc-phase 183 — Cameco T_DEPTH curves start at -0.1ft or -0.2ft
    # (legitimate above-ground tool-reference measurements). The
    # `chk_well_log_curves_min_depth_non_negative` constraint rejects
    # these. Clamp negative depths to 0 + clip matching values.
    if depths and depths[0] < 0:
        first_pos_idx = next(
            (i for i, d in enumerate(depths) if d >= 0), len(depths),
        )
        depths = depths[first_pos_idx:]
        values = values[first_pos_idx:]
    min_d = min(depths) if depths else 0.0
    max_d = max(depths) if depths else 0.0
    if max_d <= min_d:
        log.warning(
            "las_ingester.curve_skip_invalid_depth collar=%s curve=%s min=%.3f max=%.3f",
            collar_id, curve_name, min_d, max_d,
        )
        return
    step = (max_d - min_d) / max(1, len(depths) - 1) if len(depths) > 1 else None

    await conn.execute(
        """
        INSERT INTO silver.well_log_curves
            (curve_id, collar_id, curve_name, curve_unit, curve_description,
             min_depth, max_depth, step, null_value, sample_count,
             las_version, source_file, depths, values,
             workspace_id, depth_unit, created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12::float8[], $13::float8[],
                $14::uuid, 'm', NOW(), NOW())
        ON CONFLICT (collar_id, curve_name) DO UPDATE
        SET depth_unit     = EXCLUDED.depth_unit,
            min_depth      = EXCLUDED.min_depth,
            max_depth      = EXCLUDED.max_depth,
            step           = EXCLUDED.step,
            null_value     = EXCLUDED.null_value,
            sample_count   = EXCLUDED.sample_count,
            las_version    = EXCLUDED.las_version,
            source_file    = EXCLUDED.source_file,
            depths         = EXCLUDED.depths,
            values         = EXCLUDED.values,
            curve_unit     = EXCLUDED.curve_unit,
            curve_description = EXCLUDED.curve_description,
            updated_at     = NOW()
        """,
        collar_id, curve_name, curve_unit, curve_description,
        min_d, max_d, step, null_value, len(depths),
        las_version, source_file[:255], depths, values,
        workspace_id,
    )


async def _emit_provenance(
    conn: asyncpg.Connection,
    *,
    target_table: str,
    target_id: str,
    source_file: str,
    source_sha256: str,
    parser_name: str = "lasio",
    parser_version: str = "0.32",
    ingest_run_id: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO bronze.provenance
            (provenance_id, target_schema, target_table, target_id,
             source_file, source_file_sha256,
             parser_name, parser_version, ingested_at, ingest_run_id)
        VALUES (gen_random_uuid(), 'silver', $1, $2::uuid,
                $3, $4, $5, $6, NOW(), $7)
        """,
        target_table, target_id, source_file, source_sha256,
        parser_name, parser_version,
        asyncpg.pgproto.types.UUID(ingest_run_id) if ingest_run_id else None,
    )


async def ingest_las_file(
    conn: asyncpg.Connection,
    las_path: str,
    *,
    workspace_id: str,
    project_name_fallback: str = "LAS import",
    company_fallback: str = "Unknown Operator",
    ingest_run_id: str | None = None,
    project_id_override: str | None = None,
    source_epsg: int | None = None,
    hole_id_override: str | None = None,
) -> LASIngestResult:
    """Ingest one LAS file into silver.* + bronze.provenance.

    Args:
        conn: asyncpg connection (transactioned at caller level)
        las_path: path to the LAS file on disk
        workspace_id: silver.workspaces UUID for RLS scoping
        project_name_fallback: used if LAS COMP field is empty
        company_fallback: used if LAS COMP field is empty
        ingest_run_id: optional bronze.ingest_runs link
        source_epsg: CRS the operator declared for the upload; used for
            projected X/Y in the LAS header when the header names none.
        hole_id_override: the hole this file belongs to when the operator (or a
            kept-for-later record) says so, instead of the header's WELL item.

    Returns:
        LASIngestResult describing what landed. A file whose collar cannot
        be located is returned ``skipped`` with ``skipped_reason=
        'collar_unlocated'`` and a ``las_collar_unlocated`` warning; nothing
        is written for it.
    """
    p = Path(las_path)
    try:
        # Hard rule 2 — lasio.read parses the whole curve set synchronously;
        # a 3,000 m hole logged every 15 cm is 20,000 samples per curve.
        las = await asyncio.to_thread(lasio.read, str(p))
    except Exception as e:
        return LASIngestResult(
            file_path=las_path, hole_id="", project_id=None, collar_id=None,
            curves_inserted=0, skipped=True,
            skipped_reason="lasio_read_failed",
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    # Well metadata
    well = las.well
    hole_id = (hole_id_override or str(
        well.get("WELL", lasio.HeaderItem("WELL", value="")).value,
    )).strip()
    if not hole_id:
        return LASIngestResult(
            file_path=las_path, hole_id="", project_id=None, collar_id=None,
            curves_inserted=0, skipped=True,
            skipped_reason="missing_well_id",
        )

    company = str(well.get("COMP", lasio.HeaderItem("COMP", value="")).value).strip() or company_fallback
    field_name = str(well.get("FLD", lasio.HeaderItem("FLD", value="")).value).strip()
    county = str(well.get("CNTY", lasio.HeaderItem("CNTY", value="")).value).strip()
    state = str(well.get("STAT", lasio.HeaderItem("STAT", value="")).value).strip()
    date_str = str(well.get("DATE", lasio.HeaderItem("DATE", value="")).value).strip()

    # Depth unit (GIS-4): read from the header, never assumed to be feet.
    # silver.collars.total_depth and every curve depth are metres, so a
    # STOP.F 1257.5 is 383.3 m, not a 1,257 m hole. STOP's own unit wins
    # for STOP (a file may declare it where the index does not).
    depth_unit = resolve_las_depth_unit(las)
    try:
        # Not .get(): lasio's SectionItems.get() fabricates a HeaderItem for
        # a missing key instead of returning the default.
        stop_item = las.well["STOP"] if "STOP" in las.well else None  # noqa: SIM401
        stop_value = float(stop_item.value) if stop_item is not None else None
        stop_factor = (
            unit_to_metres_factor(stop_item.unit) if stop_item is not None else None
        )
        total_depth: float | None = (
            stop_value * (stop_factor if stop_factor is not None else depth_unit.factor)
            if stop_value is not None else None
        )
    except (TypeError, ValueError):
        # Reported below as las_invalid_stop_depth, with the file name.
        log.debug("las_ingester.stop_not_numeric file=%s", p.name)
        total_depth = None
    drill_date = _parse_las_date(date_str)

    # Project name — derive from company + field if both present, else fallback
    project_name = f"{company} — {field_name}" if company and field_name else project_name_fallback

    region = ", ".join(filter(None, [county, state])) or None

    if project_id_override:
        project_id = project_id_override
    else:
        project_id = await _get_or_create_project(
            conn,
            project_name=project_name,
            company=company,
            region=region,
            workspace_id=workspace_id,
        )

    warnings: list[dict[str, str]] = []
    if total_depth is None or total_depth <= 0:
        # total_depth is optional since 2026-09-29 (§04e, SME-approved): a
        # missing or non-positive STOP no longer costs the file its curves.
        # The collar (if this file creates it) is stored with NULL total
        # depth — never 0 — and the warning names the file so the header
        # can still be fixed.
        stop_raw = las.well["STOP"].value if "STOP" in las.well else None
        warnings.append({
            "code": "las_invalid_stop_depth",
            "detail": (
                f"{p.name}: well {hole_id!r} has STOP = {stop_raw!r} in its ~WELL "
                "section (the bottom depth), which is not a positive depth. The "
                "curves were loaded; the hole has no total depth from this file. "
                "Correct STOP and upload it again to record one."
            ),
        })
        log.warning("las_ingester.invalid_stop file=%s well=%s stop=%r", p.name, hole_id, stop_raw)
        total_depth = None

    unit_warning = las_depth_unit_warning(depth_unit, file_name=p.name, well=hole_id)
    if unit_warning is not None:
        warnings.append({"code": unit_warning["code"], "detail": unit_warning["detail"]})
        log.warning("las_ingester.depth_unit_assumed file=%s declared=%r", p.name, depth_unit.declared)
    georef_method: str | None = None
    collar_id = await _find_collar(conn, project_id=project_id, hole_id=hole_id)
    if collar_id is None:
        placement, header_notes = await _placement_from_header(
            conn,
            values=_header_values(well),
            project_id=project_id,
            source_epsg=source_epsg,
            hole_id=hole_id,
        )
        if placement is None:
            why = "; ".join(header_notes) or "its ~WELL section carries no coordinates"
            detail = (
                f"{p.name}: well {hole_id!r} has no collar in this project and cannot be "
                f"located ({why}). Its curves were not loaded by this call; the caller "
                "decides whether to keep the file until the collar exists "
                "(see las_pending.py)."
            )
            log.warning("las_ingester.collar_unlocated file=%s well=%s why=%s", p.name, hole_id, why)
            return LASIngestResult(
                file_path=las_path, hole_id=hole_id, project_id=project_id, collar_id=None,
                curves_inserted=0, skipped=True,
                skipped_reason="collar_unlocated",
                warnings=[{"code": "las_collar_unlocated", "detail": detail}],
            )
        for implausible in await _placement_plausibility(
            conn, project_id=project_id, hole_id=hole_id,
            placement=placement, file_name=p.name,
        ):
            warnings.append(implausible)
            log.warning(
                "las_ingester.%s file=%s well=%s", implausible["code"], p.name, hole_id,
            )
        collar_id = await _create_collar(
            conn,
            project_id=project_id,
            hole_id=hole_id,
            placement=placement,
            total_depth=total_depth,
            drill_date=drill_date,
            workspace_id=workspace_id,
        )
        georef_method = placement.georef_method
        if placement.warning is not None:
            warnings.append({**placement.warning, "detail": f"{p.name}: {placement.warning['detail']}"})
            log.warning(
                "las_ingester.%s file=%s well=%s georef=%s",
                placement.warning["code"], p.name, hole_id, placement.georef_method,
            )

    # Compute source file sha256 once
    sha = hashlib.sha256(p.read_bytes()).hexdigest()

    # Curves — skip the DEPT curve itself (it's the index); insert others
    curves_inserted = 0
    null_value = float(las.well["NULL"].value) if "NULL" in las.well else -999.25
    las_version = str(las.version["VERS"].value) if "VERS" in las.version else "2.0"
    # Metres, like every other depth in silver (GIS-4); depth_unit='m' on
    # the row tells derive_intervals so.
    depths = [float(d) * depth_unit.factor for d in las.index.tolist()]

    for curve in las.curves:
        if curve.mnemonic.upper() in ("DEPT", "DEPTH"):
            continue
        # lasio returns numpy arrays; convert to list[float] for asyncpg
        try:
            values = [
                float(v) if v is not None else null_value
                for v in curve.data.tolist()
            ]
        except Exception as e:
            log.warning(
                "las_ingester.curve_values_convert_failed file=%s curve=%s err=%s",
                las_path, curve.mnemonic, e,
            )
            continue
        await _insert_curve(
            conn,
            collar_id=collar_id,
            curve_name=curve.mnemonic[:50],
            curve_unit=(str(curve.unit)[:20] if curve.unit else None),
            curve_description=(str(curve.descr) if curve.descr else None),
            depths=depths,
            values=values,
            las_version=las_version,
            source_file=p.name,
            workspace_id=workspace_id,
            null_value=null_value,
        )
        curves_inserted += 1

    # Provenance — link the collar to the source LAS
    try:
        await _emit_provenance(
            conn,
            target_table="collars",
            target_id=collar_id,
            source_file=str(p)[:1000],
            source_sha256=sha,
            ingest_run_id=ingest_run_id,
        )
    except Exception as e:
        log.warning("las_ingester.provenance_emit_failed err=%s", e)

    return LASIngestResult(
        file_path=las_path,
        hole_id=hole_id,
        project_id=project_id,
        collar_id=collar_id,
        curves_inserted=curves_inserted,
        warnings=warnings,
        georef_method=georef_method,
    )


__all__ = [
    "ingest_las_file",
    "LASIngestResult",
]
