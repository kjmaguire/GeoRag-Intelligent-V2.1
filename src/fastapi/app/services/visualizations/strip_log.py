"""Strip-log renderer — §5 first visualisation.

Produces a per-collar lithology + assay column suitable for either:
  - **Interactive web view**: Plotly figure dict (returned as JSON for
    `react-plotly.js` to consume) — supports hover, zoom, pan.
  - **Static PDF / PNG**: matplotlib raster output — used by the Report
    Builder's PDF renderer and by the cockpit export-bundle flow.

Both renderers consume the same ``StripLogInterval`` dataclass list
(matches the ``gold.drillhole_intervals_visual`` row shape one-to-one)
so the caller writes a single PostgreSQL fetch and feeds it to either
output target.

doc-phase 185 — Phase H3 strip-log starter. Visualisations are pure
functions; no DB / network calls — the FastAPI endpoint in
``app/routers/visualizations.py`` does the DB fetch and feeds rows in.

Empty-input contract
--------------------
* Zero intervals → returns an empty figure with a clear "no data
  available" annotation (Plotly) or a single text-only PNG (matplotlib).
  No exception raised; this is the legitimate "collar exists but no
  lithology yet" path.

Coloring policy
---------------
1. If ``display_color`` is set, use it (SME-curated palette via
   gold.drillhole_intervals_visual.display_color).
2. Otherwise fall back to ``_DEFAULT_LITHOLOGY_PALETTE`` keyed by
   lithology_code prefix.
3. Otherwise neutral grey.

The PDF / Report Builder path uses the matplotlib renderer for
font-embedding reasons (Plotly's PDF export pulls Kaleido which we
deliberately don't ship in the FastAPI image to keep the layer small).
"""
from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Input model — one row of `gold.drillhole_intervals_visual`
# ----------------------------------------------------------------------------


@dataclass
class StripLogInterval:
    """One renderable interval of a drillhole strip log.

    Field set mirrors ``gold.drillhole_intervals_visual`` so the caller
    can build one of these per fetchrow result without coercion.
    """

    interval_id:        str
    collar_id:          str
    hole_id:            str
    from_depth_m:       float
    to_depth_m:         float
    lithology_code:     str | None = None
    lithology_label:    str | None = None
    display_label:      str | None = None
    display_color:      str | None = None
    assay_element_max:  str | None = None
    assay_value_max:    float | None = None
    assay_unit_max:     str | None = None
    is_mineralised:     bool = False

    @property
    def interval_length_m(self) -> float:
        return float(self.to_depth_m) - float(self.from_depth_m)

    @property
    def resolved_label(self) -> str:
        """Best-effort display label for the column."""
        return (
            self.display_label
            or self.lithology_label
            or self.lithology_code
            or "—"
        )


# ----------------------------------------------------------------------------
# Color palette (fallback when display_color is NULL)
# ----------------------------------------------------------------------------


# Hex colors keyed by lithology canonical code prefix. SME-curated for the
# uranium / unconformity-related sedimentary stack the V1 corpora target;
# operators can override per-project via the silver.lithology_codes table
# once that schema lands.
_DEFAULT_LITHOLOGY_PALETTE: dict[str, str] = {
    "SST":  "#f4d35e",  # sandstone — sand-yellow
    "CGL":  "#c89f60",  # conglomerate — coarse tan
    "PGN":  "#bc4749",  # pelitic gneiss — basement red
    "GPT":  "#8b8b8b",  # graphite — dark grey
    "MUD":  "#6b705c",  # mudstone — dark olive
    "SLT":  "#a4ac86",  # siltstone — pale olive
    "SHL":  "#5e6068",  # shale — slate grey
    "LMS":  "#cad2c5",  # limestone — pale grey
    "DOL":  "#dadec7",  # dolomite — bone
    "VEI":  "#e8c2ca",  # vein — pink
    "FLT":  "#000000",  # fault — black
    "OVB":  "#a0522d",  # overburden — earth brown
}


_MINERALISED_BORDER_COLOR = "#1f7a1f"  # dark green stroke around mineralised intervals
_NO_DATA_COLOR = "#dddddd"
_PLOT_BG = "#fafafa"


def _color_for(interval: StripLogInterval) -> str:
    """Resolve the fill colour for one interval."""
    if interval.display_color:
        return interval.display_color
    code = (interval.lithology_code or "").upper()
    for prefix, color in _DEFAULT_LITHOLOGY_PALETTE.items():
        if code.startswith(prefix):
            return color
    return _NO_DATA_COLOR


# ----------------------------------------------------------------------------
# Plotly renderer
# ----------------------------------------------------------------------------


def render_strip_log_plotly_figure(
    intervals: Sequence[StripLogInterval],
    *,
    title: str | None = None,
    width: int = 320,
    height: int = 800,
) -> dict[str, Any]:
    """Build a Plotly figure dict for one drillhole's strip log.

    Returns a JSON-serialisable dict (``go.Figure.to_dict()``-shape)
    rather than a Figure object so the FastAPI handler can ``return``
    it directly without invoking the Kaleido image-export path.
    The frontend's ``react-plotly.js`` accepts this dict verbatim.

    The strip-log layout is a vertical column:
      * x-axis: a single category (the hole_id)
      * y-axis: depth, reversed (0 at top, max depth at bottom)
      * each interval = one rectangle filled with the lithology colour
      * mineralised intervals get a dark-green stroke
      * the y-axis tick at the from_depth shows the lithology label
    """
    # Sort by from_depth so the rectangles stack top-down.
    sorted_intervals = sorted(intervals, key=lambda i: i.from_depth_m)

    if not sorted_intervals:
        return {
            "data": [],
            "layout": {
                "title": {"text": title or "Strip log (no data)"},
                "annotations": [{
                    "text": "No lithology intervals available for this collar.",
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

    max_depth = max(i.to_depth_m for i in sorted_intervals)
    hole_id = sorted_intervals[0].hole_id

    # Each interval is a rect (Plotly shape) with a hover annotation.
    shapes: list[dict[str, Any]] = []
    hover_traces: list[dict[str, Any]] = []
    tick_vals: list[float] = []
    tick_labels: list[str] = []

    for interval in sorted_intervals:
        color = _color_for(interval)
        shape: dict[str, Any] = {
            "type":    "rect",
            "xref":    "x",
            "yref":    "y",
            "x0":      0,
            "x1":      1,
            "y0":      interval.from_depth_m,
            "y1":      interval.to_depth_m,
            "fillcolor": color,
            "line":    {
                "color": (
                    _MINERALISED_BORDER_COLOR
                    if interval.is_mineralised
                    else "#666666"
                ),
                "width": 2 if interval.is_mineralised else 0.5,
            },
            "layer":   "below",
        }
        shapes.append(shape)
        tick_vals.append((interval.from_depth_m + interval.to_depth_m) / 2)
        tick_labels.append(interval.resolved_label)

        # Hover trace — a single scatter point at the interval midpoint
        # gives Plotly something to attach the hover tooltip to.
        assay_str = ""
        if interval.assay_value_max is not None and interval.assay_element_max:
            assay_str = (
                f"<br>Max assay: {interval.assay_value_max:.3g} "
                f"{interval.assay_unit_max or ''} "
                f"({interval.assay_element_max})"
            )
        mineralised_str = " ⚑ MINERALISED" if interval.is_mineralised else ""

        hover_traces.append({
            "type":      "scatter",
            "x":         [0.5],
            "y":         [(interval.from_depth_m + interval.to_depth_m) / 2],
            "mode":      "markers",
            "marker":    {"opacity": 0, "size": 1},
            "hoverinfo": "text",
            "hovertext": (
                f"<b>{interval.resolved_label}</b>{mineralised_str}"
                f"<br>{interval.from_depth_m:.2f} – "
                f"{interval.to_depth_m:.2f} m"
                f"<br>Length: {interval.interval_length_m:.2f} m"
                f"{assay_str}"
            ),
            "showlegend": False,
        })

    return {
        "data": hover_traces,
        "layout": {
            "title": {
                "text": title or f"Strip log — {hole_id}",
                "font": {"size": 14},
            },
            "shapes":  shapes,
            "xaxis": {
                "range":          [0, 1],
                "showgrid":       False,
                "zeroline":       False,
                "showticklabels": False,
                "fixedrange":     True,
            },
            "yaxis": {
                "title":     "Depth (m)",
                "range":     [max_depth, 0],   # reversed; 0 at top
                "tickmode":  "array",
                "tickvals":  tick_vals,
                "ticktext":  tick_labels,
                "tickfont":  {"size": 10},
                "showgrid":  True,
                "gridcolor": "#e8e8e8",
                "fixedrange": True,
            },
            "width":          width,
            "height":         height,
            "margin":         {"l": 80, "r": 20, "t": 50, "b": 20},
            "paper_bgcolor":  "#fff",
            "plot_bgcolor":   _PLOT_BG,
            "hovermode":      "closest",
        },
    }


# ----------------------------------------------------------------------------
# Matplotlib renderer (static PNG)
# ----------------------------------------------------------------------------


def render_strip_log_matplotlib_png(
    intervals: Sequence[StripLogInterval],
    *,
    title: str | None = None,
    width_in: float = 3.0,
    height_in: float = 9.0,
    dpi: int = 150,
) -> bytes:
    """Render one drillhole's strip log to a static PNG (bytes).

    Output is a vertical column matching the Plotly layout. Used by:
      * The Report Builder PDF renderer (embed via base64 data URI)
      * The cockpit export-bundle flow
      * Operator data-room ZIPs

    Empty-input path returns a small "no data" PNG so consumers don't
    need to special-case the empty case.
    """
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")  # headless backend; no Tkinter requirement
    import matplotlib.patches as mpatches  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415

    sorted_intervals = sorted(intervals, key=lambda i: i.from_depth_m)
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)

    if not sorted_intervals:
        ax.text(
            0.5, 0.5,
            "No lithology intervals available\nfor this collar.",
            ha="center", va="center",
            fontsize=11, color="#888",
            transform=ax.transAxes,
        )
        ax.set_axis_off()
        if title:
            ax.set_title(title, fontsize=12)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return buf.getvalue()

    max_depth = max(i.to_depth_m for i in sorted_intervals)
    hole_id = sorted_intervals[0].hole_id

    for interval in sorted_intervals:
        color = _color_for(interval)
        edge = (
            _MINERALISED_BORDER_COLOR
            if interval.is_mineralised else "#666666"
        )
        lw = 1.8 if interval.is_mineralised else 0.4
        rect = mpatches.Rectangle(
            (0, interval.from_depth_m),
            1, interval.interval_length_m,
            facecolor=color,
            edgecolor=edge,
            linewidth=lw,
        )
        ax.add_patch(rect)
        # Label at the interval midpoint
        midpoint = (interval.from_depth_m + interval.to_depth_m) / 2
        ax.text(
            1.05, midpoint,
            interval.resolved_label,
            ha="left", va="center",
            fontsize=8, color="#333",
        )

    ax.set_xlim(0, 1)
    ax.set_ylim(max_depth, 0)  # reversed
    ax.set_xticks([])
    ax.set_ylabel("Depth (m)", fontsize=10)
    ax.set_title(title or f"Strip log — {hole_id}", fontsize=11)
    ax.set_facecolor(_PLOT_BG)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.5)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
