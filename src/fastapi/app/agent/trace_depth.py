"""Measured depth along a desurveyed trace, for the 3-D drill-trace card.

GIS-8 (audit 2026-09-29). ``query_drill_traces_3d`` labelled each vertex of
``silver.drill_traces.geom`` (LINESTRING Z in EPSG:4326, no M) with
``depth_m = total_depth * i / n`` — its INDEX along the line. Survey
stations are not evenly spaced (0, 12, 24, 300 m is ordinary), so an
interval at 150 m was drawn ~18 m downhole; and a trace whose last survey
stops short of total depth clamped every deeper interval at the last
station.

Here the depth of each vertex is the cumulative 3-D length of the trace up
to it. Minimum curvature joins stations with arcs and the stored trace is
their chords, so this is the measured depth to within the chord/arc
difference — centimetres on real doglegs. When the trace stops short of
``total_depth`` its last segment is extended tangentially to it (the usual
convention below the last survey), marked ``extrapolated``.

Each point also carries ``east_m`` / ``north_m``: metre offsets from the
collar in a local tangent plane. The card can plot those against elevation
in metres instead of degrees against metres (GIS-9, frontend).

CRS at every hop: trace stored 4326 (lon, lat, z metres) -> local
equirectangular metres about the collar for lengths only -> returned as the
same 4326 lon/lat plus the local offsets. Equirectangular is exact enough
over a drill hole (< 0.01% over a few km).

GIS audit 2026-10: depth is measured from VERTEX 0, so vertex 0 has to be the
collar. Traces written before ``_TRACE_BUILDER_VERSION`` 3 had one vertex per
survey station and no collar vertex, so a survey whose first reading was at
30 m started the line at md 30: every interval was drawn 30 m uphole of where
it is, and a 30 m tail was invented at the toe to reach total depth. The
builder now writes the collar vertex; ``collar=`` here is the backstop for
traces not yet rebuilt (see ``anchor_trace_at_collar``).
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

#: Metres per degree of latitude (mean); longitude scales by cos(lat).
_M_PER_DEG_LAT = 111_320.0

#: Ignore a shortfall smaller than this rather than add a stub segment.
_EXTEND_TOLERANCE_M = 0.5

#: A first vertex closer than this (3-D) to the collar IS the collar.
_COLLAR_TOLERANCE_M = 0.5


def _offsets(
    lon: float, lat: float, lon0: float, lat0: float,
) -> tuple[float, float]:
    k = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return (lon - lon0) * k, (lat - lat0) * _M_PER_DEG_LAT


def _to_lonlat(east: float, north: float, lon0: float, lat0: float) -> tuple[float, float]:
    k = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return lon0 + (east / k if k else 0.0), lat0 + north / _M_PER_DEG_LAT


def anchor_trace_at_collar(
    points: list[tuple[float, float, float]],
    collar: tuple[float, float, float],
    total_depth: float | None,
) -> list[tuple[float, float, float]]:
    """Return ``points`` with the collar as vertex 0 if it is not already.

    ``collar`` is ``(lon, lat, z)``. A first vertex within
    ``_COLLAR_TOLERANCE_M`` of it (3-D) is the collar and the trace is
    returned unchanged. Otherwise the stored line begins below the collar -
    the pre-version-3 builder started it at the first survey station - and
    the collar is prepended. The leg it adds is the one the desurvey
    assumed (straight along the first station's attitude), so its length is
    exactly that station's measured depth.

    Left alone, deliberately, when the gap cannot be a first-station offset:

    * ``total_depth`` is unknown, or the gap is longer than it. A first
      reading deeper than the hole is impossible, so the trace is stale
      against a moved collar or a changed elevation (the rebuild fixes
      that), and gluing the collar on would shift every depth by the
      distance of the move.
    """
    if not points or total_depth is None:
        return points
    lon0, lat0, z0 = collar
    east, north = _offsets(points[0][0], points[0][1], lon0, lat0)
    gap = math.sqrt(east**2 + north**2 + (points[0][2] - z0) ** 2)
    if gap <= _COLLAR_TOLERANCE_M or gap > total_depth:
        return points
    logger.info(
        "trace_points_with_depth: stored trace starts %.1f m from its collar "
        "(built before the collar vertex was written); anchored at the collar",
        gap,
    )
    return [collar, *points]


def trace_points_with_depth(
    points: list[tuple[float, float, float]],
    total_depth: float | None,
    *,
    max_points: int,
    collar: tuple[float, float, float] | None = None,
) -> list[dict[str, float | bool]]:
    """``[{x, y, z, depth_m, east_m, north_m, extrapolated}]`` for a trace.

    ``points`` are ``(lon, lat, z)`` from the stored LINESTRING Z, collar
    first. Depths are computed on the FULL trace and only then decimated to
    ``max_points`` (first and last kept), so decimation cannot move a depth.

    ``collar`` is the hole's own ``(lon, lat, z)``. When given and the
    stored line does not start there (``anchor_trace_at_collar``), the
    collar becomes vertex 0 so depth is measured from it.
    """
    if not points:
        return []
    if collar is not None:
        points = anchor_trace_at_collar(points, collar, total_depth)
    lon0, lat0, _ = points[0]
    local = [(*_offsets(lon, lat, lon0, lat0), z) for lon, lat, z in points]

    out: list[dict[str, float | bool]] = []
    md = 0.0
    for i, ((lon, lat, z), (e, n, _z)) in enumerate(zip(points, local, strict=True)):
        if i:
            pe, pn, pz = local[i - 1]
            md += math.sqrt((e - pe) ** 2 + (n - pn) ** 2 + (z - pz) ** 2)
        out.append({
            "x": float(lon), "y": float(lat), "z": float(z),
            "depth_m": md, "east_m": e, "north_m": n, "extrapolated": False,
        })

    if (
        total_depth is not None
        and len(local) >= 2
        and total_depth - md > _EXTEND_TOLERANCE_M
    ):
        (pe, pn, pz), (e, n, z) = local[-2], local[-1]
        seg = math.sqrt((e - pe) ** 2 + (n - pn) ** 2 + (z - pz) ** 2)
        if seg > 0:
            extra = total_depth - md
            te = e + (e - pe) / seg * extra
            tn = n + (n - pn) / seg * extra
            tz = z + (z - pz) / seg * extra
            tlon, tlat = _to_lonlat(te, tn, lon0, lat0)
            out.append({
                "x": tlon, "y": tlat, "z": tz, "depth_m": float(total_depth),
                "east_m": te, "north_m": tn, "extrapolated": True,
            })

    if len(out) <= max_points:
        return out
    stride = (len(out) - 1) / (max_points - 1)
    sampled = [out[int(i * stride)] for i in range(max_points - 1)]
    sampled.append(out[-1])
    return sampled
