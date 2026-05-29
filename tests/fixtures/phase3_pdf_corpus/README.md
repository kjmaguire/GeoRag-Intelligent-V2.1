# Master-Plan Phase 3 — 50-PDF Acceptance Corpus

This corpus gates master-plan §3 (§04p PDF stack + OCR quality) sign-off.
Per `docs/phase3_master_plan_kickoff.md` Step 9, the §04p stack passes
acceptance only when 50/50 PDFs in this corpus ingest with the
ground-truth routing decisions documented in each `*.label.json`.

## Profile distribution

| Profile | Count | Subdirectory | What it tests |
|---|---|---|---|
| Native | 10 | `native/` | pdfminer.six text + pdfplumber tables; no OCR invoked; clean text layer |
| Scanned | 10 | `scanned/` | PaddleOCR PP-OCRv5 (CPU) + deskew; no text layer at all |
| Mixed | 10 | `mixed/` | Docling layout-first; per-region method selection; some pages native, some scanned |
| Table-heavy | 10 | `table_heavy/` | pdfplumber + Docling table focus; assay tables, drillhole summaries, resource estimates |
| Map-heavy | 10 | `map_heavy/` | always routes to Silver Review per §9.4 v1 deferral; not parsed |

## How to label a PDF

For each PDF added to a subdirectory, create a sibling
`<filename>.label.json` matching `_label_schema.json` shape:

```json
{
  "filename": "PLS-2024-technical-report.pdf",
  "profile": "native",
  "expected_page_count": 142,
  "expected_recommended_action": "accept",
  "expected_silver_review_page_count": 0,
  "notes_for_grader": "Clean native NI 43-101; 17-section TOC; 12 resource estimate tables on pp. 89-104.",
  "labeled_by": "Kyle Maguire",
  "labeled_at": "2026-MM-DD"
}
```

For PDFs that should partially-fail and produce review queue items:

```json
{
  "filename": "scan_drill_log_1972.pdf",
  "profile": "scanned",
  "expected_page_count": 38,
  "expected_recommended_action": "accept_with_review",
  "expected_silver_review_page_count": 6,
  "review_page_reasons": {
    "12": "ocr_confidence_below_threshold",
    "13": "ocr_confidence_below_threshold",
    "27": "rotation_undetectable",
    "28": "rotation_undetectable",
    "31": "deskew_failed_image_quality",
    "32": "deskew_failed_image_quality"
  },
  "notes_for_grader": "1972 hand-typed drill log, microfilmed scan. Pages 12-13 have water damage smudging; pages 27-28 are sideways scanned; pages 31-32 are off-axis by ~15°.",
  "labeled_by": "Kyle Maguire",
  "labeled_at": "2026-MM-DD"
}
```

For map-heavy PDFs (always route to review per v1 deferral):

```json
{
  "filename": "athabasca_basement_geology_map_1985.pdf",
  "profile": "map_heavy",
  "expected_page_count": 8,
  "expected_recommended_action": "review_all_pages",
  "expected_silver_review_page_count": 8,
  "notes_for_grader": "GSC 1985 Athabasca basement geology map — fold-out plates only, no text content. Every page should route to review.",
  "labeled_by": "Kyle Maguire",
  "labeled_at": "2026-MM-DD"
}
```

See `_label_schema.json` for the full schema with all optional fields.

## Sourcing candidate PDFs

Where to find PDFs that cover each profile:

- **Native (10)**: SEDAR+ filings of NI 43-101 reports from recent (2020+) issuers. Clean text layer, modern PDF authoring. Examples: Cameco, NexGen, Denison, IsoEnergy, Fission, Skyharbour technical reports. Aim for variety in commodity (uranium primary, gold, lithium secondary).
- **Scanned (10)**: Saskatchewan Geological Survey assessment file scans pre-1990. SMDI / SMAD / claim assessment archives. Older the better for OCR difficulty. Variations: typewritten, hand-printed, mixed handwriting + typewriting.
- **Mixed (10)**: Older NI 43-101s (2010-2015) with scanned exhibits embedded. Some assay certs scanned as image pages inside an otherwise native document. Property option agreements with scanned signature pages.
- **Table-heavy (10)**: Resource estimate technical reports with 20+ pages of grade-tonnage tables. NI 43-101 Section 14 only excerpts work well. Drillhole assay summary appendices.
- **Map-heavy (10)**: GSC / Saskatchewan Geological Survey published maps. Government claim maps. NI 43-101 figure-only appendices. Fold-out plates extracted from technical reports.

## Storage

PDFs >5 MB should be Git LFS-tracked. Configure LFS pattern in
`.gitattributes` if not already present:

```
tests/fixtures/phase3_pdf_corpus/**/*.pdf filter=lfs diff=lfs merge=lfs
```

Total corpus size estimate: 50 PDFs × ~10 MB average = ~500 MB.

## Labeling burden (SME time)

Honest estimate: 20-40 minutes per PDF to:
- Open the PDF and confirm profile classification
- Page-count, identify problem pages, write `review_page_reasons` entries
- Write `notes_for_grader`

50 PDFs × 30 min = 25 hours of Kyle's SME time. Realistic options:
- Reduce corpus to 25 PDFs (5 per profile) for v1 acceptance; expand later as the §9.8 classifier training corpus grows.
- Recruit one of the working geologists (Phase 3 master plan §35.2 mentions 2-3 advisors) for parallel labeling on the scanned + table-heavy subsets.
- Split across multiple SME sessions over weeks.

The kickoff (Step 9) is the gate; this README is the labeling spec.

## What "acceptance" means

`scripts/phase3_master_plan_acceptance.sh` ingests every PDF through
the §04p stack and verifies for each:

1. Detected profile matches `expected_profile`
2. Page count matches `expected_page_count`
3. `silver.document_ingestion_quality.recommended_action` matches `expected_recommended_action`
4. Count of `silver.low_confidence_page_reviews` rows for that pdf_id matches `expected_silver_review_page_count`
5. For each page in `review_page_reasons`, the actual review row's `reason` matches

50/50 PDFs passing all five checks = §3 done. Partial pass triggers
the Step 9 partial-acceptance protocol (RAGFlow held until failures
are debugged or grandfathered).
