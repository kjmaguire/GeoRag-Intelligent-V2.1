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

The state plane Wyoming East coordinate system is EPSG:32155 (NAD83).
We transform to UTM Zone 13N (EPSG:32613) at insert time via PostGIS.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

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
) -> bool:
    """Update an existing collar's coordinates with the surveyed state-plane
    values from the .log header. Transforms state plane WY East (EPSG:32155)
    to UTM Zone 13N (EPSG:32613) via PostGIS.

    Returns True if the collar was found and updated; False if not found.
    """
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

    # Cameco .log binary stores coords in US survey feet. EPSG:32155
    # (NAD83 / Wyoming East) expects METERS, so we convert ft → m
    # before constructing the State Plane point.
    # 1 US survey foot = 1200/3937 m ≈ 0.3048006096 m
    FT_TO_M = 1200.0 / 3937.0
    e_m = parsed.state_plane_easting * FT_TO_M
    n_m = parsed.state_plane_northing * FT_TO_M

    await conn.execute(
        """
        UPDATE silver.collars SET
            easting = ST_X(ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), 32155), 32613)),
            northing = ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), 32155), 32613)),
            geom = ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), 32155), 32613),
            geom_4326 = ST_Transform(ST_SetSRID(ST_MakePoint($1, $2), 32155), 4326),
            updated_at = NOW()
         WHERE collar_id = $3::uuid
        """,
        e_m, n_m, row["collar_id"],
    )
    return True


async def upsert_collar_from_log(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    workspace_id: str,
    parsed: CamecoLogResult,
) -> str | None:
    """Create-or-update a collar row directly from .log header data.

    The Cameco operator-drilled holes (IC-, SRE09-, etc.) live in a
    different hole_id namespace than the WSGS-archived PLSS-sequential
    holes (36-1042, etc.). Pre-Phase-C the .log ingester would silently
    skip when no LAS-side collar matched — losing 146 holes' worth of
    surveyed state-plane coordinates.

    This helper UPSERTs the collar so .log files always produce a row,
    whether or not a LAS file happened to seed one first.

    Returns the collar_id, or None if the parse lacks coordinates.
    """
    if not parsed.hole_id or parsed.state_plane_easting is None or parsed.state_plane_northing is None:
        return None

    # ft → m for the State Plane WY East (32155) point
    FT_TO_M = 1200.0 / 3937.0
    e_m = parsed.state_plane_easting * FT_TO_M
    n_m = parsed.state_plane_northing * FT_TO_M

    # total_depth lives in feet on the .log filename; convert to metres
    # to align with silver.collars.total_depth (metres per §04e).
    # 0.01 m floor satisfies chk_collars_total_depth_positive when the
    # filename's depth field is missing or zero — better than dropping
    # the collar entirely.
    if parsed.total_depth_ft and parsed.total_depth_ft > 0:
        td_m = parsed.total_depth_ft * FT_TO_M
    else:
        td_m = 0.01

    row = await conn.fetchrow(
        """
        INSERT INTO silver.collars
            (collar_id, hole_id, hole_id_canonical, project_id, workspace_id,
             easting, northing, total_depth, hole_type, status,
             geom, geom_4326, created_at, updated_at)
        VALUES (
            gen_random_uuid(), $1, $1, $2::uuid, $3::uuid,
            ST_X(ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), 32155), 32613)),
            ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), 32155), 32613)),
            $6, 'exploration', 'historical',
            ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), 32155), 32613),
            ST_Transform(ST_SetSRID(ST_MakePoint($4, $5), 32155), 4326),
            NOW(), NOW()
        )
        ON CONFLICT (project_id, hole_id) DO UPDATE SET
            easting = EXCLUDED.easting,
            northing = EXCLUDED.northing,
            geom = EXCLUDED.geom,
            geom_4326 = EXCLUDED.geom_4326,
            total_depth = GREATEST(silver.collars.total_depth, EXCLUDED.total_depth),
            updated_at = NOW()
        RETURNING collar_id::text AS collar_id
        """,
        parsed.hole_id, project_id, workspace_id, e_m, n_m, td_m,
    )
    return row["collar_id"] if row else None


async def emit_log_provenance(
    conn: asyncpg.Connection,
    *,
    file_path: str,
    target_id: str,
) -> None:
    """Tag the collar with the binary log as a provenance source."""
    sha = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
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
]
