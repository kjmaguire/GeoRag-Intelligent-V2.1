"""§5 strip-log renderer tests (Phase H3, doc-phase 185).

Pure-function tests on the Plotly + matplotlib renderers. No DB or
network — the fixture is a small list of ``StripLogInterval``
dataclasses that mirror the ``gold.drillhole_intervals_visual`` shape.
"""
from __future__ import annotations

import pytest

from app.services.visualizations import (
    StripLogInterval,
    render_strip_log_matplotlib_png,
    render_strip_log_plotly_figure,
)


# ───────────────────────────── fixtures ───────────────────────────────


def _interval(
    from_d: float,
    to_d: float,
    code: str = "SST",
    *,
    is_mineralised: bool = False,
    assay_value_max: float | None = None,
    assay_element_max: str | None = None,
    display_color: str | None = None,
) -> StripLogInterval:
    return StripLogInterval(
        interval_id="iv-" + str(int(from_d * 1000)),
        collar_id="collar-1",
        hole_id="36-1042",
        from_depth_m=from_d,
        to_depth_m=to_d,
        lithology_code=code,
        lithology_label=f"{code} label",
        display_color=display_color,
        assay_element_max=assay_element_max,
        assay_value_max=assay_value_max,
        assay_unit_max="ppm" if assay_value_max is not None else None,
        is_mineralised=is_mineralised,
    )


def _three_interval_collar() -> list[StripLogInterval]:
    """Three intervals: overburden → sandstone (mineralised) → pelitic gneiss."""
    return [
        _interval(0,   12,  code="OVB"),
        _interval(12,  175, code="SST",
                  is_mineralised=True,
                  assay_value_max=1500.0,
                  assay_element_max="U3O8_ppm"),
        _interval(175, 339, code="PGN"),
    ]


# ───────────────────────────── property tests ─────────────────────────


def test_interval_length_property() -> None:
    iv = _interval(10.5, 25.0)
    assert iv.interval_length_m == pytest.approx(14.5)


def test_resolved_label_prefers_display_label() -> None:
    iv = StripLogInterval(
        interval_id="x", collar_id="c", hole_id="h",
        from_depth_m=0, to_depth_m=10,
        lithology_code="SST", lithology_label="Sandstone",
        display_label="Sandstone (Pleistocene)",
    )
    assert iv.resolved_label == "Sandstone (Pleistocene)"


def test_resolved_label_falls_back_to_code() -> None:
    iv = StripLogInterval(
        interval_id="x", collar_id="c", hole_id="h",
        from_depth_m=0, to_depth_m=10,
        lithology_code="VEI",
    )
    assert iv.resolved_label == "VEI"


def test_resolved_label_em_dash_when_no_metadata() -> None:
    iv = StripLogInterval(
        interval_id="x", collar_id="c", hole_id="h",
        from_depth_m=0, to_depth_m=10,
    )
    assert iv.resolved_label == "—"


# ───────────────────────────── plotly renderer ────────────────────────


def test_plotly_empty_input_returns_no_data_figure() -> None:
    fig = render_strip_log_plotly_figure([], title="Empty test")
    assert fig["data"] == []
    annotations = fig["layout"]["annotations"]
    assert any("No lithology intervals" in a["text"] for a in annotations)
    assert fig["layout"]["title"]["text"] == "Empty test"


def test_plotly_three_intervals_produces_three_shapes() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    assert len(fig["layout"]["shapes"]) == 3
    # Each is a Plotly rect
    assert all(s["type"] == "rect" for s in fig["layout"]["shapes"])
    # Hover trace count matches
    assert len(fig["data"]) == 3


def test_plotly_y_axis_reversed_to_depth_grows_down() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    yrange = fig["layout"]["yaxis"]["range"]
    # First entry > second entry → reversed (depth=0 at top)
    assert yrange[0] > yrange[1]
    assert yrange[1] == 0


def test_plotly_mineralised_interval_gets_thicker_green_border() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    mineralised_shapes = [
        s for s in fig["layout"]["shapes"]
        if s["line"]["width"] > 1
    ]
    assert len(mineralised_shapes) == 1
    assert mineralised_shapes[0]["line"]["color"] == "#1f7a1f"


def test_plotly_default_palette_applied_for_sst_pgn() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    colors = [s["fillcolor"] for s in fig["layout"]["shapes"]]
    # OVB → earth brown; SST → sand-yellow; PGN → basement red
    assert colors == ["#a0522d", "#f4d35e", "#bc4749"]


def test_plotly_explicit_display_color_overrides_palette() -> None:
    iv = _interval(0, 50, code="SST", display_color="#deadbeef")
    fig = render_strip_log_plotly_figure([iv])
    assert fig["layout"]["shapes"][0]["fillcolor"] == "#deadbeef"


def test_plotly_unknown_code_falls_back_to_neutral_grey() -> None:
    iv = _interval(0, 50, code="WTF_unknown")
    fig = render_strip_log_plotly_figure([iv])
    assert fig["layout"]["shapes"][0]["fillcolor"] == "#dddddd"


def test_plotly_tick_labels_use_resolved_label() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    labels = fig["layout"]["yaxis"]["ticktext"]
    assert "OVB label" in labels  # lithology_label fallback
    assert "SST label" in labels
    assert "PGN label" in labels


def test_plotly_intervals_sorted_by_depth_even_if_input_isnt() -> None:
    intervals = [
        _interval(175, 339, code="PGN"),  # bottom
        _interval(0,   12,  code="OVB"),  # top
        _interval(12,  175, code="SST"),  # middle
    ]
    fig = render_strip_log_plotly_figure(intervals)
    shapes_top_to_bottom = sorted(
        fig["layout"]["shapes"], key=lambda s: s["y0"],
    )
    # Tick labels are produced in iteration order — the renderer sorts
    # before iterating, so the ticktext list should be top-down.
    labels = fig["layout"]["yaxis"]["ticktext"]
    assert labels[0] == "OVB label"
    assert labels[-1] == "PGN label"


def test_plotly_hover_text_includes_assay_when_present() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    hover_texts = [t["hovertext"] for t in fig["data"]]
    sst_hover = next(t for t in hover_texts if "SST" in t)
    assert "Max assay: 1.5e+03 ppm" in sst_hover or "1500" in sst_hover
    assert "U3O8_ppm" in sst_hover
    assert "MINERALISED" in sst_hover


def test_plotly_hover_text_omits_assay_when_absent() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    hover_texts = [t["hovertext"] for t in fig["data"]]
    ovb_hover = next(t for t in hover_texts if "OVB" in t)
    assert "Max assay" not in ovb_hover


def test_plotly_layout_sizing_defaults_match_doc() -> None:
    fig = render_strip_log_plotly_figure(_three_interval_collar())
    assert fig["layout"]["width"] == 320
    assert fig["layout"]["height"] == 800


# ───────────────────────────── matplotlib renderer ─────────────────────


def test_matplotlib_empty_input_returns_no_data_png() -> None:
    png = render_strip_log_matplotlib_png([], title="Empty test")
    assert isinstance(png, bytes)
    # PNG magic header — verifies we got a real PNG out
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Even an empty strip log produces > 1 KB (just the title + frame text)
    assert len(png) > 500


def test_matplotlib_three_intervals_renders_png() -> None:
    png = render_strip_log_matplotlib_png(_three_interval_collar())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # A real three-interval strip log should be substantially larger
    # than the empty case (more pixels filled with colour).
    assert len(png) > 3000


def test_matplotlib_renders_mineralised_collar_without_error() -> None:
    """The mineralised-stroke path uses a different colour + linewidth.
    Smoke that the code path doesn't blow up on real-world inputs."""
    intervals = _three_interval_collar()
    png = render_strip_log_matplotlib_png(
        intervals,
        title="36-1042 — Cameco Shirley Basin",
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Sanity: longer than the empty case
    assert len(png) > len(render_strip_log_matplotlib_png([]))


def test_matplotlib_handles_single_interval_collar() -> None:
    intervals = [_interval(0, 100, code="OVB")]
    png = render_strip_log_matplotlib_png(intervals)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_matplotlib_dpi_affects_output_size() -> None:
    intervals = _three_interval_collar()
    small = render_strip_log_matplotlib_png(intervals, dpi=72)
    big = render_strip_log_matplotlib_png(intervals, dpi=300)
    # Higher DPI → more bytes
    assert len(big) > len(small)
