"""§5 cross-section renderer tests (Phase H4)."""
from __future__ import annotations

import pytest

from app.services.visualizations import (
    CrossSectionPanel,
    panels_from_collars_projected,
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


# ---------------------------------------------------------------------------
# collars_projected JSONB expansion (canonical migration shape)
# ---------------------------------------------------------------------------


def _collars_projected() -> list[dict]:
    """Two collars in the shape the Dagster gold_cross_section_panels asset
    writes into gold.cross_section_panels.collars_projected."""
    return [
        {
            "collar_id": "c-1",
            "hole_id": "PLS-01",
            "axis_distance_m": 0.0,
            "perpendicular_offset_m": 12.5,
            "collar_elevation_m": 100.0,
            "total_depth_m": 120.0,
            "trace": [],
            "intervals": [
                {"from": 0.0, "to": 20.0, "lithology_code": "OVB",
                 "lithology_label": "Overburden", "color_hint": "#a0522d",
                 "assays": {}},
                {"from": 20.0, "to": 80.0, "lithology_code": "SST",
                 "lithology_label": "Sandstone", "color_hint": "#f4d35e",
                 "assays": {"is_mineralised": True}},
            ],
        },
        {
            "collar_id": "c-2",
            "hole_id": "PLS-02",
            "axis_distance_m": 150.0,
            "perpendicular_offset_m": 3.0,
            "collar_elevation_m": 95.0,
            "total_depth_m": 60.0,
            "trace": [],
            "intervals": [],  # no logged lithology → grey no-data column
        },
    ]


def test_expand_produces_panel_per_interval_plus_nodata_column() -> None:
    panels = panels_from_collars_projected(_collars_projected())
    # 2 intervals for PLS-01 + 1 no-data column for PLS-02
    assert len(panels) == 3
    holes = {p.hole_id for p in panels}
    assert holes == {"PLS-01", "PLS-02"}


def test_expand_converts_depth_to_elevation_via_collar_rl() -> None:
    panels = panels_from_collars_projected(_collars_projected())
    ovb = next(p for p in panels if p.hole_id == "PLS-01" and p.lithology_code == "OVB")
    # collar RL 100, interval 0→20 m depth → elevation 100 down to 80
    assert ovb.top_elevation_m == 100.0
    assert ovb.bottom_elevation_m == 80.0
    assert ovb.distance_along_m == 0.0
    assert ovb.perpendicular_offset_m == 12.5
    assert ovb.display_color == "#a0522d"


def test_expand_reads_mineralisation_from_assays_payload() -> None:
    panels = panels_from_collars_projected(_collars_projected())
    sst = next(p for p in panels if p.hole_id == "PLS-01" and p.lithology_code == "SST")
    assert sst.is_mineralised is True


def test_expand_nodata_column_spans_full_hole() -> None:
    panels = panels_from_collars_projected(_collars_projected())
    nd = next(p for p in panels if p.hole_id == "PLS-02")
    assert nd.top_elevation_m == 95.0
    assert nd.bottom_elevation_m == 35.0  # 95 - total_depth 60
    assert nd.display_color == "#dddddd"
    assert nd.is_mineralised is False


def test_expand_empty_or_none_returns_empty_list() -> None:
    assert panels_from_collars_projected(None) == []
    assert panels_from_collars_projected([]) == []


def test_expand_is_renderable_end_to_end() -> None:
    """Expanded panels feed the existing renderer unchanged."""
    panels = panels_from_collars_projected(_collars_projected())
    fig = render_cross_section_plotly_figure(panels)
    assert len(fig["layout"]["shapes"]) == 3
    png = render_cross_section_matplotlib_png(panels)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_expand_tolerates_inverted_depths_and_bad_rows() -> None:
    collars = [
        {"hole_id": "H", "collar_id": "c", "axis_distance_m": 0.0,
         "collar_elevation_m": 50.0, "intervals": [
             {"from": 30.0, "to": 10.0, "lithology_code": "X",
              "color_hint": "#111"},  # inverted depths
             {"lithology_code": "Y"},  # missing from/to → skipped
             "not-a-dict",             # junk → skipped
         ]},
        "not-a-dict-collar",           # junk → skipped
    ]
    panels = panels_from_collars_projected(collars)
    assert len(panels) == 1
    # top elevation must be the higher value regardless of depth ordering
    assert panels[0].top_elevation_m >= panels[0].bottom_elevation_m
