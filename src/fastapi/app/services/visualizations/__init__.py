"""Master-plan §5 — drillhole visualisations.

Modules:
    strip_log: per-collar lithology + assay column. Plotly + matplotlib.
    cross_section: pre-projected interval panels on a named section line.
    stereonet: equal-area (Schmidt) lower-hemisphere structural plot.

All three share the same dual-output pattern: a pure function for the
interactive Plotly JSON figure dict + a pure function for the static
matplotlib PNG. Both consume the same dataclass type.

doc-phase 185 — strip-log starter
doc-phase 186 — cross-section + stereonet added (Phase H4)
"""
from app.services.visualizations.cross_section import (
    CrossSectionPanel,
    render_cross_section_matplotlib_png,
    render_cross_section_plotly_figure,
)
from app.services.visualizations.stereonet import (
    StereonetPoint,
    render_stereonet_matplotlib_png,
    render_stereonet_plotly_figure,
)
from app.services.visualizations.strip_log import (
    StripLogInterval,
    render_strip_log_matplotlib_png,
    render_strip_log_plotly_figure,
)

__all__ = [
    "StripLogInterval",
    "render_strip_log_matplotlib_png",
    "render_strip_log_plotly_figure",
    "CrossSectionPanel",
    "render_cross_section_matplotlib_png",
    "render_cross_section_plotly_figure",
    "StereonetPoint",
    "render_stereonet_matplotlib_png",
    "render_stereonet_plotly_figure",
]
