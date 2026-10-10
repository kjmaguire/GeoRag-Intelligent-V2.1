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


# ---------------------------------------------------------------------------
# GIS audit 2026-10 (finding 1): traces stored before the collar vertex existed
# ---------------------------------------------------------------------------
_COLLAR = (_LON0, _LAT0, 480.0)


def test_legacy_trace_starting_below_the_collar_is_anchored_at_it() -> None:
    """Stations 30/100/200 and no collar vertex: vertex 0 was md 30."""
    legacy = _vertical([30, 100, 200])
    pts = trace_points_with_depth(legacy, 200.0, max_points=50, collar=_COLLAR)

    assert [p["depth_m"] for p in pts] == pytest.approx([0.0, 30.0, 100.0, 200.0])
    assert pts[0]["z"] == pytest.approx(480.0)
    assert not any(p["extrapolated"] for p in pts), "no phantom tail"


def test_without_the_collar_the_legacy_trace_is_still_shifted() -> None:
    """The default is unchanged: callers that do not pass a collar get what they had."""
    pts = trace_points_with_depth(_vertical([30, 100, 200]), 200.0, max_points=50)
    assert pts[0]["depth_m"] == 0.0 and pts[0]["z"] == pytest.approx(450.0)
    assert pts[-1]["extrapolated"] is True


def test_legacy_inclined_trace_is_anchored_by_its_plan_offset_too() -> None:
    # Due north at -45: md 30 is 21.2 m north and 21.2 m down.
    d = 30.0 / (2 ** 0.5)
    legacy = [
        (_LON0, _LAT0 + d / _M_PER_DEG_LAT, 480.0 - d),
        (_LON0, _LAT0 + 3 * d / _M_PER_DEG_LAT, 480.0 - 3 * d),
    ]
    pts = trace_points_with_depth(legacy, 90.0, max_points=50, collar=_COLLAR)

    assert [p["depth_m"] for p in pts] == pytest.approx([0.0, 30.0, 90.0], rel=1e-6)
    assert (pts[0]["x"], pts[0]["y"]) == (_LON0, _LAT0)
    assert pts[-1]["extrapolated"] is False


def test_a_trace_that_already_starts_at_the_collar_is_left_alone() -> None:
    current = _vertical([0, 30, 100, 200])
    with_collar = trace_points_with_depth(current, 200.0, max_points=50, collar=_COLLAR)
    without = trace_points_with_depth(current, 200.0, max_points=50)
    assert with_collar == without


def test_a_millimetre_of_float_noise_is_not_a_missing_collar() -> None:
    noisy = [(_LON0 + 1e-9, _LAT0 - 1e-9, 480.0 + 1e-4), (_LON0, _LAT0, 380.0)]
    pts = trace_points_with_depth(noisy, 100.0, max_points=50, collar=_COLLAR)
    assert len(pts) == 2 and pts[0]["depth_m"] == 0.0


def test_a_first_vertex_deeper_than_the_hole_is_not_a_first_station() -> None:
    """A stale trace against a moved collar must not get a km-long leg glued on."""
    far = [(_LON0 + 0.5, _LAT0, 480.0), (_LON0 + 0.5, _LAT0, 380.0)]  # ~30 km away
    pts = trace_points_with_depth(far, 100.0, max_points=50, collar=_COLLAR)
    assert len(pts) == 2 and pts[0]["depth_m"] == 0.0


def test_an_unknown_total_depth_leaves_the_trace_alone() -> None:
    pts = trace_points_with_depth(_vertical([30, 100]), None, max_points=50, collar=_COLLAR)
    assert len(pts) == 2 and pts[0]["z"] == pytest.approx(450.0)
