"""Stereonet renderer — §5 third visualisation.

Polar stereonet projection of structural measurements (bedding,
foliation, joints, faults, veins). Uses the equal-area Schmidt
projection (lower hemisphere). Same dual-output pattern as strip_log
and cross_section: Plotly dict for interactive, matplotlib PNG for
PDF embed.

The renderer accepts a list of ``StereonetPoint`` dataclasses
matching ``gold.structure_measurements_visual``. The gold asset is
responsible for converting strike/dip → pole_trend/pole_plunge so
the renderer only does the projection arithmetic.

Stereographic projection (equal-area / Schmidt):
    For a pole with trend T° and plunge P°:
        r = sqrt(2) × sin((90 - P) / 2)          (radius from center)
        θ = T (clockwise from north)
        x = r × sin(θ)
        y = r × cos(θ)

Equal-area means densities of points on the sphere map to equal
densities on the plot — the right choice for structural geology
density-plots ("Kamb contours" go on top of these later, §5.10).
"""
from __future__ import annotations

import io
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Per-measurement-kind palette + symbol (mirrors the gold table's
# `display_color` / `display_symbol` defaults).
_DEFAULT_KIND_STYLE: dict[str, tuple[str, str]] = {
    "bedding":    ("#1f77b4", "circle"),    # blue
    "foliation":  ("#2ca02c", "square"),    # green
    "joint":      ("#ff7f0e", "diamond"),   # orange
    "fault":      ("#d62728", "x"),         # red
    "vein":       ("#9467bd", "triangle-up"),  # purple
    "other":      ("#7f7f7f", "circle-open"),  # grey
}


@dataclass
class StereonetPoint:
    """One pole projection on the stereonet."""

    measurement_id:    str
    measurement_kind:  str
    pole_trend_deg:    float           # 0-360, clockwise from north
    pole_plunge_deg:   float           # 0-90, down from horizontal
    strike_deg:        float | None = None  # for hover display
    dip_deg:           float | None = None
    depth_m:           float | None = None
    confidence:        str | None = None
    display_color:     str | None = None
    display_symbol:    str | None = None


def _equal_area_project(trend_deg: float, plunge_deg: float) -> tuple[float, float]:
    """Schmidt equal-area lower-hemisphere stereographic projection.

    Returns (x, y) in normalised [-1, 1] space (radius = 1 is the
    primitive circle / equator).
    """
    # Convert to radians
    t = math.radians(trend_deg)
    p = math.radians(plunge_deg)

    # r = sqrt(2) × sin((90 - plunge)/2)
    r = math.sqrt(2.0) * math.sin((math.pi / 2 - p) / 2)
    x = r * math.sin(t)
    y = r * math.cos(t)
    return x, y


def render_stereonet_plotly_figure(
    points: Sequence[StereonetPoint],
    *,
    title: str | None = None,
    width: int = 600,
    height: int = 600,
) -> dict[str, Any]:
    """Build a Plotly figure dict for an equal-area stereonet."""
    if not points:
        return {
            "data": [],
            "layout": {
                "title": {"text": title or "Stereonet (no data)"},
                "annotations": [{
                    "text": "No structural measurements available.",
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
            },
        }

    # Group points by measurement_kind so each gets its own trace
    by_kind: dict[str, list[StereonetPoint]] = {}
    for p in points:
        by_kind.setdefault(p.measurement_kind, []).append(p)

    traces: list[dict[str, Any]] = []
    for kind, pts in sorted(by_kind.items()):
        default_color, default_symbol = _DEFAULT_KIND_STYLE.get(
            kind, _DEFAULT_KIND_STYLE["other"],
        )
        # All points in this group share style; allow per-point override.
        xs, ys, hovers = [], [], []
        for pt in pts:
            x, y = _equal_area_project(pt.pole_trend_deg, pt.pole_plunge_deg)
            xs.append(x)
            ys.append(y)
            hovers.append(
                f"<b>{kind}</b><br>"
                + (f"Strike: {pt.strike_deg:.1f}°<br>" if pt.strike_deg is not None else "")
                + (f"Dip: {pt.dip_deg:.1f}°<br>" if pt.dip_deg is not None else "")
                + f"Pole trend: {pt.pole_trend_deg:.1f}°<br>"
                + f"Pole plunge: {pt.pole_plunge_deg:.1f}°"
                + (f"<br>Depth: {pt.depth_m:.1f} m" if pt.depth_m is not None else "")
                + (f"<br>Confidence: {pt.confidence}" if pt.confidence else "")
            )
        traces.append({
            "type":      "scatter",
            "x":         xs,
            "y":         ys,
            "mode":      "markers",
            "name":      f"{kind} (n={len(pts)})",
            "marker":    {
                "color":  pts[0].display_color or default_color,
                "size":   8,
                "symbol": pts[0].display_symbol or default_symbol,
                "line":   {"color": "#333", "width": 0.5},
            },
            "hoverinfo": "text",
            "hovertext": hovers,
        })

    # Primitive circle + N/E/S/W tick marks
    primitive = _make_primitive_circle()

    return {
        "data": [primitive] + traces,
        "layout": {
            "title": {
                "text": title or "Equal-area stereonet (lower hemisphere)",
                "font": {"size": 14},
            },
            "xaxis": {
                "range":     [-1.15, 1.15],
                "showgrid":  False,
                "zeroline":  False,
                "showticklabels": False,
                "scaleanchor": "y",  # force square aspect
                "scaleratio": 1.0,
            },
            "yaxis": {
                "range":     [-1.15, 1.15],
                "showgrid":  False,
                "zeroline":  False,
                "showticklabels": False,
            },
            "width":          width,
            "height":         height,
            "margin":         {"l": 30, "r": 30, "t": 50, "b": 30},
            "paper_bgcolor":  "#fff",
            "plot_bgcolor":   "#fff",
            "showlegend":     True,
            "annotations": [
                {"x": 0, "y": 1.08, "text": "<b>N</b>", "showarrow": False,
                 "font": {"size": 14}, "xref": "x", "yref": "y"},
                {"x": 1.08, "y": 0, "text": "<b>E</b>", "showarrow": False,
                 "font": {"size": 14}, "xref": "x", "yref": "y"},
                {"x": 0, "y": -1.08, "text": "<b>S</b>", "showarrow": False,
                 "font": {"size": 14}, "xref": "x", "yref": "y"},
                {"x": -1.08, "y": 0, "text": "<b>W</b>", "showarrow": False,
                 "font": {"size": 14}, "xref": "x", "yref": "y"},
            ],
        },
    }


def _make_primitive_circle() -> dict[str, Any]:
    """Trace for the unit primitive circle of the stereonet."""
    n = 120
    xs = [math.cos(2 * math.pi * k / n) for k in range(n + 1)]
    ys = [math.sin(2 * math.pi * k / n) for k in range(n + 1)]
    return {
        "type":     "scatter",
        "x":        xs,
        "y":        ys,
        "mode":     "lines",
        "line":     {"color": "#333", "width": 1.5},
        "showlegend": False,
        "hoverinfo": "skip",
        "name":     "primitive",
    }


def render_stereonet_matplotlib_png(
    points: Sequence[StereonetPoint],
    *,
    title: str | None = None,
    width_in: float = 7.0,
    height_in: float = 7.0,
    dpi: int = 150,
) -> bytes:
    """Render an equal-area stereonet to a static PNG."""
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)

    # Primitive circle
    import numpy as np  # noqa: PLC0415
    theta = np.linspace(0, 2 * math.pi, 200)
    ax.plot(np.cos(theta), np.sin(theta), color="#333", linewidth=1.5)

    if not points:
        ax.text(
            0, 0, "No structural measurements available.",
            ha="center", va="center", fontsize=11, color="#888",
        )
    else:
        by_kind: dict[str, list[StereonetPoint]] = {}
        for p in points:
            by_kind.setdefault(p.measurement_kind, []).append(p)

        for kind, pts in sorted(by_kind.items()):
            default_color, _ = _DEFAULT_KIND_STYLE.get(
                kind, _DEFAULT_KIND_STYLE["other"],
            )
            xs, ys = zip(*(
                _equal_area_project(p.pole_trend_deg, p.pole_plunge_deg)
                for p in pts
            ))
            ax.scatter(
                xs, ys,
                s=40,
                color=pts[0].display_color or default_color,
                edgecolor="#333",
                linewidth=0.5,
                label=f"{kind} (n={len(pts)})",
                zorder=5,
            )

        ax.legend(loc="upper right", fontsize=9)

    # Compass labels
    for label, x, y in [("N", 0, 1.08), ("E", 1.08, 0),
                        ("S", 0, -1.08), ("W", -1.08, 0)]:
        ax.text(x, y, label, ha="center", va="center",
                fontsize=12, fontweight="bold")

    ax.set_xlim(-1.2, 1.2)
    ax.set_ylim(-1.2, 1.2)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    ax.set_title(title or "Equal-area stereonet (lower hemisphere)",
                 fontsize=11)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
