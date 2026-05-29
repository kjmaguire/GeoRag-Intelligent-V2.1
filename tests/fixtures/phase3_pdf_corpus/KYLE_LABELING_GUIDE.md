# 50-PDF Acceptance Corpus — Labeling Guide for Kyle

**Audience:** Kyle (SME). Doc-phase 67 (2026-05-13) — pre-prepared
during the overnight autonomous run so you can attack the labeling
work efficiently at 8am.

**Goal:** Build a 50-PDF corpus (10 per profile × 5 profiles) labeled
with ground-truth expectations so `scripts/phase3_master_plan_acceptance.sh`
can validate that the §04p stack routes correctly. This is the master-plan
§3 Step 9 gate; without it, Step 10 (RAGFlow retirement) can't proceed.

---

## How the corpus integrates with the §04p stack

When the acceptance script runs, for each `<filename>.pdf` it:

1. Reads sibling `<filename>.label.json` for ground-truth
2. Ingests the PDF through the §04p chain (preflight → profile → dispatch parser → route_page → summarize → persist)
3. Compares actual outcomes against the label JSON:
   - Does the profiler classify the PDF correctly? (`expected_profile` matches `silver.profile_result.document_profile`)
   - Does the doc-level recommendation match? (`expected_recommended_action` matches `silver.document_ingestion_quality.recommended_action`)
   - Is the right number of pages flagged for review? (`expected_silver_review_page_count` matches `COUNT(silver.low_confidence_page_reviews)`)
   - For pages that should be reviewed, does the actual reason match the expected reason?

If all 50/50 pass, §3 acceptance gate clears.

---

## Time budget

| Per-PDF action | Estimated minutes |
|---|---|
| Open PDF, scan visually | 2-3 |
| Confirm profile classification | 1-2 |
| Note page count | <1 |
| Identify problem pages, write `review_page_reasons` entries | 5-15 (only for non-native PDFs that should produce review rows) |
| Write `notes_for_grader` | 1-2 |
| **Per-PDF total** | **~10-25 min** |
| **50-PDF total** | **~8-20 hours** |

Realistic suggestion: target 25 PDFs first (5 per profile), prove out the acceptance script + §04p stack, then expand to 50 if needed for tuning. The doc-phase 49 `LABELING_TRACKER.md` already flags the 25-PDF reduction as an option.

---

## Recommended labeling order

Easiest → hardest by SME effort:

### 1. Native (10 PDFs) — ~15 min each
**Source:** SEDAR+ NI 43-101 filings (modern, post-2020).

Pre-vetted candidate companies + projects:

| Company | Project | URL pattern |
|---|---|---|
| Cameco | McArthur River | sedarplus.ca → Cameco → NI 43-101 |
| NexGen | Arrow Deposit | sedarplus.ca → NexGen → NI 43-101 |
| Denison | Wheeler River | sedarplus.ca → Denison → NI 43-101 |
| IsoEnergy | Larocque East | sedarplus.ca → IsoEnergy → NI 43-101 |
| Fission Uranium | PLS / Triple R | sedarplus.ca → Fission Uranium → NI 43-101 |
| Skyharbour | Russell Lake | sedarplus.ca → Skyharbour → NI 43-101 |
| Northshore Resources | (varies) | sedarplus.ca → Northshore → NI 43-101 |
| Forum Energy | (varies) | sedarplus.ca → Forum → NI 43-101 |

Aim for variety in:
- Commodity (>=6 uranium, >=2 gold, >=2 other — lithium / copper / nickel)
- Filing date (recent = cleaner text layer; older = test the threshold)
- Page count (mix of small <50pp and large >300pp)

Most of these are PUBLIC documents on SEDAR+ — download a few that match your portfolio's commodity mix.

### 2. Table-heavy (10 PDFs) — ~10 min each
**Source:** NI 43-101 Section 14 (Mineral Resource Estimate) excerpts +
drillhole assay appendices.

The fast path: take 10 of the native PDFs from above and EXTRACT just
their resource estimate sections. Tools: any PDF extract utility, or
just label the full PDF as table-heavy if its tables-per-page ratio is
high.

### 3. Map-heavy (10 PDFs) — ~5 min each
**Source:** GSC / SK Geological Survey published maps. Available
free at:

- `geology.gov.sk.ca` → publications → maps
- `geoscan.nrcan.gc.ca` → maps
- Government claim maps from `econ.gov.sk.ca`

These are EASY because the label is almost always:
- `profile: map_heavy`
- `expected_recommended_action: review_all_pages`
- All pages → review with reason `map_heavy_v1_deferral`

### 4. Mixed (10 PDFs) — ~25 min each (most labor)
**Source:** Older NI 43-101 PDFs (2010-2015) with scanned exhibits
embedded. The hardest because you need to identify WHICH pages are
native and which are scanned.

Pre-vetted candidates: older Saskatchewan-area technical reports
from junior explorers that include scanned signature pages or
scanned assay certificates as exhibits.

### 5. Scanned (10 PDFs) — ~15 min each
**Source:** Saskatchewan Geological Survey assessment files pre-1990.
Available at:

- `econ.gov.sk.ca` → Assessment file system → search by mine name / commodity
- Look for old typewritten drill log scans, especially from the 1970s-1980s uranium boom in Athabasca

These usually have:
- `profile: scanned`
- `expected_recommended_action: accept_with_review` (some pages will need review)
- Some pages flagged with reasons like `rotation_undetectable`, `deskew_failed_image_quality`, or `handwriting_unparseable`

---

## Label JSON quick reference

```json
{
  "filename": "example.pdf",
  "profile": "native|scanned|mixed|table_heavy|map_heavy",
  "expected_page_count": 142,
  "expected_recommended_action": "accept|accept_with_review|review_all_pages|reject",
  "expected_silver_review_page_count": 0,
  "review_page_reasons": {
    "12": "ocr_confidence_below_threshold",
    "27": "rotation_undetectable"
  },
  "notes_for_grader": "Free-text — what makes this PDF interesting",
  "labeled_by": "Kyle Maguire",
  "labeled_at": "2026-05-13"
}
```

Full schema with all valid enum values: `_label_schema.json`

---

## Workflow at 8am

1. Make a fresh subdirectory for THIS labeling session:
   `tests/fixtures/phase3_pdf_corpus/_session_2026-05-13/`
2. Drop one PDF into the appropriate profile subdir per the
   "Recommended labeling order" above
3. Open it in your usual PDF viewer
4. Mentally classify (most NI 43-101s are native; scanned ones
   are obviously different)
5. Write the `<filename>.label.json` sibling file
6. Update the markdown checkbox in `LABELING_TRACKER.md` for that profile
7. Commit + push periodically (LFS-track PDFs over 5 MB per the
   tracker's instructions)

After ~5 PDFs per profile (25 total), pause and run:

```bash
bash scripts/phase3_master_plan_acceptance.sh
```

Doc-phase 67 NOTE: this script does NOT exist yet — it's part of
the Step 9 work that needs to be opened. The §04p stack is fully
wired for it; just need to:
1. Create the script that iterates `tests/fixtures/phase3_pdf_corpus/**/*.pdf`
2. For each: call `app.ocr._orchestrator.orchestrate()` directly (skip Hatchet, easier for batch validation)
3. Compare result.document_profile, document_summary.recommended_action, etc. against `*.label.json`
4. Output: 50/50 pass or per-PDF diff

That script is ~150 lines of Python. Worth doing as one of the
first 8am tasks before you start labeling, so you can validate the
first 5 PDFs end-to-end before grinding through the rest.

---

## What's tested already vs what Step 9 still needs

The §04p backend is fully wired (doc-phases 49-66):
- Preflight, profile, parse_native, parse_scanned, parse_mixed, parse_table_heavy, render, quality_graph — all implemented + tested
- Orchestrator chain works (test_ocr_orchestrator.py)
- Persistence layer works (test_ocr_persist_integration.py)
- Render endpoint works (test_ocr_render_endpoint.py)
- Hatchet ingest_pdf wires it in (test_ocr_ingest_helper.py)
- Re-OCR Hatchet workflow ready (doc-phase 63)
- Audit + Reverb on disposition (doc-phase 64)
- Prometheus alerts on §04p failures (doc-phase 65)
- End-to-end smoke test runs through the Hatchet workflow successfully (doc-phase 66)

What's missing for Step 9:
1. The actual corpus PDFs (this guide is to help you build them)
2. The acceptance script (`scripts/phase3_master_plan_acceptance.sh`)
3. SME sign-off on the 50/50 pass result

After Step 9 passes, Step 10 retires RAGFlow.

---

## Profile classifier thresholds — currently conservative

`src/fastapi/app/ocr/profile.py` defines:

```python
NATIVE_TEXT_DENSITY_MIN = 0.005      # chars / sq-pt
SCANNED_TEXT_DENSITY_MAX = 0.0005
TABLE_HEAVY_TABLES_PER_PAGE_MIN = 3
DOC_TABLE_HEAVY_PAGE_FRACTION = 0.5
DOC_SCANNED_PAGE_FRACTION = 0.8
```

These were set against the single PLS-2024 fixture (doc-phase 51).
The 50-PDF corpus is the first real distribution. Expect to tune
these during labeling — when a PDF you confidently labeled as
`native` gets classified as `mixed`, the most likely cause is the
threshold being too tight.

Where to tune: just edit the constants in `profile.py`. Each line
has a docstring noting the corpus-tuning expectation.

---

End of guide. Good luck — the path is clear, the §04p stack is
ready, the only remaining work is your SME labeling time.
