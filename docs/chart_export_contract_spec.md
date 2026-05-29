# Chart Export Contract Spec (§17.4 implementation guide)

**Doc-phase 72** — reads master-plan §17.4 + §17.5; specs the
implementation contract for §5.9 + later visual chart deliverables.

---

## Master plan §17.4 — verbatim

> Every chart exported (in a report, as a download, embedded in chat)
> carries:
> - **source data** — reference to underlying Gold table rows or external source
> - **method** — generation method (e.g., "minimum curvature desurvey, 5m intervals")
> - **filters** — applied filters (assay min, depth range, lithology subset)
> - **CRS** — when spatial, the coordinate reference system used
> - **citations** — citations to source documents for any data shown
> - **confidence/warnings** — per-data-point confidence; warnings if any inputs are flagged in QA
>
> This contract is enforced in the Map/Chart Planner Agent (§15.4); a
> chart cannot ship without this metadata.

---

## Implementation contract (lock-in for §5.9)

Each chart-producing endpoint MUST return a payload of shape:

```json
{
  "chart": {
    "type": "strip_log | cross_section | stereonet | other",
    "format": "plotly_html | plotly_json | matplotlib_png | other",
    "content": "<inline HTML or base64 PNG or JSON figure>"
  },
  "export_metadata": {
    "source_data": {
      "gold_tables": ["gold.drillhole_intervals_visual", "..."],
      "row_count": 142,
      "row_ids": ["uuid1", "uuid2", "..."],
      "external_sources": []
    },
    "method": "minimum curvature desurvey, 5m intervals",
    "filters": {
      "depth_min_m": 0,
      "depth_max_m": 500,
      "lithology_subset": ["BIF", "SST"],
      "assay_min": null
    },
    "crs": "EPSG:4326",
    "citations": [
      {"source_chunk_id": "uuid", "document_id": "uuid", "page": 14}
    ],
    "confidence_warnings": [
      {"row_id": "uuid", "field": "ocr_text", "warning": "OCR confidence 0.62"}
    ]
  }
}
```

### Per-chart-type minimum fields

| Chart | source_data must include | method must mention |
|---|---|---|
| Strip log | `gold.drillhole_intervals_visual` + the `collar_id` | "lithology bars + assay overlay" or similar |
| Cross-section | `gold.cross_section_panels` + the section_name | "minimum curvature desurvey" + "section line projection" |
| Stereonet | `gold.structure_measurements_visual` | "equal_area projection" (or equal_angle) |

### Enforcement gate (per §17.4 last sentence)

The Map/Chart Planner Agent (§15.4) refuses to ship a chart without
all 6 fields populated (citations can be `[]` if no underlying
document supports the chart, e.g. pure-numeric-derived plots).

For §5.6-5.8 endpoints (no Planner Agent in the loop yet — that's
§7+), the validation happens at the endpoint return: pydantic model
with NotEmpty constraints on every field except `external_sources`
+ `confidence_warnings`.

---

## §17.5 visual agents list (for §5.10 + §5.11 + later)

LangGraph (cognition) — 6 agents:
1. **Drillhole Visual QA Agent** — validates drillhole data is visualization-ready  ← §5.10
2. **Cross-Section Planner Agent** — selects optimal section orientation given drillhole array
3. **Stereonet Data Validator Agent** — validates structural measurements before stereonet plot
4. **Geochem QA Agent** — validates geochem data before plot (unit consistency, detection limits)
5. **Caption Agent** — drafts technical captions per chart
6. **Visual Readiness Agent** — explains why a visualization is or isn't possible  ← §5.11

Activepieces / Kestra (operational) — 3 workflows:
- Visual Report Delivery Workflow — bundles chart packages for delivery
- Failed Visual QA Alert Workflow — alerts on QA failures
- ArcGIS/QGIS Export Workflow — exports visual-ready data for desktop GIS use

§5 explicitly ships **only #1 (Drillhole Visual QA) + #6 (Visual Readiness)**.
Agents #2-5 ship in later phases (§7-§9 timeframe).

---

## Pydantic model for §5.6-5.8 endpoints

```python
from pydantic import BaseModel, Field
from typing import Literal

class ChartExportSource(BaseModel):
    gold_tables: list[str]
    row_count: int = Field(..., ge=0)
    row_ids: list[str] = Field(default_factory=list, max_length=1000)
    external_sources: list[dict] = Field(default_factory=list)

class ChartExportCitation(BaseModel):
    source_chunk_id: str | None = None
    document_id: str | None = None
    page: int | None = None

class ChartExportConfidenceWarning(BaseModel):
    row_id: str
    field: str
    warning: str

class ChartExportMetadata(BaseModel):
    source_data: ChartExportSource
    method: str = Field(..., min_length=10)
    filters: dict
    crs: str | None = None
    citations: list[ChartExportCitation] = Field(default_factory=list)
    confidence_warnings: list[ChartExportConfidenceWarning] = Field(default_factory=list)

class ChartExportPayload(BaseModel):
    chart_type: Literal["strip_log", "cross_section", "stereonet", "other"]
    format: Literal["plotly_html", "plotly_json", "matplotlib_png", "other"]
    content: str  # inline HTML or base64 PNG or JSON
    export_metadata: ChartExportMetadata
```

This is the response model for `GET /internal/v1/viz/{chart_type}`.

---

## Recommendation

§5.9 implementation = make sure every §5.6-5.8 endpoint returns a
`ChartExportPayload`. The Pydantic model itself IS the enforcement
gate — FastAPI's response_model validation refuses to send a response
that doesn't conform.

For doc-phase 72: this spec is sufficient. Implementation happens
in the §5.6-5.8 endpoint code (after the image rebuild lets us
actually import plotly + matplotlib in the FastAPI process).

---

End of spec.
