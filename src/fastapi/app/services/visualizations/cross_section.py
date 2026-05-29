"""Cross-section renderer — §5 second visualisation.

Consumes pre-projected panels from `gold.cross_section_panels` (one
panel per drillhole interval projected onto a named section line).
Renders either:
  - Plotly figure dict (interactive)
  - matplotlib PNG (static, for Report Builder PDF)

The renderer is pure-function — same shape as `strip_log.py`. The
caller fetches `CrossSectionPanel` instances from PG and feeds them
in. Empty input → clean "no panels for this section line" figure.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any, Sequence


logger = logging.getLogger(__name__)


@dataclass
class CrossSectionPanel:
    """One pre-projected interval panel on a section line."""

    panel_id:             str
    section_line_id:      str
    interval_id:          str
    collar_id:            str
    hole_id:              str
    distance_along_m:     float
    top_elevation_m:      float
    bottom_elevation_m:   float
    panel_width_m:        float = 5.0
    lithology_code:       str | None = None
    display_label:        str | None = None
    display_color:        str | None = None
    is_mineralised:       bool = False
    perpendicular_offset_m: float = 0.0

    @property
    def height_m(self) -> float:
        return float(self.top_elevation_m - self.bottom_elevation_m)


_MINERALISED_BORDER = "#1f7a1f"
_NO_DATA_COLOR = "#dddddd"
_PLOT_BG = "#fafafa"


def render_cross_section_plotly_figure(
    panels: Sequence[CrossSectionPanel],
    *,
    title: str | None = None,
    width: int = 1200,
    height: int = 600,
) -> dict[str, Any]:
    """Build a Plotly figure dict for a cross-section.

    X-axis: distance along the section line (m).
    Y-axis: elevation (m). Vertical (not reversed — elevation grows up).
    Each panel = one rectangle filled with its lithology colour.
    Mineralised intervals get the dark-green stroke.
    """
    if not panels:
        return {
            "data": [],
            "layout": {
                "title": {"text": title or "Cross section (no data)"},
                "annotations": [{
                    "text": "No interval panels for this section line.",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5,
                    "showarrow": False,
                    "font": {"size": 14, "color": "#888"},
                }],
                "xaxis": {"visible": False},
                "yaxis": {"visible": False},
                "width": width,
                "height": height,
                "paper_bgcolor": "#fff",
                "plot_bgcolor": _PLOT_BG,
            },
        }

    sorted_panels = sorted(panels, key=lambda p: p.distance_along_m)

    # Compute layout bounds
    min_d = min(p.distance_along_m - p.panel_width_m / 2 for p in sorted_panels)
    max_d = max(p.distance_along_m + p.panel_width_m / 2 for p in sorted_panels)
    min_elev = min(p.bottom_elevation_m for p in sorted_panels)
    max_elev = max(p.top_elevation_m for p in sorted_panels)

    shapes: list[dict[str, Any]] = []
    hover_traces: list[dict[str, Any]] = []
    collar_label_positions: dict[str, float] = {}

    for p in sorted_panels:
        color = p.display_color or _NO_DATA_COLOR
        half_w = p.panel_width_m / 2
        shapes.append({
            "type":    "rect",
            "xref":    "x",
            "yref":    "y",
            "x0":      p.distance_along_m - half_w,
            "x1":      p.distance_along_m + half_w,
            "y0":      p.bottom_elevation_m,
            "y1":      p.top_elevation_m,
            "fillcolor": color,
            "line":    {
                "color": _MINERALISED_BORDER if p.is_mineralised else "#666666",
                "width": 1.5 if p.is_mineralised else 0.3,
            },
            "layer":   "below",
        })
        hover_traces.append({
            "type":      "scatter",
            "x":         [p.distance_along_m],
            "y":         [(p.top_elevation_m + p.bottom_elevation_m) / 2],
            "mode":      "markers",
            "marker":    {"opacity": 0, "size": 1},
            "hoverinfo": "text",
            "hovertext": (
                f"<b>{p.hole_id}</b><br>"
                f"{p.display_label or p.lithology_code or '—'}"
                f"{' ⚑ MINERALISED' if p.is_mineralised else ''}<br>"
                f"Top: {p.top_elevation_m:.1f} m<br>"
                f"Bottom: {p.bottom_elevation_m:.1f} m<br>"
                f"Thickness: {p.height_m:.2f} m<br>"
                f"Along section: {p.distance_along_m:.1f} m<br>"
                f"Offset: {p.perpendicular_offset_m:.1f} m"
            ),
            "showlegend": False,
        })

        # Collar label at the topmost panel for each hole
        if p.hole_id not in collar_label_positions:
            collar_label_positions[p.hole_id] = p.distance_along_m
        else:
            collar_label_positions[p.hole_id] = max(
                collar_label_positions[p.hole_id],
                p.distance_along_m,
            )

    return {
        "data": hover_traces,
        "layout": {
            "title": {
                "text": title or "Cross section",
                "font": {"size": 14},
            },
            "shapes":  shapes,
            "xaxis": {
                "title":     "Distance along section (m)",
                "range":     [min_d - 10, max_d + 10],
                "showgrid":  True,
                "gridcolor": "#e8e8e8",
            },
            "yaxis": {
                "title":     "Elevation (m)",
                "range":     [min_elev - 5, max_elev + 5],
                "showgrid":  True,
                "gridcolor": "#e8e8e8",
                "scaleanchor": "x",  # 1:1 aspect ratio (geologically honest)
                "scaleratio": 1.0,
            },
            "width":          width,
            "height":         height,
            "margin":         {"l": 80, "r": 30, "t": 50, "b": 60},
            "paper_bgcolor":  "#fff",
            "plot_bgcolor":   _PLOT_BG,
            "hovermode":      "closest",
            "annotations": [
                {
                    "x":  d,
                    "y":  max_elev + 3,
                    "text": h,
                    "showarrow": False,
                    "font": {"size": 9, "color": "#333"},
                    "xref": "x", "yref": "y",
                }
                for h, d in collar_label_positions.items()
            ],
        },
    }


def render_cross_section_matplotlib_png(
    panels: Sequence[CrossSectionPanel],
    *,
    title: str | None = None,
    width_in: float = 12.0,
    height_in: float = 6.0,
    dpi: int = 150,
) -> bytes:
    """Render a cross-section to a static PNG."""
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.patches as mpatches  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415

    sorted_panels = sorted(panels, key=lambda p: p.distance_along_m)
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)

    if not sorted_panels:
        ax.text(
            0.5, 0.5,
            "No interval panels for this section line.",
            ha="center", va="center",
            fontsize=12, color="#888",
            transform=ax.transAxes,
        )
        ax.set_axis_off()
        if title:
            ax.set_title(title, fontsize=12)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return buf.getvalue()

    for p in sorted_panels:
        color = p.display_color or _NO_DATA_COLOR
        edge = _MINERALISED_BORDER if p.is_mineralised else "#666666"
        lw = 1.2 if p.is_mineralised else 0.3
        rect = mpatches.Rectangle(
            (p.distance_along_m - p.panel_width_m / 2, p.bottom_elevation_m),
            p.panel_width_m, p.height_m,
            facecolor=color,
            edgecolor=edge,
            linewidth=lw,
        )
        ax.add_patch(rect)

    min_d = min(p.distance_along_m - p.panel_width_m / 2 for p in sorted_panels)
    max_d = max(p.distance_along_m + p.panel_width_m / 2 for p in sorted_panels)
    min_elev = min(p.bottom_elevation_m for p in sorted_panels)
    max_elev = max(p.top_elevation_m for p in sorted_panels)
    ax.set_xlim(min_d - 10, max_d + 10)
    ax.set_ylim(min_elev - 5, max_elev + 5)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("Distance along section (m)", fontsize=10)
    ax.set_ylabel("Elevation (m)", fontsize=10)
    ax.set_title(title or "Cross section", fontsize=11)
    ax.set_facecolor(_PLOT_BG)
    ax.grid(True, color="#e8e8e8", linewidth=0.5)

    # Collar labels at the top of each hole
    seen: set[str] = set()
    for p in sorted_panels:
        if p.hole_id in seen:
            continue
        seen.add(p.hole_id)
        ax.text(
            p.distance_along_m, max_elev + 3, p.hole_id,
            ha="center", va="bottom", fontsize=8, color="#333",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
