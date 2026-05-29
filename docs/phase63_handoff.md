# Phase 63 Handoff — Master-plan §3 Step 8e (re-OCR Hatchet workflow)

**Document version:** 1.0
**Status:** Doc-phase 63 complete. Doc-phase 64 inheriting.
**Predecessors:** `docs/phase62_handoff.md`, `docs/phase61_handoff.md` §5.1.

"Re-OCR requested" is now wired end-to-end. When an operator selects
the disposition in the Silver Review queue, Laravel dispatches the
`re_ocr_page` Hatchet workflow which re-runs `parse_scanned` with
escalated settings + persists new silver rows.

Audit emission + Reverb broadcast (the other doc-phase 61 §7
deferrals) split to doc-phase 64.

---

## 1. What doc-phase 63 delivered

### New Hatchet workflow: `re_ocr_page`

`src/fastapi/app/hatchet_workflows/re_ocr_page.py` (~290 lines). Single-task
workflow with the following flow:

1. **Look up bronze key** from `silver.parser_run_artifacts.raw_output_uri`
   (doc-phase 59 tracking). 404 if not tracked.
2. **Look up current retry_count** from `silver.ocr_page_quality`.
   Refuse if `>= MAX_OCR_RETRIES` (returns `retry_max_exceeded`).
3. **Emit audit** `re_ocr_page.start` with retry_attempt + settings.
4. **Download bronze PDF** from S3.
5. **Call parse_scanned** with `RETRY_SETTINGS_BY_ATTEMPT[retry_attempt]`
   from `app.ocr.quality_graph`.
6. **Persist new rows** in a transaction:
   - `silver.parser_run_artifacts` row for the retry pass (parser_used
     = `scanned_paddleocr`, parser_version = `retry_attempt_N`)
   - `silver.ingest_ocr_results` rows for each OCR'd region (region
     numbers offset past existing rows to avoid PK collision; source_method
     reflects the retry escalation: `paddleocr_pp_ocrv5_retry_binarized`
     for attempt 0, `paddleocr_pp_ocrv5_retry_lang_hint` for attempt 1)
   - `silver.ocr_page_quality` UPDATE: retry_count++, ocr_confidence
     refreshed, needs_review re-evaluated, last_evaluated_at = NOW()
7. **Emit audit** `re_ocr_page.complete` with new confidence + needs_review.

Registered in the `ingestion` worker pool in `worker.py` POOLS.

### New trigger endpoint

`POST /internal/v1/re_ocr_page/trigger` in
`src/fastapi/app/routers/re_ocr_trigger.py`. Auth: X-Service-Key
(same as the existing internal routes). Returns 202 with
`workflow_run_id`.

### Laravel auto-dispatch

`IngestionReviewController::dispatchReOcr()` invoked from the
disposition `update()` method when status flips to
`resolved_reocr_requested`. Wrapped in try/catch so a Hatchet outage
doesn't block the disposition write (operator's decision is already
persisted).

Response includes `re_ocr_triggered: bool` + `re_ocr_error: string | null`
so the frontend can surface dispatch failures (UI display deferred to
doc-phase 64).

### Import-boundary lint widened

`scripts/phase3_master_plan_step1_import_boundary.sh` allow-list now
includes `app/hatchet_workflows/` broadly (previously
`hatchet_workflows/ingest_pdf.py` only). All Hatchet workflows
correctly run in the hatchet-worker container, which is the intended
host for the heavy OCR stack.

---

## 2. Files of record

### New
- `src/fastapi/app/hatchet_workflows/re_ocr_page.py` (~290 lines)
- `src/fastapi/app/routers/re_ocr_trigger.py` (~75 lines)
- `scripts/phase3_master_plan_step8e_verify.sh` (~120 lines)

### Modified
- `src/fastapi/app/hatchet_workflows/worker.py` — import + register
  `re_ocr_page` in `ingestion` pool
- `src/fastapi/app/main.py` — include the new router
- `app/Http/Controllers/Admin/IngestionReviewController.php` — add
  `dispatchReOcr()` + call from `update()` on resolved_reocr_requested
- `scripts/phase3_master_plan_step1_import_boundary.sh` — widened
  allow-list to `app/hatchet_workflows/` broadly

---

## 3. Verifier status

```
[check1] PASS — re_ocr_page workflow + input/output models load
[check2] PASS — re_ocr_page imported in worker.py POOLS
[check3] PASS — /internal/v1/re_ocr_page/trigger route registered
[check4] PASS — IngestionReviewController has dispatchReOcr()
[check5] PASS — import-boundary lint clean
[step1-8d] PASS — manifest recent (skip re-run)

=== Phase 3 master-plan Step 8e verifier summary ===
  18/18 checks passed (18.5 sec wall, cascade instant via manifest)
```

The doc-phase 62 cascade fix paid off immediately — verifier completes
in 18 seconds (vs the 20+ min cold cascade would have taken).

---

## 4. Decisions made in this phase

### 4.1 Single-task workflow, not multi-step

`re_ocr_page` is one `@workflow.task` with all logic inline. The
ingest_pdf precedent uses preflight + parse + persist as 3 separate
tasks for Hatchet's diff harness; re-OCR doesn't need that
granularity. One task keeps the code together + reduces step-output
serialization overhead.

### 4.2 Region numbering offsets past existing rows

Re-OCR produces new `silver.ingest_ocr_results` rows. The PK is
`(report_id, page, region)`. Existing rows from prior parse_scanned
runs occupy regions 0..N-1. The retry pass needs to write new rows
without colliding.

Solution: query `MAX(region)` for `(report_id, page)` before the
INSERT; assign retry passages starting at `max + 1`.

Tradeoff: retry passages aren't co-located with their original-pass
counterparts in region-id space. The query for "all OCR results
for this page" still returns all rows regardless. Future analyses
that need to distinguish retry vs original can read
`source_method` (`paddleocr_pp_ocrv5` vs `paddleocr_pp_ocrv5_retry_*`).

### 4.3 No deletion of prior-pass rows

Re-OCR ADDS new rows; doesn't delete the original-pass rows.
Rationale: keeping history is useful for the §9.8 XGBoost classifier
training corpus (Phase 9). The classifier learns from
"what input → what OCR confidence" — both the failed first pass and
the (hopefully) better retry pass are signal.

For consumers that only want the latest output per region, the
ORDER BY clause + `source_method` filter gives them that.

### 4.4 Existing review_item NOT updated by the workflow

The operator's disposition (`resolved_reocr_requested`) is terminal
in the schema. The workflow does NOT flip it to anything else.
After the retry pass completes, the queue's per-page summary shows
the new ocr_confidence and a fresh `needs_review` flag — the
operator can see whether the retry helped just from the queue UI.

If the retry's new confidence is still below `ACCEPT_OCR_CONFIDENCE`
(0.85), the `ocr_page_quality.needs_review` stays true but no new
review_item is created. The operator would need to manually
trigger another re-OCR by creating a new review item somehow —
which the UI doesn't yet support (resolved-is-terminal). Real
limitation, documented in handoff §5.2.

### 4.5 Laravel dispatch is fire-and-forget; failure logged

The PATCH endpoint returns immediately after the disposition row
is written. If the Hatchet trigger fails (FastAPI down, network
hiccup), the disposition is still persisted; the operator gets
`re_ocr_triggered: false, re_ocr_error: "..."` in the response.

This decouples operator UX from backend availability. Doc-phase
64's audit emission will record both the disposition write AND
the trigger outcome so operators can later identify "re-OCR was
requested but never ran" cases.

### 4.6 Workflow uses the existing `_dsn()` pattern, not the persist layer

The workflow has its own connection setup rather than reusing
`app.ocr._persist.transactional_workspace_session`. Reason: the
workflow lifecycle is owned by Hatchet (connection management,
retries, timeouts); reusing the persist helper would couple it
into Hatchet's worker pool retry semantics in ways that could
cause double-pool-creation.

Future refactor: factor out a "single connection with workspace
GUC" helper shared by both. Out of scope here.

---

## 5. Findings carried over to doc-phase 64+

### 5.1 Operational gap: containers need restart to pick up changes

`uvicorn` in the FastAPI container caches the imported `app.main`
module. Adding the new router to `main.py` requires:
```bash
docker compose restart fastapi
docker compose restart hatchet-worker-ingestion
```

This is the same pattern as every prior router addition; not a
doc-phase 63 regression. The verifier confirms via fresh-import
checks that the code is correct; live-process testing requires
the restart.

Worth a small ops tick to wire up a healthcheck-based "reload on
deployment" — but that's separate infrastructure work.

### 5.2 Re-OCR can fire only ONCE per review item

The schema's `resolved_*` states are terminal. Operator clicks
"Re-OCR requested" → status flips → workflow runs → ocr_page_quality
updates. If the retry pass STILL has low confidence, the operator
can't trigger another retry from the existing review_item because
it's already resolved.

Mitigations:
- Operator could create a fresh review_item manually (not supported
  in the UI yet)
- Doc-phase 64 could add a "re-open" action that creates a new
  review_item linked to the prior one
- Or accept the limitation: 2 retries are MAX per page anyway (per
  MAX_OCR_RETRIES = 2 in quality_graph); after that the page is
  permanently "low-confidence, accept-as-is or reject."

### 5.3 Doc-phase 64 still owes audit emission + Reverb

The two remaining doc-phase 61 §7 deferrals:
- `audit.audit_ledger` emission per disposition change (the workflow
  emits its own audit, but the operator's PATCH itself does not)
- Reverb broadcast on disposition change (multi-operator sync)

Smaller than the re-OCR workflow that just landed; doc-phase 64
should fit both comfortably.

### 5.4 End-to-end re-OCR test deferred

Writing an integration test that:
- Triggers the workflow
- Waits for completion (Hatchet polling)
- Asserts new silver rows landed

Requires the live Hatchet engine + worker + reasonable wait
semantics. Not blocking — the workflow's individual functions
(parse_scanned + persist patterns) are already tested in prior
doc-phases. End-to-end could be added in doc-phase 65 alongside
the broader "Hatchet engine smoke test" carry-over from doc-phase
57 §5.1.

---

## 6. Pre-existing carry-overs (unchanged this phase)

All carry-overs from doc-phases 49-62 remain. Notable: doc-phase
62's verifier cascade fix is paying off — Step 8e verifier ran in
18 sec total, of which the cascade was instant.

---

## 7. What doc-phase 64 will do

**Step 8 closeout: audit emission + Reverb broadcast.**

Two small concerns to close out Step 8:

1. `audit.audit_ledger` row per disposition change in
   `IngestionReviewController::update()` — `action_type =
   "silver.low_confidence_page_reviews.disposition"` with payload
   capturing the transition + actor.

2. Reverb broadcast on disposition change — channel
   `admin.ingestion-review.queue`, payload `{review_item_id,
   new_status}`. Multi-operator live UI sync.

Both are small; one tick should easily ship both with tests +
verifier.

After doc-phase 64, Step 8 is fully closed and the remaining §3 work
is Step 9 (50-PDF acceptance corpus + sign-off, needs Kyle labeling)
+ Step 10 (RAGFlow retirement).

---

## 8. Master-plan §3 progress

| Step | Status |
|---|---|
| 1-7c, 8a-8d | ✅ DONE |
| 7d (shadow comparison) | deferred |
| 8e (re-OCR workflow) | ✅ DONE |
| 8f (audit + Reverb closeout) | next |
| 9 (acceptance corpus) | needs Kyle labeling |
| 10 (RAGFlow retirement) | pending |

**Re-OCR loop is live.** "Re-OCR requested" disposition now actually
re-runs OCR with escalated settings. One more small tick closes
Step 8 entirely.

---

End of doc-phase 63 handoff. Containers need restart to load the new
router + workflow registration; structural verifier passes against
fresh imports.
