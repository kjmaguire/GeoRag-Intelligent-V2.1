"""Cameco binary .log header parser for Wyoming uranium drillhole archive.

Doc-phase 179 — Phase B Tier 1.

Cameco gamma-tool logs are proprietary binary format. The first ~2KB
contains a fixed-position text header carrying the same metadata as
the paired LAS file PLUS surveyed coordinates (the LAS file has
LAT/LON='NA' but the .log has the actual state plane easting/northing).

Strategy:
  1. Read the first 4096 bytes (header zone)
  2. Decode as latin-1 (forgiving — binary tail won't crash)
  3. Regex out: hole_id, basin, county, state, section/township/range,
     hole_name, easting (E=), northing (N=)
  4. If a collar already exists for hole_id (LAS ingest happened first),
     UPDATE its easting/northing with the surveyed values
  5. Else, create a stub collar

The E=/N= pair is NAD83 / Wyoming East in US survey feet. The operator
declares it as EPSG:3736 (the ftUS code, used as-is) or EPSG:32155 (the
metre code; the feet are converted to metres first) — see LOG_COORD_EPSGS.
PostGIS transforms from the declared system straight to geom_4326 (the
32613 ``geom`` twin was retired 2026-09-29, §04e);
silver.collars.easting/northing keep the file's own E=/N= numbers (GIS-6).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.services.ingest.file_hash import sha256_file

log = logging.getLogger("georag.ingest.cameco_log")


# Cameco .log binary format observations (doc-phase 182 verified):
#   - hole_id is the leading filename segment (e.g. "36-1042_08-13-12_*.log"
#     or "IC-11_10-03-12_*.log" — operator naming varies)
#   - binary blob contains positional text fields:
#       "PROCESSED9057C" or "ORIGINAL 9057C" — tool ID
#       "CAMECO RESOURCES" — company (no "SVCS" suffix)
#       "SHIRLEY BASIN" — field name
#       "E=NNNNNN N=NNNNNN" — surveyed state-plane WY East coords
#       "HOLMGREN" or similar — surveyor / pad ID
#   - hole_id from the binary is unreliable (positional truncation),
#     so we extract from the filename instead.
#
# Phase C tuning (2026-05-18): broadened the filename regex to accept
# alphanumeric prefixes (IC-11, WY-1234, F-22, etc.) so non-Cameco-style
# hole IDs in the Wyoming archive no longer get rejected before parse.
_COORDS_RE = re.compile(rb"E=(\d+)\s+N=(\d+)")
_BASIN_RE = re.compile(rb"([A-Z][A-Z\s]+BASIN)\b")
_TOOL_RE = re.compile(rb"(?:PROCESSED|ORIGINAL)\s*(\d+[A-Z])")
_HOLE_ID_FILENAME_RE = re.compile(r"^([A-Z0-9]+-[A-Z0-9]+)_")
# Cameco filename layout (observed 2026-05-18, Phase C tuning):
#   <hole_id>_<date>_<time>_<tool>_<step>_<dip>_<total_depth_ft>_<kind>.log
# Example: IC-7_09-25-12_10-02_9057C_.10_0.70_1257.50_ORE.log
#                                              ^^^^^^^ total depth in feet
# Capture the 3rd-to-last underscore-delimited float field.
_TOTAL_DEPTH_FILENAME_RE = re.compile(
    r"_(-?\d+(?:\.\d+)?)_[A-Z]+\.log$", re.IGNORECASE
)


#: The CRS this binary format writes its E=/N= pair in (NAD83 / Wyoming East,
#: US survey feet) -- a property of the FORMAT, not of the project it is
#: dropped into. The file states no CRS of its own, so a caller may only
#: create or move a collar from it when the operator has DECLARED one of these
#: EPSG codes for the upload (``source_epsg``). Without that, a .log dropped
#: into a project from anywhere else would be placed in Wyoming.
#:
#: GIS-19 (approved 2026-09-29): EPSG:3736 is the honest code for this data —
#: NAD83 / Wyoming East in US survey feet — and used to be refused, because
#: only the metre code 32155 was accepted and the ftUS -> m conversion was
#: done here. Both are accepted now, each with its own unit handling. The
#: East Central zone (32156 / 3737) is NOT accepted until a real Shirley
#: Basin header confirms which zone the files use (needs Kyle).
LOG_COORD_EPSG = 32155
LOG_COORD_EPSG_FTUS = 3736

#: 1 US survey foot = 1200/3937 m.
_US_FT_TO_M = 1200.0 / 3937.0

#: declared EPSG -> factor applied to the file's E=/N= before ST_SetSRID.
LOG_COORD_EPSGS: dict[int, float] = {
    LOG_COORD_EPSG: _US_FT_TO_M,     # metre CRS: convert the ftUS values
    LOG_COORD_EPSG_FTUS: 1.0,        # ftUS CRS: the values are already in its unit
}


def log_crs_declared(source_epsg: int | None) -> bool:
    """Whether the operator declared a CRS this format's coordinates can use."""
    return source_epsg in LOG_COORD_EPSGS


def _source_point(parsed: CamecoLogResult, source_epsg: int) -> tuple[float, float]:
    """The .log's E=/N= in the declared CRS's own unit."""
    factor = LOG_COORD_EPSGS[source_epsg]
    assert parsed.state_plane_easting is not None and parsed.state_plane_northing is not None
    return parsed.state_plane_easting * factor, parsed.state_plane_northing * factor


@dataclass
class CamecoLogResult:
    file_path: str
    hole_id: str | None
    state_plane_easting: float | None
    state_plane_northing: float | None
    basin: str | None
    county: str | None
    state: str | None
    section: int | None
    township: int | None
    range: int | None
    total_depth_ft: float | None
    collar_updated: bool = False
    skipped: bool = False
    skipped_reason: str | None = None


def parse_cameco_log_header(file_path: str, *, header_bytes: int = 4096) -> CamecoLogResult:
    """Parse the embedded text header of a Cameco binary .log file.

    Returns a CamecoLogResult with extracted fields or skipped_reason.
    """
    p = Path(file_path)
    try:
        with open(p, "rb") as f:
            header = f.read(header_bytes)
    except OSError as e:
        return CamecoLogResult(
            file_path=file_path, hole_id=None, state_plane_easting=None,
            state_plane_northing=None, basin=None, county=None, state=None,
            section=None, township=None, range=None, total_depth_ft=None,
            skipped=True, skipped_reason=f"read_failed:{e}",
        )

    result = CamecoLogResult(
        file_path=file_path, hole_id=None, state_plane_easting=None,
        state_plane_northing=None, basin=None, county=None, state=None,
        section=None, township=None, range=None, total_depth_ft=None,
    )

    # Extract hole_id from filename (binary header is unreliable due to
    # positional truncation; filename is the source of truth)
    m_fn = _HOLE_ID_FILENAME_RE.match(p.name)
    if m_fn:
        result.hole_id = m_fn.group(1)

    # Extract total_depth_ft from filename (3rd-to-last float field)
    m_td = _TOTAL_DEPTH_FILENAME_RE.search(p.name)
    if m_td:
        try:
            td = float(m_td.group(1))
            if td > 0:
                result.total_depth_ft = td
        except ValueError:
            pass

    # Extract surveyed state-plane WY East coordinates from binary
    m = _COORDS_RE.search(header)
    if m:
        result.state_plane_easting = float(m.group(1))
        result.state_plane_northing = float(m.group(2))

    # Extract basin
    m = _BASIN_RE.search(header)
    if m:
        result.basin = m.group(1).decode("latin-1", errors="replace").strip()

    # County / state inferred from basin (Shirley Basin → Carbon, WY)
    if result.basin and "SHIRLEY" in result.basin.upper():
        result.county = "CARBON"
        result.state = "WY"

    if not result.hole_id:
        result.skipped = True
        result.skipped_reason = "filename_pattern_unmatched"

    return result


async def update_collar_with_log_coords(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    parsed: CamecoLogResult,
    source_epsg: int | None = None,
) -> bool:
    """Update an existing collar's coordinates with the surveyed state-plane
    values from the .log header. Transforms the declared state-plane system
    (EPSG:3736 / 32155) straight to EPSG:4326 (geom_4326) via PostGIS.

    Returns True if the collar was found and updated; False if not found --
    and False, touching nothing, unless ``source_epsg`` is one of
    LOG_COORD_EPSGS: the file carries no CRS, so it is used only when the
    operator declared it.
    """
    if source_epsg is None or not log_crs_declared(source_epsg):
        return False
    if not parsed.hole_id or parsed.state_plane_easting is None or parsed.state_plane_northing is None:
        return False

    row = await conn.fetchrow(
        """
        SELECT collar_id::text AS collar_id FROM silver.collars
         WHERE project_id = $1::uuid AND hole_id = $2
         LIMIT 1
        """,
        project_id, parsed.hole_id,
    )
    if not row:
        return False

    # The point in the DECLARED system's own unit (ftUS for 3736, metres
    # for 32155). easting/northing keep the file's E=/N= as given (GIS-6):
    # they used to be overwritten with 32613 metres, so the same columns
    # meant a different CRS on every ingest path.
    x, y = _source_point(parsed, source_epsg)

    await conn.execute(
        """
        UPDATE silver.collars SET
            easting = $4,
            northing = $5,
            geom_4326 = ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), $6::int), 4326),
            georef_method = 'declared',
            updated_at = NOW()
         WHERE collar_id = $3::uuid
        """,
        x, y, row["collar_id"],
        parsed.state_plane_easting, parsed.state_plane_northing, source_epsg,
    )
    return True


async def upsert_collar_from_log(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    workspace_id: str,
    parsed: CamecoLogResult,
    source_epsg: int | None = None,
) -> str | None:
    """Create-or-update a collar row directly from .log header data.

    The Cameco operator-drilled holes (IC-, SRE09-, etc.) live in a
    different hole_id namespace than the WSGS-archived PLSS-sequential
    holes (36-1042, etc.). Pre-Phase-C the .log ingester would silently
    skip when no LAS-side collar matched — losing 146 holes' worth of
    surveyed state-plane coordinates.

    This helper UPSERTs the collar so .log files always produce a row,
    whether or not a LAS file happened to seed one first.

    Returns the collar_id, or None if the parse lacks coordinates -- or if
    ``source_epsg`` is not one of LOG_COORD_EPSGS (the file states no CRS;
    see ``log_crs_declared``). Nothing is written in either case.
    """
    if source_epsg is None or not log_crs_declared(source_epsg):
        return None
    if not parsed.hole_id or parsed.state_plane_easting is None or parsed.state_plane_northing is None:
        return None

    # The point in the declared system's own unit; see _source_point.
    x, y = _source_point(parsed, source_epsg)
    FT_TO_M = _US_FT_TO_M

    # total_depth lives in feet on the .log filename; convert to metres
    # to align with silver.collars.total_depth (metres per §04e). A missing
    # or zero depth field is NULL — total_depth is optional since 2026-09-29
    # (§04e, SME-approved); it used to be floored to an invented 0.01 m.
    td_m = (
        parsed.total_depth_ft * FT_TO_M
        if parsed.total_depth_ft and parsed.total_depth_ft > 0 else None
    )

    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, hole_id_canonical, project_id, workspace_id,
             easting, northing, total_depth, hole_type, status, georef_method,
             geom_4326, created_at, updated_at)
        VALUES (
            gen_random_uuid(), $1, silver.canonical_hole_id($1), $2::uuid, $3::uuid,
            $7, $8,
            $6, 'exploration', 'historical', 'declared',
            ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), $9::int), 4326),
            NOW(), NOW()
        )
        ON CONFLICT (project_id, hole_id_canonical) WHERE hole_id_canonical IS NOT NULL
        DO UPDATE SET
            easting = EXCLUDED.easting,
            northing = EXCLUDED.northing,
            geom_4326 = EXCLUDED.geom_4326,
            georef_method = EXCLUDED.georef_method,
            total_depth = GREATEST(silver.collars.total_depth, EXCLUDED.total_depth),
            updated_at = NOW()
        RETURNING collar_id::text AS collar_id
        """,
        parsed.hole_id, project_id, workspace_id, x, y, td_m,
        parsed.state_plane_easting, parsed.state_plane_northing, source_epsg,
    )
    return row["collar_id"] if row else None


async def emit_log_provenance(
    conn: asyncpg.Connection,
    *,
    file_path: str,
    target_id: str,
) -> None:
    """Tag the collar with the binary log as a provenance source."""
    # Off the event loop, streamed (ING-18).
    sha = await asyncio.to_thread(sha256_file, file_path)
    await conn.execute(
        """
        INSERT INTO bronze.provenance
            (provenance_id, target_schema, target_table, target_id,
             source_file, source_file_sha256,
             parser_name, parser_version, ingested_at)
        VALUES (gen_random_uuid(), 'silver', 'collars', $1::uuid,
                $2, $3, 'cameco_log_header', '1.0', NOW())
        """,
        target_id, str(file_path)[:1000], sha,
    )


__all__ = [
    "parse_cameco_log_header",
    "update_collar_with_log_coords",
    "upsert_collar_from_log",
    "emit_log_provenance",
    "CamecoLogResult",
    "LOG_COORD_EPSG",
    "LOG_COORD_EPSG_FTUS",
    "LOG_COORD_EPSGS",
    "log_crs_declared",
]
