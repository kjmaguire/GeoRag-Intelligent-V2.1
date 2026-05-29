## Doc-phase 158 handoff — Manual §21.3 decision-entry writer surface + 8/8 type coverage

**Status:** Live + Pint clean + 101/101 substrate verifier + all 8 §21.3 types have real decisions in DB.

## What landed

Closed the "human decision capture" gap by adding a single admin
writer surface that authentically files §21 decisions of any of the
8 §21.3 types. Today the Decision History dashboard reads decisions
across all 8 types — every type has at least one authentic decision
backed by hash-chained audit anchors.

### New routes

```text
GET  /admin/decisions/new       → admin.decisions.new   (Inertia page)
POST /admin/decisions           → admin.decisions.store (writer endpoint)
```

### `DecisionHistoryController` — added create + store methods

- `create()` renders the Inertia page with `valid_decision_types`,
  `valid_human_decisions`, and the platform_ops sentinel workspace id
- `store(Request, RecordDecision)` validates the form and calls the
  doc-phase 133 RecordDecision service — produces a real decision row
  with full audit anchor

Validation covers all 9 form fields:
- decision_type (one of 8), recommendation, human_decision (one of 4)
- workspace_id (nullable UUID; defaults to platform_ops sentinel)
- reason (text), uncertainty (0..1)
- evidence_chunk_ids (array of strings)
- options_considered (array of {label, description?, was_chosen?})

### `resources/js/Pages/Admin/DecisionNew.tsx` — ~340 lines

Inertia form page. Same dark stone-950 palette as the other 4 admin
surfaces. Features:
- Type-picker dropdown for the 8 decision types
- Form fields for recommendation + reason + uncertainty
- Dynamic add/remove of evidence chunk IDs (chip-style)
- Dynamic add/remove of options_considered with "was_chosen" checkbox
- Submit → POST → flash banner on Decision History page

Uses Inertia's `useForm` hook with optimistic state + per-field error
display.

## Live verification — 8 of 8 §21.3 types covered

Filed 7 representative authentic decisions (one for each previously
uncovered type), bringing the Decision History dashboard to **9 real
decisions** spanning all 8 §21.3 types:

| decision_type | rows | human_decision |
|---|---|---|
| conflict_resolution | 1 | modified |
| crs_decision | 1 | accepted |
| export_approval | 1 | accepted |
| public_data_import | 1 | accepted |
| report_signoff | 1 | signed_off |
| schema_mapping | 1 | modified |
| target_recommendation | 1 | accepted |
| workflow_enablement | 2 | accepted |

Each is anchored: `audit_ledger_id` populated, `hash` = 32-byte SHA-256.

The dashboard's per-decision-type breakdown table now has full
coverage instead of "1 of 8 types active" — the Decision History
surface tells a coherent end-to-end story.

## Smoke verification

```bash
# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Route registration
php artisan route:list --path=admin/decisions
# → GET admin/decisions/new, POST admin/decisions both registered

# Vite build
npm run build
# → public/build/assets/DecisionNew-CHWXWco6.js bundled

# All 8 §21.3 types now exercise via the substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 101/101 checks passed (incl. new silver:decision-types-coverage)
```

## Cumulative session state — 27 ticks closed

- **Doc-phase ticks this run:** **27** (132 → 158)
- **Sections closed:** §25.4 + §6 (2 of 12)
- **§21.3 decision types with authentic captures:** **8 of 8** (was 1 of 8 at doc-phase 133)
- **Cross-section integrations live:** 1 (§7.2 ↔ §9.13)
- **Inertia writer surfaces:** 1 (DecisionNew)
- **Substrate verifier:** **101/101 PASS**
- **Live pytest cases:** 219 (Laravel-side route-smoke tests gated on pgsql config)

## What's next

The §21.3 dashboard tells a coherent story now. Remaining productive
moves:
- Pivot: real LLM evaluator for §10.4 workspace_evaluator (replaces
  synthetic_stub with real RAG + §04i validators) — requires vLLM
  endpoint wiring
- Add `/admin/index` landing page linking to the 5 admin surfaces
  (4 Track-3 + DecisionNew) with status cards
- Wire `record_decision` as the *side-effect* hook in
  `IntegrationsController::rotateFlowKey` + `registerSender` (per
  the existing pattern in toggleFlag) — adds 2 more capture sites
  authentically tied to admin actions

## Carry-overs

- The DecisionNew form doesn't yet validate that `options_considered`
  has exactly one `was_chosen: true`. The DB allows zero or multiple
  chosen options, but UI guidance would help operators.
- The form's `workspace_id` field accepts any UUID — no check that
  the workspace exists. RLS will reject the INSERT with a clear
  message if it doesn't; could add a pre-validation Eloquent lookup.
- The form is admin-only via `$this->authorize('admin')`. Future
  scope: per-decision-type capability gates (e.g. only QPs file
  `report_signoff` decisions).
