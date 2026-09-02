# ADR 0019: Cohere Parse v5 replaces Azure Document Intelligence as the OCR engine

- **Date**: 2026-09-02
- **Status**: Proposed
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: the "Azure Document Intelligence" row and dispatch tree in `georag-architecture.html` §04p; the Document Intelligence notes in ADR-0005 (§ "Billing" and the tiling paragraph)

## Context

Since 2026-07-28 (#28) the §04p PDF stack has used **Azure Document
Intelligence** (`prebuilt-layout`) as the primary OCR engine for scanned
pages, with Tesseract as the last-resort fallback. It worked, and it was the
only Cognitive Services account left outside the Azure AI Foundry resource:
its own endpoint and key (`georag-document-intel-cc`), its own SDK
(`azure-ai-documentintelligence` + `azure-core`), its own three alert
rules, an async-SDK-on-a-background-loop adapter, and a per-page billing
model that drove a great deal of code — pikepdf page slicing so a 40 MB
report was not re-uploaded per page, block batching (2026-08-20) and sparse
selection batching (2026-08-23) to cut round-trips, a per-document page
budget with refunds (2026-08-14/23), and lossless raster tiling under the
service's 10,000 px dimension cap with polygon remapping and seam
deduplication (`image_tiling.py`).

On 2026-08-27 Cohere released **Parse v5** (`parse-v5.0`), a 2.3B
vision-language document parser that returns reading-order Markdown per
page, tables as HTML with bounding boxes, and image descriptions. It is in
the Foundry catalog as `Cohere-parse-v5` — served from the **same Foundry
resource and credentials** GeoRAG already uses for Command A+, Embed v4 and
Rerank v4 — at roughly $1.50 per 1,000 pages on Cohere's own API (Foundry
Preview pricing unverified). Cohere's ParseBench puts it ahead of Document
Intelligence on tables and content faithfulness (79.2 vs 69.3).

The decision was prompted by wanting one vendor surface for every model
call, one credential to rotate, one account to alert on, and a parser that
emits structured Markdown and HTML tables natively rather than a word
stream that the adapter reassembled into lines.

## Options considered

| Option | License / SKU | Effort | Outcome |
|---|---|---|---|
| A. Keep Document Intelligence | Azure Cognitive Services, GA | None | Rejected — separate account, SDK and alerts; word-stream output; the tiling and slicing machinery exists only to work around its limits. Its strengths are real: per-word confidence and polygons, a GA SKU, and a proven 10,000 px path for plan sheets. |
| B. Document Intelligence primary, Parse as a fallback | both | Medium | Rejected — two paid engines on one ladder doubles the cost surface and keeps every DI-specific workaround alive; nothing would ever exercise the fallback. |
| C. **Parse only, Tesseract floor** | **Foundry `Cohere-parse-v5` (Preview)** ✅ | Medium | Chosen — see Decision. |
| D. Self-hosted PaddleOCR 3.x (ADR-0016) | Apache-2.0 | High | Rejected — GPU residency on the ingest worker was the original OOM driver (ADR-0018); the corpus is a fraction of a page per second and does not justify a resident model. |

## Decision

`OCR_ENGINE=cohere_parse` selects `app/services/ingest/cohere_parse_client.py`,
which calls `POST {AZURE_FOUNDRY_ENDPOINT}/providers/cohere/v2/parse` with
the shared `api-key` and `AZURE_FOUNDRY_PARSE_DEPLOYMENT` (`Cohere-parse-v5`),
one rendered page image per request. Azure Document Intelligence is
**removed**, not kept as a fallback; Tesseract remains the floor.

### What stays the same

- The parser's OCR ladder shape: `_ocr_single_page` per page, sparse page
  grouping on the mixed path, block grouping on the whole-document path,
  per-document page budget with refunds, `Table (OCR, page N, #k)` sections
  from engine table grids, the multi-signal quality router and
  `silver.review_queue`, `INSERT_PASSAGE_SQL`, the Qdrant payload.
- Tesseract's role, its per-word confidence, and every Tesseract test.
- `AZURE_FOUNDRY_ENDPOINT` / `AZURE_FOUNDRY_API_KEY` — the same resource.
- The Foundry `ClientErrors` / `ServerErrors` alert rules, which now cover
  OCR as well as the answer path.

### What changed

- Adapter: `document_intelligence_client.py` (Azure aio SDK on a background
  loop) → `cohere_parse_client.py` (sync httpx + `with_foundry_retry`,
  pypdfium2 rendering under `COHERE_PARSE_MAX_PIXELS`, thread-pool fan-out
  bounded by `PDF_OCR_PAGE_CONCURRENCY`). `PageOcrResult` moved to
  `ocr_types.py` and gained `confidence_reported`.
- New `html_table.py` (HTML table → row-major grid, spans propagated —
  the same fill rule the DI adapter used on its `cells` collection) and
  `ocr_engine.py` (the retired `azure_document_intelligence` value logs
  CRITICAL and runs Tesseract instead of silently meaning Tesseract).
- Deleted: `image_tiling.py` and the tiled rung of the ladder (Parse
  returns no word polygons); pikepdf page slicing (the engine renders from
  the file path); `azure-ai-documentintelligence` and `azure-core` from
  the service's dependencies. `trusted_image.py` keeps the Pillow
  pixel-limit helpers the TIFF normaliser needs.
- Provenance: `ocr_method='cohere_parse'`, `parser_used='ocr_cohere_parse'`,
  `ocr_confidence` **NULL** for Parse pages (NULL now means "no engine
  confidence"; `ocr_method` is the discriminator). `PARSER_VERSION` 2.1.0.
  Migration `2026_09_02_070000_add_cohere_parse_to_ocr_method_check`
  admits the label; `document_intelligence` stays for historical rows.
- Quality router: `OcrQualitySignals.confidence_reported`; the
  confidence bands are skipped for engines that report none; new
  per-engine `floor_tier` (`spot_check` | `mandatory_review`) because no
  threshold value can keep a clean no-confidence page out of auto-accept.
  Compose, `.env.example` and `.env.production.example` all ship
  `by_ocr_method.cohere_parse.floor_tier = spot_check`.
- Routing decision `spot_check` is now distinct from `review_required`
  (`OcrQualityAssessment.review_queue_routing_decision`): a spot-check page
  gets a `silver.review_queue` row (queued for a human sample, enum value
  `review_required`, `payload.ocr_quality_tier = spot_check`) but its
  passages stay `ocr_status = 'accepted'`. Only `review_required` pages
  demote. Without this split the floor would have stamped every scanned
  passage `low_confidence`, prefixed it with the "do not quote numbers"
  banner, and ranked every scanned assay table below every born-digital
  paragraph before top-K truncation — a retrieval regression, not a
  quality gate. **SME decision point**: this also lifts tesseract
  spot-check pages (0.70–0.85 mean under the shipped bands) out of
  demotion, which the `.env.example` note already called the inverse of
  intent.
- Section-level rule (`pdf_report._assign_ocr_metadata`): `ocr_method` is
  first-page-wins, `ocr_confidence` is min-over-pages — and a section whose
  winning method reports no confidence (`cohere_parse`) persists NULL even
  when a tesseract page in its span carries a number, so `ocr_method`
  stays the discriminator.
- `COMMENT ON COLUMN` for `ocr_confidence` and `ocr_method` updated in the
  same migration; `georag_ocr_pages_total{engine}` is incremented by the
  tesseract rungs too, so it can answer "did the corpus silently downgrade".
- Env: `OCR_ENGINE=cohere_parse`, `AZURE_FOUNDRY_PARSE_DEPLOYMENT`,
  `COHERE_PARSE_TIMEOUT_S`, `COHERE_PARSE_MAX_PIXELS`,
  `COHERE_PARSE_OUTPUT_FORMAT`, `COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS`,
  `OCR_PAGES_PER_BATCH` (was `AZURE_DI_PAGES_PER_BATCH`; now "pages per
  concurrent group"), `OCR_MAX_PAGES_PER_DOC` (was
  `AZURE_DI_MAX_PAGES_PER_DOC`, still honoured with a warning). Removed:
  `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT/_KEY`, `AZURE_DI_MODEL_ID`,
  `AZURE_DI_OUTPUT_MARKDOWN`, `AZURE_DI_OCR_HIGH_RESOLUTION`.
- Metric: `georag_di_ocr_pages_total` → `georag_ocr_pages_total{engine}`.
- Alerts: the three `georag-document-intel-cc-*` rules are retired from
  `deploy/azure/alerts/create-alerts.sh`.

## Migration mechanics (for future reference)

1. Run `ops/validation/cohere_parse_probe.sh` with live Foundry credentials
   and set `_PARSE_PATH`, `COHERE_PARSE_MAX_PIXELS` and the response
   adapter in `cohere_parse_client.py` from its report; commit scrubbed
   responses over the synthetic fixtures in
   `src/fastapi/tests/fixtures/cohere_parse/`. **The client's wire shape is
   written from the published API description and has not yet been
   verified live.**
2. Deploy the Laravel migration (CD runs `artisan migrate` before the
   image rolls, so the CHECK admits `cohere_parse` first).
3. On `hatchet-worker`: set `OCR_ENGINE=cohere_parse` and
   `AZURE_FOUNDRY_PARSE_DEPLOYMENT`; remove the `docintel-endpoint` /
   `docintel-key` secret references (dead). A worker left on the old value
   logs one CRITICAL line and runs Tesseract for every page.
4. Roll the image. Watch `georag_ocr_pages_total{engine="cohere_parse"}`,
   the Foundry `ClientErrors` alert (Parse adds per-page volume to the
   shared TPM), and `ocr_method` on new `silver.document_passages` rows.
5. Point of no return: none for data — DI-era rows keep their label and
   confidence. Decommission `georag-document-intel-cc` and delete its three
   alert rules by hand once no run has touched it for a fortnight.
6. Re-ingest is NOT required for existing documents; new scans use Parse.

## Gotchas hit during the migration (worth knowing for next time)

1. `tiff_to_pdf.py` imported the Pillow pixel-limit helpers from
   `image_tiling.py`; tiling could not simply be deleted.
2. `OCR_ENGINE` was a bare string compare with a Tesseract default, so a
   worker env still carrying the old value would have downgraded the whole
   corpus silently. `ocr_engine.selected_engine()` makes it CRITICAL.
3. `silver.review_queue.confidence_record` is `numeric(4,3) NOT NULL`; Parse
   review rows write 0.0 there and `confidence_per_field.confidence_reported
   = false` says why.
4. pypdfium2 is not thread-safe: pages are rendered under a lock, then
   posted concurrently.
5. `test_ocr_escalation_on_quality.py` pinned the ladder's *source text*;
   it was rewritten for two rungs rather than patched.
6. The Foundry key is shared with the embedder, so "Foundry is configured"
   is not consent to run Parse — the group pass checks the engine is
   selected AND configured.

## Consequences

### Positive

- One vendor surface, one credential, one account to alert on; one fewer
  SDK and one fewer Cognitive Services resource.
- Reading-order Markdown and HTML tables natively; tables keep their
  spans through `html_table_to_grid`.
- Roughly 700 lines of DI-specific workaround (slicing, tiling, the
  background event loop, positional remaps) deleted.

### Negative

- **No per-word confidence.** The review router judges Parse pages on
  content signals only; `floor_tier` keeps them in spot-check until a
  calibration artefact exists (hand-label ~200 pages across the corpus).
- **Oversized plan sheets are downscaled, not tiled.** An A0 sheet at the
  4 MP default renders at ~55 DPI and loses small annotation text. A
  text-only tile-and-merge (no polygons needed) is the follow-up if the
  `downscaled` warning count is material on the corpus.
- **Preview SKU.** Foundry lists `Cohere-parse-v5` as Preview with a
  retirement date of 2026-12-15; the deployment name is an env var and must
  be re-checked before then.
- **Shared quota.** Parse's per-page volume shares Foundry TPM with
  embed/rerank/LLM; `OCR_PAGES_PER_BATCH` and `PDF_OCR_PAGE_CONCURRENCY`
  are the throttles. The Foundry `ClientErrors > 50 / 15m` alert was tuned
  for the answer path and can fire on a retried-and-recovered 429 storm
  during a large scanned ingest (recorded in the on-call runbook).
- **Memory profile.** Each in-flight request holds one page PNG (rendered
  inside the worker, under a lock); the resident set is bounded by
  `PDF_OCR_PAGE_CONCURRENCY`, not `OCR_PAGES_PER_BATCH`. A 4 MP lossless
  PNG is ~5–15 MB, so the default of 4 in flight is well inside the
  worker's budget (ADR-0018).
- **The partial index `idx_document_passages_low_ocr_confidence`**
  (`WHERE ocr_confidence IS NOT NULL AND ocr_confidence < 0.75`) is blind
  to every Parse page by construction; the Phase 6 OCR Quality Agent must
  key on `ocr_status` / `ocr_method` instead (follow-up below).

## Verification (this commit)

- FastAPI: the CI unit filter passes; new/ported tests —
  `test_cohere_parse_client.py`, `test_html_table.py`, `test_ocr_engine.py`,
  `test_cohere_parse_page_groups.py`, `test_cohere_parse_pixel_cap.py`,
  `test_ocr_page_budget.py`, `test_ocr_whole_doc_parallel.py`,
  `test_trusted_image.py`, rewritten `test_ocr_escalation_on_quality.py`.
- `scripts/check_pyproject_covers_imports.py`,
  `check_settings_have_readers.py`, `check_fastapi_lock_export.py`,
  `check_silent_exception_handlers.py` all pass; the silent-handler
  baseline was regenerated (it was stale against HEAD).
- Laravel: `php -l` locally; the CI `Laravel (Pint + PHPUnit)` and
  `Migrations under production privileges` jobs passed on the PR head.
- Senior-reviewer checkpoint run 2026-09-02 (this PR); its blocking and
  important findings are folded in above.
- Live (needs credentials): the probe (step 1 above), then
  `ops/validation/ocr_cpu_smoke.sh` with `OCR_ENGINE=cohere_parse`, then a
  re-ingest of `src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf`
  through Hatchet — short pages carry `ocr_method='cohere_parse'` with
  `ocr_confidence IS NULL`; a scanned variant yields
  `parser_used='ocr_cohere_parse'` and `Table (OCR, page N, #k)` sections.

## Follow-ups (NOT part of this ADR; tracked separately)

- Set `calibrated_from` for a `cohere_parse` band after hand-labelling
  ~200 Parse pages; then drop `floor_tier` — trigger: first fortnight of
  live Parse ingest complete.
- Feed Parse image blocks into `ReportParseResult.figure_manifest` (the
  persist branch that consumes it has been a working no-op since docling
  was removed) — trigger: after the probe confirms the block shape.
- Text-only tile-and-merge for A0 plan sheets — trigger: `downscaled`
  warnings material on the corpus.
- Make `silver.review_queue.confidence_record` nullable — trigger: the
  review UI shows "0.000" for Parse pages and confuses reviewers.
- Re-key `idx_document_passages_low_ocr_confidence` (or the Phase 6 OCR
  Quality Agent's query) on `ocr_status`/`ocr_method` — trigger: the
  agent's first run against a Parse-ingested corpus.
- Re-run the §04i golden-query and hallucination-failure sets against a
  corpus whose scanned passages carry `ocr_confidence IS NULL` and
  `ocr_method = 'cohere_parse'` — trigger: before Status: Accepted.
- Tighten the `ocr_method` CHECK to drop `document_intelligence` with the
  non-fatal pattern — trigger: no row carries it.
- Re-check the `Cohere-parse-v5` SKU — trigger: before 2026-12-15.
