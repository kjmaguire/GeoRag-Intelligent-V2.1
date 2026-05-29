# Phase 58 Handoff — Master-plan §3 Step 8a (Silver Review queue scaffold)

**Document version:** 1.0
**Status:** Doc-phase 58 complete. Doc-phase 59 inheriting.
**Predecessors:** `docs/phase57_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

First frontend tick in master-plan §3. Step 8 (Silver Review UI) is
being split across multiple doc-phase ticks because the full feature
set (queue + rendered page thumbnails + disposition controls + Reverb
broadcast) is materially bigger than one tick.

Doc-phase 58 (this) ships the read-only queue list scaffold.
Doc-phases 59-60 will add detail panel + disposition controls.

---

## 1. What doc-phase 58 delivered

| Artifact | Purpose |
|---|---|
| `App\Http\Controllers\Admin\IngestionReviewController` | Read-only Laravel controller; index() fetches `silver.low_confidence_page_reviews` joined to `silver.reports` + `silver.ocr_page_quality`, returns 200-row queue + summary counts |
| Route: `GET /admin/ingestion-review` (name: `admin.ingestion-review`) | Registered under the existing `auth:sanctum` group in `routes/web.php` |
| `resources/js/Pages/Admin/IngestionReview.tsx` | Inertia v3 + React 19 page: summary panel (pending count + 24h delta + top reason) + filter bar (status / reason / workspace) + queue table with status pill + confidence cell + report info |
| `tests/Feature/Admin/IngestionReviewTest.php` | 6 feature tests: guest 302 / non-admin 403 / admin empty queue / seeded row visible / status filter narrows / invalid status rejects |
| `scripts/phase3_master_plan_step8a_verify.sh` | 4 doc-phase-specific checks + cascade through all 9 prior step verifiers |

### Queue display columns

| Column | Source |
|---|---|
| Report title | `silver.reports.title` (via JOIN; falls back to `(untitled)` if NULL) |
| Page | `silver.low_confidence_page_reviews.page` |
| Reason | `silver.low_confidence_page_reviews.reason` (CHECK enum) |
| Status | `silver.low_confidence_page_reviews.status` (CHECK enum) — pill color-coded |
| Parser | `silver.ocr_page_quality.parser_used` (via JOIN) |
| OCR conf | `silver.ocr_page_quality.ocr_confidence` (color: green ≥85%, amber 50-85%, red <50%) |
| Layout conf | `silver.ocr_page_quality.layout_confidence` |
| Retries | `silver.ocr_page_quality.retry_count` |
| Created | `silver.low_confidence_page_reviews.created_at` |

Ordering: pending first, then assigned, then in_review, then resolved; secondary sort by created_at desc. Limit 200 rows.

### Filters (Inertia query-string driven)

- `?workspace_id=<uuid>` — filter to one workspace
- `?status=<enum>` — narrow to a specific status
- `?reason=<enum>` — narrow to a specific routing reason

Filter changes trigger a fresh `router.get()` round-trip (preserveState=false) so the summary panel + queue table re-render together.

### Auth model

- Auth gate: `$this->authorize('admin')` (matches every other `/admin/*` controller)
- Admin views read across all workspaces — the silver RLS policies have an explicit "GUC unset ⇒ all rows visible" branch, so no GUC needs to be set in the admin controller (same pattern as `ShadowRunsController` was, `WorkflowRunController` is, etc.)

---

## 2. Files of record

### New
- `app/Http/Controllers/Admin/IngestionReviewController.php` (~190 lines)
- `resources/js/Pages/Admin/IngestionReview.tsx` (~271 lines)
- `tests/Feature/Admin/IngestionReviewTest.php` (~135 lines, 6 tests)
- `scripts/phase3_master_plan_step8a_verify.sh`

### Modified
- `routes/web.php` — one route registration

---

## 3. Verifier status

```
[check1] PASS — /admin/ingestion-review route registered
[check2] PASS — IngestionReviewController class loads
[check3] PASS — IngestionReview.tsx exists (271 lines)
[check4] PASS — IngestionReviewTest.php parses + class loads
[step1] PASS — verifier still green
[step2] PASS — verifier still green
[step3] PASS — verifier still green
[step4] PASS — verifier still green
[step5] PASS — verifier still green
[step6] PASS — verifier still green
[step7a] PASS — verifier still green
[step7b] PASS — verifier still green
[step7c] PASS — verifier still green

=== Phase 3 master-plan Step 8a verifier summary ===
  (13 checks total; all must pass)
```

(verifier final summary will be updated when the cascade completes — the 7c-cascade reruns Steps 1-7b which includes Docling, taking ~60-90 sec)

---

## 4. Decisions made in this phase

### 4.1 Scaffold-only, not full Step 8

Step 8 in the §3 kickoff bundles 4 features: queue list, rendered
page thumbnails, parser/confidence breakdown panel, and disposition
controls with Reverb broadcast. That's too much for one tick. Split:

- **8a (THIS)**: queue list + summary panel + filters (read-only)
- **8b (next)**: detail panel — rendered page image + parser breakdown
- **8c (after)**: disposition controls + Reverb broadcast

Each sub-tick stands alone. Doc-phase 59 can ship 8b without 8a
having to know about it; 60 can ship 8c independently.

### 4.2 Inertia v3 + React 19 idioms

Follows existing admin pages (`CacheTelemetry.tsx`, `WorkflowRuns.tsx`):
- `AppLayout` wrapper (auth + nav handled there)
- `Head` for page title
- Inertia props typed in a single `PageProps` interface
- Tailwind utility classes; no custom CSS
- shadcn/ui components NOT used here (none of the existing admin
  pages use them; consistency wins). Doc-phase 59's detail panel
  may introduce shadcn `Dialog` / `Sheet` for the modal pattern.

### 4.3 Filter changes use `router.get()` not `useForm`

The existing pattern across admin pages varies; for this scaffold I
chose `router.get(url, params, { preserveState: false })` because
the summary panel needs to recompute when filters change — partial
updates via `preserveState: true` would leave summary stale.

Doc-phase 59+ can switch to `useForm` if disposition controls need
form semantics (CSRF, validation errors, optimistic update).

### 4.4 Test class uses `RequiresPostgres` trait

The 6 tests follow the same pattern as `CacheTelemetryTest.php` and
others: they require real PG (silver tables, RLS) so they `setUp()`
themselves skip when the SQLite-backed local suite runs them. The
canonical run is in the release-rehearsal CI job against real
Postgres.

The verifier (check 4) confirms the test file PARSES + the class
LOADS — structural assertion only. Behavioural assertion happens
in CI.

### 4.5 Dual-tree drift surfaced again

While running `php artisan route:list`, surfaced that the WSL
canonical tree was MISSING `app/Http/Controllers/Admin/AgentConfig/`
+ `ShadowRunsController.php` — both present in the Windows tree.
Fixed by `cp -r` from `/mnt/c/.../Admin/AgentConfig` →
`/home/georag/projects/georag/.../Admin/`.

This is the third documented Windows ↔ WSL drift in §3 work. Worth
escalating to a separate doc-phase tick: write a `diff -r`-based
sync verifier that catches these on every step verifier run. The
recurring pattern from doc-phase 44 still bites.

### 4.6 200-row hard cap on queue

Initial cap matches the existing `WorkflowRunController` /
`ShadowRunsController` pattern. Pagination + virtualized scrolling
not needed for v1 — Silver Review queue depth typically stays in
the dozens, not thousands. If real-world depth ever exceeds 200,
doc-phase 60+ can add pagination.

---

## 5. Findings carried over to doc-phase 59+

### 5.1 Detail panel needs FastAPI render endpoint

Step 8b (item detail panel) needs a way to render the PDF page
image for display. The `app.ocr.render.render_page()` function from
doc-phase 52 produces PNG bytes; doc-phase 59 needs to expose this
as an HTTP endpoint (likely `GET /internal/v1/ocr/render?report_id=...&page=...`)
that the React detail panel fetches.

Two design choices for doc-phase 59:
- **(a) Inline blob:** `<img src="/internal/v1/ocr/render?...">` with the FastAPI endpoint returning `Content-Type: image/png` directly.
- **(b) Cached blob:** FastAPI renders once and stores in SeaweedFS under a deterministic key; the page renders an `<img src="/admin/review-pages/...">` that Laravel reverse-proxies to S3.

(a) is simpler. (b) avoids re-rendering on every detail open. Decide
in doc-phase 59 based on render latency on real PDFs (smoke-bench
showed ~50-80 ms/page at scale=2.0 — likely (a) is fine).

### 5.2 Disposition controls need new endpoint

Step 8c (disposition controls) needs PATCH endpoints:
- `PATCH /admin/ingestion-review/{review_item_id}` — update status + resolution_notes
- Probably also a way to trigger re-OCR: POST to a workflow that re-runs `parse_scanned` with escalated settings on the specific page

The Reverb broadcast on disposition change is a small addition once
the PATCH endpoint exists.

### 5.3 Top-nav entry not yet added

The new `/admin/ingestion-review` page is reachable via URL only —
no top-nav link in `AppLayout`. Worth adding in doc-phase 59 alongside the detail
panel. The existing admin top-nav structure should make this a one-line addition once we know which page slot to use.

### 5.4 Real-world QA: queue is empty until §04p ingests run

The §04p stack (live since doc-phase 57's dual-write) will populate
this queue organically as new PDFs flow through `ingest_pdf`. Until
then, the queue is empty and the page shows the "No review items
match the current filters." state. Worth surfacing this to the user
as expected behavior, not a bug.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Table review confidence thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround
- Permission management is still ad-hoc (DELETE grant + others)
- Windows ↔ WSL dual-tree sync (third instance documented this tick)
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- Docling deprecation warning on table image extraction (benign)
- Retry settings escalation logic is opinionated
- `_compute_doc_quality_score` is a placeholder
- No end-to-end Hatchet engine test yet
- ParseOut shape unchanged (future optimization)

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 59 will do

**Master-plan §3 Step 8b — Silver Review detail panel.**

Build the detail panel that appears when clicking a queue row:

- Rendered page image (via new FastAPI render endpoint backed by
  `app.ocr.render.render_page()`)
- Parser used + per-confidence breakdown (OCR / layout / table)
- Retry log: how many retries fired, with what escalated settings
- Extracted text content from `silver.ingest_extractions` (or
  `silver.ingest_ocr_results` for scanned pages) for the affected page
- Reason code + status (existing, formatted)

Deliverables:
- New FastAPI endpoint `GET /internal/v1/ocr/render` (token-gated, workspace-scoped)
- Laravel reverse-proxy (or direct image fetch with auth) — TBD per § 5.1
- React detail panel as a `<Sheet>` (shadcn) or inline modal
- Behaviour test for the FastAPI render endpoint
- New verifier: `scripts/phase3_master_plan_step8b_verify.sh`

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
| 7d. Shadow comparison (deferred — see doc-phase 57 §7) | deferred | possibly never |
| 8a. Silver Review queue scaffold | ✅ DONE | 58 |
| 8b. Silver Review detail panel | next | 59 |
| 8c. Disposition controls + Reverb broadcast | pending | 60 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 61-62 |
| 10. RAGFlow retirement + cleanup | pending | 62-63 |

**8.33 of 10 main steps complete.** Frontend slice begins.

---

End of doc-phase 58 handoff. Silver Review queue is visible at
`/admin/ingestion-review`.
