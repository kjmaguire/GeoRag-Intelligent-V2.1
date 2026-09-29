"""GIS-6 (audit 2026-09-29): east/west/north/south-most rank on the ground.

easting/northing hold what each source gave. In a project with LAS-placed
collars (source lon/lat) and CSV collars (UTM metres), max(easting) picks the
UTM hole whatever the geography. Ranking uses geom_4326's lon/lat.
"""
from __future__ import annotations

from app.agent.tool_result_helpers import _build_collar_aggregates
from app.agent.tools import CollarRecord


def _c(hole: str, e: float, n: float, lon: float | None, lat: float | None) -> CollarRecord:
    return CollarRecord(
        hole_id=hole, collar_id=hole, easting=e, northing=n, elevation=0.0,
        total_depth=100.0, hole_type="DD", azimuth=0.0, dip=-90.0,
        status="done", drill_date=None, longitude=lon, latitude=lat,
    )


def test_mixed_source_grids_rank_by_longitude() -> None:
    lines = _build_collar_aggregates([
        # UTM 13N metres, ~ -105.0
        _c("CSV-1", 500000.0, 6340000.0, -105.0, 57.2),
        # Source lon/lat stored as given, further EAST on the ground
        _c("LAS-1", -104.5, 57.1, -104.5, 57.1),
    ])
    text = "\n".join(lines)
    assert "Easternmost hole: LAS-1" in text
    assert "Westernmost hole: CSV-1" in text
    assert "Northernmost hole: CSV-1" in text


def test_legacy_rows_without_position_fall_back_to_the_columns() -> None:
    lines = _build_collar_aggregates([
        _c("A", 500000.0, 6340000.0, None, None),
        _c("B", 501000.0, 6341000.0, None, None),
    ])
    assert "Easternmost hole: B" in "\n".join(lines)
