"""Drill target recommendation — suggests optimal next-hole locations.

Given the spatial distribution of existing drill holes and their assay
grades, this module identifies untested areas with high predicted grade
potential and recommends collar positions for the next phase of drilling.

Method:
  1. Inverse Distance Weighting (IDW) interpolation of grade values onto
     a regular grid covering the project footprint.
  2. Grade-thickness product computation at each collar.
  3. Gap analysis — find grid cells >N metres from any existing collar
     that have high interpolated grade.
  4. Rank candidates by predicted grade × distance-from-nearest-hole
     (information gain proxy).

Why IDW instead of kriging:
  Kriging (pykrige/scikit-gstat) requires fitting a variogram which needs
  >20 spatially distributed samples to produce a reliable model. Our dev
  dataset has 10-25 samples — too few for stable variogram estimation.
  IDW is a robust fallback that produces useful spatial trends even with
  sparse data. When a real dataset with >50 samples is available, swap
  to ordinary kriging by replacing the _interpolate_idw function.

Usage:
    from app.agent.drill_targeting import recommend_targets
    targets = recommend_targets(collars, assay_samples)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DrillTarget:
    """A recommended drill target location."""
    easting: float
    northing: float
    longitude: float | None
    latitude: float | None
    predicted_grade: float
    element: str
    distance_to_nearest_hole: float
    information_gain_score: float  # higher = more valuable
    rationale: str
    rank: int


def recommend_targets(
    collars: list,  # CollarRecord objects
    assay_samples: list | None = None,  # AssaySample objects
    grid_spacing: float = 100.0,  # metres between grid nodes
    min_distance: float = 200.0,  # minimum metres from any existing collar
    n_targets: int = 3,
    element: str = "U3O8_ppm",
) -> list[DrillTarget]:
    """Recommend optimal next-hole locations.

    Args:
        collars: List of CollarRecord with easting/northing/longitude/latitude.
        assay_samples: List of AssaySample with hole_id and value.
        grid_spacing: Grid node spacing in metres.
        min_distance: Minimum distance from existing collars (metres).
        n_targets: Number of targets to return.
        element: Assay element name for grade interpolation.

    Returns:
        Ranked list of DrillTarget objects.
    """
    if len(collars) < 2:
        logger.info("drill_targeting: need >=2 collars, got %d", len(collars))
        return []

    # Build collar-grade map (max grade per hole)
    grade_by_hole: dict[str, float] = {}
    if assay_samples:
        for s in assay_samples:
            if s.hole_id not in grade_by_hole or s.value > grade_by_hole[s.hole_id]:
                grade_by_hole[s.hole_id] = s.value

    # Assign grades to collar positions
    collar_points: list[tuple[float, float, float]] = []  # (easting, northing, grade)
    for c in collars:
        grade = grade_by_hole.get(c.hole_id, 0.0)
        collar_points.append((c.easting, c.northing, grade))

    if not any(g > 0 for _, _, g in collar_points):
        logger.info("drill_targeting: no grade data available, using depth as proxy")
        collar_points = [(c.easting, c.northing, c.total_depth) for c in collars]

    # Define grid bounds with padding
    eastings = [p[0] for p in collar_points]
    northings = [p[1] for p in collar_points]
    pad = grid_spacing * 3

    e_min, e_max = min(eastings) - pad, max(eastings) + pad
    n_min, n_max = min(northings) - pad, max(northings) + pad

    # Generate grid nodes
    grid_nodes: list[tuple[float, float]] = []
    e = e_min
    while e <= e_max:
        n = n_min
        while n <= n_max:
            grid_nodes.append((e, n))
            n += grid_spacing
        e += grid_spacing

    logger.info(
        "drill_targeting: %d grid nodes, %d collars, %d with grades",
        len(grid_nodes),
        len(collars),
        len(grade_by_hole),
    )

    # IDW interpolation + distance filtering
    candidates: list[tuple[float, float, float, float]] = []  # (e, n, predicted_grade, dist_nearest)

    for ge, gn in grid_nodes:
        # Distance to nearest collar
        min_dist = min(
            math.sqrt((ge - ce) ** 2 + (gn - cn) ** 2)
            for ce, cn, _ in collar_points
        )

        # Skip if too close to existing collar
        if min_dist < min_distance:
            continue

        # IDW interpolation (power=2)
        predicted = _interpolate_idw(ge, gn, collar_points, power=2)
        candidates.append((ge, gn, predicted, min_dist))

    if not candidates:
        logger.info("drill_targeting: no candidate locations found")
        return []

    # Score: predicted_grade × log(distance) — balances high grade with information gain
    for i, (e, n, grade, dist) in enumerate(candidates):
        score = grade * math.log(max(dist, 1))
        candidates[i] = (e, n, grade, dist, score)  # type: ignore

    # Sort by score descending, take top N
    candidates.sort(key=lambda x: x[4], reverse=True)  # type: ignore
    top = candidates[:n_targets]

    # Build collar lookup for longitude/latitude estimation
    collar_by_en: dict[tuple[float, float], tuple[float | None, float | None]] = {}
    for c in collars:
        collar_by_en[(c.easting, c.northing)] = (c.longitude, c.latitude)

    # Estimate lon/lat via nearest collar (rough but sufficient for map display)
    targets: list[DrillTarget] = []
    for rank, (e, n, grade, dist, score) in enumerate(top, start=1):  # type: ignore
        # Find nearest collar for lon/lat reference
        nearest_collar = min(
            collars,
            key=lambda c: math.sqrt((e - c.easting) ** 2 + (n - c.northing) ** 2),
        )
        # Linear offset estimate (crude but works at UTM scale)
        lon = None
        lat = None
        if nearest_collar.longitude is not None:
            de = e - nearest_collar.easting
            dn = n - nearest_collar.northing
            # ~111,000 m per degree lat, ~111,000 * cos(lat) per degree lon
            cos_lat = math.cos(math.radians(nearest_collar.latitude or 56))
            lon = nearest_collar.longitude + de / (111_000 * cos_lat)
            lat = nearest_collar.latitude + dn / 111_000

        direction = ""
        if nearest_collar.easting != e or nearest_collar.northing != n:
            bearing = math.degrees(math.atan2(e - nearest_collar.easting, n - nearest_collar.northing))
            if bearing < 0:
                bearing += 360
            compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((bearing + 22.5) / 45) % 8]
            direction = f"{dist:.0f} m {compass} of {nearest_collar.hole_id}"

        targets.append(DrillTarget(
            easting=round(e, 1),
            northing=round(n, 1),
            longitude=round(lon, 6) if lon else None,
            latitude=round(lat, 6) if lat else None,
            predicted_grade=round(grade, 1),
            element=element,
            distance_to_nearest_hole=round(dist, 0),
            information_gain_score=round(score, 1),
            rationale=f"Rank #{rank}: Predicted {element} grade {grade:,.0f} at {direction}. "
                      f"This location is {dist:.0f} m from the nearest existing collar, "
                      f"providing maximum information gain for resource delineation.",
            rank=rank,
        ))

    logger.info("drill_targeting: recommended %d targets", len(targets))
    return targets


def _interpolate_idw(
    x: float, y: float,
    points: list[tuple[float, float, float]],
    power: float = 2,
) -> float:
    """Inverse Distance Weighting interpolation at (x, y).

    Args:
        x, y: Query point coordinates.
        points: List of (x, y, value) known data points.
        power: Distance weighting exponent (higher = more local).

    Returns:
        Interpolated value.
    """
    weights = []
    values = []

    for px, py, pv in points:
        dist = math.sqrt((x - px) ** 2 + (y - py) ** 2)
        if dist < 1e-6:
            return pv  # exact match
        w = 1.0 / (dist ** power)
        weights.append(w)
        values.append(pv)

    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0

    return sum(w * v for w, v in zip(weights, values)) / total_weight
