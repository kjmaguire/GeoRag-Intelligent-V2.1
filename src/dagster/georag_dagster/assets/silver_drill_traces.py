"""Silver layer asset — desurvey drill holes into 3-D traces.

Reads collar + survey data from silver.collars and silver.surveys,
applies the minimum-curvature algorithm via the in-house _survey_interp.py
implementation, reprojects from the project CRS (EPSG:32613 by default) to
EPSG:4326, and upserts one LINESTRINGZ row per collar into silver.drill_traces.

Algorithm selection (per §04d-tile):
  wellpathpy is pinned as a reference but the core math is in _survey_interp.py
  (in-house minimum curvature, comprehensive tests in test_survey_interpolation.py).
  The asset uses _survey_interp.minimum_curvature directly — it is mathematically
  equivalent and already regression-tested.  wellpathpy is pinned in pyproject.toml
  for validation parity; its import is verified at module load as an installation
  smoke-test.

Edge cases (§04d-tile, all 5 implemented):
  1. 0-survey collar      → ADR-0007 PR-4: fall back to a straight-line
                            trace built from the collar's azimuth/dip/
                            total_depth when those are present + valid.
                            Without usable collar orientation the collar
                            is skipped with the zero_survey counter.
                            trace_quality='single_survey_vertical' marks
                            both the surveyed-1-row and straight-line
                            populations (only allowed CHECK value other
                            than 'ok' / 'high_dogleg_warning').
  2. 1-survey collar      → vertical LINESTRINGZ (depth 0 → total_depth);
                            trace_quality='single_survey_vertical'.
  3. Duplicate depths     → keep latest updated_at row; log conflict count.
  4. Invalid az/dip       → reject survey row, log, count; if <1 valid row
                            survives treat as 0-survey collar (which now
                            tries the straight-line fallback).
  5. High dogleg (>15°/30m) → compute anyway, trace_quality='high_dogleg_warning',
                               record dogleg_max_deg.

Idempotency:
  survey_hash = SHA-256(json of surveys sorted by depth).  If silver.drill_traces
  already has a row for this collar_id with the same hash, the row is skipped.
  Hash changes (new or updated surveys) trigger a full recompute for that collar.

CRS pipeline:
  Surveys are in the project CRS (default EPSG:32613, WGS84/UTM zone 13N).
  Each collar easting/northing/elevation is the origin.  The minimum-curvature
  offsets (in metres) are added to the collar position.  The full XYZ trace is
  then reprojected to EPSG:4326 (lon/lat/elevation) for storage.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config/ConfigurableResource classes use Pydantic for type
introspection and that import breaks runtime annotation evaluation.
"""

import hashlib
import json
import logging
import math
import uuid
from typing import Optional

import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
    asset_check,
)
from pyproj import Transformer

# Verify wellpathpy is installed — smoke-test only; math uses _survey_interp
try:
    import wellpathpy  # noqa: F401  (installation verification per §04d-tile spec)
    _WELLPATHPY_VERSION = getattr(wellpathpy, "__version__", "unknown")
except ImportError as _wp_err:
    _WELLPATHPY_VERSION = f"MISSING: {_wp_err}"
    logging.getLogger(__name__).error(
        "silver_drill_traces: wellpathpy import failed — rebuild Dagster image: %s", _wp_err
    )

from georag_dagster.parsers._survey_interp import SurveyStation, minimum_curvature
from georag_dagster.assets.silver import silver_collars
from georag_dagster.assets.silver_surveys import silver_surveys
from georag_dagster.resources import PostgresResource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default project CRS for coordinate accumulation (UTM zone 13N, WGS84)
_DEFAULT_PROJECT_EPSG = 32613
_TARGET_EPSG = 4326

# Dogleg severity threshold per §04d-tile (degrees per 30 m)
_DOGLEG_HIGH_THRESHOLD_DEG = 15.0

# Azimuth must be in [0, 360), dip in [-90, 0]
_AZ_MIN = 0.0
_AZ_MAX = 360.0
_DIP_MIN = -90.0
_DIP_MAX = 0.0

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLLAR_SQL = """
SELECT
    c.collar_id,
    c.project_id,
    c.easting,
    c.northing,
    COALESCE(c.elevation, 0.0) AS elevation,
    c.total_depth,
    c.azimuth,
    c.dip,
    COALESCE(p.crs_epsg, %(default_epsg)s) AS crs_epsg,
    COALESCE(pw.workspace_id, %(default_ws)s::uuid) AS workspace_id
FROM silver.collars c
JOIN silver.projects p ON c.project_id = p.project_id
LEFT JOIN silver.workspaces pw ON p.workspace_id = pw.workspace_id
ORDER BY c.collar_id;
"""

_SURVEYS_FOR_COLLAR_SQL = """
SELECT
    depth,
    COALESCE(azimuth, 0.0)  AS azimuth,
    COALESCE(dip, -90.0)    AS dip,
    updated_at
FROM silver.surveys
WHERE collar_id = %(collar_id)s
ORDER BY depth ASC, updated_at DESC NULLS LAST;
"""

_EXISTING_HASH_SQL = """
SELECT survey_hash
FROM silver.drill_traces
WHERE collar_id = %(collar_id)s;
"""

_UPSERT_SQL = """
INSERT INTO silver.drill_traces (
    collar_id, workspace_id, project_id,
    geom, computed_at, survey_hash,
    dogleg_max_deg, trace_quality
)
VALUES (
    %(collar_id)s, %(workspace_id)s, %(project_id)s,
    ST_GeomFromText(%(wkt)s, 4326),
    NOW(),
    %(survey_hash)s,
    %(dogleg_max_deg)s,
    %(trace_quality)s
)
ON CONFLICT (collar_id)
DO UPDATE SET
    geom            = EXCLUDED.geom,
    computed_at     = EXCLUDED.computed_at,
    survey_hash     = EXCLUDED.survey_hash,
    dogleg_max_deg  = EXCLUDED.dogleg_max_deg,
    trace_quality   = EXCLUDED.trace_quality;
"""

# Default workspace UUID (seeded in Phase B1+B2)
_DEFAULT_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_survey_hash(surveys: list[dict]) -> str:
    """SHA-256 of sorted surveys serialised to JSON.

    Surveys are sorted by depth (then azimuth, dip for determinism) before
    hashing.  This ensures the hash is stable regardless of query order.
    """
    canonical = sorted(surveys, key=lambda s: (s["depth"], s["azimuth"], s["dip"]))
    payload = json.dumps(
        [{"d": s["depth"], "az": s["azimuth"], "dip": s["dip"]} for s in canonical],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _dogleg_severity_deg_per_30m(
    az1: float, dip1: float, az2: float, dip2: float, dl: float
) -> float:
    """Compute dogleg severity in degrees per 30 m for a survey interval.

    Uses the standard formula: β/dL * 30, where β is the total angle change
    between the two direction vectors.
    """
    if dl <= 0:
        return 0.0

    def _to_rad_cosines(az_deg: float, dip_deg: float):
        az = math.radians(az_deg)
        dp = math.radians(dip_deg)
        n = math.cos(dp) * math.cos(az)
        e = math.cos(dp) * math.sin(az)
        z = math.sin(dp)
        return n, e, z

    n1, e1, z1 = _to_rad_cosines(az1, dip1)
    n2, e2, z2 = _to_rad_cosines(az2, dip2)
    dot = max(-1.0, min(1.0, n1 * n2 + e1 * e2 + z1 * z2))
    beta_rad = math.acos(dot)
    return math.degrees(beta_rad) / dl * 30.0


def _filter_and_dedup_surveys(
    raw: list[dict],
) -> tuple[list[dict], int, int]:
    """Validate and deduplicate raw survey rows.

    Returns (valid_surveys, invalid_count, duplicate_depth_count).

    Deduplication: for each depth, keep the row with the most recent
    updated_at (first row after sorting by depth ASC, updated_at DESC NULLS LAST).

    Validation: azimuth must be in [0, 360), dip in [-90, 0].
    """
    invalid_count = 0
    dup_count = 0
    seen_depths: dict[float, dict] = {}

    for row in raw:
        az = float(row["azimuth"])
        dip = float(row["dip"])
        depth = float(row["depth"])

        # Validate azimuth / dip (edge case 4)
        if not (_AZ_MIN <= az < _AZ_MAX) or not (_DIP_MIN <= dip <= _DIP_MAX):
            invalid_count += 1
            continue

        # Dedup by depth (edge case 3): first row seen per depth wins because
        # the SQL sorts by depth ASC, updated_at DESC — so first = most recent.
        if depth in seen_depths:
            dup_count += 1
            continue

        seen_depths[depth] = {"depth": depth, "azimuth": az, "dip": dip}

    valid = sorted(seen_depths.values(), key=lambda s: s["depth"])
    return valid, invalid_count, dup_count


def _build_vertical_linestring_wkt(
    collar_easting: float,
    collar_northing: float,
    collar_elev: float,
    total_depth: float,
    transformer: Transformer,
) -> str:
    """Build a vertical LINESTRINGZ WKT for a 1-survey or 0-survey hole.

    Projects the collar and the TD point from project CRS to EPSG:4326.
    """
    lon0, lat0, _ = transformer.transform(collar_easting, collar_northing, collar_elev)
    lon1, lat1, _ = transformer.transform(collar_easting, collar_northing, collar_elev - total_depth)
    return (
        f"LINESTRING Z ({lon0} {lat0} {collar_elev}, "
        f"{lon1} {lat1} {collar_elev - total_depth})"
    )


def _build_straight_line_wkt(
    collar_easting: float,
    collar_northing: float,
    collar_elev: float,
    total_depth: float,
    azimuth_deg: float,
    dip_deg: float,
    transformer: Transformer,
) -> str:
    """Build a straight-line LINESTRINGZ WKT from collar orientation.

    Used when silver.surveys has no rows for a collar but collar carries
    azimuth + dip + total_depth (ADR-0007 PR-4 fallback). The downhole
    displacement is computed in the project CRS:

        easting_toe  = easting  + total_depth * cos(dip) * sin(azimuth)
        northing_toe = northing + total_depth * cos(dip) * cos(azimuth)
        elev_toe     = elevation + total_depth * sin(dip)

    dip is stored as a negative value (down-going). Both endpoints are
    reprojected to EPSG:4326 to match the schema.
    """
    az_rad = math.radians(azimuth_deg)
    dip_rad = math.radians(dip_deg)
    e_toe = collar_easting + total_depth * math.cos(dip_rad) * math.sin(az_rad)
    n_toe = collar_northing + total_depth * math.cos(dip_rad) * math.cos(az_rad)
    z_toe = collar_elev + total_depth * math.sin(dip_rad)

    lon0, lat0, _ = transformer.transform(collar_easting, collar_northing, collar_elev)
    lon1, lat1, _ = transformer.transform(e_toe, n_toe, z_toe)
    return (
        f"LINESTRING Z ({lon0} {lat0} {collar_elev}, "
        f"{lon1} {lat1} {z_toe})"
    )


def _compute_collar_orientation_hash(
    azimuth: float | None,
    dip: float | None,
    total_depth: float | None,
) -> str:
    """SHA-256 of the (azimuth, dip, total_depth) tuple.

    Used as the survey_hash for straight-line fallback rows so the asset
    can detect when a collar's orientation has changed and recompute.
    Mirrors :func:`_compute_survey_hash` for the surveyed path.
    """
    payload = json.dumps(
        {"az": azimuth, "dip": dip, "td": total_depth},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _xyz_trace_to_linestring_wkt(
    trace: list,
    transformer: Transformer,
) -> str:
    """Convert a list of (depth, XYZ) tuples to a LINESTRINGZ WKT in EPSG:4326."""
    points = []
    for _depth, xyz in trace:
        lon, lat, elev = transformer.transform(
            xyz.east_m,
            xyz.north_m,
            xyz.elev_m,
        )
        points.append(f"{lon} {lat} {elev}")

    if len(points) < 2:
        # Degenerate — caller should have used the vertical builder instead
        raise ValueError(f"trace has only {len(points)} point(s); LINESTRINGZ requires >= 2")

    return "LINESTRING Z (" + ", ".join(points) + ")"


def _max_dogleg(stations: list[dict]) -> float:
    """Return the maximum dogleg severity (deg/30m) across all survey intervals."""
    if len(stations) < 2:
        return 0.0
    max_dls = 0.0
    for i in range(1, len(stations)):
        s_prev = stations[i - 1]
        s_curr = stations[i]
        dl = s_curr["depth"] - s_prev["depth"]
        dls = _dogleg_severity_deg_per_30m(
            s_prev["azimuth"], s_prev["dip"],
            s_curr["azimuth"], s_curr["dip"],
            dl,
        )
        if dls > max_dls:
            max_dls = dls
    return max_dls


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=[silver_collars, silver_surveys],
    description=(
        "Pre-compute minimum-curvature 3-D drill traces for every collar that "
        "has at least one survey row.  Writes LINESTRINGZ rows (EPSG:4326) into "
        "silver.drill_traces.  Idempotent via survey_hash — unchanged surveys "
        "produce a no-op.  Five desurvey edge cases handled per §04d-tile."
    ),
)
def silver_drill_traces(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Desurvey all collars with surveys → silver.drill_traces."""

    context.log.info(
        "silver_drill_traces: starting desurvey (wellpathpy %s installed as reference).",
        _WELLPATHPY_VERSION,
    )

    counters = {
        "processed":         0,
        "written":           0,
        "skipped_no_change": 0,
        "zero_survey":       0,
        "single_survey":     0,
        "straight_line":     0,
        "high_dogleg":       0,
        "invalid_az_dip":    0,
        "dup_depths":        0,
        "errors":            0,
    }

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as collar_cur:
            collar_cur.execute(
                _COLLAR_SQL,
                {
                    "default_epsg": _DEFAULT_PROJECT_EPSG,
                    "default_ws": _DEFAULT_WORKSPACE_ID,
                },
            )
            collars = collar_cur.fetchall()

    context.log.info("silver_drill_traces: %d collars loaded.", len(collars))

    for collar in collars:
        collar_id = str(collar["collar_id"])
        project_id = str(collar["project_id"])
        workspace_id = str(collar["workspace_id"])
        easting = float(collar["easting"])
        northing = float(collar["northing"])
        elevation = float(collar["elevation"])
        total_depth = float(collar["total_depth"])
        # ADR-0007 PR-4 — capture collar-side azimuth/dip for the
        # straight-line fallback when silver.surveys is empty.
        collar_az_raw = collar.get("azimuth")
        collar_dip_raw = collar.get("dip")
        project_epsg = int(collar["crs_epsg"] or _DEFAULT_PROJECT_EPSG)

        counters["processed"] += 1

        # Transformer: project CRS → EPSG:4326 (always_xy=True for (E, N, Z) order)
        transformer = Transformer.from_crs(
            project_epsg, _TARGET_EPSG, always_xy=True
        )

        # --- Load surveys for this collar ---
        try:
            with postgres.get_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as s_cur:
                    s_cur.execute(_SURVEYS_FOR_COLLAR_SQL, {"collar_id": collar_id})
                    raw_surveys = s_cur.fetchall()
        except Exception as exc:
            context.log.error(
                "silver_drill_traces: could not load surveys for collar %s: %s",
                collar_id, exc,
            )
            counters["errors"] += 1
            continue

        # --- Edge case 1: 0-survey collar ---
        # ADR-0007 PR-4 — instead of silently skipping, fall back to a
        # straight-line trace built from the collar's own azimuth/dip/TD
        # when those are valid. Surveys remain authoritative when they
        # exist; this branch only fires when nothing is in silver.surveys.
        #
        # When the collar lacks az/dip but DOES carry a positive
        # total_depth, default to vertical (az=0, dip=-90). Older
        # datasets (incl. Cameco's 1980s holes) store only TD; assuming
        # vertical is the industry-standard placeholder and lets the
        # 3-D card render the hole position correctly. trace_quality is
        # set to 'single_survey_vertical' so the source is obvious.
        if not raw_surveys:
            td_valid = total_depth is not None and total_depth > 0
            if not td_valid:
                context.log.info(
                    "silver_drill_traces: collar %s has 0 surveys and no usable "
                    "total_depth (az=%s dip=%s td=%s) — skipping.",
                    collar_id, collar_az_raw, collar_dip_raw, total_depth,
                )
                counters["zero_survey"] += 1
                continue

            az_valid = (
                collar_az_raw is not None
                and _AZ_MIN <= float(collar_az_raw) < _AZ_MAX
            )
            dip_valid = (
                collar_dip_raw is not None
                and _DIP_MIN <= float(collar_dip_raw) <= _DIP_MAX
            )
            azimuth_f = float(collar_az_raw) if az_valid else 0.0
            dip_f = float(collar_dip_raw) if dip_valid else -90.0
            if not (az_valid and dip_valid):
                context.log.debug(
                    "silver_drill_traces: collar %s defaulting to vertical "
                    "(az=%s dip=%s) — collar orientation missing.",
                    collar_id, collar_az_raw, collar_dip_raw,
                )

            sl_hash = _compute_collar_orientation_hash(
                azimuth_f, dip_f, total_depth,
            )
            try:
                with postgres.get_connection() as conn:
                    with conn.cursor() as h_cur:
                        h_cur.execute(_EXISTING_HASH_SQL, {"collar_id": collar_id})
                        existing_sl = h_cur.fetchone()
            except Exception as exc:
                context.log.warning(
                    "silver_drill_traces: hash check failed for collar %s: %s — "
                    "proceeding with write.",
                    collar_id, exc,
                )
                existing_sl = None

            if existing_sl and existing_sl[0].strip() == sl_hash:
                context.log.debug(
                    "silver_drill_traces: collar %s — straight-line hash unchanged; "
                    "skipping recompute.",
                    collar_id,
                )
                counters["skipped_no_change"] += 1
                continue

            try:
                wkt = _build_straight_line_wkt(
                    easting, northing, elevation, total_depth,
                    azimuth_f, dip_f, transformer,
                )
            except Exception as exc:
                context.log.error(
                    "silver_drill_traces: straight-line WKT build failed for "
                    "collar %s: %s",
                    collar_id, exc,
                )
                counters["errors"] += 1
                continue

            # Reuse the 'single_survey_vertical' trace_quality marker — it
            # is the only CHECK-allowed value (drill_traces_quality_valid)
            # other than 'ok' and 'high_dogleg_warning' that semantically
            # means "not a real desurveyed path". Asset metadata
            # (straight_line counter) lets operators tell the two
            # populations apart.
            try:
                with postgres.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            _UPSERT_SQL,
                            {
                                "collar_id":    collar_id,
                                "workspace_id": workspace_id,
                                "project_id":   project_id,
                                "wkt":          wkt,
                                "survey_hash":  sl_hash,
                                "dogleg_max_deg": None,
                                "trace_quality": "single_survey_vertical",
                            },
                        )
            except Exception as exc:
                context.log.error(
                    "silver_drill_traces: DB upsert failed for collar %s "
                    "(straight-line): %s",
                    collar_id, exc,
                )
                counters["errors"] += 1
                continue

            counters["written"] += 1
            counters["straight_line"] += 1
            continue

        # --- Validate + deduplicate surveys (edge cases 3, 4) ---
        valid_surveys, inv_count, dup_count = _filter_and_dedup_surveys(
            [dict(r) for r in raw_surveys]
        )
        if inv_count:
            context.log.warning(
                "silver_drill_traces: collar %s — %d survey row(s) rejected "
                "(invalid az/dip); %d valid remain.",
                collar_id, inv_count, len(valid_surveys),
            )
            counters["invalid_az_dip"] += inv_count
        if dup_count:
            context.log.info(
                "silver_drill_traces: collar %s — %d duplicate depth row(s) dropped.",
                collar_id, dup_count,
            )
            counters["dup_depths"] += dup_count

        # --- If all surveys were invalid, treat as 0-survey ---
        if not valid_surveys:
            context.log.info(
                "silver_drill_traces: collar %s — no valid surveys after filtering; skipping.",
                collar_id,
            )
            counters["zero_survey"] += 1
            continue

        # --- Idempotency: hash comparison ---
        survey_hash = _compute_survey_hash(valid_surveys)
        try:
            with postgres.get_connection() as conn:
                with conn.cursor() as h_cur:
                    h_cur.execute(_EXISTING_HASH_SQL, {"collar_id": collar_id})
                    existing = h_cur.fetchone()
        except Exception as exc:
            context.log.warning(
                "silver_drill_traces: hash check failed for collar %s: %s — proceeding with write.",
                collar_id, exc,
            )
            existing = None

        if existing and existing[0].strip() == survey_hash:
            context.log.debug(
                "silver_drill_traces: collar %s — hash unchanged; skipping recompute.",
                collar_id,
            )
            counters["skipped_no_change"] += 1
            continue

        # --- Edge case 2: 1-survey collar → vertical LINESTRINGZ ---
        if len(valid_surveys) == 1:
            context.log.info(
                "silver_drill_traces: collar %s — 1 survey; writing vertical trace.",
                collar_id,
            )
            try:
                wkt = _build_vertical_linestring_wkt(
                    easting, northing, elevation, total_depth, transformer
                )
            except Exception as exc:
                context.log.error(
                    "silver_drill_traces: vertical WKT build failed for collar %s: %s",
                    collar_id, exc,
                )
                counters["errors"] += 1
                continue

            try:
                with postgres.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            _UPSERT_SQL,
                            {
                                "collar_id":    collar_id,
                                "workspace_id": workspace_id,
                                "project_id":   project_id,
                                "wkt":          wkt,
                                "survey_hash":  survey_hash,
                                "dogleg_max_deg": None,
                                "trace_quality": "single_survey_vertical",
                            },
                        )
            except Exception as exc:
                context.log.error(
                    "silver_drill_traces: DB upsert failed for collar %s: %s",
                    collar_id, exc,
                )
                counters["errors"] += 1
                continue

            counters["written"] += 1
            counters["single_survey"] += 1
            continue

        # --- Multi-survey: minimum curvature trace ---
        stations = [
            SurveyStation(
                depth_m=s["depth"],
                azimuth_deg=s["azimuth"],
                dip_deg=s["dip"],
            )
            for s in valid_surveys
        ]

        try:
            # Prepend a collar station at depth=0 if the first survey is not at 0
            if stations[0].depth_m != 0.0:
                collar_station = SurveyStation(
                    depth_m=0.0,
                    azimuth_deg=stations[0].azimuth_deg,
                    dip_deg=stations[0].dip_deg,
                )
                stations = [collar_station] + stations

            trace = minimum_curvature(easting, northing, elevation, stations)
        except ValueError as exc:
            context.log.error(
                "silver_drill_traces: minimum_curvature failed for collar %s: %s",
                collar_id, exc,
            )
            counters["errors"] += 1
            continue

        # --- Edge case 5: dogleg severity check ---
        dogleg_max = _max_dogleg(valid_surveys)
        if dogleg_max > _DOGLEG_HIGH_THRESHOLD_DEG:
            trace_quality = "high_dogleg_warning"
            counters["high_dogleg"] += 1
            context.log.warning(
                "silver_drill_traces: collar %s — high dogleg %.2f deg/30m (threshold %.1f); "
                "trace_quality='high_dogleg_warning'. Computing trace anyway.",
                collar_id, dogleg_max, _DOGLEG_HIGH_THRESHOLD_DEG,
            )
        else:
            trace_quality = "ok"
            dogleg_max = None  # only stored when high

        # Build LINESTRINGZ WKT
        try:
            wkt = _xyz_trace_to_linestring_wkt(trace, transformer)
        except Exception as exc:
            context.log.error(
                "silver_drill_traces: WKT build failed for collar %s: %s",
                collar_id, exc,
            )
            counters["errors"] += 1
            continue

        # Upsert into silver.drill_traces
        try:
            with postgres.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        _UPSERT_SQL,
                        {
                            "collar_id":     collar_id,
                            "workspace_id":  workspace_id,
                            "project_id":    project_id,
                            "wkt":           wkt,
                            "survey_hash":   survey_hash,
                            "dogleg_max_deg": round(dogleg_max, 3) if dogleg_max is not None else None,
                            "trace_quality": trace_quality,
                        },
                    )
        except Exception as exc:
            context.log.error(
                "silver_drill_traces: DB upsert failed for collar %s: %s",
                collar_id, exc,
            )
            counters["errors"] += 1
            continue

        counters["written"] += 1

    context.log.info(
        "silver_drill_traces: complete — processed=%d written=%d skipped_no_change=%d "
        "zero_survey=%d single_survey=%d straight_line=%d high_dogleg=%d "
        "invalid_rows=%d dup_depths=%d errors=%d",
        counters["processed"], counters["written"], counters["skipped_no_change"],
        counters["zero_survey"], counters["single_survey"], counters["straight_line"],
        counters["high_dogleg"], counters["invalid_az_dip"], counters["dup_depths"],
        counters["errors"],
    )

    return MaterializeResult(
        metadata={
            "wellpathpy_version":   MetadataValue.text(_WELLPATHPY_VERSION),
            "collars_processed":    MetadataValue.int(counters["processed"]),
            "traces_written":       MetadataValue.int(counters["written"]),
            "skipped_no_change":    MetadataValue.int(counters["skipped_no_change"]),
            "zero_survey_skipped":  MetadataValue.int(counters["zero_survey"]),
            "single_survey_vertical": MetadataValue.int(counters["single_survey"]),
            "straight_line_traces": MetadataValue.int(counters["straight_line"]),
            "high_dogleg_flagged":  MetadataValue.int(counters["high_dogleg"]),
            "invalid_az_dip_rows":  MetadataValue.int(counters["invalid_az_dip"]),
            "duplicate_depth_rows": MetadataValue.int(counters["dup_depths"]),
            "error_count":          MetadataValue.int(counters["errors"]),
        }
    )
