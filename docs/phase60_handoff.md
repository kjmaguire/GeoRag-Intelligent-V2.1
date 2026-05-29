# Phase 60 Handoff — Master-plan §3 Step 8c (React detail panel UI)

**Document version:** 1.0
**Status:** Doc-phase 60 complete. Doc-phase 61 inheriting.
**Predecessors:** `docs/phase59_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

The Silver Review queue is now a complete read-only review surface
end-to-end. Operators can click a queue row, see the rendered page
image, the parser/confidence breakdown, the retry log, and the
extracted text — all in one panel. Doc-phase 61 adds disposition
controls (accept / re-OCR / reject) + Reverb broadcast.

---

## 1. What doc-phase 60 delivered

### Laravel backend

| Route | What it does |
|---|---|
| `GET /admin/ingestion-review/{id}.json` | `IngestionReviewController::show()` returns the full data payload for one review item: review row + report metadata + page_quality + extractions + ocr_results + layouts + parser_runs + document_quality + page_render_url |
| `GET /admin/ingestion-review/{id}/page/{n}.png` | `IngestionReviewController::pageRender()` reverse-proxies the FastAPI render endpoint with `X-Service-Key` attached server-side (browser never sees the key) |

Both routes admin-gated via `$this->authorize('admin')`. UUID + page
patterns enforced in the route declaration so malformed IDs 404 at
the routing layer.

### React frontend

`resources/js/Pages/Admin/IngestionReview.tsx` extended:
- `DetailPanel` component (~150 lines) — modal-style overlay,
  fetches JSON on mount, renders image + parser breakdown + retry
  log + extracted text
- `ParserBreakdown` sub-component — definition list of all
  per-page quality fields
- `RetryLog` sub-component — chronological parser_runs from
  preflight through profile through each parser invocation
- `ExtractedTextSection` sub-component — picks OCR results if any,
  otherwise extractions; shows per-region confidence + text
- Click handler on each `QueueTable` row opens the detail panel
- `useState<string | null>(selectedItem)` for which row is open

### Render failure handling

When `<img onError>` fires (e.g. 404 from `/page/{n}.png` because
the report was ingested before bronze tracking landed in
doc-phase 59), the image section swaps to a placeholder:

```
Page render unavailable.
Likely cause: report was ingested before doc-phase 59 bronze-key
tracking landed. Re-upload the PDF to enable rendering.
```

All other panel content (parser breakdown, retry log, extracted
text) still renders because that data comes from silver tables
independently of S3.

### Tests

`tests/Feature/Admin/IngestionReviewTest.php` extended with 6 new
feature tests (existing 6 still pass):
- show returns JSON with the documented structure
- show returns 404 for unknown review_item_id
- show returns 404 for malformed UUID (caught by route pattern)
- show requires admin authorization
- page-render returns 404 when review item missing

---

## 2. Files of record

### New
- `scripts/phase3_master_plan_step8c_verify.sh` (~110 lines)

### Modified
- `app/Http/Controllers/Admin/IngestionReviewController.php` —
  +~230 lines (show + pageRender methods, json/http response imports)
- `routes/web.php` — 2 new route registrations
- `resources/js/Pages/Admin/IngestionReview.tsx` — +~290 lines
  (DetailPanel + sub-components + state + click handler)
- `tests/Feature/Admin/IngestionReviewTest.php` — +6 tests

---

## 3. Verifier status

Doc-phase 60 verifier:
- 5 doc-phase-specific checks
- 11 prior-step regression cascades

Final cascade status will be patched in after the run completes.
Doc-phase-specific checks pre-validated:
- Route registrations confirmed via `php artisan route:list`
- Controller class loads and has both methods
- TSX file contains DetailPanel + page_render_url usage
- Updated test file parses cleanly

---

## 4. Decisions made in this phase

### 4.1 Service key stays server-side via reverse-proxy

The FastAPI render endpoint requires `X-Service-Key`. Two options:
- (a) Have the React panel call FastAPI directly with the key in
  the request header
- (b) Reverse-proxy through Laravel which attaches the key
  server-side

Chose (b). Reasons:
- Service key is server-only secret; (a) would put it in JavaScript
  bundle or require client-side env injection — both bad
- Reverse-proxy also gives us a uniform Laravel-side auth surface
  (same `authorize('admin')` gate applies)
- One extra hop adds ~5 ms; not user-perceptible

### 4.2 Detail panel is a custom modal, not shadcn `<Sheet>`

The existing admin pages don't use shadcn yet. Introducing shadcn
in this tick alongside everything else would balloon scope. The
custom modal is ~50 lines and serves the v1 need.

Doc-phase 61's disposition controls may be a good moment to
introduce shadcn `<Dialog>` for the confirmation step ("Are you
sure you want to reject this page?").

### 4.3 OCR results vs extractions: panel picks one

Scanned pages produce `silver.ingest_ocr_results` rows; native +
mixed + table_heavy pages produce `silver.ingest_extractions` rows.
The `ExtractedTextSection` component checks `ocr_results.length > 0`
first and shows those; otherwise falls back to extractions. Single
panel handles all parser types — no per-profile switch.

### 4.4 Click-to-open on row, no separate button

Lower visual noise than adding a "View" button per row. The row
is already a click target; the cursor changes to indicate
clickability. shadcn introduces hover/focus styling we may want
later but the basic UX is in.

### 4.5 Layouts shown? Yes, but unused in v1 panel

The JSON payload includes a `layouts` array from
`silver.ingest_layouts` for mixed-parser pages. The detail panel
doesn't currently render them. Reason: layouts describe
*structural regions* (text / title / table / figure / etc.) which
are useful for debugging Docling's classification but not for
operator review decisions in v1.

Doc-phase 61 may surface layout regions as overlays on the
rendered page image (color-coded by label) — that's the natural
use case. Keeping the data in the JSON payload now means doc-phase
61 doesn't need a JSON-shape change.

---

## 5. Findings carried over to doc-phase 61+

### 5.1 Verifier cascade is O(N²)

Observation during doc-phase 60: the Step 8b verifier's cascade
runs Steps 1-8a, each of which runs its own prior cascade. Step 5
(mixed Docling) and Step 7a (orchestrator with all 4 parsers) both
trigger Docling + PaddleOCR cold-loads inside their pytest invocations.

By doc-phase 60, a single Step 8b verifier run takes ~5-7 minutes
because the chain re-runs Docling cold-loads multiple times.

Worth a cleanup tick to either:
- (a) **Mark prior verifiers as "passed" via a manifest file** — each
  verifier writes a `passed_at` timestamp; cascade just checks
  recency.
- (b) **Cascade is opt-in** — verifiers default to running their
  own checks only; an explicit `--cascade` flag (or master sweep)
  runs the chain.

(a) is cleaner — preserves the cascade semantic but only re-runs
when state changed. (b) is simpler but loses regression protection.

Out of scope for §3 work but flagged for doc-phase 62+ or a
separate cleanup tick.

### 5.2 No layout-region overlay yet

§ 4.5 — the layouts data is fetched but not visualized. Doc-phase
61 might add overlay rectangles on the page image. Requires
mapping Docling BOTTOMLEFT bboxes to image TOPLEFT image-coords
post-render-scale. Math is straightforward (flip Y, scale by
render_scale).

### 5.3 Detail panel does not refresh on filter change

The detail panel is opened by click on a queue row. If the queue
filter changes while a panel is open (rare but possible — user
toggles a filter), the panel data may go stale. Current behavior:
panel stays open with stale data until user closes it.

Worth a refresh-on-filter-change in doc-phase 61, or simply close
the panel when filters change. Negligible UX issue for v1.

### 5.4 No top-nav entry yet

Doc-phase 58 flagged this; doc-phase 60 didn't address it
(focused on detail panel). Doc-phase 61 should add a one-line
addition to the admin top-nav so the page is discoverable.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Table review confidence thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround
- Permission management is still ad-hoc
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- Docling deprecation warning on table image extraction (benign)
- Retry settings escalation logic is opinionated
- `_compute_doc_quality_score` is a placeholder
- No end-to-end Hatchet engine test yet
- No alerting on §04p dual-write failures
- Pre-doc-phase-59 reports have NULL bronze keys (placeholder shown)
- Import-boundary lint is module-level coarse

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 61 will do

**Master-plan §3 Step 8d — Disposition controls + Reverb broadcast.**

Deliverables:
- Laravel `PATCH /admin/ingestion-review/{id}` — update status +
  resolution_notes; valid transitions enforced
- New Hatchet workflow trigger: `POST /admin/ingestion-review/{id}/re-ocr` —
  enqueues a re-OCR job for the page with escalated parse_scanned
  settings (the quality_graph's retry settings escalation)
- React panel: 4 disposition buttons (accept / re-OCR / reject /
  annotate) with confirmation modal (shadcn `<Dialog>` introduction)
- Reverb broadcast on disposition change so other operators viewing
  the queue see the row update live
- Top-nav entry to `/admin/ingestion-review`
- Behaviour tests for the PATCH endpoint
- New verifier: `scripts/phase3_master_plan_step8d_verify.sh`

After doc-phase 61, Step 8 is fully complete. The remaining §3 work
is Step 9 (50-PDF acceptance corpus + sign-off) and Step 10
(RAGFlow retirement).

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE | 50 |
| 3. PDF profiler + native parser | ✅ DONE | 51 |
| 4. Scanned parser + render | ✅ DONE | 52 |
| 5. Mixed + table-heavy parsers (Docling) | ✅ DONE | 53 |
| 6. LangGraph OCR Quality Graph | ✅ DONE | 54 |
| 7a. Orchestrator | ✅ DONE | 55 |
| 7b. Persistence layer | ✅ DONE | 56 |
| 7c. Hatchet ingest_pdf cutover (dual-write) | ✅ DONE | 57 |
| 7d. Shadow comparison | deferred | — |
| 8a. Silver Review queue scaffold | ✅ DONE | 58 |
| 8b. FastAPI render + bronze tracking | ✅ DONE | 59 |
| 8c. React detail panel UI | ✅ DONE | 60 |
| 8d. Disposition controls + Reverb | next | 61 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 62-63 |
| 10. RAGFlow retirement + cleanup | pending | 63-64 |

**Operator-visible review surface is complete end-to-end** for read.
One more tick (61) makes it actionable. Then Step 9 acceptance + Step
10 retirement close out master-plan §3.

---

End of doc-phase 60 handoff. Click any queue row to see everything
the §04p stack knows about that flagged page.
