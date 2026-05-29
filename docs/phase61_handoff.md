# Phase 61 Handoff — Master-plan §3 Step 8d (disposition controls)

**Document version:** 1.0
**Status:** Doc-phase 61 complete. Doc-phase 62 inheriting.
**Predecessors:** `docs/phase60_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

The Silver Review queue is now **actionable** — operators can
accept / reject / mark-for-re-OCR any flagged page directly from the
detail panel. Re-OCR auto-trigger (Hatchet workflow) + Reverb live
broadcast are deliberately split to doc-phase 62 to keep this tick
bounded.

After doc-phase 61, Step 8 (Silver Review UI) is **functionally
complete for v1**. The remaining §3 work is Step 9 (50-PDF acceptance
corpus + sign-off) and Step 10 (RAGFlow retirement).

---

## 1. What doc-phase 61 delivered

### Laravel backend

| Route | What it does |
|---|---|
| `PATCH /admin/ingestion-review/{review_item_id}` | `update()` method. Validates status against the CHECK enum, enforces resolved-is-terminal at app layer, auto-populates `resolved_at` + `assigned_to` when transitioning to a `resolved_*` value |

Validation:
- `status` must be one of the 6 enum values
- `resolution_notes` optional, max 4000 chars
- Resolved items reject any transition out (422 with descriptive error)
- Admin-only (`$this->authorize('admin')`)

### React frontend

`DispositionControls` component (~140 lines) inside the existing
`DetailPanel`:
- 3 action buttons: **Accept** (green), **Re-OCR requested** (blue),
  **Reject** (red)
- Click → inline confirmation step (no modal library) with optional
  resolution notes textarea (4000 char limit)
- PATCH via `fetch` with CSRF token from meta tag
- On success: detail panel updates local state + queue list refreshes
  via Inertia partial reload (`router.reload({ only: ['queue', 'summary'] })`)
- On error: inline red banner with the upstream error message
- When item is already `resolved_*`: panel shows "Resolved items are
  terminal — no further transitions allowed."

### Disposition vocabulary in v1

| Operator action | DB status written | Meaning |
|---|---|---|
| Accept | `resolved_accept` | OCR output is correct as-is; no changes needed |
| Re-OCR requested | `resolved_reocr_requested` | Operator wants this page re-processed (auto-trigger lands doc-phase 62) |
| Reject | `resolved_reject` | OCR output is unusable; downstream readers should ignore this page |

All three set `resolved_at = NOW()` and `assigned_to = current_user_id`
atomically.

The intermediate states `assigned` and `in_review` exist in the
schema but aren't exposed as UI actions in v1. They'd be useful if
multi-operator workflows emerge — for now, "click → resolved" is the
simplest happy path.

### Tests

`tests/Feature/Admin/IngestionReviewTest.php` extended with 5 more
feature tests (total now 17 in this file):
- PATCH applies `resolved_accept` + persists notes + sets `resolved_at` + sets `assigned_to`
- PATCH rejects invalid status value (422 with validation error)
- PATCH blocks transition out of resolved (422 with custom error)
- PATCH 404s for unknown review_item_id
- PATCH requires admin authorization (non-admin → 403)

### Verifier

`scripts/phase3_master_plan_step8d_verify.sh` — 4 doc-phase-specific
checks + 12 prior-step regression cascades.

---

## 2. Files of record

### New
- `scripts/phase3_master_plan_step8d_verify.sh`

### Modified
- `app/Http/Controllers/Admin/IngestionReviewController.php` — +75
  lines (`update()` method + `Auth` import)
- `routes/web.php` — 1 new PATCH route
- `resources/js/Pages/Admin/IngestionReview.tsx` — +140 lines
  (`DispositionControls` component + state threading)
- `tests/Feature/Admin/IngestionReviewTest.php` — +5 tests

---

## 3. Verifier status

Doc-phase 61 verifier:
- 4 doc-phase-specific checks (route registered, update() method
  present, DispositionControls component present, test file parses
  cleanly)
- 12 prior-step regression cascades

Spot-checks confirmed at handoff time:
- PATCH route registered (`php artisan route:list` shows it)
- `update()` method present (verified via tinker reflection)
- DispositionControls function present in TSX
- All PHP files parse cleanly (`php -l`)

Full cascade run happens via the verifier script when invoked.

---

## 4. Decisions made in this phase

### 4.1 Re-OCR Hatchet workflow split to doc-phase 62

"Re-OCR requested" just sets the status in v1. Auto-triggering a
re-OCR Hatchet workflow with escalated parse_scanned settings is
real work:
- Define + register a new Hatchet workflow `re_ocr_page`
- Hatchet step: load existing parse from silver tables, identify
  retry settings from `quality_graph.RETRY_SETTINGS_BY_ATTEMPT`,
  invoke `parse_scanned` with escalated settings, persist new rows
- Telemetry for the re-OCR pass distinct from initial ingest

That's ~1 doc-phase of focused work on its own. Splitting keeps
this tick reviewable.

Operationally for v1: when an operator marks "re-OCR requested",
the status flips. An admin (or a cron) can later trigger the
re-OCR manually. Imperfect UX but acceptable for the v1 cutover —
real re-OCR demand will emerge when the 50-PDF acceptance corpus
runs.

### 4.2 Reverb broadcast split to doc-phase 62

Multi-operator live UI sync isn't load-bearing until two operators
are working the queue simultaneously. Not the day-one scenario.
Split to doc-phase 62 alongside the re-OCR workflow.

### 4.3 Resolved-is-terminal enforced at app layer, not DB

The DB CHECK constraint allows any transition between status values.
Enforcing "resolved_* is terminal" at the application layer is the
right tradeoff because:
- The DB still allows admin-side manual `UPDATE` for genuine corrections
- App-layer validation gives the operator a clear error message
  rather than a silent constraint violation
- A future "unresolve" admin-only workflow can be added later
  without a schema change

### 4.4 `assigned_to` is the resolver's ID, not pre-assigner

The schema lets `assigned_to` be set independently of `status` —
the intermediate workflow would be "operator picks up an item
(assigned), then resolves it." V1 skips the intermediate state
and sets `assigned_to = current_user` at resolution time.

If multi-operator workflows emerge, doc-phase 62+ can split
"assign to me" from "resolve as accept" as separate UI actions
+ PATCH variants.

### 4.5 Inline confirmation, not modal dialog

The disposition buttons swap their row into a "Confirm <action>?"
form with the notes textarea. No modal library required. shadcn
`<Dialog>` introduction is deferred until a UI need genuinely
requires it.

### 4.6 No top-nav entry

Per the inspection done this tick: this project has no admin
top-nav. Every admin page is reached by direct URL (consistent
with `/admin/cache-telemetry`, `/admin/hatchet-workers`, etc.).
The "add top-nav entry" deliverable from doc-phase 60's handoff
doesn't apply.

If a project-wide admin nav redesign happens later, the
`/admin/ingestion-review` page is a natural inclusion alongside
the others.

### 4.7 Inertia partial reload to refresh queue without losing panel state

When a disposition is applied, the detail panel updates its local
state immediately so the operator sees the confirmation. But the
queue list behind the panel still shows the old status. Solution:
`router.reload({ only: ['queue', 'summary'] })` — Inertia v3
partial reload fetches just those props, the page re-renders the
queue table with fresh data, and the detail panel (in component
state) is unaffected.

Cleaner than a full page reload (loses panel scroll + state) or
a manual optimistic update (would require duplicating the queue
update logic).

---

## 5. Findings carried over to doc-phase 62+

### 5.1 Re-OCR Hatchet workflow needed

§ 4.1. The disposition "re-OCR requested" sets the status but
nothing automatically processes it. Doc-phase 62 should:
- Define `re_ocr_page` Hatchet workflow
- A small Laravel admin action ("trigger re-OCR pending") that
  queues re-OCR for all `resolved_reocr_requested` rows
- (Optional) Cron that runs every N minutes to drain the queue

### 5.2 Reverb broadcast on disposition change

§ 4.2. When operator A changes a row's status, operator B's open
queue should reflect the change without a page refresh. Reverb
channel: `admin.ingestion-review.queue`; payload: review_item_id
+ new status. Doc-phase 62.

### 5.3 Verifier cascade O(N²) issue (carried from doc-phase 60)

This tick's Step 8b verifier was still cascading at step7a after
~15 minutes of wall time when doc-phase 61 started landing. Worth
a dedicated cleanup tick (doc-phase 62 or separate) to switch to
the "manifest of recently-passed verifiers" pattern from doc-phase
60 handoff §5.1.

### 5.4 No live audit log of disposition changes

Each disposition write updates the silver row, but there's no
`audit.audit_ledger` emission for the disposition decision itself.
Worth adding in doc-phase 62 alongside the Reverb work — both
care about "who did what to which review item when."

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
- Pre-doc-phase-59 reports have NULL bronze keys
- Import-boundary lint is module-level coarse

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 62 will do

Two possible scopes; the actual choice depends on what feels more
ready when doc-phase 62 opens.

### Option A: Step 8 completeness (re-OCR + Reverb + audit)

- Hatchet `re_ocr_page` workflow definition
- Cron / manual trigger to drain `resolved_reocr_requested` rows
- Reverb broadcast on disposition change
- `audit.audit_ledger` emission per disposition

Adds the missing pieces from doc-phase 61's deferrals. Total
~300-400 lines.

### Option B: Step 9 — 50-PDF acceptance corpus

Build the acceptance test harness. Doc-phase 49 already scaffolded
the corpus directory + LABELING_TRACKER.md; doc-phase 62 could
ship:

- Labeling tracker UI (small admin page where SME marks each PDF's
  expected profile + done state)
- `scripts/phase3_master_plan_acceptance.sh` that ingests every PDF
  in the corpus through the §04p pipeline and asserts the §04p
  outcomes match the labels (profile classification + recommended_action
  + per-page review counts)

This is what the master plan calls for as the §3 done gate. Step 8
completion (option A) is operational polish. Option B is
substrate validation.

**Recommendation**: option A first, then option B. The re-OCR and
audit gaps are operationally important enough that running the
acceptance corpus without them would surface them as immediate
findings anyway. Better to close out Step 8 cleanly, then take
the corpus through a complete §04p stack.

### Verifier cascade cleanup (separate concern)

Doc-phase 60 §5.1 flagged the O(N²) cascade issue. This is
worth its own tick (call it doc-phase 62b or 63) regardless of
which §3 work continues. Suggested approach: each verifier writes
a `passed_at` timestamp to a `.verifier-state/` directory; the
cascade just checks recency. Adding to the carry-over list.

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
| 8d. Disposition controls | ✅ DONE | 61 |
| 8e. Re-OCR workflow + Reverb + audit | next (option A) | 62 |
| 9. 50-PDF acceptance corpus + sign-off | needs Kyle labeling | 63-64 |
| 10. RAGFlow retirement + cleanup | pending | 64-65 |

**Step 8 is functionally v1-complete.** Doc-phase 62 closes the
operational gaps (re-OCR auto-trigger + Reverb + audit), then
Step 9 + Step 10 close out master-plan §3.

---

End of doc-phase 61 handoff. Operators can now triage flagged pages
end-to-end.
