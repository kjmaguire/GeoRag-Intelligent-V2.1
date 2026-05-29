# CC-01 Partial-Items Implementation Kickoff

**Source:** `C:\Users\GeoRAG\Downloads\georag-cc-01-partial-items.md`
**Authorised:** 2026-05-23 (Kyle — "do all 5", clarified to all 6 with Item 3 as scaffold-only).
**Status:** Active — multi-day phased run.

---

## 0. Verification recap

Findings already reported back to Kyle (see prior chat turn). One-line per item:

| # | Item | Verdict (today) |
|---|------|-----------------|
| 1 | Drill log + assay extraction | PARTIAL — schema + parsers shipped, no upload/review surface |
| 2 | Spatial uncertainty + CRS | PARTIAL (schema only, raster layer); no UI |
| 3 | Qwen-VL map digitisation | PARTIAL — VL stack wired for PDF only; map pipeline NOT STARTED |
| 4 | Agentic retrieval (6 subgraphs) | SHIPPED as 1-graph + 6-intents (spec literalism gap) |
| 5 | Assessment report structured summary | NOT STARTED for the specific feature |
| 6 | Export paths | SHIPPED 4/5 formats; DXF missing; no review-status filter |

`georag-v1-consolidated-plan.html` is **not on disk** — only `georag-architecture.html` (v1.49) and `georag-missing-features.html` are present. Flagged.

---

## 1. Order + sequencing (Kyle's directive)

Highest user value first:

1. **Item 5** — Assessment report structured summary (geologist-facing flagship)
2. **Item 1** — Drill log upload + Silver Review Queue integration
3. **Item 2** — Spatial uncertainty + CRS provenance fields
4. **Item 6** — DXF exporter + review-status filter
5. **Item 4** — ADR + architecture doc update (no code rewrite — accept current arch)
6. **Item 3 stub** — Map ingest route stub + control_points schema scaffold

---

## 2. Item-by-item plan

### Item 5 — Assessment report structured summary

**Composes** existing `/pdf/summarize_section` (Qwen-VL with claim→provenance) rather than rebuilding extraction.

| Step | Surface | Files | Notes |
|------|---------|-------|-------|
| 5.1 | Schema | `database/migrations/*_create_silver_assessment_report_summaries.php` | Table keyed on `pdf_id` (SHA-256 from §04p); FK to silver.reports nullable; jsonb sections; completeness_checklist; timestamps |
| 5.2 | Pydantic | `src/fastapi/app/models/assessment_summary.py` | `AssessmentReportSummary`, `SummarySection`, `CompletenessItem` |
| 5.3 | Service | `src/fastapi/app/services/assessment_summarizer.py` | Orchestrates 9 section extractions; each calls `pdf_vl.summarize_section` with section-specific prompt + heuristic page-range selection from `silver.pdf_layout_regions` |
| 5.4 | Router | `src/fastapi/app/routers/assessment_summary.py` | `POST /assessment_summary/{pdf_id}` (regenerate), `GET /assessment_summary/{pdf_id}` (cached fetch) |
| 5.5 | Laravel bridge | `app/Http/Controllers/Foundry/AssessmentSummaryController.php` | Inertia render + FastAPI proxy via existing `FastApiJwtMinter` |
| 5.6 | UI | `resources/js/Pages/Foundry/AssessmentSummary.tsx` | 9 collapsible sections + completeness checklist sidebar; each claim links to PDF viewer at source page+bbox |
| 5.7 | Tests | `src/fastapi/tests/test_assessment_summarizer.py`, feature test in Laravel | Mock VL backend; golden response shape |

**9 sections** (per spec): property_project, location, commodities, operator, year, work_performed, qa_qc, recommendations, completeness_checklist.

**Completeness checklist** logic (v1): rule-based on expected NI 43-101 §1–§27 sections — flag any expected section heading not found in `silver.pdf_layout_regions` for that pdf_id. (LLM-assisted v2 later.)

### Item 1 — Drill log upload + SRQ integration

| Step | Surface | Notes |
|------|---------|-------|
| 1.1 | Upload route | `POST /api/v1/projects/{project}/drill-uploads` — accepts CSV/XLSX/PDF, persists to bronze MinIO, triggers Dagster `silver_collars` / `silver_lithology` / `silver_samples` materialise |
| 1.2 | SRQ promotion | Extend `silver_review_queue` model with `target_table` for drill rows; surface in existing review UI |
| 1.3 | Unit ambiguity | Detector in `src/dagster/.../parsers/csv_sample.py` — flag rows where the unit column is missing or matches ambiguous tokens (`%` vs `ppm` for Au, etc.); attach flag to review record |
| 1.4 | Overlap detection | New silver-side validator on lithology/sample intervals — detect rows where `[from_depth, to_depth)` overlaps another row for the same hole; flag in SRQ |
| 1.5 | UI | `Pages/Foundry/DrillUploads.tsx` — list + status + drill-into-review |

### Item 2 — Spatial uncertainty + CRS provenance

| Step | Surface | Notes |
|------|---------|-------|
| 2.1 | Migration | Add `spatial_uncertainty_m REAL`, `crs_confidence REAL`, `georef_method VARCHAR(32)` to `silver.collars` and `silver.spatial_features` (raster already has `crs_confidence`) |
| 2.2 | Populate | Update `silver_collars` / `silver_spatial` Dagster assets to write these fields. `georef_method` enum: `declared` \| `detected` \| `assumed` \| `manual` |
| 2.3 | UI | Render uncertainty radius ring in `MapView.tsx` when `spatial_uncertainty_m > 0`; add CRS-confidence badge to drillhole detail |

### Item 6 — DXF + review-status filter

| Step | Surface | Notes |
|------|---------|-------|
| 6.1 | DxfExporter | `app/Services/Exports/DxfExporter.php` proxies to new FastAPI `/internal/exports/dxf` endpoint that emits DXF via `ezdxf` (Python) |
| 6.2 | Enum + job | Add `dxf` to `StoreExportRequest::rules()` enum + `GenerateExportJob` dispatch |
| 6.3 | Review filter | Add `filters.review_status` to `StoreExportRequest` (default: `accepted_only`); each exporter applies `WHERE review_status IN (...)` |

### Item 4 — ADR + doc update only

| Step | Surface | Notes |
|------|---------|-------|
| 4.1 | ADR | `docs/adr/ADR-0006-agentic-retrieval-single-graph.md` — record the decision rationale (functional equivalence, lower compile cost, simpler debug surface) |
| 4.2 | Doc edit | Update §04j in `georag-architecture.html` to describe "one StateGraph + six routed intents" instead of "six subgraphs" |

### Item 3 stub — Map ingest scaffold

| Step | Surface | Notes |
|------|---------|-------|
| 3.1 | Migration | `silver.control_points` table: `point_id uuid PK`, `source_pdf_id char(64)`, `pixel_xy point`, `world_xy geometry(Point, 4326)`, `georef_confidence real`, `method varchar(32)` |
| 3.2 | Route stub | `POST /maps/ingest` returns `501 not_implemented` with `Retry-After: milestone-2-vl-decision` header; route registered + tested |
| 3.3 | Doc | Note in §04j that map digitisation is gated on M2 |

---

## 3. Check-in cadence

This is a multi-day run. After each Item completes (5 → 1 → 2 → 6 → 4 → 3-stub) I report status, then proceed unless Kyle stops me. If I hit an architectural fork mid-Item I escalate per the `feedback_aggressive_interpretation` rule — pick the architecturally-correct call and document, only stop for genuine ambiguity.

## 4. What I'm NOT doing

- Re-architecting agentic retrieval (Item 4 is doc-only).
- Implementing full VL map digitisation (Item 3 is scaffold-only).
- Touching `georag-architecture.html` except Item 4's §04j edit and Item 3's §04j map-gate note.
- Adding new dependencies beyond `ezdxf` (Python, for DXF export).
