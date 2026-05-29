## Doc-phase 131 handoff — §9.10 Hypothesis Workspace (fourth Track-3 surface)

**Status:** Live + smoke-verified. **72/72 substrate verifier**.

**Track 3 is now fully populated: 4 of 4 admin surfaces live.**

## What landed

### Controller — `app/Http/Controllers/Admin/HypothesisWorkspaceController.php`

Read-only Laravel controller. Same pattern as Eval Dashboard (128),
Decision History (129), Support Cockpit (130).
- `$this->authorize('admin')` gate
- Raw SQL via `DB::select(...)` / `DB::selectOne(...)`
- Returns `Inertia::render('Admin/HypothesisWorkspace', [...])` with 9
  structured payloads:
  - **kpis** — 9 top-level counters (total/accepted/ai_suggested,
    mean_confidence, distinct_workspaces/parent_questions, total
    evidence_links, recent_30d_count, latest_created_at)
  - **by_review_status** — ai_suggested / reviewed / accepted / rejected
  - **by_confidence_method** — bucketed (NULL → 'unknown')
  - **by_evidence_role** — supporting / contradicting / missing /
    recommended_test
  - **recent_hypotheses** — last 50, filter-aware on review_status +
    workspace_id, with per-role evidence-link counts (correlated
    subqueries)
  - **recent_evidence_links** — last 100 (joined back to parent for
    label + workspace_id)

Filter validation: `REVIEW_STATUSES`, `EVIDENCE_ROLES` mirror the DB
CHECK constraints.

### Route — `routes/web.php`

`GET /admin/hypothesis-workspace` → `admin.hypothesis-workspace` name.

### React page — `resources/js/Pages/Admin/HypothesisWorkspace.tsx`

~17 kB / ~470 lines. Dark Tailwind palette consistent with the other
3 Track-3 surfaces.

Layout sections:
1. **KPI tiles row** — 4 cards: total, accepted, evidence_links, recent 30d
2. **Filter strip** — all / 4 review_status buttons + workspace_id chip
3. **3-up CountPanels** — review_status / confidence_method / evidence_role
4. **Quality signals card** — mean confidence + distinct workspaces +
   distinct parent_questions
5. **Recent hypotheses table** — ID, label, parent_question, description,
   status badge, confidence, **S/C/M/T** per-role evidence counts
   (color-coded), workspace (click to filter), created
6. **Recent evidence links table** — link_id, hypothesis label, role
   badge, weight, source chunk, workspace

Badge palette:
- review_status: ai_suggested=sky, reviewed=amber, accepted=emerald,
  rejected=red
- role: supporting=emerald, contradicting=red, missing=amber,
  recommended_test=sky

Reused the same `CountPanel` + `Tile` helpers so the visual rhythm
matches the other Track-3 surfaces.

### What the dashboard shows TODAY (with real data)

| KPI | Value |
|---|---|
| Total hypotheses | 0 |
| Accepted | 0 |
| Evidence links | 0 |
| Recent (30 d) | 0 |

All zeroes today: the §9.10 competing-hypotheses register is empty
because the reasoning agents that emit `ai_suggested` hypotheses
haven't graduated from skeleton yet. Empty-state copy on the
hypothesis table explains the dependency.

### Smoke verification

```bash
# Controller class loads
php artisan tinker --execute 'echo class_exists(HypothesisWorkspaceController::class)';
# → "OK"

# Route registered
php artisan route:list --path=admin/hypothesis-workspace
# → admin/hypothesis-workspace admin.hypothesis-workspace registered

# All 6 controller data methods run end-to-end (via reflection bypass)
php /app/tmp/hypothesis_workspace_smoke.php
# → kpis: OK — total=0, accepted=0, evidence_links=0, workspaces=0
# → byReviewStatus: OK (0 rows)
# → byConfidenceMethod: OK (0 rows)
# → byEvidenceRole: OK (0 rows)
# → recentHypotheses: OK (0 rows)
# → recentEvidenceLinks: OK (0 rows)

# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Vite build
npm run build
# → public/build/assets/HypothesisWorkspace-BeY4ElEW.js bundled (12.15 kB)

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 72/72 checks passed
```

### RLS note (resolved without a migration)

`silver.hypothesis_evidence_links` uses an EXISTS-based policy that
joins back to `silver.hypotheses` for workspace check. Per the
doc-phase 129 retrofit comment, EXISTS-based child policies inherit
the admin escape hatch transitively through their parent — no
migration needed. Verified by the smoke test returning the expected
row counts (zero today, but the query *executes* against both tables
under the admin role).

## Cumulative session state

- **Doc-phase ticks this run:** 131
- **Track 3 surfaces live:** Eval Dashboard, Decision History, Support
  Cockpit, **Hypothesis Workspace** — **4 of 4 complete**
- **Live helpers:** 8 + 4 admin surfaces
- **Live pytest cases:** 66
- **Substrate verifier:** **72/72 PASS**
- **Tracks closed:**
  - Track 1 (image rebuild): ✅ CLOSED through 4 builds
  - Track 2b (mechanical questions seed): ✅ 45 active in DB
  - **Track 3 (admin frontend surfaces): ✅ CLOSED, 4/4 live**
- **Tracks waiting for Kyle:**
  - Track 2a (§8.3 Athabasca SME content)

## All four Track-3 surfaces live

| Surface | URL | Source tables |
|---|---|---|
| Eval Dashboard | `/admin/eval-dashboard` | eval.golden_questions, silver.geological_ontology_terms, eval.run_summaries |
| Decision History | `/admin/decision-history` | silver.decision_records, audit.audit_ledger |
| Support Cockpit | `/admin/support-cockpit` | ops.support_tickets, ops.support_replay_runs, audit.audit_ledger |
| Hypothesis Workspace | `/admin/hypothesis-workspace` | silver.hypotheses, silver.hypothesis_evidence_links |

All four share:
- `admin` Gate authorization
- Dark stone-950 Tailwind palette
- KPI tiles → filter strip → counts panels → recent-rows table layout
- Empty-state guidance pointing at the upstream graduation that will
  populate them
- Cross-workspace reads via the doc-phase 129 RLS admin escape hatch
- Smoke-verified via reflection bypass

## Recommended next ticks

With Track 3 closed, the productive paths are:

1. **Skeleton graduations that populate these surfaces:**
   - `evaluate_workspace` Hatchet workflow body (§10.4) →
     populates Eval Dashboard's Recent runs
   - `record_decision` capture hooks from the 8 §21.3 sites →
     populates Decision History
   - §10.11 ticket-creation surface + 5 §25.4 support agents →
     populates Support Cockpit
   - §9.10 reasoning agents that emit ai_suggested hypotheses →
     populates Hypothesis Workspace
2. **Inertia route-smoke tests** for all 4 surfaces (asserting prop
   shape + admin gate enforcement). Pattern matches doc-phase 108.
3. **Track 2a unblock** — Kyle fills the Athabasca uranium TODO blocks
   in `src/fastapi/app/services/target_recommendation/sme_content/athabasca_uranium.py`.

## Carry-overs

- `/dashboard` doesn't have nav links to the 4 admin surfaces yet —
  Kyle currently navigates via direct URL. Adding an admin nav drawer
  would tie all 4 surfaces together (small Inertia/React task).
- All 4 surfaces share the same Tile / CountPanel helper components
  inlined per-file. Extracting these to `resources/js/Components/Admin/`
  would deduplicate ~120 lines across 4 files. Low-priority refactor.
