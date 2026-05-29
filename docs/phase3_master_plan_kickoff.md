# GeoRAG Master-Plan Phase 3 Implementation Kickoff

**Document version:** 1.0 (skeleton — fill detail per step before opening it)
**Status:** Draft. Master-plan Phase 3 build plan against
`GeoRAG_master_plan_v2.4.2.md` §3 + §9 + `docs/adr/0002-04p-stack-replaces-ragflow.md`.
**Audience:** Claude Code in agent mode (primary), Kyle as product owner (oversight).
**Date:** 2026-05-12

---

## Numbering note

Doc-phase numbering and master-plan numbering diverged between
doc-phase 11 and doc-phase 48 (see "Phase 18-48 autonomous run" memory).
To disambiguate, this kickoff uses the `_master_plan_` infix in the
filename. Implementation work proceeds under the existing autonomous-loop
doc-phase cadence — each doc-phase tick (49+) opens one step from the
plan below, lands code + verifier + handoff, and the master-plan §3
"done" criterion gates closure of the master-plan phase, not the
doc-phase numbering.

When this document and the master plan disagree on Phase 3 specifics,
**this document wins** — the master plan describes the destination,
this document describes the path. ADR-0002 governs the canonical
parser decision.

---

## What this document is

Translates master plan §3 + §9 into a step-by-step build plan with
explicit acceptance per step. Mirrors the shape of
`docs/phase1_implementation_kickoff.md`.

Phase 3 is **§04p PDF stack + OCR quality**. Per master plan §3:

> Replace any v1.49 RAGFlow remnants with the §04p stack and ship the
> Silver Review queue at full strength.
>
> Deliverables:
> - qpdf/pikepdf preflight, pypdfium2 rendering, pdfminer.six +
>   pdfplumber, Docling, PaddleOCR PP-OCRv5 + PP-StructureV3 fully
>   wired into Hatchet `ingest_pdf`
> - `silver.ocr_page_quality`, `silver.document_ingestion_quality`,
>   `silver.table_extraction_quality`, `silver.parser_run_artifacts`,
>   `silver.low_confidence_page_reviews` populated per ingestion
> - LangGraph OCR Quality Graph routing pages
> - OCR quality classifier (XGBoost) trained on first 1,000 reviewed
>   pages with SHAP explanations
> - Silver Review queue UI extended with per-page evidence + parser
>   used + confidence breakdown
>
> **Done when:** a representative test corpus (50 PDFs spanning native,
> scanned, mixed, table-heavy, map-heavy) ingests with correct routing
> decisions, and the Silver Review queue handles the cases that fail
> automation.

---

## Phase 3 done definition

Translated into concrete acceptance tests (see § Acceptance tests below):

1. `georag-ocr` container running, healthy, exposing parser endpoints
   to the Hatchet `ingestion` worker pool over the internal docker
   network.
2. Eight `silver.*` tables created with RLS and indexes:
   - Five quality tables (§9.6): `ocr_page_quality`,
     `document_ingestion_quality`, `table_extraction_quality`,
     `parser_run_artifacts`, `low_confidence_page_reviews`.
   - Three per-region extraction tables (§9.3): `ingest_extractions`,
     `ingest_layouts`, `ingest_ocr_results`.

   Every PDF ingest writes rows to at minimum `ocr_page_quality`,
   `parser_run_artifacts`, `document_ingestion_quality`, and the
   appropriate §9.3 extraction table per parser path.
3. PDF profiler classifies a held-out PDF as one of
   {native, scanned, mixed, map-heavy, table-heavy} with documented
   heuristics; result drives parser routing.
4. Native PDFs ingest via pdfminer.six + pdfplumber; the silver
   passages table receives chunks with per-page provenance
   `(pdf_id, page, bbox, source_method, extraction_confidence)`.
5. Scanned PDFs ingest via PaddleOCR PP-OCRv5 CPU + deskew; OCR
   confidence + retry counts captured in `ocr_page_quality`.
6. Mixed and table-heavy PDFs route through Docling layout-first;
   `layout_confidence` + `table_confidence` captured.
7. LangGraph OCR Quality Graph routes low-confidence pages to one of:
   re-OCR (different engine settings, max 2 retries), Silver Review
   queue, or reject with reason.
8. Hatchet `ingest_pdf` parse step replaced with §04p dispatch; shadow
   harness runs §04p alongside RAGFlow for the cutover window;
   structured diff captured in a new shadow table.
9. Silver Review UI shipped as an extension to the admin surface; each
   queue item shows rendered page image + parser used + confidence
   breakdown + reviewer disposition controls.
10. 50-PDF acceptance corpus committed under
    `tests/fixtures/phase3_pdf_corpus/` with ground-truth labels;
    `scripts/phase3_master_plan_acceptance.sh` ingests the corpus and
    verifies correct routing decisions per document.
11. RAGFlow v0.17.2 container removed from `docker-compose.yml`;
    `src/dagster/georag_dagster/parsers/pdf_report.py` archived to
    `_archived/` after the acceptance corpus passes.

Phase 3 does NOT ship: the XGBoost OCR quality classifier (deferred to
§9.8 / master-plan Phase 9 per ADR-0002 — the *substrate* lands here,
the *classifier* trains later from accumulated labels). Map-heavy
automated parsing (§9.4 explicitly defers this to v2; this phase
routes map-heavy pages to Silver Review only).

---

## Step plan (10 steps)

Detail per step gets filled in when the step opens. The skeleton below
locks scope + dependencies + acceptance shape.

### Step 1 — `app/ocr/` module scaffolding + CPU-OCR smoke-bench

**Note**: Step 1 was originally scoped as a separate `georag-ocr`
container per the first revision of ADR-0002. Pre-work on 2026-05-12
surfaced that the §04p libs are already installed in
`georag/fastapi:latest`. ADR-0002 was amended to host the parsers as
an in-process module; this step shrank accordingly.

**Deliverables:**
- New Python package `src/fastapi/app/ocr/` with skeleton modules:
  - `preflight.py` — qpdf/pikepdf preflight (encrypted, repair,
    page count, magic bytes)
  - `profile.py` — PDF profile classification (native / scanned /
    mixed / map-heavy / table-heavy)
  - `parse_native.py` — pdfminer.six + pdfplumber
  - `parse_scanned.py` — PaddleOCR PP-OCRv5 (CPU) + deskew
  - `parse_mixed.py` — Docling layout-first, per-region dispatch
  - `parse_table_heavy.py` — pdfplumber + Docling table focus
  - `render.py` — pypdfium2 page-to-image
  - `quality_graph.py` — placeholder for the Step 6 LangGraph routing
  - `__init__.py` — exports the typed-async function surface
- Each skeleton module: typed async function signature, docstring,
  `NotImplementedError` body, unit-test stub at
  `src/fastapi/tests/test_ocr_<modulename>.py` that asserts the
  function imports (no behaviour assertion yet — that's Steps 3–6).
- **Import-boundary lint**:
  `scripts/phase3_master_plan_step1_import_boundary.sh`. Greps for
  `import app.ocr` or `from app.ocr` and fails if the importer is
  outside the allow-list (`hatchet_workflows/ingest_pdf.py`, the
  `tests/test_ocr_*.py` files, and `app/ocr/` itself).
- **CPU-OCR smoke-bench** at `ops/validation/ocr_cpu_smoke.sh`:
  - Reads three PDFs (one native, one scanned, one mixed) from
    `tests/fixtures/phase3_pdf_corpus_smoke/` (separate from the 50-PDF
    acceptance corpus — these are throwaway latency-measurement
    inputs).
  - Invokes each parser directly via
    `docker exec georag-hatchet-worker-ingestion python -c '...'`
    (no HTTP, no service to call — uses the in-process module).
  - Measures wall-clock per-page latency.
  - Writes JSON report to
    `ops/validation/reports/ocr_cpu_smoke_<timestamp>.json`.
- Smoke-bench fixture seed: existing `src/dagster/tests/fixtures/reports/PLS-2024-Technical-Report.pdf`
  copied to `tests/fixtures/phase3_pdf_corpus_smoke/native_PLS-2024.pdf`;
  plus a synthetic scanned PDF + a synthetic mixed PDF generated by a
  helper script.

**Definition of done:**
- `app/ocr/` package importable from inside the running
  `georag-hatchet-worker-ingestion` container; each parser module
  exports the documented async function signature.
- Import-boundary lint passes (no route handler imports `app.ocr`).
- Smoke-bench measured latency is within 5× the ADR-0002 estimate
  ranges (1–25 sec/page native, 5–150 sec/page scanned). If measured
  latency is >5×, halt Phase 3 and reopen ADR-0002.

**Verifier:** `scripts/phase3_master_plan_step1_verify.sh`.

---

### Step 2 — §9.3 + §9.6 silver tables (migrations)

**Deliverables:**
- Eight Laravel migrations under `database/migrations/` creating:
  - **§9.6 quality tables**:
    - `silver.ocr_page_quality`
    - `silver.document_ingestion_quality`
    - `silver.table_extraction_quality`
    - `silver.parser_run_artifacts`
    - `silver.low_confidence_page_reviews`
  - **§9.3 per-region extraction tables**:
    - `silver.ingest_extractions` — text regions, keyed by
      `(pdf_id, page, region)`, columns per §9.3 provenance contract
      `(pdf_id, page, bbox, source_method, extraction_confidence)`.
    - `silver.ingest_layouts` — Docling layout regions, same key.
    - `silver.ingest_ocr_results` — OCR per-region output, same key.
- Schemas verbatim from master plan §9.3 and §9.6.
- RLS policies: workspace-scoped via `workspace_id` (FK to
  `silver.workspaces` per Phase 0 decision #5).
- Indexes on `(pdf_id, page)` and `(pdf_id, page, region)` per table.
- Raw SQL companion under `database/raw/phase3_master_plan/` for
  RLS + index DDL that doesn't fit cleanly in Laravel migration DSL.

**Definition of done:** all eight tables present in `silver.*`; RLS
enabled and tested with a non-superuser role; migrations idempotent
(roll back + re-run clean).

**Verifier:** `scripts/phase3_master_plan_step2_verify.sh`.

---

### Step 3 — PDF profiler + native parser path

**Deliverables:**
- `app/profile_pdf` endpoint in `georag-ocr` classifies a PDF as
  one of {native, scanned, mixed, map-heavy, table-heavy} based on:
  text-extraction-density per page, image-area-fraction per page,
  detected table count, layout complexity score.
- Native path: pdfminer.six text + pdfplumber tables.
- Writes to `silver.document_passages` (existing) +
  `silver.parser_run_artifacts` + `silver.document_ingestion_quality`.

**Definition of done:** a known-native PDF (NI 43-101 from existing
fixtures) ingests via the new path; resulting chunks have non-null
`bbox` per region; ingestion-quality row shows `parser_used = "native"`.

**Verifier:** `scripts/phase3_master_plan_step3_verify.sh`.

---

### Step 4 — Scanned parser path (PaddleOCR CPU)

**Deliverables:**
- Scanned path: PaddleOCR PP-OCRv5 (CPU, `device=cpu`) preceded by
  deskew preprocessing (Hough-transform-based, threshold configurable).
- Writes per-page `ocr_confidence` + `rotation_applied` +
  `deskew_applied` to `silver.ocr_page_quality`.
- Configurable retry policy: max 2 re-OCR retries with different
  engine settings (binarization threshold, language hint).

**Definition of done:** a known-scanned PDF (added to fixtures in
this step) ingests via the scanned path; `ocr_confidence` populated
for every page; retry_count = 0 or > 0 only when the first pass
scored below threshold.

**Verifier:** `scripts/phase3_master_plan_step4_verify.sh`.

---

### Step 5 — Layout-aware parser (Docling) for mixed and table-heavy

**Deliverables:**
- Mixed path: Docling layout-first, per-region method selection;
  text regions → pdfminer.six, table regions → pdfplumber +
  PP-StructureV3 table cell parse, image regions → rendered for
  optional OCR.
- Table-heavy path: Docling table-region focus + pdfplumber;
  low-confidence tables flagged for Silver Review.
- Writes `layout_confidence` to `ocr_page_quality` and table-level
  rows to `silver.table_extraction_quality`.

**Definition of done:** a mixed-content PDF and a table-heavy PDF
(added to fixtures) ingest via the correct paths; layout_confidence
and table_confidence populated; tables in `silver.tables` have
per-cell provenance.

**Verifier:** `scripts/phase3_master_plan_step5_verify.sh`.

---

### Step 6 — LangGraph OCR Quality Graph + routing

**Deliverables:**
- LangGraph graph at `src/fastapi/app/agent/ocr_quality_graph.py`
  (or equivalent in `georag-ocr` service).
- Routes pages by composite confidence (OCR × layout × table)
  to: re-OCR (max 2 retries, escalating engine settings),
  Silver Review (writes `silver.low_confidence_page_reviews` row),
  or reject with reason (corrupted / password-protected / etc.).
- Map-heavy pages always route to Silver Review (per §9.4
  v1-deferral).

**Definition of done:** synthetic low-confidence pages (added to
fixtures) trigger the correct route; review rows appear with
`reason` populated; rejected PDFs surface in
`silver.document_ingestion_quality.recommended_action = "reject"`.

**Verifier:** `scripts/phase3_master_plan_step6_verify.sh`.

---

### Step 7 — Hatchet `ingest_pdf` cutover + shadow comparison

**Deliverables:**
- Hatchet `ingest_pdf.py` parse step rewritten to invoke `georag-ocr`
  over the internal docker network (replaces the current call to
  `parse_pdf_report()`).
- **Shadow comparison without a new shadow_runs-style table** —
  per Phase 47/48 dead-code post-mortem (the original
  `silver.shadow_runs` sunset left orphan INSERTs that broke ingest
  for weeks). Instead:
  - When the workspace feature flag `shadow_phase3_pdf=on`, the
    Hatchet step runs **both** parsers and writes parser_run_artifacts
    rows for each: one with `parser_used = "p04p"` (the canonical
    parse), one with `parser_used = "ragflow_shadow"` (the shadow
    parse).
  - A view `silver.v_phase3_shadow_diff` joins the two artifact rows
    per `pdf_id` and exposes per-passage / per-table diff metrics.
    The Silver Review UI extension (Step 8) renders this view.
  - When the flag is off, only the canonical `p04p` row writes.
  - At Step 10, the feature flag is removed and the `ragflow_shadow`
    rows age out via a one-time delete; the view is dropped. **No
    table sunset required.** Caller-grep diagnostic from Phase 47
    still applies: before dropping the view, grep for references.
- Feature flag in `workspace.feature_flags` controls the per-workspace
  rollout; default starts at 100% §04p canonical + 100% RAGFlow
  shadow during the acceptance window.
- Sunset of the shadow comparison happens in Step 10 once acceptance
  passes.

**Definition of done:** the existing pre-Phase-3 PDFs ingest through
the new path with zero golden-test regressions; `v_phase3_shadow_diff`
returns rows for every dual-parsed PDF; the diff metrics are
inspectable in the Step 8 UI.

**Verifier:** `scripts/phase3_master_plan_step7_verify.sh`. **Also
re-runs `scripts/phase31_master_sweep.sh`** to confirm the
golden-test 30–31/31 baseline is preserved.

---

### Step 8 — Silver Review UI extension

**Deliverables:**
- New Inertia page at `/admin/ingestion-review` or extension of
  the existing admin surface.
- Queue list: items from `silver.low_confidence_page_reviews`
  ordered by `assigned_to` + `created_at`.
- Item detail: rendered page image from pypdfium2 (cached in
  SeaweedFS), parser used, confidence breakdown
  (OCR / layout / table), reviewer disposition controls
  (accept / re-OCR / reject / annotate).
- Reverb event when reviewer changes disposition.

**Definition of done:** review queue renders with at least 5 test
items; reviewer disposition writes back to
`silver.low_confidence_page_reviews.status` + `resolution_notes`.

**Verifier:** `scripts/phase3_master_plan_step8_verify.sh`.

---

### Step 9 — 50-PDF acceptance corpus + sign-off

**Deliverables:**
- 50 PDFs committed (or LFS-tracked) under
  `tests/fixtures/phase3_pdf_corpus/`.
- Distribution: 10 native + 10 scanned + 10 mixed + 10 table-heavy
  + 10 map-heavy.
- Per-PDF ground-truth label JSON: expected profile classification,
  expected page count, expected `recommended_action`, expected
  Silver Review row count.
- `scripts/phase3_master_plan_acceptance.sh` ingests the corpus
  through the §04p stack and verifies every PDF's actual outcome
  matches its label.

**Definition of done:** 50/50 acceptance passes; deltas (if any) are
documented and either fixed or explicitly grandfathered with a Kyle
sign-off note in the handoff.

**Partial-acceptance protocol** (when <50/50 passes):
- 45–49/50: debug and re-run; do NOT advance to Step 10. Each failing
  PDF gets a debug session and either fixed-in-code or
  grandfathered (with Kyle sign-off + reason logged in the handoff).
- 40–44/50: halt §3 closure. The §04p stack is not ready; reopen
  ADR-0002 to evaluate scope reduction (e.g. PP-StructureV3 disabled,
  Docling layout heuristic simplified) or to revisit GPU OCR.
- <40/50: catastrophic acceptance failure. §04p stack does not
  replace RAGFlow. ADR-0002 superseded by a follow-on ADR documenting
  the failure mode and the path forward (likely: layer §04p quality
  tables on top of RAGFlow, the option-A path originally rejected
  but now warranted by data).

**Verifier:** `scripts/phase3_master_plan_acceptance.sh`.

**SME effort:** labeling 50 PDFs is Kyle's work, not autonomous-loop
work. Budget a labeling session before this step opens. See
`tests/fixtures/phase3_pdf_corpus/LABELING_TRACKER.md` for per-PDF
tracking + the reduce-to-25 fallback option if the time budget is
prohibitive.

---

### Step 10 — RAGFlow retirement + cleanup

**Deliverables:**
- RAGFlow v0.17.2 service removed from `docker-compose.yml`.
- `src/dagster/georag_dagster/parsers/pdf_report.py` moved to
  `src/dagster/georag_dagster/parsers/_archived/pdf_report_v149.py`
  with a one-line header pointing to the §04p replacement.
- `silver.v_phase3_shadow_diff` view dropped via migration after a
  caller-grep diagnostic (per Phase 47 pattern).
- One-time `DELETE FROM silver.parser_run_artifacts WHERE parser_used = 'ragflow_shadow'`
  to clean shadow rows.
- Feature flag `workspace.feature_flags.shadow_phase3_pdf` removed
  from the table + every reading caller.
- Master-plan Phase 3 handoff at `docs/phase3_master_plan_handoff.md`.

**Definition of done:** stack is one parser path (`georag-ocr` only);
grep for `RAGFlow` returns hits only in archived files and
retrospective docs; `phase3_master_plan_acceptance.sh` still passes;
golden test 30–31/31 still passes; caller-grep for the dropped view
returns zero hits outside the migration that drops it.

**Verifier:** `scripts/phase3_master_plan_step10_verify.sh`.

---

## Acceptance tests (the §3 done gate)

```bash
# Aggregate test — runs each step verifier + the corpus acceptance + golden test
bash scripts/phase3_master_plan_acceptance.sh
```

Expected output: every step verifier green, 50/50 corpus passes,
golden-test 30–31/31 holds, RAGFlow grep returns archive-only hits.

---

## Out of scope (explicitly deferred)

- **XGBoost OCR quality classifier** — substrate (labeled training rows)
  accumulates from Step 5 onward; the classifier trains later under
  master-plan §9 / Phase 9 when the 1,000-reviewed-page threshold is
  hit.
- **Automated map-heavy parsing** — §9.4 explicitly defers to v2.
- **Vision-language model for hard pages** — §9.7 explicitly forbids
  adding one. Pages that PP-StructureV3 + Docling cannot parse route
  to Silver Review.
- **Re-ingest workflow for RAGFlow-parsed `silver.reports` rows** —
  defer; new uploads use §04p, existing rows stay as-is until a
  re-ingest sweep is opened in a later master-plan phase.
- **GPU OCR** — deferred per ADR-0002. CPU OCR carries the workload
  for the foreseeable workstation-scale deployment.

---

## Dependencies + risks

- **PaddlePaddle CPU wheel install on Linux/WSL** has known sharp
  edges (protobuf, numpy version pinning). Budget 1–2 doc-phase
  ticks for environment debugging in Step 1.
- **Docling install** pulls `transformers` + `torch` (CPU) → ~2 GB
  Python deps. Image-size budget for `georag-ocr`: target 4 GB,
  alarm at 6 GB.
- **The 50-PDF corpus needs SME labeling time** (Step 9). Cannot be
  done autonomously.
- **Shadow harness in Step 7** doubles ingest cost for the cutover
  window (RAGFlow + §04p both run per upload). Acceptable for the
  acceptance period; sunset in Step 10.
- **Existing `silver.reports` rows** parsed by RAGFlow are not
  re-ingested in this phase — explicit choice per ADR-0002.

---

## How Claude Code should read this document

This is the shape, not the detail. Each step's actual implementation
detail (file paths, function signatures, schema column types, etc.)
gets filled in when the step opens as a doc-phase tick. The autonomous
loop cadence applies (kickoff → steps → verifier → handoff per the
"Autonomous-run cadence" memory).

If a step's definition of done cannot be met because of an upstream
gap or ambiguity (PaddlePaddle wheel won't install, Docling layout
heuristics don't classify a fixture correctly, etc.), **halt and
surface the gap** rather than invent a workaround. The five silver
tables, the LangGraph routing graph, and the Silver Review UI are
non-negotiable deliverables — every workaround that defers them
compromises §9.8 and the master plan's hallucination-prevention story.

Build slow, verify often.
