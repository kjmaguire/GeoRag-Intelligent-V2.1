"""Which coordinate system a collar/geochem table's coordinates are in, and
whether the positions that come out of it are believable.

GIS audit 2026-09-29 (§04b). Three findings meet here:

GIS-1 — decimal degrees read as UTM metres
    ``_drill_schema`` aliases Longitude/Latitude to easting/northing, and the
    tabular writer stamped whatever EPSG the upload or project declared (or
    32613) on the values. ``ST_SetSRID(ST_MakePoint(-105.5, 57.3), 32613)``
    transforms to (-109.49, 0.0005): every hole a few metres north of the
    equator, and with a project CRS set, stamped 'declared' with no warning.
    Kyle's rule (2026-09-29): data must land in the right place for ANY
    company, so a file whose coordinates ARE longitude/latitude (by header,
    or by degree-like values — ``_drill_schema.coordinate_mode_reason``) is
    placed as EPSG:4326 whatever the project CRS says, unless the upload
    itself declared a geographic EPSG (4269, 4267, ...), which is honoured.

GIS-2 — no CRS anywhere
    Kyle's decision: keep placing undeclared projected coordinates as
    EPSG:32613 (the platform default), but say so loudly — see
    ``ingest_tabular._assumed_crs_warning``. The decision here only marks
    the result ``assumed``.

GIS-13 / GIS-15 — plausibility
    A wrong UTM zone or datum is not detectable from the CRS's area of use
    (a zone's area spans every valid easting). The only independent check is
    WHERE the result lands relative to what is already known about the
    project: its boundary, or the median of its already-placed collars.
    :func:`plausibility_warnings` WARNS about collars that land far away,
    outside the CRS's area of use, across a hemisphere, or that do not
    transform at all. It never refuses (Kyle, 2026-09-29).

CRS at every hop, after this module:
    source file (declared/project/detected EPSG) -> geom_4326 via PostGIS
    ST_Transform from that EPSG -> rendered by Martin from geom_4326/geom.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

#: Distance beyond the project's known extent at which a collar is called
#: implausible. Kyle has not set this; 100 km is wide enough for any single
#: property and still catches a wrong UTM zone (~360 km per 6 degrees at
#: 57 N) and positive-west longitudes (thousands of km). It only warns.
AOI_WARN_KM = 100.0

#: Slack around a CRS's published area of use, in degrees.
_AREA_SLACK_DEG = 1.0

_MAX_NAMED = 5


@dataclass
class CollarCrsDecision:
    """The EPSG to read a file's easting/northing as, and how sure we are."""

    epsg: int
    #: 'declared' | 'detected' | 'assumed' (chk_collars_georef_method).
    georef_method: str
    assumed: bool
    coordinate_mode: str
    #: 0..1, written to silver.collars.crs_confidence.
    crs_confidence: float
    warnings: list[dict[str, Any]] = field(default_factory=list)
    #: A warning dict when the coordinates must NOT be placed at all — only
    #: for a self-contradicting declaration, never for a missing one.
    refusal: dict[str, Any] | None = None


def _crs(epsg: int) -> Any | None:
    from pyproj import CRS  # noqa: PLC0415
    from pyproj.exceptions import CRSError  # noqa: PLC0415

    try:
        return CRS.from_epsg(int(epsg))
    except (CRSError, ValueError, TypeError):
        logger.debug("CRS lookup failed", exc_info=True)
        return None


def _is_geographic(epsg: int | None) -> bool | None:
    if epsg is None:
        return None
    crs = _crs(epsg)
    return None if crs is None else bool(crs.is_geographic)


def _axis_unit_is_metre(epsg: int) -> bool | None:
    crs = _crs(epsg)
    if crs is None or not crs.axis_info:
        return None
    unit = (crs.axis_info[0].unit_name or "").lower()
    return unit in ("metre", "meter")


def decide_collar_crs(
    *,
    eastings: list[float | None],
    northings: list[float | None],
    easting_column: str | None,
    northing_column: str | None,
    declared_epsg: int | None,
    project_epsg: int | None,
    default_epsg: int,
    label: str,
) -> CollarCrsDecision:
    """Choose the source EPSG for one table's coordinates.

    ``declared_epsg`` is the per-upload override (the import wizard's EPSG
    field); ``project_epsg`` is ``silver.projects.crs_epsg``; ``default_epsg``
    the platform fallback. ``label`` names the file/sheet in warnings.
    """
    from georag_geoparsers._drill_schema import coordinate_mode_reason  # noqa: PLC0415
    from georag_geoparsers._header_match import header_unit  # noqa: PLC0415

    mode, reason = coordinate_mode_reason(
        eastings, northings,
        easting_column=easting_column, northing_column=northing_column,
    )
    evidence = (
        f"the columns are named '{easting_column}' / '{northing_column}'"
        if reason == "header"
        else "every value is within +/-180 / +/-90 with a spread of a few degrees"
    )

    if mode == "geographic":
        if declared_epsg is not None and _is_geographic(declared_epsg):
            return CollarCrsDecision(declared_epsg, "declared", False, mode, 1.0)
        if declared_epsg is None and project_epsg is not None and _is_geographic(project_epsg):
            return CollarCrsDecision(project_epsg, "declared", False, mode, 0.9)

        warnings: list[dict[str, Any]] = []
        if declared_epsg is not None:
            warnings.append({
                "code": "collar_crs_geographic_override",
                "message": (
                    f"{label}: coordinates are longitude/latitude, so the "
                    f"declared EPSG:{declared_epsg} was not used"
                ),
                "detail": (
                    f"EPSG:{declared_epsg} was declared for {label}, but it is a "
                    f"projected system and {evidence}. Reading degrees as "
                    f"projected metres puts every hole within a few metres of "
                    f"the system's false origin (for UTM, the equator), so the "
                    f"coordinates were read as WGS 84 longitude/latitude "
                    f"(EPSG:4326). If they are latitude/longitude on another "
                    f"datum, re-upload declaring it (NAD83 = EPSG:4269, NAD27 = "
                    f"EPSG:4267)."
                ),
            })
        else:
            other = (
                f" rather than the project's EPSG:{project_epsg}"
                if project_epsg is not None else ""
            )
            warnings.append({
                "code": "collar_crs_geographic_detected",
                "message": (
                    f"{label}: coordinates read as longitude/latitude "
                    f"(WGS 84, EPSG:4326){other}"
                ),
                "detail": (
                    f"{label} gives no coordinate system, and {evidence}, so "
                    f"its coordinates were read as WGS 84 longitude/latitude "
                    f"(EPSG:4326){other}. If they are latitude/longitude on "
                    f"another datum, re-upload declaring it (NAD83 = EPSG:4269, "
                    f"NAD27 = EPSG:4267 — NAD27 differs from WGS 84 by up to a "
                    f"few hundred metres)."
                ),
            })
        return CollarCrsDecision(
            4326, "detected", False, mode,
            0.8 if reason == "header" else 0.7, warnings,
        )

    # Projected (or not decidable as degrees).
    if declared_epsg is not None:
        if _is_geographic(declared_epsg):
            refusal = {
                "code": "collar_crs_mismatch",
                "message": (
                    f"{label}: EPSG:{declared_epsg} is longitude/latitude but "
                    f"the coordinates are not degrees — collars not placed"
                ),
                "detail": (
                    f"EPSG:{declared_epsg} was declared for {label}. It is a "
                    f"geographic (degree) system, but the easting/northing "
                    f"values lie outside +/-180 / +/-90 or are spread too "
                    f"widely to be degrees, so they cannot be what the "
                    f"declaration says. Nothing was placed rather than "
                    f"placed somewhere invented. Re-upload declaring the "
                    f"projected EPSG the coordinates were surveyed in."
                ),
            }
            return CollarCrsDecision(
                declared_epsg, "declared", False, mode, 0.0, [], refusal,
            )
        decision = CollarCrsDecision(declared_epsg, "declared", False, mode, 1.0)
    elif project_epsg is not None and _is_geographic(project_epsg) is False:
        decision = CollarCrsDecision(project_epsg, "declared", False, mode, 0.9)
    else:
        decision = CollarCrsDecision(default_epsg, "assumed", True, mode, 0.3)

    # A feet-labelled coordinate column under a metre CRS is a 3.28x
    # misplacement waiting to happen; the parser already said "not
    # converted", this says which CRS it is being read under.
    feet = [
        c for c in (easting_column, northing_column)
        if c and header_unit(c) == "ft"
    ]
    if feet and _axis_unit_is_metre(decision.epsg):
        decision.warnings.append({
            "code": "collar_crs_unit_mismatch",
            "message": (
                f"{label}: coordinate columns are labelled in feet but "
                f"EPSG:{decision.epsg} is in metres"
            ),
            "detail": (
                f"{', '.join(repr(c) for c in feet)} name feet, and the "
                f"coordinates were read as EPSG:{decision.epsg}, whose unit "
                f"is the metre. If the header is right the holes are "
                f"misplaced — re-upload declaring the feet-based EPSG code "
                f"(State Plane systems have one, e.g. EPSG:3736)."
            ),
        })
        decision.crs_confidence = min(decision.crs_confidence, 0.2)
    return decision


# ---------------------------------------------------------------------------
# Plausibility (GIS-13, GIS-15)
# ---------------------------------------------------------------------------


@dataclass
class ProjectReference:
    """Where the project is already known to be, in WGS 84."""

    lon: float
    lat: float
    #: Extra tolerance for the reference's own size (a boundary's radius).
    radius_km: float
    source: str


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


async def project_reference(
    conn: asyncpg.Connection,
    project_id: str,
    *,
    exclude_hole_ids: list[str] | None = None,
) -> ProjectReference | None:
    """The project's boundary, else the median of its trusted collars.

    ``exclude_hole_ids`` leaves out the holes being (re)written: a corrected
    re-upload must not be judged against its own previous wrong positions.
    Collars stored 'assumed' are not trusted as a reference either. Any
    database error returns None — a missing reference skips the check, it
    never fails an ingest.
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT ST_X(ST_Centroid(geom_boundary)) AS lon,
                   ST_Y(ST_Centroid(geom_boundary)) AS lat,
                   ST_XMin(geom_boundary) AS xmin, ST_XMax(geom_boundary) AS xmax,
                   ST_YMin(geom_boundary) AS ymin, ST_YMax(geom_boundary) AS ymax
              FROM silver.projects
             WHERE project_id = $1::uuid AND geom_boundary IS NOT NULL
            """,
            project_id,
        )
        if row is not None and row["lon"] is not None:
            lon, lat = float(row["lon"]), float(row["lat"])
            radius = max(
                haversine_km(lon, lat, float(x), float(y))
                for x in (row["xmin"], row["xmax"])
                for y in (row["ymin"], row["ymax"])
            )
            return ProjectReference(lon, lat, radius, "the project boundary")

        row = await conn.fetchrow(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_X(geom_4326)) AS lon,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_Y(geom_4326)) AS lat,
                   count(*) AS n
              FROM silver.collars
             WHERE project_id = $1::uuid
               AND geom_4326 IS NOT NULL
               AND georef_method IS DISTINCT FROM 'assumed'
               AND NOT (hole_id = ANY($2::text[]))
            """,
            project_id, list(exclude_hole_ids or []),
        )
    except Exception:  # noqa: BLE001 — no reference skips the check; it never fails an ingest
        logger.debug("no plausibility reference for this project", exc_info=True)
        return None
    if row is None or not row["n"] or row["lon"] is None:
        return None
    return ProjectReference(
        float(row["lon"]), float(row["lat"]), 0.0,
        f"the project's {int(row['n'])} other placed collar(s)",
    )


def to_lonlat(
    epsg: int, points: list[tuple[float, float]],
) -> list[tuple[float, float] | None]:
    """Transform source (x, y) pairs to WGS 84 (lon, lat); None per failure."""
    from pyproj import Transformer  # noqa: PLC0415

    try:
        tf = Transformer.from_crs(f"EPSG:{int(epsg)}", "EPSG:4326", always_xy=True)
    except Exception:  # noqa: BLE001 — an unusable CRS means nothing transforms
        logger.debug("transformer unavailable", exc_info=True)
        return [None] * len(points)
    out: list[tuple[float, float] | None] = []
    for x, y in points:
        try:
            lon, lat = tf.transform(x, y)
        except Exception:  # noqa: BLE001
            logger.debug("point (%s, %s) did not transform", x, y, exc_info=True)
            out.append(None)
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lat) > 90 or abs(lon) > 180:
            out.append(None)
        else:
            out.append((float(lon), float(lat)))
    return out


def _within_area(area: Any, lon: float, lat: float) -> bool:
    """Inside a pyproj AreaOfUse, with slack, antimeridian-aware.

    NAD83 (EPSG:4269) publishes west=167.65, east=-40.73: its area CROSSES
    the antimeridian (the Aleutians), so a plain ``west <= lon <= east``
    rejects every point in it.
    """
    if not (area.south - _AREA_SLACK_DEG <= lat <= area.north + _AREA_SLACK_DEG):
        return False
    west, east = area.west - _AREA_SLACK_DEG, area.east + _AREA_SLACK_DEG
    if area.west <= area.east:
        return bool(west <= lon <= east)
    return bool(lon >= west or lon <= east)


def plausibility_warnings(
    *,
    epsg: int,
    points: list[tuple[str, float, float]],
    reference: ProjectReference | None,
    label: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Warn about collars whose WGS 84 position is not believable.

    ``points`` are ``(hole_id, x, y)`` in ``epsg``. Returns the warnings
    (one per kind, naming up to five holes) and the set of hole ids flagged,
    so the caller can lower their ``crs_confidence``. Never refuses.
    """
    if not points:
        return [], set()
    crs = _crs(epsg)
    area = crs.area_of_use if crs is not None else None
    placed = to_lonlat(epsg, [(x, y) for _, x, y in points])

    no_transform: list[str] = []
    outside_area: list[str] = []
    far: list[tuple[str, float]] = []
    hemisphere: list[str] = []
    for (hole_id, _x, _y), ll in zip(points, placed, strict=True):
        if ll is None:
            no_transform.append(hole_id)
            continue
        lon, lat = ll
        if area is not None and not _within_area(area, lon, lat):
            outside_area.append(hole_id)
        if reference is not None:
            dist = haversine_km(lon, lat, reference.lon, reference.lat)
            if dist > reference.radius_km + AOI_WARN_KM:
                far.append((hole_id, dist))
                if (lon > 0) != (reference.lon > 0) and abs(lon) > 1 and abs(reference.lon) > 1:
                    hemisphere.append(hole_id)

    warnings: list[dict[str, Any]] = []

    def _named(ids: list[str]) -> str:
        head = ", ".join(repr(h) for h in ids[:_MAX_NAMED])
        return head + (f" and {len(ids) - _MAX_NAMED} more" if len(ids) > _MAX_NAMED else "")

    if no_transform:
        warnings.append({
            "code": "collar_position_untransformable",
            "message": f"{label}: {len(no_transform)} collar(s) do not transform from EPSG:{epsg}",
            "detail": (
                f"{_named(no_transform)} could not be converted from EPSG:{epsg} "
                f"to longitude/latitude — the values are outside anything that "
                f"system can describe. Check the coordinate columns and the "
                f"declared EPSG."
            ),
        })
    if outside_area:
        warnings.append({
            "code": "collar_outside_crs_area",
            "message": (
                f"{label}: {len(outside_area)} collar(s) fall outside the area "
                f"EPSG:{epsg} is defined for"
            ),
            "detail": (
                f"{_named(outside_area)} land outside the published area of use "
                f"of EPSG:{epsg}"
                + (f" ({area.name})" if area is not None and area.name else "")
                + ". That usually means the coordinates are in a different "
                "system (wrong zone, feet read as metres, or swapped axes). "
                "They were placed anyway; check them on the map."
            ),
        })
    if far and reference is not None:
        worst = max(d for _, d in far)
        hemi = (
            f" {len(hemisphere)} of them are in the opposite hemisphere of "
            f"longitude — a longitude written positive-west, or a missing "
            f"minus sign, looks exactly like this."
            if hemisphere else ""
        )
        warnings.append({
            "code": "collar_far_from_project",
            "message": (
                f"{label}: {len(far)} collar(s) land more than "
                f"{AOI_WARN_KM:.0f} km from the rest of the project "
                f"(furthest {worst:,.0f} km)"
            ),
            "detail": (
                f"Read as EPSG:{epsg}, {_named([h for h, _ in far])} land up to "
                f"{worst:,.0f} km from {reference.source}. A wrong UTM zone "
                f"moves a hole ~360 km east or west; a wrong datum or "
                f"hemisphere much further.{hemi} They were placed anyway — if "
                f"the position is wrong, re-upload declaring the correct EPSG."
            ),
        })

    flagged = set(no_transform) | set(outside_area) | {h for h, _ in far}
    return warnings, flagged
