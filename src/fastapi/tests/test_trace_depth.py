"""GIS-8 (audit 2026-09-29): 3-D trace vertices carry MEASURED depth.

The card used ``depth_m = total_depth * i / n`` by vertex index. With
stations at 0, 12, 24 and 300 m on a 300 m hole, the vertices were labelled
0/100/200/300 and an interval at 150 m was drawn ~18 m downhole.
"""
from __future__ import annotations

import pytest

from app.agent.trace_depth import trace_points_with_depth

_LON0, _LAT0 = -105.6231, 57.2104
_M_PER_DEG_LAT = 111_320.0


def _vertical(depths: list[float], elev: float = 480.0) -> list[tuple[float, float, float]]:
    return [(_LON0, _LAT0, elev - d) for d in depths]


def test_uneven_stations_get_their_true_depths() -> None:
    pts = trace_points_with_depth(_vertical([0, 12, 24, 300]), 300.0, max_points=50)
    assert [p["depth_m"] for p in pts] == pytest.approx([0, 12, 24, 300])
    assert not any(p["extrapolated"] for p in pts)


def test_short_trace_is_extended_to_total_depth() -> None:
    pts = trace_points_with_depth(_vertical([0, 50, 100]), 250.0, max_points=50)
    assert pts[-1]["depth_m"] == pytest.approx(250.0)
    assert pts[-1]["extrapolated"] is True
    assert pts[-1]["z"] == pytest.approx(480.0 - 250.0)
    assert pts[-1]["x"] == pytest.approx(_LON0)


def test_inclined_trace_depth_and_local_offsets() -> None:
    # 100 m due north at -45 degrees: 70.71 m north, 70.71 m down.
    d = 100.0 / (2 ** 0.5)
    pts = trace_points_with_depth(
        [(_LON0, _LAT0, 480.0), (_LON0, _LAT0 + d / _M_PER_DEG_LAT, 480.0 - d)],
        100.0, max_points=50,
    )
    assert pts[-1]["depth_m"] == pytest.approx(100.0, rel=1e-6)
    assert pts[-1]["north_m"] == pytest.approx(d, rel=1e-6)
    assert pts[-1]["east_m"] == pytest.approx(0.0, abs=1e-6)


def test_decimation_keeps_depths_of_the_full_trace() -> None:
    depths = [float(i) for i in range(0, 301)]
    pts = trace_points_with_depth(_vertical(depths), 300.0, max_points=10)
    assert len(pts) == 10
    assert pts[0]["depth_m"] == 0.0 and pts[-1]["depth_m"] == pytest.approx(300.0)
    for p in pts:  # each kept vertex keeps ITS OWN depth
        assert p["depth_m"] == pytest.approx(480.0 - p["z"])


def test_empty_trace() -> None:
    assert trace_points_with_depth([], 100.0, max_points=50) == []
