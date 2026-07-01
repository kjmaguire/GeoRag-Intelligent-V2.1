"""LAS file ingester for Wyoming Cameco / WSGS uranium drillhole archive.

Doc-phase 179 — Phase B Tier 1.

Reads LAS 2.0 well-log files via `lasio`, lands:
  - One row in `silver.projects` per unique (company × field) pair
  - One row in `silver.collars` per unique hole_id within a project
  - N rows in `silver.well_log_curves` per LAS file (one per curve)
  - One row in `bronze.provenance` per ingested record

Coordinate handling:
  Cameco LAS files have LAT/LON='NA' (proprietary scrubbing). We derive
  approximate coordinates from the PLSS Township-Range-Section in the
  inner-zip directory name + the LAS `LOC` field. For Shirley Basin
  (T28N R79W) the reference point is approximate UTM Zone 13N
  (EPSG:32613). Per-hole offset is derived from a deterministic hash of
  hole_id, keeping holes within the section boundary (~1 mile).

The geom is constructed at insert time via PostGIS ST_MakePoint +
ST_Transform for the 4326 mirror column.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import asyncpg
import lasio

log = logging.getLogger("georag.ingest.las")


# PLSS section centroids — keyed by "{township}{N|S}{range}{E|W}{section}".
# Reference centroids in UTM Zone 13N (EPSG:32613).
# For Shirley Basin operations (T28N R79W), the reference is approximate;
# Cameco operations cluster in section 36.
PLSS_REFERENCE_UTM: dict[str, tuple[float, float]] = {
    # Format: "TTTNRRRWSS" → (easting_m, northing_m) in EPSG:32613
    "028N079W36": (471_000.0, 4_657_000.0),  # Shirley Basin, Carbon Co, WY
    # Additional sections added as new clusters land
}

# Default Wyoming UTM 13N coordinates when section not in lookup
DEFAULT_UTM_FALLBACK: tuple[float, float] = (480_000.0, 4_660_000.0)


@dataclass
class LASIngestResult:
    """Outcome of a single LAS file ingestion."""
    file_path: str
    hole_id: str
    project_id: str | None
    collar_id: str | None
    curves_inserted: int
    skipped: bool = False
    skipped_reason: str | None = None
    error: str | None = None


def _parse_plss_loc(loc: str) -> tuple[int, int, int] | None:
    """Parse a LAS LOC field like '36    28    79' → (section, township, range).

    Returns None if the format doesn't match.
    """
    if not loc:
        return None
    parts = re.findall(r"\d+", loc)
    if len(parts) >= 3:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    return None


def _hole_offset_meters(hole_id: str) -> tuple[float, float]:
    """Deterministic small offset within a PLSS section based on hole_id.

    Returns (delta_easting, delta_northing) where each is in (-800, +800)
    meters — keeps holes within the ~1-mile section boundary.
    """
    h = hashlib.sha256(hole_id.encode()).hexdigest()
    # Two hex chunks, normalize to (-1, 1), scale to ±800m
    de = (int(h[:8], 16) / 0xFFFFFFFF - 0.5) * 1600.0
    dn = (int(h[8:16], 16) / 0xFFFFFFFF - 0.5) * 1600.0
    return (de, dn)


def _derive_coordinates(
    plss_section_key: str | None,
    hole_id: str,
) -> tuple[float, float]:
    """Return (easting, northing) in UTM Zone 13N (EPSG:32613).

    Uses PLSS_REFERENCE_UTM if the section is known, else falls back to
    DEFAULT_UTM_FALLBACK. Adds a deterministic per-hole offset.
    """
    base = PLSS_REFERENCE_UTM.get(plss_section_key or "", DEFAULT_UTM_FALLBACK)
    de, dn = _hole_offset_meters(hole_id)
    return (base[0] + de, base[1] + dn)


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


async def _get_or_create_project(
    conn: asyncpg.Connection,
    *,
    project_name: str,
    company: str,
    region: str,
    workspace_id: str,
    commodity: str = "uranium",
) -> str:
    """Idempotently fetch or create a `silver.projects` row.

    Returns the project_id (UUID as string).
    """
    slug = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-")[:200]
    row = await conn.fetchrow(
        "SELECT project_id::text AS project_id FROM silver.projects WHERE slug = $1 LIMIT 1",
        slug,
    )
    if row:
        return row["project_id"]
    row = await conn.fetchrow(
        """
        INSERT INTO silver.projects
            (project_id, project_name, slug, company, region, commodity,
             crs_datum, crs_epsg, orientation_reference, status, workspace_id,
             created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5,
                'EPSG:32613', 32613, 'grid_north', 'active', $6::uuid,
                NOW(), NOW())
        RETURNING project_id::text AS project_id
        """,
        project_name, slug, company, region, commodity, workspace_id,
    )
    log.info("las_ingester.project_created name=%s slug=%s", project_name, slug)
    return row["project_id"]


async def _get_or_create_collar(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    hole_id: str,
    easting: float,
    northing: float,
    total_depth: float,
    drill_date: date | None,
    workspace_id: str,
) -> str:
    """Idempotently fetch or create a `silver.collars` row.

    Returns the collar_id (UUID as string).
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
    # Canonicalize hole_id on insert so the chat retrieval path can join on
    # silver.collars.hole_id_canonical without waiting on a backfill sweep.
    # Mirrors the rule baked into the CSV parser
    # (parsers/_hole_id.py::canonicalize): strip separators + uppercase.
    hole_id_canonical = re.sub(r"[ \-_./]+", "", (hole_id or "").strip()).upper() or None

    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, hole_id_canonical, project_id, easting, northing, total_depth,
             hole_type, status, drill_date, geom, geom_4326,
             workspace_id, created_at, updated_at)
        VALUES (gen_random_uuid(), $1, $2, $3::uuid, $4, $5, $6,
                'exploration', 'active', $7,
                ST_SetSRID(ST_MakePoint($4, $5), 32613),
                ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), 32613), 4326),
                $8::uuid, NOW(), NOW())
        RETURNING collar_id::text AS collar_id
        """,
        hole_id, hole_id_canonical, project_id, easting, northing, total_depth, drill_date,
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
    """Insert or replace a `silver.well_log_curves` row for one LAS curve."""
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
             workspace_id, created_at, updated_at)
        VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12::float8[], $13::float8[],
                $14::uuid, NOW(), NOW())
        ON CONFLICT (collar_id, curve_name) DO UPDATE
        SET min_depth      = EXCLUDED.min_depth,
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
    project_name_fallback: str = "Wyoming WSGS Uranium Archive",
    company_fallback: str = "Unknown Operator",
    plss_section_key: str | None = None,
    ingest_run_id: str | None = None,
    project_id_override: str | None = None,
) -> LASIngestResult:
    """Ingest one LAS file into silver.* + bronze.provenance.

    Args:
        conn: asyncpg connection (transactioned at caller level)
        las_path: path to the LAS file on disk
        workspace_id: silver.workspaces UUID for RLS scoping
        project_name_fallback: used if LAS COMP field is empty
        company_fallback: used if LAS COMP field is empty
        plss_section_key: e.g. "028N079W36" — overrides LOC-field parse
        ingest_run_id: optional bronze.ingest_runs link

    Returns:
        LASIngestResult describing what landed.
    """
    p = Path(las_path)
    try:
        las = lasio.read(str(p))
    except Exception as e:
        return LASIngestResult(
            file_path=las_path, hole_id="", project_id=None, collar_id=None,
            curves_inserted=0, skipped=True,
            skipped_reason="lasio_read_failed",
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    # Well metadata
    well = las.well
    hole_id = str(well.get("WELL", lasio.HeaderItem("WELL", value="")).value).strip()
    if not hole_id:
        return LASIngestResult(
            file_path=las_path, hole_id="", project_id=None, collar_id=None,
            curves_inserted=0, skipped=True,
            skipped_reason="missing_well_id",
        )

    company = str(well.get("COMP", lasio.HeaderItem("COMP", value="")).value).strip() or company_fallback
    field = str(well.get("FLD", lasio.HeaderItem("FLD", value="")).value).strip()
    county = str(well.get("CNTY", lasio.HeaderItem("CNTY", value="")).value).strip()
    state = str(well.get("STAT", lasio.HeaderItem("STAT", value="")).value).strip()
    loc = str(well.get("LOC", lasio.HeaderItem("LOC", value="")).value).strip()
    date_str = str(well.get("DATE", lasio.HeaderItem("DATE", value="")).value).strip()

    # PLSS parse — if LOC field present, derive section_key
    plss_parsed = _parse_plss_loc(loc)
    if not plss_section_key and plss_parsed:
        section, township, range_ = plss_parsed
        # Format as "TTTN" + "RRRW" + "SS" — assume N township + W range
        # (Wyoming is all N township; range W is dominant in W Wyoming)
        plss_section_key = f"{township:03d}N{range_:03d}W{section:02d}"

    easting, northing = _derive_coordinates(plss_section_key, hole_id)
    total_depth = float(las.well["STOP"].value) if "STOP" in las.well else 0.0
    drill_date = _parse_las_date(date_str)

    # Project name — derive from company + field if both present, else fallback
    project_name = f"{company} — {field}" if company and field else project_name_fallback

    region = ", ".join(filter(None, [county, state])) or "Wyoming"

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

    if total_depth <= 0:
        return LASIngestResult(
            file_path=las_path, hole_id=hole_id, project_id=project_id, collar_id=None,
            curves_inserted=0, skipped=True,
            skipped_reason="invalid_total_depth",
        )

    collar_id = await _get_or_create_collar(
        conn,
        project_id=project_id,
        hole_id=hole_id,
        easting=easting,
        northing=northing,
        total_depth=total_depth,
        drill_date=drill_date,
        workspace_id=workspace_id,
    )

    # Compute source file sha256 once
    sha = hashlib.sha256(p.read_bytes()).hexdigest()

    # Curves — skip the DEPT curve itself (it's the index); insert others
    curves_inserted = 0
    null_value = float(las.well["NULL"].value) if "NULL" in las.well else -999.25
    las_version = str(las.version["VERS"].value) if "VERS" in las.version else "2.0"
    depths = list(las.index.tolist())

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
    )


__all__ = [
    "ingest_las_file",
    "LASIngestResult",
    "PLSS_REFERENCE_UTM",
]
