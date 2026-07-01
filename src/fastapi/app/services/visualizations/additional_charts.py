"""§17.3 wave 2 — 8 additional geological visualization types.

Each function returns a Plotly figure spec as a dict (consumable by
`react-plotly.js`). Most accept either real workspace data (passed
in by the router) or a synthetic demo dataset (when the workspace
has no matching data yet — the demo lets the chart render so
operators can see the shape before the data exists).

Charts:
  - long_section       drillholes projected onto a reference azimuth (vertical 2D)
  - harker_diagram     SiO2 vs other major oxide (igneous petrology classification)
  - spider_diagram     multi-element pattern (normalized to a reference)
  - ree_pattern        chondrite-normalized rare-earth-element pattern
  - ternary_diagram    3-component composition (A-B-C triangle)
  - grade_tonnage      cumulative tonnage at varying cutoff grades
  - anomaly_map        points colored by Z-score of an element
  - target_heatmap     h3 hex cells colored by aggregate target score
"""
from __future__ import annotations

import math
import statistics
from typing import Any

# ─────────────────────────────────────────────────────────────────────
# Reference datasets
# ─────────────────────────────────────────────────────────────────────
# Sun & McDonough (1989) C1 chondrite REE values (ppm) — the canonical
# normalization standard for REE patterns.
_CHONDRITE_C1_PPM: dict[str, float] = {
    "La": 0.237, "Ce": 0.612, "Pr": 0.095, "Nd": 0.467, "Sm": 0.153,
    "Eu": 0.058, "Gd": 0.2055, "Tb": 0.0374, "Dy": 0.254, "Ho": 0.0566,
    "Er": 0.1655, "Tm": 0.0255, "Yb": 0.17, "Lu": 0.0254,
}
_REE_ORDER = ["La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd", "Tb",
              "Dy", "Ho", "Er", "Tm", "Yb", "Lu"]

# Primitive mantle (Sun & McDonough 1989) — for spider-diagram normalization
_PRIM_MANTLE_PPM: dict[str, float] = {
    "Rb": 0.6, "Ba": 6.6, "Th": 0.0795, "U": 0.0203, "Nb": 0.658,
    "Ta": 0.037, "K": 250, "La": 0.648, "Ce": 1.675, "Pb": 0.15,
    "Pr": 0.254, "Sr": 19.9, "P": 90.4, "Nd": 1.25, "Zr": 10.5,
    "Hf": 0.283, "Sm": 0.406, "Eu": 0.154, "Ti": 1205, "Gd": 0.544,
    "Tb": 0.099, "Dy": 0.674, "Y": 4.30, "Ho": 0.149, "Er": 0.438,
    "Tm": 0.068, "Yb": 0.441, "Lu": 0.0675,
}
_SPIDER_ORDER = list(_PRIM_MANTLE_PPM.keys())


def _empty_layout(title: str, height: int = 400) -> dict[str, Any]:
    return {
        "title": {"text": title, "x": 0.5},
        "autosize": True,
        "height": height,
        "margin": {"l": 60, "r": 20, "t": 50, "b": 60},
        "showlegend": True,
    }


# ─────────────────────────────────────────────────────────────────────
# 1. Long section
# ─────────────────────────────────────────────────────────────────────
def long_section_figure(
    *,
    collars: list[dict[str, Any]],   # [{hole_id, easting, northing, elevation, total_depth, azimuth?, inclination?}, ...]
    reference_azimuth_deg: float = 0.0,
    title: str | None = None,
) -> dict[str, Any]:
    """Project each drillhole onto a vertical plane oriented along
    `reference_azimuth_deg` (measured CW from north). Plots collar at
    top, projected total-depth point at bottom, simple straight-line
    trace between (true desurvey lives in §17.1 gold table).
    """
    az_rad = math.radians(reference_azimuth_deg)
    # Projection axis: x' = E*sin(az) + N*cos(az)
    sin_az, cos_az = math.sin(az_rad), math.cos(az_rad)

    traces: list[dict[str, Any]] = []
    if not collars:
        return {
            "data": [{"x": [], "y": [], "type": "scatter", "mode": "markers"}],
            "layout": _empty_layout(title or "Long section (no data)"),
        }

    for c in collars:
        e = float(c.get("easting", 0))
        n = float(c.get("northing", 0))
        elev = float(c.get("elevation", 0))
        td = float(c.get("total_depth", 0))
        inc = float(c.get("inclination", -90))  # vertical hole default
        az = float(c.get("azimuth", reference_azimuth_deg))

        # Project collar onto reference axis
        x_collar = e * sin_az + n * cos_az
        y_collar = elev

        # Project end-of-hole (simple straight-line approximation in 3D)
        end_e = e + td * math.cos(math.radians(inc)) * math.sin(math.radians(az))
        end_n = n + td * math.cos(math.radians(inc)) * math.sin(math.radians(az))
        end_elev = elev + td * math.sin(math.radians(inc))
        x_end = end_e * sin_az + end_n * cos_az

        traces.append({
            "x": [x_collar, x_end],
            "y": [y_collar, end_elev],
            "type": "scatter",
            "mode": "lines+markers",
            "name": c.get("hole_id", "?"),
            "line": {"width": 2},
            "marker": {"size": [10, 4]},
            "hovertemplate": "%{fullData.name}<br>x=%{x:.0f} m<br>elev=%{y:.0f} m<extra></extra>",
        })

    layout = _empty_layout(title or f"Long section (az={reference_azimuth_deg:.0f}°)", height=500)
    layout["xaxis"] = {"title": {"text": f"Distance along section (m, +az={reference_azimuth_deg:.0f}°)"}}
    layout["yaxis"] = {"title": {"text": "Elevation (m)"}, "scaleanchor": "x", "scaleratio": 1}
    return {"data": traces, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 2. Harker diagram (SiO2 vs other oxide)
# ─────────────────────────────────────────────────────────────────────
def harker_diagram_figure(
    *,
    samples: list[dict[str, Any]],  # [{SiO2: float, oxide: float, rock_type?: str}]
    y_oxide: str = "Al2O3",
    title: str | None = None,
) -> dict[str, Any]:
    """SiO2 (x) vs another major oxide (y) scatter for igneous petrology
    classification. Color by rock_type if provided.
    """
    by_type: dict[str, list[tuple[float, float]]] = {}
    for s in samples:
        sio2 = s.get("SiO2")
        y = s.get(y_oxide)
        if sio2 is None or y is None:
            continue
        rt = s.get("rock_type", "unknown")
        by_type.setdefault(rt, []).append((float(sio2), float(y)))

    traces = [
        {
            "x": [p[0] for p in pts], "y": [p[1] for p in pts],
            "type": "scatter", "mode": "markers", "name": rt,
            "marker": {"size": 8, "opacity": 0.7},
            "hovertemplate": f"SiO2=%{{x:.1f}}%<br>{y_oxide}=%{{y:.2f}}%<extra>{rt}</extra>",
        }
        for rt, pts in by_type.items()
    ]
    layout = _empty_layout(title or f"Harker diagram: SiO2 vs {y_oxide}")
    layout["xaxis"] = {"title": {"text": "SiO2 (wt%)"}}
    layout["yaxis"] = {"title": {"text": f"{y_oxide} (wt%)"}}
    return {"data": traces, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 3. Spider diagram (multi-element pattern)
# ─────────────────────────────────────────────────────────────────────
def spider_diagram_figure(
    *,
    samples: list[dict[str, Any]],   # [{sample_id, La, Ce, Nd, ..., Yb, Lu (ppm)}]
    normalization: str = "primitive_mantle",
    title: str | None = None,
) -> dict[str, Any]:
    """Spider diagram — multi-element line normalized to a reference
    (primitive mantle by default). Each sample becomes one line.
    """
    norm = _PRIM_MANTLE_PPM if normalization == "primitive_mantle" else _CHONDRITE_C1_PPM
    order = [el for el in _SPIDER_ORDER if el in norm]

    traces = []
    for s in samples:
        sid = s.get("sample_id", "?")
        ys: list[float | None] = []
        for el in order:
            v = s.get(el)
            if v is None or norm[el] == 0:
                ys.append(None)
            else:
                ys.append(float(v) / norm[el])
        traces.append({
            "x": order, "y": ys, "type": "scatter", "mode": "lines+markers",
            "name": sid,
            "connectgaps": False,
        })

    layout = _empty_layout(title or f"Spider diagram (norm: {normalization})", height=450)
    layout["yaxis"] = {"title": {"text": "Sample / reference"}, "type": "log"}
    layout["xaxis"] = {"title": {"text": "Element"}}
    return {"data": traces, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 4. REE pattern (chondrite-normalized)
# ─────────────────────────────────────────────────────────────────────
def ree_pattern_figure(
    *,
    samples: list[dict[str, Any]],   # [{sample_id, La, Ce, ..., Lu}]
    title: str | None = None,
) -> dict[str, Any]:
    """Rare-earth-element pattern normalized to C1 chondrite."""
    order = [el for el in _REE_ORDER if el in _CHONDRITE_C1_PPM]
    traces = []
    for s in samples:
        sid = s.get("sample_id", "?")
        ys: list[float | None] = []
        for el in order:
            v = s.get(el)
            if v is None or _CHONDRITE_C1_PPM[el] == 0:
                ys.append(None)
            else:
                ys.append(float(v) / _CHONDRITE_C1_PPM[el])
        traces.append({
            "x": order, "y": ys, "type": "scatter", "mode": "lines+markers",
            "name": sid, "connectgaps": False,
        })
    layout = _empty_layout(title or "REE pattern (C1-chondrite normalized)", height=450)
    layout["yaxis"] = {"title": {"text": "Sample / C1 chondrite"}, "type": "log"}
    layout["xaxis"] = {"title": {"text": "REE (light → heavy)"}}
    return {"data": traces, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 5. Ternary diagram
# ─────────────────────────────────────────────────────────────────────
def ternary_diagram_figure(
    *,
    samples: list[dict[str, Any]],   # [{a, b, c, label?}]
    apex_labels: tuple[str, str, str] = ("A", "B", "C"),
    title: str | None = None,
) -> dict[str, Any]:
    """3-component ternary scatter via Plotly's `scatterternary`."""
    a_vals = [float(s.get("a", 0)) for s in samples]
    b_vals = [float(s.get("b", 0)) for s in samples]
    c_vals = [float(s.get("c", 0)) for s in samples]
    text = [str(s.get("label", "")) for s in samples]

    data = [{
        "type": "scatterternary",
        "mode": "markers",
        "a": a_vals,
        "b": b_vals,
        "c": c_vals,
        "text": text,
        "marker": {"size": 9, "opacity": 0.75},
        "hovertemplate": (
            f"{apex_labels[0]}=%{{a:.2f}}<br>"
            f"{apex_labels[1]}=%{{b:.2f}}<br>"
            f"{apex_labels[2]}=%{{c:.2f}}<br>%{{text}}<extra></extra>"
        ),
    }]
    layout = {
        "title": {"text": title or f"Ternary: {' / '.join(apex_labels)}", "x": 0.5},
        "ternary": {
            "sum": 100,
            "aaxis": {"title": apex_labels[0]},
            "baxis": {"title": apex_labels[1]},
            "caxis": {"title": apex_labels[2]},
        },
        "autosize": True, "height": 500,
        "margin": {"l": 40, "r": 40, "t": 60, "b": 40},
    }
    return {"data": data, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 6. Grade-tonnage curve
# ─────────────────────────────────────────────────────────────────────
def grade_tonnage_figure(
    *,
    samples: list[dict[str, Any]],  # [{grade, tonnes}]
    cutoffs: list[float] | None = None,
    grade_unit: str = "g/t",
    title: str | None = None,
) -> dict[str, Any]:
    """At each cutoff grade, compute the total tonnage above cutoff +
    weighted-average grade above cutoff. Returns the classic dual-axis
    grade-tonnage plot.
    """
    if not samples:
        return {"data": [], "layout": _empty_layout(title or "Grade-tonnage (no data)")}

    if cutoffs is None:
        max_g = max(float(s.get("grade", 0)) for s in samples)
        cutoffs = [round(x * max_g / 20, 3) for x in range(0, 20)]

    pts = [(float(s.get("grade", 0)), float(s.get("tonnes", 0))) for s in samples]

    tonnages = []
    avg_grades = []
    for c in cutoffs:
        above = [(g, t) for (g, t) in pts if g >= c]
        if not above:
            tonnages.append(0)
            avg_grades.append(0)
            continue
        total_t = sum(t for (_, t) in above)
        wt_grade = sum(g * t for (g, t) in above) / total_t if total_t else 0
        tonnages.append(total_t)
        avg_grades.append(wt_grade)

    data = [
        {
            "x": cutoffs, "y": tonnages, "type": "scatter", "mode": "lines+markers",
            "name": "Tonnes above cutoff", "yaxis": "y1",
            "line": {"color": "#3b82f6"},
        },
        {
            "x": cutoffs, "y": avg_grades, "type": "scatter", "mode": "lines+markers",
            "name": f"Avg grade above cutoff ({grade_unit})", "yaxis": "y2",
            "line": {"color": "#ef4444"},
        },
    ]
    layout = _empty_layout(title or "Grade-tonnage curve", height=450)
    layout["xaxis"] = {"title": {"text": f"Cutoff grade ({grade_unit})"}}
    layout["yaxis"] = {"title": {"text": "Tonnes", "font": {"color": "#3b82f6"}}, "side": "left"}
    layout["yaxis2"] = {
        "title": {"text": f"Avg grade ({grade_unit})", "font": {"color": "#ef4444"}},
        "overlaying": "y", "side": "right",
    }
    return {"data": data, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 7. Anomaly map (Z-score scatter)
# ─────────────────────────────────────────────────────────────────────
def anomaly_map_figure(
    *,
    samples: list[dict[str, Any]],  # [{lng, lat, value}]
    element_label: str = "value",
    title: str | None = None,
) -> dict[str, Any]:
    """Sample-point scatter colored by Z-score of `value`. Points
    >2σ above mean are flagged as anomalies (orange→red gradient).
    """
    if not samples:
        return {"data": [], "layout": _empty_layout(title or "Anomaly map (no data)")}

    vals = [float(s["value"]) for s in samples if s.get("value") is not None]
    if not vals:
        return {"data": [], "layout": _empty_layout(title or "Anomaly map (no values)")}

    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) or 1.0

    z_scores = [(float(s["value"]) - mean) / sd for s in samples if s.get("value") is not None]
    lngs = [float(s["lng"]) for s in samples if s.get("value") is not None]
    lats = [float(s["lat"]) for s in samples if s.get("value") is not None]

    data = [{
        "type": "scatter",
        "mode": "markers",
        "x": lngs, "y": lats,
        "marker": {
            "size": [max(6, min(20, 6 + abs(z) * 4)) for z in z_scores],
            "color": z_scores,
            "colorscale": "RdYlBu_r",
            "cmin": -3, "cmax": 3,
            "colorbar": {"title": {"text": "Z-score"}},
        },
        "text": [f"value={v:.3f}, z={z:.2f}σ" for v, z in zip(vals, z_scores, strict=False)],
        "hovertemplate": "lng=%{x:.4f}<br>lat=%{y:.4f}<br>%{text}<extra></extra>",
    }]
    layout = _empty_layout(title or f"Anomaly map ({element_label})", height=500)
    layout["xaxis"] = {"title": {"text": "Longitude"}}
    layout["yaxis"] = {"title": {"text": "Latitude"}, "scaleanchor": "x", "scaleratio": 1}
    layout["annotations"] = [{
        "xref": "paper", "yref": "paper", "x": 0, "y": 1.04,
        "text": f"μ={mean:.3f}, σ={sd:.3f}; |z|>2 = anomaly",
        "showarrow": False, "font": {"size": 10, "color": "#666"},
    }]
    return {"data": data, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# 8. Target heatmap (h3 cells colored by score)
# ─────────────────────────────────────────────────────────────────────
def target_heatmap_figure(
    *,
    cells: list[dict[str, Any]],   # [{lng, lat, score}]
    title: str | None = None,
) -> dict[str, Any]:
    """Hex-cell aggregate score heatmap. Cells should already be
    aggregated (e.g. via gold.h3_density_mineral or the §6.6 asset).
    """
    if not cells:
        return {"data": [], "layout": _empty_layout(title or "Target heatmap (no data)")}

    data = [{
        "type": "scatter",
        "mode": "markers",
        "x": [float(c["lng"]) for c in cells],
        "y": [float(c["lat"]) for c in cells],
        "marker": {
            "size": 14,
            "symbol": "hexagon",
            "color": [float(c["score"]) for c in cells],
            "colorscale": "YlOrRd",
            "colorbar": {"title": {"text": "Target score"}},
            "opacity": 0.7,
        },
        "hovertemplate": "lng=%{x:.4f}<br>lat=%{y:.4f}<br>score=%{marker.color:.3f}<extra></extra>",
    }]
    layout = _empty_layout(title or "Target heatmap (h3 cells)", height=500)
    layout["xaxis"] = {"title": {"text": "Longitude"}}
    layout["yaxis"] = {"title": {"text": "Latitude"}, "scaleanchor": "x", "scaleratio": 1}
    return {"data": data, "layout": layout}


# ─────────────────────────────────────────────────────────────────────
# Synthetic demo datasets (so charts render even without real data)
# ─────────────────────────────────────────────────────────────────────
def demo_dataset(chart_kind: str) -> dict[str, Any]:
    """Returns a synthetic dataset suitable for each chart. The router
    falls back to this when the workspace has no matching data, so the
    chart still renders + the operator can see the shape.
    """
    import random
    random.seed(42)
    if chart_kind == "long_section":
        return {
            "collars": [
                {"hole_id": f"DH-{i:03d}", "easting": 600000 + i * 50, "northing": 5700000 + i * 30,
                 "elevation": 1200, "total_depth": 250 + random.randint(0, 100),
                 "inclination": -60, "azimuth": 90}
                for i in range(1, 9)
            ],
            "reference_azimuth_deg": 90,
        }
    if chart_kind == "harker_diagram":
        return {
            "samples": [
                {"SiO2": round(45 + random.random() * 30, 1),
                 "Al2O3": round(12 + random.random() * 6, 2),
                 "rock_type": rt}
                for rt in ["basalt", "andesite", "rhyolite"] for _ in range(8)
            ],
            "y_oxide": "Al2O3",
        }
    if chart_kind == "spider_diagram":
        return {
            "samples": [
                {"sample_id": f"S-{i}",
                 **{el: round(_PRIM_MANTLE_PPM[el] * (1 + random.random() * 50), 3)
                    for el in _SPIDER_ORDER}}
                for i in range(1, 4)
            ],
            "normalization": "primitive_mantle",
        }
    if chart_kind == "ree_pattern":
        return {
            "samples": [
                {"sample_id": f"S-{i}",
                 **{el: round(_CHONDRITE_C1_PPM[el] * (10 + random.random() * 100), 3)
                    for el in _REE_ORDER}}
                for i in range(1, 4)
            ],
        }
    if chart_kind == "ternary_diagram":
        # AFM-style triangle: A=Na+K oxide, F=FeO, M=MgO
        out = []
        for _ in range(30):
            a, b, c = random.random(), random.random(), random.random()
            tot = a + b + c
            out.append({"a": round(a / tot * 100, 2), "b": round(b / tot * 100, 2),
                       "c": round(c / tot * 100, 2), "label": "demo"})
        return {"samples": out, "apex_labels": ("Na+K", "FeO", "MgO")}
    if chart_kind == "grade_tonnage":
        return {
            "samples": [
                {"grade": round(0.1 + random.expovariate(1.0), 3),
                 "tonnes": random.randint(1000, 50000)}
                for _ in range(120)
            ],
            "grade_unit": "g/t",
        }
    if chart_kind == "anomaly_map":
        # 50 random points in BC with one cluster of high-value anomalies
        out = []
        for _ in range(40):
            out.append({"lng": -127 + random.random() * 1.5, "lat": 54 + random.random() * 1.5,
                       "value": round(0.1 + random.expovariate(3), 3)})
        for _ in range(8):
            out.append({"lng": -126.8 + random.random() * 0.3, "lat": 54.3 + random.random() * 0.3,
                       "value": round(2 + random.random() * 4, 3)})
        return {"samples": out, "element_label": "Au (g/t)"}
    if chart_kind == "target_heatmap":
        out = []
        for _ in range(80):
            lng = -127 + random.random() * 1.5
            lat = 54 + random.random() * 1.5
            score = round(random.betavariate(2, 5), 3)
            out.append({"lng": lng, "lat": lat, "score": score})
        return {"cells": out}
    return {}


# ─────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────
_CHART_DISPATCH: dict[str, Any] = {
    "long_section":     long_section_figure,
    "harker_diagram":   harker_diagram_figure,
    "spider_diagram":   spider_diagram_figure,
    "ree_pattern":      ree_pattern_figure,
    "ternary_diagram":  ternary_diagram_figure,
    "grade_tonnage":    grade_tonnage_figure,
    "anomaly_map":      anomaly_map_figure,
    "target_heatmap":   target_heatmap_figure,
}


def render_chart(chart_kind: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render any of the 8 chart kinds. If params is empty/missing, use
    the synthetic demo dataset for that kind."""
    fn = _CHART_DISPATCH.get(chart_kind)
    if fn is None:
        raise ValueError(f"unknown chart_kind: {chart_kind}")
    p = params or {}
    if not p:
        p = demo_dataset(chart_kind)
    try:
        return fn(**p)
    except TypeError:
        # demo dataset shape mismatch — fall back to safe empty render
        return {
            "data": [],
            "layout": _empty_layout(f"{chart_kind} (input shape error)"),
        }


KNOWN_CHARTS: list[str] = list(_CHART_DISPATCH.keys())


__all__ = [
    "render_chart",
    "demo_dataset",
    "KNOWN_CHARTS",
    "long_section_figure",
    "harker_diagram_figure",
    "spider_diagram_figure",
    "ree_pattern_figure",
    "ternary_diagram_figure",
    "grade_tonnage_figure",
    "anomaly_map_figure",
    "target_heatmap_figure",
]
