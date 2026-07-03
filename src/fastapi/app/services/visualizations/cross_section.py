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
    """One rendered interval rectangle on a cross-section.

    The renderers read only the geometry (``distance_along_m``,
    ``top_elevation_m``, ``bottom_elevation_m``, ``panel_width_m``), the
    label/colour, ``is_mineralised`` and ``perpendicular_offset_m`` (hover).

    ``panel_id`` / ``section_line_id`` / ``interval_id`` belonged to the
    never-applied per-interval table shape (archived 2026-07-02 to
    ``database/raw/_archive/phase5-20-cross-section-panels.sql``). The
    canonical migration shape stores one gold row per (project_id,
    section_name) with a ``collars_projected`` JSONB array, so those ids
    don't exist per interval — they're optional here purely for back-compat
    with older callers/tests that still pass them.
    """

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
    collar_id:            str | None = None
    panel_id:             str | None = None
    section_line_id:      str | None = None
    interval_id:          str | None = None

    @property
    def height_m(self) -> float:
        return float(self.top_elevation_m - self.bottom_elevation_m)


_MINERALISED_BORDER = "#1f7a1f"
_NO_DATA_COLOR = "#dddddd"
_PLOT_BG = "#fafafa"


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    """Best-effort float coercion for JSONB-sourced numbers/strings."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def panels_from_collars_projected(
    collars_projected: Sequence[dict[str, Any]] | None,
    *,
    panel_width_m: float = 5.0,
) -> list[CrossSectionPanel]:
    """Expand a ``gold.cross_section_panels.collars_projected`` JSONB array
    into flat renderable :class:`CrossSectionPanel` rectangles.

    Each element of ``collars_projected`` is one collar the Dagster
    ``gold_cross_section_panels`` asset projected onto the section axis::

        {
          "collar_id": "...", "hole_id": "...",
          "axis_distance_m": <float>, "perpendicular_offset_m": <float>,
          "collar_elevation_m": <float>, "total_depth_m": <float>,
          "trace": [{"axis_m", "perp_m", "elevation_m"}, ...],
          "intervals": [
              {"from", "to", "lithology_code", "lithology_label",
               "color_hint", "assays"}, ...
          ]
        }

    Interval depths are downhole metres; we convert to elevation via the
    collar RL (``elevation = collar_elevation_m - depth``) — a vertical
    projection, the same convention the strip-log renderer uses. Trace-aware
    (deviated-hole) elevation is a future refinement. Holes with a positive
    ``total_depth_m`` but no logged intervals still get a single grey
    "no-data" column so the drill hole is visible on the section.
    """
    panels: list[CrossSectionPanel] = []
    for collar in collars_projected or []:
        if not isinstance(collar, dict):
            continue
        hole_id = str(collar.get("hole_id") or collar.get("collar_id") or "?")
        raw_collar_id = collar.get("collar_id")
        collar_id = str(raw_collar_id) if raw_collar_id is not None else None
        axis = _coerce_float(collar.get("axis_distance_m"), 0.0) or 0.0
        perp = _coerce_float(collar.get("perpendicular_offset_m"), 0.0) or 0.0
        collar_elev = _coerce_float(collar.get("collar_elevation_m"), 0.0) or 0.0

        made_panel_for_hole = False
        for iv in collar.get("intervals") or []:
            if not isinstance(iv, dict):
                continue
            top_depth = _coerce_float(iv.get("from"))
            bot_depth = _coerce_float(iv.get("to"))
            if top_depth is None or bot_depth is None:
                continue
            top_elev = collar_elev - top_depth
            bot_elev = collar_elev - bot_depth
            # top must be the higher elevation; swap if depths were inverted
            if bot_elev > top_elev:
                top_elev, bot_elev = bot_elev, top_elev
            assays = iv.get("assays")
            is_min = bool(
                iv.get("is_mineralised")
                or (isinstance(assays, dict) and assays.get("is_mineralised"))
            )
            panels.append(CrossSectionPanel(
                hole_id=hole_id,
                collar_id=collar_id,
                distance_along_m=axis,
                top_elevation_m=top_elev,
                bottom_elevation_m=bot_elev,
                panel_width_m=panel_width_m,
                lithology_code=iv.get("lithology_code"),
                display_label=iv.get("lithology_label") or iv.get("lithology_code"),
                display_color=iv.get("color_hint") or iv.get("display_color"),
                is_mineralised=is_min,
                perpendicular_offset_m=perp,
            ))
            made_panel_for_hole = True

        if not made_panel_for_hole:
            total_depth = _coerce_float(collar.get("total_depth_m"), 0.0) or 0.0
            if total_depth > 0:
                panels.append(CrossSectionPanel(
                    hole_id=hole_id,
                    collar_id=collar_id,
                    distance_along_m=axis,
                    top_elevation_m=collar_elev,
                    bottom_elevation_m=collar_elev - total_depth,
                    panel_width_m=panel_width_m,
                    lithology_code=None,
                    display_label="no logged lithology",
                    display_color=_NO_DATA_COLOR,
                    is_mineralised=False,
                    perpendicular_offset_m=perp,
                ))
    return panels


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
