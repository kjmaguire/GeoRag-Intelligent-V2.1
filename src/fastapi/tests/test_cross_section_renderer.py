"""§5 cross-section renderer tests (Phase H4)."""
from __future__ import annotations

from app.services.visualizations import (
    CrossSectionPanel,
    render_cross_section_matplotlib_png,
    render_cross_section_plotly_figure,
)


def _panel(
    distance: float, top: float, bottom: float,
    hole: str = "PLS-01", code: str = "SST",
    color: str = "#f4d35e", mineralised: bool = False,
) -> CrossSectionPanel:
    return CrossSectionPanel(
        panel_id=f"p-{int(distance*100)}-{int(top*100)}",
        section_line_id="line-1",
        interval_id="iv-1",
        collar_id=f"c-{hole}",
        hole_id=hole,
        distance_along_m=distance,
        top_elevation_m=top,
        bottom_elevation_m=bottom,
        lithology_code=code,
        display_label=code,
        display_color=color,
        is_mineralised=mineralised,
    )


def _two_hole_section() -> list[CrossSectionPanel]:
    return [
        _panel(0,   100, 80, hole="PLS-01", code="OVB", color="#a0522d"),
        _panel(0,   80,  20, hole="PLS-01", code="SST", color="#f4d35e",
               mineralised=True),
        _panel(0,   20, -50, hole="PLS-01", code="PGN", color="#bc4749"),
        _panel(150, 95,  75, hole="PLS-02", code="OVB", color="#a0522d"),
        _panel(150, 75, -30, hole="PLS-02", code="PGN", color="#bc4749"),
    ]


def test_panel_height_property() -> None:
    p = _panel(0, 100, 50)
    assert p.height_m == 50.0


def test_plotly_empty_input_produces_no_data_figure() -> None:
    fig = render_cross_section_plotly_figure([])
    assert fig["data"] == []
    assert any("No interval panels" in a["text"]
               for a in fig["layout"]["annotations"])


def test_plotly_two_hole_section_produces_five_shapes() -> None:
    fig = render_cross_section_plotly_figure(_two_hole_section())
    assert len(fig["layout"]["shapes"]) == 5
    # All rectangles
    assert all(s["type"] == "rect" for s in fig["layout"]["shapes"])


def test_plotly_mineralised_panel_gets_thicker_green_border() -> None:
    fig = render_cross_section_plotly_figure(_two_hole_section())
    mineralised = [s for s in fig["layout"]["shapes"]
                   if s["line"]["width"] > 1]
    assert len(mineralised) == 1
    assert mineralised[0]["line"]["color"] == "#1f7a1f"


def test_plotly_elevation_y_axis_grows_up() -> None:
    """Elevation axis should NOT be reversed (geological convention)."""
    fig = render_cross_section_plotly_figure(_two_hole_section())
    yrange = fig["layout"]["yaxis"]["range"]
    assert yrange[0] < yrange[1]  # min elevation < max elevation


def test_plotly_aspect_ratio_is_1_to_1() -> None:
    """Cross-sections should be geologically honest: 1:1 horizontal:vertical."""
    fig = render_cross_section_plotly_figure(_two_hole_section())
    assert fig["layout"]["yaxis"]["scaleanchor"] == "x"
    assert fig["layout"]["yaxis"]["scaleratio"] == 1.0


def test_plotly_collar_labels_appear_at_top() -> None:
    fig = render_cross_section_plotly_figure(_two_hole_section())
    annotation_texts = [a["text"] for a in fig["layout"]["annotations"]]
    assert "PLS-01" in annotation_texts
    assert "PLS-02" in annotation_texts


def test_matplotlib_empty_input_returns_png() -> None:
    png = render_cross_section_matplotlib_png([], title="Empty test")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 500


def test_matplotlib_two_hole_section_renders() -> None:
    png = render_cross_section_matplotlib_png(_two_hole_section())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Two-hole section produces substantial content
    assert len(png) > 4000


def test_matplotlib_aspect_ratio_preserved() -> None:
    """The matplotlib output uses ax.set_aspect('equal'). Verify the
    PNG renders without error on a non-square data range (which is
    where aspect-equal misbehaves if axis ranges are wrong)."""
    panels = [
        _panel(0, 100, 80),
        _panel(500, 100, 80),  # wide horizontal range
    ]
    png = render_cross_section_matplotlib_png(panels)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
