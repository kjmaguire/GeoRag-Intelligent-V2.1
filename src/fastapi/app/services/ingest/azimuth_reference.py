"""Which north a drill azimuth is measured from, and the correction to apply.

GIS-12 (audit 2026-09-29). Desurvey (``promote_silver_to_gold._promote_traces``)
assembles each trace in the collar's OWN UTM zone, so every azimuth is
implicitly treated as grid north of that zone. A true-north azimuth then
carries the meridian convergence as an error (2.5-2.7 degrees at 58-64 N,
~20 m over a 500 m hole); a magnetic one carries the whole declination
(often 5-15 degrees in this domain).

Kyle's decisions (2026-09-29): the DEFAULT is unchanged — no convergence or
declination is applied — unless the data DECLARES its azimuth reference.
Two declarations are read, most specific first:

1. ``silver.surveys.azimuth_reference`` — per station, populated from an
   azimuth-reference column in the survey file (``Azimuth_Ref``,
   ``Az_Reference``, ``North_Ref`` ...). CHECK-constrained to
   true / magnetic / grid; NULL when the file had no such column.
2. ``silver.projects.orientation_reference`` — the project default, set in
   the project settings. BOH / TOH (the core-orientation mark: bottom/top of
   hole, NOT an azimuth reference) declare nothing; grid / true / magnetic
   do; legacy 'grid_north' (the LAS ingester's old stamp) reads as grid.

``silver.projects.magnetic_declination`` — degrees, EAST POSITIVE — is
required for a magnetic reference (from either source); without it nothing
is applied and the trace is counted as ``traces_azimuth_reference_unapplied``
so the gap is visible rather than smoothed away.

The collar's own azimuth (a hole with fewer than two survey stations) comes
from the collar table, not a survey file, so only the project default
applies to it.

As-built assumption, stated plainly: with no recognised reference, azimuths
are taken as grid north of the collar's own UTM zone. For a project whose
CRS is that zone (the common case) that is exactly grid north of the
project grid; for true-north surveys it is wrong by the convergence.

Spellings live in ``georag_geoparsers._azimuth_reference`` — the parser that
fills silver.surveys.azimuth_reference reads through the same function.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from georag_geoparsers._azimuth_reference import canonical_azimuth_reference

logger = logging.getLogger(__name__)

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
    return canonical_azimuth_reference(raw) or "none"


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
        # true = magnetic + declination (east positive); then true -> grid.
        return AzimuthCorrection(float(magnetic_declination) + local_true, "magnetic")

    # Grid north of the PROJECT grid. Only differs from the collar's local
    # zone when the project CRS is a different projected system.
    if project_epsg is None or int(project_epsg) == int(local_epsg):
        return AzimuthCorrection(0.0, "grid")
    try:
        project_true = true_north_bearing_in_grid(int(project_epsg), lon, lat)
    except Exception:  # noqa: BLE001 — a geographic/unusable project CRS: nothing to convert
        logger.debug("EPSG:%s has no grid north", project_epsg, exc_info=True)
        return AzimuthCorrection(0.0, "grid", note=f"EPSG:{project_epsg} has no grid north")
    # grid_P -> true: a - project_true; true -> local grid: + local_true.
    return AzimuthCorrection(local_true - project_true, "grid")


def apply(azimuth: float, correction: AzimuthCorrection) -> float:
    """``azimuth + correction`` wrapped to [0, 360)."""
    if correction.degrees == 0.0:
        return azimuth
    return (azimuth + correction.degrees) % 360.0


@dataclass
class StationCorrections:
    """Survey rows with azimuths in the collar's local grid, and what was done."""

    rows: list[dict[str, Any]]
    #: Any station's azimuth was actually changed.
    corrected: bool = False
    #: A reference was declared (file or project) but could not be applied —
    #: magnetic with no project declination. Surfaced, never guessed.
    unapplied_notes: list[str] = field(default_factory=list)
    #: Which source declared each distinct reference used: 'survey' | 'project'.
    sources: set[str] = field(default_factory=set)


def correct_survey_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    project_reference: str | None,
    magnetic_declination: float | None,
    project_epsg: int | None,
    local_epsg: int,
    lon: float,
    lat: float,
) -> StationCorrections:
    """Apply each station's DECLARED reference, the survey's own first.

    A row's ``azimuth_reference`` (silver.surveys, from the file) wins over
    *project_reference*; with neither recognised the azimuth is untouched.
    Rows keep every other key, so the result feeds ``_clean_stations``
    unchanged. A NULL azimuth stays NULL (it is dropped there).
    """
    cache: dict[str, AzimuthCorrection] = {}
    out = StationCorrections(rows=[])
    for row in rows:
        own = canonical_azimuth_reference(row.get("azimuth_reference"))
        reference = own or project_reference
        new_row = dict(row)
        azimuth = row.get("azimuth")
        if azimuth is not None and _reference_kind(reference) != "none":
            key = _reference_kind(reference)
            if key not in cache:
                cache[key] = azimuth_correction(
                    orientation_reference=key,
                    magnetic_declination=magnetic_declination,
                    project_epsg=project_epsg,
                    local_epsg=local_epsg,
                    lon=lon, lat=lat,
                )
            corr = cache[key]
            out.sources.add("survey" if own else "project")
            if corr.degrees:
                new_row["azimuth"] = apply(float(azimuth), corr)
                out.corrected = True
            elif corr.note and corr.note not in out.unapplied_notes:
                out.unapplied_notes.append(corr.note)
        out.rows.append(new_row)
    return out
