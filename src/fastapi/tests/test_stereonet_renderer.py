"""§5 stereonet renderer tests (Phase H4)."""
from __future__ import annotations

import math

import pytest

from app.services.visualizations import (
    StereonetPoint,
    render_stereonet_matplotlib_png,
    render_stereonet_plotly_figure,
)
from app.services.visualizations.stereonet import _equal_area_project


def _point(
    trend: float, plunge: float,
    kind: str = "bedding",
    strike: float | None = None,
    dip: float | None = None,
) -> StereonetPoint:
    return StereonetPoint(
        measurement_id=f"m-{kind}-{int(trend)}-{int(plunge)}",
        measurement_kind=kind,
        pole_trend_deg=trend,
        pole_plunge_deg=plunge,
        strike_deg=strike,
        dip_deg=dip,
    )


# ──────────────────── projection math sanity ────────────────────


def test_projection_vertical_pole_at_center() -> None:
    """A pole with plunge 90° (= horizontal plane) projects to (0, 0)."""
    x, y = _equal_area_project(0, 90)
    assert abs(x) < 1e-9
    assert abs(y) < 1e-9


def test_projection_horizontal_pole_at_north_lies_on_primitive() -> None:
    """A pole with trend 0° + plunge 0° (= vertical plane striking E-W,
    pole points horizontally north) projects to (0, sqrt(2)·sin(45°)) = (0, 1)."""
    x, y = _equal_area_project(0, 0)
    # Equal-area: r = sqrt(2) × sin((90-0)/2) = sqrt(2) × sin(45°) = 1
    assert abs(x) < 1e-9
    assert abs(y - 1.0) < 1e-9


def test_projection_horizontal_pole_at_east() -> None:
    x, y = _equal_area_project(90, 0)
    assert abs(x - 1.0) < 1e-9
    assert abs(y) < 1e-9


def test_projection_45_degree_plunge_at_north() -> None:
    """45° plunge to the north → r = sqrt(2) × sin(22.5°) ≈ 0.541."""
    x, y = _equal_area_project(0, 45)
    expected_r = math.sqrt(2) * math.sin(math.radians(22.5))
    assert abs(x) < 1e-9
    assert abs(y - expected_r) < 1e-9


# ──────────────────── Plotly renderer ──────────────────────────


def test_plotly_empty_input_produces_no_data_figure() -> None:
    fig = render_stereonet_plotly_figure([])
    assert fig["data"] == []
    assert any("No structural measurements" in a["text"]
               for a in fig["layout"]["annotations"])


def test_plotly_n_e_s_w_compass_labels_present() -> None:
    fig = render_stereonet_plotly_figure([
        _point(45, 30, kind="bedding"),
    ])
    texts = [a["text"] for a in fig["layout"]["annotations"]]
    assert "<b>N</b>" in texts
    assert "<b>E</b>" in texts
    assert "<b>S</b>" in texts
    assert "<b>W</b>" in texts


def test_plotly_groups_points_by_kind() -> None:
    """Each measurement_kind gets its own trace (with name + color)."""
    points = [
        _point(10, 20, kind="bedding"),
        _point(50, 40, kind="bedding"),
        _point(90, 60, kind="foliation"),
        _point(180, 10, kind="fault"),
    ]
    fig = render_stereonet_plotly_figure(points)
    # 1 primitive circle + 3 trace groups
    assert len(fig["data"]) == 4
    trace_names = [t.get("name", "") for t in fig["data"]
                   if t.get("name") and t["name"] != "primitive"]
    assert any("bedding" in n for n in trace_names)
    assert any("foliation" in n for n in trace_names)
    assert any("fault" in n for n in trace_names)


def test_plotly_aspect_ratio_forced_square() -> None:
    fig = render_stereonet_plotly_figure([_point(0, 30)])
    assert fig["layout"]["xaxis"]["scaleanchor"] == "y"
    assert fig["layout"]["xaxis"]["scaleratio"] == 1.0


def test_plotly_point_coords_inside_unit_circle() -> None:
    """Every projected point must be inside the primitive circle (r ≤ 1).
    Equal-area projection of the lower hemisphere lives in r ∈ [0, 1]."""
    points = [
        _point(t, p, kind="bedding")
        for t in (0, 90, 180, 270)
        for p in (0, 30, 60, 90)
    ]
    fig = render_stereonet_plotly_figure(points)
    # Skip the primitive trace (line); inspect the data traces
    for trace in fig["data"]:
        if trace.get("mode") != "markers":
            continue
        for x, y in zip(trace["x"], trace["y"]):
            r = math.sqrt(x * x + y * y)
            assert r <= 1.0 + 1e-9, f"Point ({x}, {y}) outside unit circle"


def test_plotly_hover_includes_strike_dip_when_available() -> None:
    points = [_point(45, 30, kind="bedding", strike=315, dip=60)]
    fig = render_stereonet_plotly_figure(points)
    bedding_trace = next(
        t for t in fig["data"] if "bedding" in (t.get("name") or "")
    )
    hover = bedding_trace["hovertext"][0]
    assert "Strike: 315" in hover
    assert "Dip: 60" in hover


# ──────────────────── matplotlib renderer ──────────────────────


def test_matplotlib_empty_input_returns_png() -> None:
    png = render_stereonet_matplotlib_png([])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 500


def test_matplotlib_renders_with_multiple_kinds() -> None:
    points = [
        _point(10, 20, kind="bedding"),
        _point(90, 40, kind="foliation"),
        _point(180, 60, kind="fault"),
    ]
    png = render_stereonet_matplotlib_png(points)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Multi-kind stereonet should be > empty baseline
    empty = render_stereonet_matplotlib_png([])
    assert len(png) > len(empty)
