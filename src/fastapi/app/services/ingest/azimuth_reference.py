"""Which north a drill azimuth is measured from, and the correction to apply.

GIS-12 (audit 2026-09-29). Desurvey (``promote_silver_to_gold._promote_traces``)
assembles each trace in the collar's OWN UTM zone, so every azimuth is
implicitly treated as grid north of that zone. A true-north azimuth then
carries the meridian convergence as an error (2.5-2.7 degrees at 58-64 N,
~20 m over a 500 m hole); a magnetic one carries the whole declination
(often 5-15 degrees in this domain).

Kyle's decision (2026-09-29): the DEFAULT is unchanged — no convergence or
declination is applied — unless the data DECLARES its azimuth reference.
As built, the only declaration there is to read is project-level:

* ``silver.projects.orientation_reference`` — a varchar(10) whose values in
  practice are 'BOH'/'TOH' (the core-orientation mark: bottom/top of hole,
  which is NOT an azimuth reference and is ignored here), 'grid_north'
  (stamped by the LAS ingester) and, from the factory, 'grid'/'true'.
  Recognised here: true / true_north / tn; magnetic / magnetic_north / mag /
  mn; grid / grid_north / gn.
* ``silver.projects.magnetic_declination`` — degrees, east positive.
  Required for a magnetic reference; without it nothing is applied.

Survey files carry no per-row reference column in silver.surveys, so a
per-file declaration would need a schema change (Kyle).

As-built assumption, stated plainly: with no recognised reference, azimuths
are taken as grid north of the collar's own UTM zone. For a project whose
CRS is that zone (the common case) that is exactly grid north of the
project grid; for true-north surveys it is wrong by the convergence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_TRUE = frozenset({"true", "true_north", "truenorth", "tn"})
_MAGNETIC = frozenset({"magnetic", "magnetic_north", "magneticnorth", "mag", "mn"})
_GRID = frozenset({"grid", "grid_north", "gridnorth", "gn"})

#: Northward step used to measure the direction of true north in a grid.
_STEP_DEG = 1e-4


@dataclass(frozen=True)
class AzimuthCorrection:
    """Degrees to ADD to a recorded azimuth to get local-zone grid azimuth."""

    degrees: float
    #: 'none' | 'true' | 'magnetic' | 'grid'
    reference: str
    #: Why nothing was applied, when degrees == 0 for a declared reference.
    note: str | None = None


def _reference_kind(raw: str | None) -> str:
    token = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if token in _TRUE:
        return "true"
    if token in _MAGNETIC:
        return "magnetic"
    if token in _GRID:
        return "grid"
    return "none"


def true_north_bearing_in_grid(epsg: int, lon: float, lat: float) -> float:
    """Clockwise angle from grid north to TRUE north at (lon, lat) in *epsg*.

    Measured, not taken from a sign convention: a short step due north along
    the meridian is projected and its grid bearing read off. East of a UTM
    central meridian in the northern hemisphere true north lies WEST of grid
    north, so this is negative there.
    """
    from pyproj import Transformer  # noqa: PLC0415

    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{int(epsg)}", always_xy=True)
    e0, n0 = tf.transform(lon, lat)
    e1, n1 = tf.transform(lon, lat + _STEP_DEG)
    return math.degrees(math.atan2(e1 - e0, n1 - n0))


def azimuth_correction(
    *,
    orientation_reference: str | None,
    magnetic_declination: float | None,
    project_epsg: int | None,
    local_epsg: int,
    lon: float,
    lat: float,
) -> AzimuthCorrection:
    """The correction for one collar, or zero when nothing is declared."""
    kind = _reference_kind(orientation_reference)
    if kind == "none":
        return AzimuthCorrection(0.0, "none")

    local_true = true_north_bearing_in_grid(local_epsg, lon, lat)

    if kind == "true":
        # A direction at true azimuth a sits at grid azimuth a + (bearing of
        # true north in the grid).
        return AzimuthCorrection(local_true, "true")

    if kind == "magnetic":
        if magnetic_declination is None:
            return AzimuthCorrection(
                0.0, "magnetic",
                note="magnetic reference declared without a declination; not applied",
            )
        return AzimuthCorrection(float(magnetic_declination) + local_true, "magnetic")

    # Grid north of the PROJECT grid. Only differs from the collar's local
    # zone when the project CRS is a different projected system.
    if project_epsg is None or int(project_epsg) == int(local_epsg):
        return AzimuthCorrection(0.0, "grid")
    try:
        project_true = true_north_bearing_in_grid(int(project_epsg), lon, lat)
    except Exception:  # noqa: BLE001 — a geographic/unusable project CRS: nothing to convert
        return AzimuthCorrection(0.0, "grid", note=f"EPSG:{project_epsg} has no grid north")
    # grid_P -> true: a - project_true; true -> local grid: + local_true.
    return AzimuthCorrection(local_true - project_true, "grid")


def apply(azimuth: float, correction: AzimuthCorrection) -> float:
    """``azimuth + correction`` wrapped to [0, 360)."""
    if correction.degrees == 0.0:
        return azimuth
    return (azimuth + correction.degrees) % 360.0
