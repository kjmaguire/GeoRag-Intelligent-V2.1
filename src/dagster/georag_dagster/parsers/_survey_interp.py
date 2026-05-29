"""Minimum-curvature downhole interpolation.

Standalone math module — no Dagster, no DB, no I/O.  Wired into a future
sample-XYZ materialization asset (Sprint 2b); for Sprint 2 it is a clean
library that tests can exercise directly.

Convention notes
----------------
- All depth inputs are in metres, positive downhole.
- Azimuth inputs are in degrees, 0–360 (north-up, clockwise).
- Dip inputs are in the **DB down-negative convention** (vertical = -90, flat
  drilling = 0).  The caller MUST normalize via
  ``_dip_convention.normalize_dip`` before passing values here.
- Internal math uses radians throughout.  The public API is degrees-in /
  metres-out.  No radians ever cross the module boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SurveyStation:
    """A single downhole survey reading."""

    depth_m: float       # Downhole depth, metres, >= 0
    azimuth_deg: float   # Azimuth, degrees, 0–360 (north-up)
    dip_deg: float       # Dip, degrees, DOWN-NEGATIVE convention (0 to -90)


@dataclass(frozen=True)
class XYZ:
    """A Cartesian position relative to the collar."""

    east_m: float    # Easting offset from collar, metres
    north_m: float   # Northing offset from collar, metres
    elev_m: float    # Elevation offset from collar: 0 at collar, negative downhole


# ---------------------------------------------------------------------------
# Internal math helpers
# ---------------------------------------------------------------------------

def _deg2rad(deg: float) -> float:
    return math.radians(deg)


def _validate_stations(stations: list[SurveyStation]) -> None:
    """Raise ValueError for invalid station inputs."""
    for s in stations:
        if s.dip_deg > 0:
            raise ValueError(
                f"minimum_curvature: dip_deg {s.dip_deg} at depth {s.depth_m} m is > 0 "
                f"(up-going). Normalize via _dip_convention.normalize_dip first."
            )
        if s.dip_deg < -90:
            raise ValueError(
                f"minimum_curvature: dip_deg {s.dip_deg} at depth {s.depth_m} m is < -90 "
                f"(impossible). Check data."
            )

    # Monotonically increasing depth check
    for i in range(1, len(stations)):
        if stations[i].depth_m <= stations[i - 1].depth_m:
            raise ValueError(
                f"minimum_curvature: stations must be monotonically increasing in depth. "
                f"depth[{i-1}]={stations[i-1].depth_m} >= depth[{i}]={stations[i].depth_m}"
            )


def _direction_cosines(azimuth_rad: float, dip_rad: float) -> tuple[float, float, float]:
    """Return (north, east, up) direction cosines for a given azimuth and dip.

    Dip convention: down-negative means sin(dip) is negative for downward
    motion (elev decreases).

    Standard minimum-curvature formulation:
      N = cos(dip) * cos(az)
      E = cos(dip) * sin(az)
      Z = sin(dip)          # negative for downward because dip < 0
    """
    cos_dip = math.cos(dip_rad)
    sin_dip = math.sin(dip_rad)
    cos_az = math.cos(azimuth_rad)
    sin_az = math.sin(azimuth_rad)
    return cos_dip * cos_az, cos_dip * sin_az, sin_dip


def _ratio_factor(beta_rad: float) -> float:
    """Return the minimum-curvature ratio factor RF = (2/β)*tan(β/2).

    When β ≈ 0 (straight segment) RF → 1.0 (L'Hopital's rule limit).
    A small-angle threshold avoids division by zero.
    """
    if abs(beta_rad) < 1e-10:
        return 1.0
    return (2.0 / beta_rad) * math.tan(beta_rad / 2.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def minimum_curvature(
    collar_easting: float,
    collar_northing: float,
    collar_elevation: float,
    stations: list[SurveyStation],
) -> list[tuple[float, XYZ]]:
    """Compute XYZ positions along a drill hole using minimum-curvature.

    Returns a list of ``(depth, XYZ)`` pairs, one per station, with absolute
    XYZ coordinates in the same CRS units as the collar coordinates.

    The XYZ at each station is accumulated from the collar, applying the
    balanced-tangent (minimum-curvature) correction between consecutive
    station pairs.

    Parameters
    ----------
    collar_easting, collar_northing, collar_elevation:
        Absolute position of the drill hole collar in the project CRS.
    stations:
        Survey stations in monotonically increasing depth order.  The caller
        decides whether to prepend a collar station at depth=0.

    Returns
    -------
    list of (depth_m, XYZ) tuples.  Empty for empty input.

    Raises
    ------
    ValueError
        If any dip is > 0 (up-going) or < -90 (impossible), or if stations
        are not monotonically increasing in depth.

    Notes
    -----
    Standard minimum-curvature formulae:

        dL = depth[i+1] - depth[i]
        β  = dogleg angle between (az_i, dip_i) and (az_{i+1}, dip_{i+1})
        RF = 2/β * tan(β/2)   (ratio factor; RF=1 when β=0)
        ΔN = dL/2 * (N_i + N_{i+1}) * RF
        ΔE = dL/2 * (E_i + E_{i+1}) * RF
        ΔZ = dL/2 * (Z_i + Z_{i+1}) * RF

    where N/E/Z are the direction cosines described in ``_direction_cosines``.
    """
    if not stations:
        return []

    _validate_stations(stations)

    result: list[tuple[float, XYZ]] = []

    # Accumulate absolute position starting from the collar
    abs_east = collar_easting
    abs_north = collar_northing
    abs_elev = collar_elevation

    # First station: position is the collar offset by a straight projection
    # from depth=0 to stations[0].depth_m using only the first station's
    # attitude (tangent method for the initial segment if no collar station).
    # If the caller wants exact collar-start behaviour they should prepend a
    # station at depth=0.

    # Station 0 — propagate from collar at depth 0 down to stations[0].depth_m
    az0_rad = _deg2rad(stations[0].azimuth_deg)
    dip0_rad = _deg2rad(stations[0].dip_deg)
    n0, e0, z0 = _direction_cosines(az0_rad, dip0_rad)

    dL0 = stations[0].depth_m
    # For the first segment (collar → station 0) use tangent method (no previous station)
    abs_east += dL0 * e0
    abs_north += dL0 * n0
    abs_elev += dL0 * z0

    result.append((
        stations[0].depth_m,
        XYZ(
            east_m=abs_east,
            north_m=abs_north,
            elev_m=abs_elev - collar_elevation,  # relative to collar
        ),
    ))

    # Subsequent stations — use minimum curvature between consecutive pairs
    for i in range(1, len(stations)):
        s_prev = stations[i - 1]
        s_curr = stations[i]

        dL = s_curr.depth_m - s_prev.depth_m

        az1_rad = _deg2rad(s_prev.azimuth_deg)
        dip1_rad = _deg2rad(s_prev.dip_deg)
        az2_rad = _deg2rad(s_curr.azimuth_deg)
        dip2_rad = _deg2rad(s_curr.dip_deg)

        n1, e1, z1 = _direction_cosines(az1_rad, dip1_rad)
        n2, e2, z2 = _direction_cosines(az2_rad, dip2_rad)

        # Dogleg angle β — angle between the two direction vectors
        # dot product of unit vectors: n1*n2 + e1*e2 + z1*z2
        dot = n1 * n2 + e1 * e2 + z1 * z2
        # Clamp to [-1, 1] to guard against floating-point noise
        dot = max(-1.0, min(1.0, dot))
        beta = math.acos(dot)

        rf = _ratio_factor(beta)

        delta_north = (dL / 2.0) * (n1 + n2) * rf
        delta_east = (dL / 2.0) * (e1 + e2) * rf
        delta_elev = (dL / 2.0) * (z1 + z2) * rf

        abs_east += delta_east
        abs_north += delta_north
        abs_elev += delta_elev

        result.append((
            s_curr.depth_m,
            XYZ(
                east_m=abs_east,
                north_m=abs_north,
                elev_m=abs_elev - collar_elevation,
            ),
        ))

    return result


def interpolate_sample_xyz(
    sample_depth: float,
    station_xyz: list[tuple[float, XYZ]],
) -> Optional[XYZ]:
    """Linearly interpolate an XYZ at *sample_depth* from pre-computed station XYZs.

    Uses the output of :func:`minimum_curvature` directly.  No extrapolation —
    returns None if *sample_depth* is outside the range covered by the stations.

    Parameters
    ----------
    sample_depth:
        Downhole depth at which to interpolate, metres.
    station_xyz:
        List of ``(depth, XYZ)`` tuples from :func:`minimum_curvature`.

    Returns
    -------
    XYZ at *sample_depth*, or None if out of range.
    """
    if not station_xyz:
        return None

    depths = [d for d, _ in station_xyz]
    min_depth = depths[0]
    max_depth = depths[-1]

    if sample_depth < min_depth or sample_depth > max_depth:
        return None

    # Exact match shortcut
    for depth, xyz in station_xyz:
        if math.isclose(depth, sample_depth, rel_tol=1e-9):
            return xyz

    # Find bracketing stations
    for i in range(len(station_xyz) - 1):
        d0, xyz0 = station_xyz[i]
        d1, xyz1 = station_xyz[i + 1]

        if d0 <= sample_depth <= d1:
            span = d1 - d0
            if span < 1e-12:
                return xyz0  # degenerate zero-length segment

            t = (sample_depth - d0) / span
            return XYZ(
                east_m=xyz0.east_m + t * (xyz1.east_m - xyz0.east_m),
                north_m=xyz0.north_m + t * (xyz1.north_m - xyz0.north_m),
                elev_m=xyz0.elev_m + t * (xyz1.elev_m - xyz0.elev_m),
            )

    return None  # should not be reached
